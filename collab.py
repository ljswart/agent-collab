#!/usr/bin/env python3
"""Agent mailbox: a project-agnostic evidence exchange between two coding agents.

Two agents share a working tree but have no direct message channel. This gives them
one: append-only outboxes, content-addressed provenance, and a vocabulary that keeps
"I read your message" distinct from "I approve this revision".

Install once, use in any project:

    python /path/to/agent-collab/collab.py init             # scaffold ./collab here
    python collab/collab.py --from claude --inbox           # read your mail
    python collab/collab.py --from claude --type finding \\
        --severity P1 --ref src/thing.py:42 \\
        --claim "..." --evidence-file /tmp/repro.sh --expect "..."

The mailbox lives in the PROJECT (`<root>/collab/`); this file is the shared tool.
Provenance is filled in automatically, because a rule that relies on an agent
remembering to paste a commit SHA is a rule that gets skipped when it matters most.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
DEFAULT_AGENTS = ("claude", "codex")
TYPES = (
    "ping",
    "finding",
    "claim",
    "question",
    "handoff",
    "escalate",
    "received",
    "reproduced",
    "disputed",
    "approved",
)
REQUIRED = ("ts", "from", "type", "provenance")
GIT = shutil.which("git") or "git"


# --------------------------------------------------------------------------- paths


def find_root(start=None):
    """Project root: the git top level containing `start`, else `start` itself."""
    start = Path(start or os.environ.get("COLLAB_ROOT") or Path.cwd()).resolve()
    result = subprocess.run(  # noqa: S603 - fixed executable, no shell, literal args
        [GIT, "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return start


def mailbox(root):
    return Path(root) / "collab"


def load_config(root):
    """Optional `<root>/collab/config.json`.

    Keys, all optional:
      agents            two agent names (default claude, codex)
      provenance_files  extra files hashed into every message, e.g. a frozen data
                        manifest, so research claims name their inputs
      exclude           extra repo-relative paths kept OUT of the reviewed snapshot
    """
    path = mailbox(root) / "config.json"
    config = {"agents": list(DEFAULT_AGENTS), "provenance_files": [], "exclude": []}
    if path.is_file():
        config.update(json.loads(path.read_text()))
    if len(config["agents"]) != 2:
        raise ValueError("collab/config.json must name exactly two agents")
    return config


def runtime_state_paths(config):
    """Mailbox state excluded from the reviewed snapshot.

    Without this, sending any message changes the fingerprint and immediately stales
    every outstanding approval -- the mailbox lives inside the repo it reviews.
    """
    names = {f"collab/{agent}.outbox.jsonl" for agent in config["agents"]}
    names.add("collab/.lock")
    return names | set(config.get("exclude", []))


def _is_runtime_state(path, config):
    if path in runtime_state_paths(config):
        return True
    return path.startswith("collab/.") and path.endswith(".cursor")


# ---------------------------------------------------------------------- provenance


def _git(root, *args):
    """Run a fixed git subcommand. Fails closed.

    A provenance block that silently degraded to empty would let an approval name a
    snapshot nobody verified.
    """
    result = subprocess.run(  # noqa: S603 - fixed executable, no shell, literal args
        [GIT, *args], cwd=root, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def reviewed_snapshot(root, config):
    """Content hash of every modified or untracked file, excluding mailbox state.

    Hashing `git status --porcelain` plus `git diff HEAD` is not enough: an untracked
    file appears only as a bare "?? path" whose CONTENT never enters the hash, so two
    materially different versions of a new source file fingerprint identically. This
    walks the actual bytes instead.
    """
    paths = set()
    paths.update(p for p in _git(root, "diff", "--name-only", "HEAD").splitlines() if p)
    paths.update(
        p for p in _git(root, "ls-files", "--others", "--exclude-standard").splitlines() if p
    )
    reviewed = sorted(p for p in paths if not _is_runtime_state(p, config))
    digest = hashlib.sha256()
    for path in reviewed:
        full = Path(root) / path
        content = full.read_bytes() if full.is_file() else b"<deleted>"
        digest.update(path.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()[:16], reviewed


def provenance(root, config):
    """Identify exactly what state a message refers to."""
    snapshot, reviewed = reviewed_snapshot(root, config)
    out = {
        "commit": _git(root, "rev-parse", "HEAD")[:12] or None,
        "snapshot_sha256": snapshot,
        "uncommitted_paths": len(reviewed),
        "clean": not reviewed,
    }
    for relative in config.get("provenance_files", []):
        target = Path(root) / relative
        if target.is_file():
            key = Path(relative).stem + "_sha256"
            out[key] = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    return out


# ------------------------------------------------------------------------- mailbox


def outbox(root, agent):
    return mailbox(root) / f"{agent}.outbox.jsonl"


def read_all(path, strict=False):
    """Parse complete, newline-terminated records only.

    A torn append leaves a partial trailing line. Parsing it raises, which would
    discard every complete record before it. The incomplete tail is deferred, not
    skipped, so a cursor never advances past a record not read whole.
    """
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines(keepends=True):
        if not line.endswith("\n"):
            break
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if strict:
                raise
            print(f"  ! skipping malformed record: {exc}", file=sys.stderr)
    return records


@contextmanager
def _lock(root):
    path = mailbox(root) / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX fallback
        yield
        return
    with open(path) as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def next_id(root, agent):
    """Next sequential id. Only meaningful while the mailbox lock is held."""
    return f"{agent}-{len(read_all(outbox(root, agent))) + 1:04d}"


def append(root, agent, message, config=None):
    """Allocate the id and append inside ONE critical section.

    Allocating outside the lock lets two senders read the same count and issue
    duplicate ids.
    """
    config = config or load_config(root)
    missing = [key for key in REQUIRED if not message.get(key)]
    if missing:
        raise ValueError(f"refusing to send a record missing {missing}")
    if agent not in config["agents"]:
        raise ValueError(f"unknown agent {agent!r}; expected one of {config['agents']}")
    path = outbox(root, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(root):
        message["id"] = next_id(root, agent)
        with open(path, "a") as handle:
            handle.write(json.dumps(message, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return message


def show_inbox(root, agent, config, show_all=False):
    other = [a for a in config["agents"] if a != agent][0]
    cursor = mailbox(root) / f".{agent}.cursor"
    seen = int(cursor.read_text().strip()) if cursor.exists() else 0
    messages = read_all(outbox(root, other))
    pending = messages if show_all else messages[seen:]
    if not pending:
        print(f"no new messages from {other} ({len(messages)} total)")
        return pending
    for message in pending:
        # Every field is optional at display time: one malformed record must not hide
        # every genuine message behind it.
        prov = message.get("provenance") or {}
        head = f"{message.get('id', '<no id>')}  {str(message.get('type', 'unknown')).upper()}"
        if message.get("severity"):
            head += f"  [{message['severity']}]"
        if message.get("replies_to"):
            head += f"  re: {message['replies_to']}"
        if message.get("status") and message["status"] != "open":
            head += f"  ({message['status']})"
        print("=" * 78)
        print(head)
        print(
            f"  at {message.get('ts', '<no ts>')}  commit {prov.get('commit')} "
            f"snapshot {prov.get('snapshot_sha256')}"
        )
        if not set(REQUIRED) | {"id"} <= set(message):
            missing = sorted((set(REQUIRED) | {"id"}) - set(message))
            print(f"  ! INCOMPLETE RECORD, missing {missing} -- treat with suspicion")
        for label in ("ref", "claim", "expect"):
            if message.get(label):
                print(f"  {label + ':':9s} {message[label]}")
        if message.get("evidence"):
            kind = message.get("evidence_kind", "command")
            print(f"  evidence ({kind}) -- INSPECT BEFORE RUNNING:")
            for line in message["evidence"].splitlines():
                print(f"    | {line}")
    cursor.write_text(str(len(messages)))
    print("=" * 78)
    print(f"{len(pending)} message(s); cursor advanced to {len(messages)}")
    return pending


# --------------------------------------------------------------------------- watch

# Message ids are generated as "<agent>-NNNN". Anything else came from a hand-written
# record, so it is replaced rather than passed on.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _substitute(command, fresh, other):
    """Fill {ids}/{count}/{agent} in a --exec template, shell-escaped.

    Every value crosses a trust boundary: it is read from the OTHER agent's outbox,
    which this protocol treats as untrusted by design. Interpolating it raw into a
    shell command is remote code execution on the watcher -- and quoting the
    placeholder in the template does not help, because a crafted value simply closes
    the quote. So each value is shlex-quoted, and ids that do not look like ids are
    replaced outright.
    """
    ids = ",".join(
        (i if SAFE_ID.match(i := str(m.get("id", ""))) else "<malformed-id>") for m in fresh
    )
    return (
        command.replace("{ids}", shlex.quote(ids))
        .replace("{count}", shlex.quote(str(len(fresh))))
        .replace("{agent}", shlex.quote(str(other)))
    )


def watch(
    root,
    config,
    watcher,
    interval=5.0,
    command=None,
    once=False,
    from_start=False,
    stream=None,
):
    """Poll the other agent's outbox and report each new message.

    The watch cursor is deliberately SEPARATE from the agent's read cursor
    (`.watch.<watcher>.cursor` vs `.<watcher>.cursor`). A watcher that advanced the
    read cursor would consume the agent's unread queue, so `--inbox` would then show
    nothing and the messages the watcher announced would be invisible to the agent
    that needs to act on them.

    Polling rather than inotify: the mailbox often lives on a Windows drive mounted
    into WSL, where inotify events are unreliable. A 5s poll costs nothing and works
    on every filesystem.
    """
    import time

    stream = stream or sys.stdout
    other = [a for a in config["agents"] if a != watcher][0]
    path = outbox(root, other)
    cursor = mailbox(root) / f".watch.{watcher}.cursor"
    seen = (
        0
        if from_start
        else (int(cursor.read_text().strip()) if cursor.exists() else len(read_all(path)))
    )
    print(
        f"watching {path.name} for {watcher} (from record {seen}, every {interval}s)",
        file=stream,
        flush=True,
    )
    while True:
        messages = read_all(path)
        fresh = messages[seen:]
        if fresh:
            for message in fresh:
                print(
                    f"[{message.get('ts', '?')}] {message.get('id', '?')} "
                    f"{str(message.get('type', '?')).upper()}"
                    f"{' [' + message['severity'] + ']' if message.get('severity') else ''}"
                    f" {(message.get('claim') or '')[:100]}",
                    file=stream,
                    flush=True,
                )
            seen = len(messages)
            cursor.write_text(str(seen))
            if command:
                filled = _substitute(command, fresh, other)
                print(f"  -> {filled}", file=stream, flush=True)
                subprocess.run(filled, shell=True, cwd=root, check=False)  # noqa: S602
            if once:
                return fresh
        elif once:
            return []
        time.sleep(interval)


# ---------------------------------------------------------------------------- init

SHIM = '''#!/usr/bin/env python3
"""Thin shim so agents can run `python collab/collab.py` from the project root."""

import runpy
import sys
from pathlib import Path

TOOL = Path({tool!r})
if not TOOL.is_file():
    sys.exit(f"agent-collab tool not found at {{TOOL}} -- see {{TOOL.parent}}/QUICKSTART.md")
sys.argv[0] = str(TOOL)
runpy.run_path(str(TOOL), run_name="__main__")
'''


def init(root, agents, force=False):
    box = mailbox(root)
    box.mkdir(parents=True, exist_ok=True)
    created = []
    for agent in agents:
        path = outbox(root, agent)
        if not path.exists():
            path.touch()
            created.append(path.name)
    config_path = box / "config.json"
    if force or not config_path.exists():
        config_path.write_text(
            json.dumps(
                {"agents": list(agents), "provenance_files": [], "exclude": []},
                indent=2,
            )
            + "\n"
        )
        created.append("config.json")
    for name, dest in (
        ("OWNERSHIP.md", box / "OWNERSHIP.md"),
        ("AGENTS.md", Path(root) / "AGENTS.md"),
    ):
        source = TEMPLATES / name
        if source.is_file() and (force or not dest.exists()):
            text = source.read_text().replace("{{TOOL}}", str(HERE))
            dest.write_text(text)
            created.append(str(dest.relative_to(root)))
    shim = box / "collab.py"
    if force or not shim.exists():
        shim.write_text(SHIM.format(tool=str(HERE / "collab.py")))
        created.append("collab/collab.py")
    readme = box / "README.md"
    if force or not readme.exists():
        readme.write_text(
            f"# Agent mailbox\n\nProtocol and full documentation: `{HERE}/README.md`\n\n"
            f"```bash\npython collab/collab.py --from <agent> --inbox\n```\n"
        )
        created.append("collab/README.md")
    print(f"initialised mailbox in {box}")
    for name in created:
        print(f"  + {name}")
    print("\nAdd to .gitignore if you do not want mailbox traffic in history:")
    print("  collab/*.outbox.jsonl\n  collab/.lock\n  collab/.*.cursor")
    return created


# ---------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", nargs="?", choices=("init", "status", "watch"), default=None)
    parser.add_argument("--root", help="project root (default: git top level of cwd)")
    parser.add_argument("--from", dest="agent", default=os.environ.get("COLLAB_AGENT"))
    parser.add_argument("--agents", nargs=2, metavar=("A", "B"), default=list(DEFAULT_AGENTS))
    parser.add_argument("--force", action="store_true", help="init: overwrite existing files")
    parser.add_argument("--inbox", action="store_true")
    parser.add_argument("--all", action="store_true", help="with --inbox, show history")
    parser.add_argument("--type", choices=TYPES)
    parser.add_argument("--severity", choices=("P1", "P2", "P3"))
    parser.add_argument("--ref")
    parser.add_argument("--claim")
    parser.add_argument("--expect")
    parser.add_argument("--evidence")
    parser.add_argument("--evidence-file")
    parser.add_argument("--evidence-kind", choices=("command", "citation"), default="command")
    parser.add_argument("--replies-to")
    parser.add_argument("--task", help="task id, for independent threads under one agent")
    parser.add_argument("--status", choices=("open", "fixed", "withdrawn"), default="open")
    parser.add_argument("--interval", type=float, default=5.0, help="watch: poll seconds")
    parser.add_argument(
        "--exec",
        dest="exec_cmd",
        help="watch: shell command per new batch; {ids} {count} {agent}",
    )
    parser.add_argument("--once", action="store_true", help="watch: exit after one batch")
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="watch: include messages already in the outbox",
    )
    args = parser.parse_args(argv)

    root = find_root(args.root)
    if args.command == "init":
        init(root, args.agents, force=args.force)
        return 0

    config = load_config(root)
    if args.command == "status":
        print(f"root:    {root}")
        print(f"agents:  {', '.join(config['agents'])}")
        for agent in config["agents"]:
            print(f"  {agent}.outbox.jsonl: {len(read_all(outbox(root, agent)))} message(s)")
        prov = provenance(root, config)
        print(
            f"commit {prov['commit']}  snapshot {prov['snapshot_sha256']}  "
            f"uncommitted {prov['uncommitted_paths']}"
        )
        return 0

    if not args.agent:
        parser.error("--from is required (or set COLLAB_AGENT)")
    if args.agent not in config["agents"]:
        parser.error(f"--from must be one of {config['agents']}")
    if args.command == "watch":
        watch(
            root,
            config,
            args.agent,
            interval=args.interval,
            command=args.exec_cmd,
            once=args.once,
            from_start=args.from_start,
        )
        return 0
    if args.inbox:
        show_inbox(root, args.agent, config, args.all)
        return 0
    if not args.type:
        parser.error("--type is required when sending (or use --inbox)")

    evidence = args.evidence
    if args.evidence_file:
        evidence = Path(args.evidence_file).read_text()
    if args.type in ("finding", "claim") and not evidence:
        parser.error("a finding or claim needs --evidence or --evidence-file")

    message = {
        "id": None,  # assigned under the mailbox lock
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "from": args.agent,
        "type": args.type,
        "provenance": provenance(root, config),
        "status": args.status,
    }
    message.update(
        {
            key: value
            for key, value in (
                ("severity", args.severity),
                ("ref", args.ref),
                ("claim", args.claim),
                ("expect", args.expect),
                ("replies_to", args.replies_to),
                ("task", args.task),
            )
            if value
        }
    )
    if evidence:
        message["evidence"] = evidence
        message["evidence_kind"] = args.evidence_kind

    append(root, args.agent, message, config)
    print(
        f"sent {message['id']} ({message['type']}) @ {message['provenance']['commit']} "
        f"snapshot={message['provenance']['snapshot_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
