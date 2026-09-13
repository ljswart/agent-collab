"""Portable command line; transactional state lives in the Git common directory."""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import __version__, notifications, safety
from .provenance import find_root
from .store import Store

PROTOCOL = """# Agents working in this repository

All agent instances use agent-collab 0.2. Run `agent-collab --version` before working.
Protocol: https://github.com/ljswart/agent-collab (README.md and SECURITY.md).

- Use your unique instance ID with `agent-collab --from ID --inbox` at checkpoints.
- A file change does not wake Codex. Receipt requires an explicit `received` reply.
- Send to explicit `--to` recipients; omission broadcasts to other registered agents.
- Inspect evidence before running it; use isolated fixtures for reproductions.
- Claim paths with `agent-collab claim-path --from ID --path PATH`; renew leases.
- Use separate worktrees. Their mailboxes share the Git common directory automatically.
- An approval names its proposal, full commit and snapshot. Verify `check-approval`
  immediately before integration on a fixed revision. Receipt is not approval.
- Two agents agreeing is not evidence. Show findings, disputes and decisions to the user.
- This is a cooperating-agent protocol under one trusted OS owner, not a sandbox.
"""
SHIM = '''#!/usr/bin/env python3
"""Portable shim; install agent-collab in the active Python environment."""
from agent_collab.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
'''


def initialize(root, settings, force=False):
    settings = safety.config(settings)  # before any writes, including state creation
    root = find_root(root)
    with Store(root) as store:
        store.initialize(settings)
    safety.safe_write(root, "AGENTS.md", PROTOCOL, overwrite=force)
    safety.safe_write(root, "CLAUDE.md", "@AGENTS.md\n")
    safety.safe_write(root, "collab/collab.py", SHIM, overwrite=force)
    safety.safe_write(
        root,
        "collab/README.md",
        "# Agent mailbox\n\nRun `agent-collab status`. State is shared across Git worktrees.\n",
        overwrite=force,
    )
    print("initialized agent-collab 0.2; existing JSONL traffic requires explicit migrate")
    print(
        "Existing AGENTS.md/CLAUDE.md are preserved; ensure they include the mailbox instructions."
    )


def parser():
    p = argparse.ArgumentParser(description="Durable local multi-agent evidence exchange")
    p.add_argument(
        "command",
        nargs="?",
        choices=(
            "init",
            "status",
            "register",
            "configure",
            "watch",
            "activity",
            "export",
            "migrate",
            "check-approval",
            "retry",
            "claim-path",
            "release-path",
            "prune",
        ),
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--root")
    p.add_argument("--from", dest="agent", default=os.environ.get("COLLAB_AGENT"))
    p.add_argument("--agents", nargs="+", default=["claude", "codex"])
    p.add_argument("--to", nargs="+")
    p.add_argument("--config", help="init: optional configuration JSON file")
    p.add_argument(
        "--force", action="store_true", help="init: regenerate framework instructions/shim"
    )
    p.add_argument("--inbox", action="store_true")
    p.add_argument(
        "--all", action="store_true", help="inbox: include read messages, without consuming"
    )
    p.add_argument("--after", type=int, default=0, help="pagination sequence, exclusive")
    p.add_argument("--limit", type=int, default=safety.MAX_BATCH)
    p.add_argument("--type", choices=safety.TYPES)
    p.add_argument("--severity", choices=("P1", "P2", "P3"))
    for key in (
        "ref",
        "claim",
        "expect",
        "evidence",
        "evidence-file",
        "replies-to",
        "task",
        "path",
    ):
        p.add_argument("--" + key)
    p.add_argument("--evidence-kind", choices=("command", "citation"), default="command")
    p.add_argument("--status", choices=("open", "fixed", "withdrawn"), default="open")
    p.add_argument("--commit", help="approval: full reviewed commit")
    p.add_argument("--snapshot", help="approval: full reviewed snapshot SHA-256")
    p.add_argument("--id", help="check-approval: approval message ID")
    p.add_argument("--consumer", default="watch")
    p.add_argument("--types", nargs="+", choices=safety.TYPES)
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--exec-argv", help="JSON argv array; absolute executable; event is JSON stdin")
    p.add_argument("--exec", dest="removed_exec", help=argparse.SUPPRESS)
    p.add_argument("--once", action="store_true")
    p.add_argument("--follow", action="store_true", help="activity: follow messages")
    p.add_argument(
        "--events", action="store_true", help="activity: show administrative audit events"
    )
    p.add_argument(
        "--skip-invalid", action="store_true", help="migrate: quarantine invalid records"
    )
    p.add_argument("--lease-seconds", type=int, default=900)
    p.add_argument("--before", type=float, help="prune: Unix timestamp cutoff")
    p.add_argument("--archive", help="prune: new absolute archive file outside repository")
    return p


def _execute(args):
    if not 1 <= args.limit <= safety.MAX_BATCH or args.after < 0:
        raise ValueError("limit must be 1..100 and after must be nonnegative")
    if args.before is not None and not math.isfinite(args.before):
        raise ValueError("before must be a finite timestamp")
    if args.removed_exec is not None:
        raise ValueError("--exec shell templates were removed; use --exec-argv with JSON stdin")
    if args.command == "init":
        settings = {"agents": args.agents}
        if args.config:
            with open(args.config, "rb") as handle:
                settings = safety.decode(handle.read(safety.MAX_RECORD + 1))
        initialize(args.root, settings, args.force)
        return 0
    with Store(args.root) as store:
        store.settings()
        if args.command == "configure":
            if not args.config:
                raise ValueError("configure requires --config")
            with open(args.config, "rb") as handle:
                store.configure(safety.decode(handle.read(safety.MAX_RECORD + 1)))
        elif args.command == "status":
            print(safety.render(store.status()))
        elif args.command in ("activity", "export"):
            after = args.after
            while True:
                if args.events:
                    rows = store.db.execute(
                        "SELECT * FROM audit WHERE seq>? ORDER BY seq LIMIT ?",
                        (after, min(args.limit, safety.MAX_BATCH)),
                    ).fetchall()
                    for row in rows:
                        print(safety.render(dict(row)), flush=True)
                    after = rows[-1]["seq"] if rows else after
                else:
                    previous = after
                    rows = store.activity(after=after, limit=args.limit)
                    for seq, message in rows:
                        print(
                            safety.render(
                                message
                                if args.command == "export"
                                else {"seq": seq, "message": message}
                            ),
                            flush=True,
                        )
                    after = store.last_scan
                if args.command == "export" and (rows or not args.events and after > previous):
                    continue
                if not args.follow:
                    break
                time.sleep(2)
        elif args.command == "check-approval":
            if not args.id:
                raise ValueError("check-approval requires --id")
            print(safety.render(store.check_approval(args.id)))
        elif args.command == "prune":
            if args.before is None or not args.archive:
                raise ValueError("prune requires --before and --archive")
            print(safety.render({"archived": store.prune(args.before, Path(args.archive))}))
        else:
            if not args.agent:
                raise ValueError("--from is required")
            if args.command == "register":
                store.register(args.agent)
            elif args.command == "watch":
                command = json.loads(args.exec_argv) if args.exec_argv else None
                return notifications.watch(
                    store,
                    args.agent,
                    consumer=args.consumer,
                    command=command,
                    types=args.types,
                    interval=args.interval,
                    timeout=args.timeout,
                    once=args.once,
                )
            elif args.command == "retry":
                print(safety.render({"retried": store.retry_failed(args.agent, args.consumer)}))
            elif args.command in ("claim-path", "release-path"):
                if not args.path:
                    raise ValueError("path operation requires --path")
                if args.command == "claim-path":
                    store.claim(args.agent, args.path, seconds=args.lease_seconds)
                else:
                    store.release(args.agent, args.path)
            elif args.command == "migrate":
                if not args.path:
                    raise ValueError("migrate requires --path to a legacy JSONL outbox")
                print(
                    safety.encode(
                        {
                            "imported": store.migrate(
                                Path(args.path), args.agent, skip_invalid=args.skip_invalid
                            )
                        }
                    )
                )
            elif args.inbox:
                rows = store.inbox(
                    args.agent, all_messages=args.all, after=args.after, limit=args.limit
                )
                for seq, message in rows:
                    print(safety.render({"seq": seq, "message": message}))
                sys.stdout.flush()
                if not args.all:
                    store.mark_read(args.agent, [seq for seq, _ in rows])
            else:
                if not args.type:
                    raise ValueError("sending requires --type")
                fields = {
                    key: getattr(args, key)
                    for key in (
                        "claim",
                        "ref",
                        "expect",
                        "evidence",
                        "evidence_kind",
                        "replies_to",
                        "task",
                        "severity",
                        "status",
                    )
                    if getattr(args, key) is not None
                }
                if args.evidence_file:
                    with open(args.evidence_file, "rb") as handle:
                        raw = handle.read(safety.MAX_RECORD + 1)
                    if len(raw) > safety.MAX_RECORD:
                        raise ValueError("evidence file is too large")
                    fields["evidence"] = raw.decode("utf-8")
                subject = (args.commit, args.snapshot) if args.commit and args.snapshot else None
                print(
                    safety.encode(
                        store.send(
                            args.agent, kind=args.type, to=args.to, fields=fields, subject=subject
                        )
                    )
                )
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return _execute(args)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(safety.render({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
