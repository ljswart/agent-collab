# agent-collab

A shared mailbox for two coding agents (Claude Code and Codex CLI) editing one repository.

Without a channel they overwrite each other's edits, re-review work that has moved on, and
agree on things neither has checked. This gives them append-only outboxes,
content-addressed provenance, and a vocabulary that keeps *"I read this"* apart from *"I
approve this revision"*. It is an evidence exchange, not a chat.

```bash
python /path/to/agent-collab/collab.py init        # once, in the project root

# A reports something, with a way to check it
python collab/collab.py --from claude --type finding --severity P1 \
  --ref src/sizing.py:151 \
  --claim "position size reads a price that is not yet observable" \
  --evidence-file /tmp/repro.py --expect "P&L differs: 1005.20 vs 1188.71"

# B reads it, checks it, answers
python collab/collab.py --from codex --inbox
python collab/collab.py --from codex --type reproduced --replies-to claude-0001 \
  --status fixed --claim "reproduced and fixed" --evidence "pytest -q" --expect "8 passed"
```

Python 3.11+, `git`, one file, standard library only. `pytest` only for the tests.

- [QUICKSTART.md](QUICKSTART.md) — install, configure, CLI reference
- [NOTIFICATIONS.md](NOTIFICATIONS.md) — how each agent learns mail has arrived

## Layout

```
<project>/
  AGENTS.md                  # points both agents at this protocol
  collab/
    collab.py                # shim -> this tool
    config.json              # agent names, provenance files, exclusions
    claude.outbox.jsonl      # claude appends; codex reads
    codex.outbox.jsonl       # codex appends; claude reads
    OWNERSHIP.md             # who holds which paths
    .lock  .<agent>.cursor  .watch.<agent>.cursor
```

Each agent appends only to its own outbox and never edits it. One writer per file makes
write conflicts structurally impossible.

## Message

```json
{"id": "claude-0007", "ts": "…", "from": "claude",
 "type": "finding | claim | question | handoff | escalate | received | reproduced | disputed | approved | ping",
 "replies_to": "codex-0004", "severity": "P1|P2|P3", "task": "optional-thread-id",
 "ref": "src/module.py:151",
 "claim": "one sentence, falsifiable",
 "evidence": "a command that reproduces it, or a citation",
 "evidence_kind": "command | citation",
 "expect": "what the evidence should show",
 "provenance": {"commit": "…", "snapshot_sha256": "…", "uncommitted_paths": 3, "clean": false},
 "status": "open | fixed | withdrawn"}
```

`id` and `provenance` are filled in by the tool. `finding` and `claim` require evidence.

## The eight rules

**1. Provenance names exactly what was reviewed.** Every message carries `commit` and
`snapshot_sha256`, a hash over the bytes of every modified or untracked file. Hashing
`git status` + `git diff` is not enough: an untracked file appears as `?? path` and its
contents never enter the hash. Mailbox files are excluded from the snapshot; otherwise
sending a message would stale every open approval. An approval is scoped to the revision
it names. If either value has moved, it is stale and must be reissued.

**2. Ownership is explicit.** `collab/OWNERSHIP.md` records who holds which paths. A
transfer is a `handoff` that takes effect when the other agent replies `received`. For
simultaneous work in one area use separate git worktrees, so the reviewer tests a fixed
revision.

**3. Receipt is not agreement.**

| type | means |
|---|---|
| `received` | I have the message. No claim about correctness. |
| `reproduced` | I ran the evidence and got the stated result. Paste the real output. |
| `disputed` | I checked and disagree; here is my evidence. |
| `approved` | This exact revision is correct and may be integrated. Must carry `--replies-to`. |

Only `approved` authorises integration, and only for the revision it names. A message is
not delivered until it is answered with `received`.

**4. Evidence commands are inspected before they run.** An `evidence` field is code
written by another process. Read it before executing it; never pipe it to a shell unseen.
If it would write outside the repo, touch credentials, reach the network, or delete
anything, do not run it: reply `disputed` and ask for a narrower reproduction. Findings
without a runnable repro (architecture, docs, scope, "the conclusion overreaches the
evidence") use `evidence_kind: "citation"`.

**5. Reproductions must not mutate shared state.** A test, repro or diagnostic never
writes to a live mailbox, the other agent's files, or shared source. "Append and restore"
is a lost-update race with tidy-looking cleanup. Build a temporary git repository, point
`--root` at it, exercise that. Verify rather than assume:

```bash
python - <<'EOF'
import hashlib, subprocess
from pathlib import Path
watch = ["collab/claude.outbox.jsonl", "collab/codex.outbox.jsonl", "collab/collab.py"]
snap = lambda: {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in watch}
before = snap(); subprocess.run(["python", "-m", "pytest", "-q"]); after = snap()
print("mutated:", [p for p in before if before[p] != after[p]] or "NONE")
EOF
```

**6. Disagreement is bounded and does not block the queue.** After two failed rounds on
one claim, both agents send `escalate`: position in five lines or fewer, plus the evidence
it rests on. Unrelated work continues. Where a test can settle it, write the failing test;
if no test can be written, the claim is not concrete enough to act on.

**7. Agreement between agents is not evidence.** Two agents concurring does not make a
claim true, and is weakest where it feels strongest: performance, profitability, "this is
now correct". Convergence is a reason to check harder. Record what was measured, not what
was agreed. (Independent support: [Qiu & Gill 2026](https://arxiv.org/abs/2608.18167)
report agents converging without evidence as the dominant failure of cooperative review.)

**8. One integrator commits.** The designated agent commits only revisions the other has
`approved` by `commit` + `snapshot_sha256`. Record the commit identity in `OWNERSHIP.md`.

## Things that went wrong

Each rule traces to one of these.

| what happened | what it produced |
|---|---|
| Fingerprint was blind to new-file contents | Rule 1: content-addressed snapshot |
| Sending a message changed the fingerprint | Rule 1: mailbox excluded |
| Two senders allocated the same message id | id assigned inside the append lock |
| A torn append crashed the reader, losing every earlier record | parse complete records only; defer the tail |
| One record missing `ts` hid the whole mailbox | every field optional at display; validated on send |
| Test probes were written into the *other agent's* outbox | Rule 5 |
| A test read, wrote and "restored" the live mailbox | Rule 5: restore is a lost-update race |
| `--force` was parsed then dropped before reaching `init()` | flags wired end to end and tested |
| A watcher would have consumed the agent's unread queue | separate watch cursor ([NOTIFICATIONS.md](NOTIFICATIONS.md)) |
| A `Monitor` regex assumed key order; 16 of 22 records were missed | match keys independently, or parse the JSON |
| `watch --exec` interpolated the counterpart's `id` into a shell command | substituted values are `shlex`-quoted and ids validated |

## Security

Two places execute or trust input from the other agent.

- **`evidence` fields.** Rule 4. The tool prints them under `INSPECT BEFORE RUNNING` and
  never executes them itself.
- **`watch --exec`** runs your shell command with `shell=True`. Substituted values are
  `shlex`-quoted, and an `id` that does not match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`
  becomes `<malformed-id>`. Quoting the placeholder in your template would not have been
  enough — a crafted value closes the quote — so the escaping happens at substitution.
  The command itself is still yours: do not build one that re-evaluates its arguments.

Shell escaping assumes a POSIX shell. Mailbox files are plain text and may end up in git
history; do not put credentials, tokens or customer data in a message. Locking uses
`fcntl`; on non-POSIX systems appends are not serialised and concurrent senders may
duplicate ids. Reporting: [SECURITY.md](SECURITY.md).

## Scope and related work

Two agents, one repository, append-only files, rules that were each paid for. Not a
broker, task queue or agent framework.

- [OpenMOSS/claude-codex-handoff](https://github.com/OpenMOSS/claude-codex-handoff):
  same shape (per-direction JSONL, cursors, ids under lock) plus task leases and a
  cron wake; no provenance hashing or evidence vocabulary.
- [A2A](https://a2a-protocol.org/latest/), [MCP](https://modelcontextprotocol.io):
  networked protocols, a different layer. This tool is for agents with no such channel.
- [AGENTS.md](https://agents.md/): the generated file follows the spec. Claude Code reads
  `CLAUDE.md`, not `AGENTS.md`; see [QUICKSTART.md](QUICKSTART.md).

## License

MIT, see [LICENSE](LICENSE).
