# agent-collab

A durable, local evidence exchange for cooperating coding agents. Give each instance
its own ID, point it at the project's `AGENTS.md`, and use the same CLI from Claude Code,
Codex, or another agent that can run commands. Multiple instances of one provider work too.

Version 0.2 is an alpha for **1–32 registered instances on one Linux/WSL host**, under
one trusted OS owner. It is not an isolation boundary between malicious processes.
Read [SECURITY.md](SECURITY.md) before choosing a deployment model.

```bash
# In a Git project with an initial commit, after installing the package:
agent-collab init --agents claude-builder codex-reviewer claude-tests

agent-collab --from claude-builder --to codex-reviewer claude-tests \
  --type finding --claim 'A reproducible failure' --evidence 'pytest -q tests/test_example.py'

agent-collab --from codex-reviewer --inbox
# Use the actual returned message ID:
agent-collab --from codex-reviewer --type received --replies-to <message-id>
```

- [Quickstart and CLI](QUICKSTART.md)
- [Notifications and provider integration](NOTIFICATIONS.md)
- [Upgrade from JSONL mailboxes](MIGRATION.md)
- [Security model and reporting](SECURITY.md)
- [Validation and measured limits](VALIDATION.md)

## What the framework does

SQLite transactions persist messages, recipient delivery state, notification jobs and
ownership leases in `<git-common-dir>/agent-collab/`. Separate Git worktrees automatically
share that store while each sender fingerprints its own worktree. No daemon, server,
network listener, model API key, or runtime Python dependency is required.

Messages carry a project identity; cross-repository file references are rejected. Use
a separate mailbox for each project and put external citations in evidence.
Message IDs are independent of history length. Indexed reads return bounded pages;
no watcher reparses an old outbox. JSONL remains an explicit import/export format.
A send returns success after the database transaction commits. At-least-once notification
attempts can duplicate an external side effect after a crash, so callbacks must deduplicate
by message ID. Successful notification is not proof of agent receipt.

Messages include a schema version, sender, explicit recipients, type, timestamp, evidence,
reply ID and full commit/snapshot provenance. Omitting `--to` broadcasts to the other
registered instances; a reply defaults to the original sender. Message bodies are limited
to 64 KiB and reads to 100 records per page. The default retained-message capacity is
100,000, with an explicit error when full. See configuration and retention in the quickstart.

## The protocol

1. **Evidence before agreement.** Findings and claims need evidence. Inspect commands
   before running them; never execute an evidence field automatically. Reproduce in a
   disposable repository. Record actual output and uncertainty.
2. **Receipt is separate from approval.** `received` confirms reading, `reproduced` records
   a checked result, `disputed` records disagreement, and `approved` authorizes a specific
   proposal under the cooperating-agent protocol. Reading an inbox is not a receipt reply.
3. **Approvals identify the reviewed subject.** An approval requires an existing proposal,
   its full commit and snapshot, and a matching current worktree. Run `check-approval`
   immediately before integration on a fixed revision. It rejects stale approvals. This
   tool does not intercept arbitrary `git commit` commands or hold integration credentials.
4. **Own paths explicitly.** `claim-path` atomically acquires a lease and rejects overlapping
   claims by other agents. Renew during long work; release when done. Discuss handoffs in
   messages and use separate worktrees for independent changes. Leases coordinate willing
   participants; they do not stop arbitrary filesystem writes.
5. **Disagreement remains visible.** After two unsuccessful exchanges on one claim, send
   `escalate` with each position and its evidence. Unrelated work continues. The discussion
   rule is an agent instruction, not a claim that the software understands arguments.
6. **One integrator, visible decisions.** Choose an integrator and commit identity for the
   project. The integrator checks approval before integration. Record an explicit user
   override as an escalation message before acting; never manufacture approval afterward.

Full snapshot hashing includes file bytes, executable mode, symlink target, deletions,
submodule state and named provenance inputs. Git paths use NUL delimiters. Two passes
reject observable worktree mutation; review immutable revisions for integration. This
is not an atomic snapshot against an adversary racing file changes. Missing declared
provenance inputs fail closed. Full relative input paths avoid basename collisions.

## See the communication

```bash
agent-collab activity --follow           # full messages, evidence and agent verdicts
agent-collab activity --events --follow  # leases, configuration and notification outcomes
agent-collab status                      # queue totals, failures, quarantine and state location
agent-collab export > messages.jsonl     # complete message export, in bounded pages
```

Outputs are JSON/JSONL with terminal controls escaped. Sequence numbers support pagination
with `--after`. The audit records what happened; an agent's claim remains a claim until
its evidence is checked. Raw evidence is retained in messages, never secretly executed.
Administrative activity is a cooperating-owner audit trail, not tamper-proof storage.

## Compatibility

Python 3.11–3.13 are covered by the CI matrix. The measured local environment and limits
are in [VALIDATION.md](VALIDATION.md). Linux/WSL POSIX filesystem operations are required;
native Windows fails explicitly. Keep authoritative state on a tested local Linux
filesystem. Network filesystems and distributed deployments are not supported. Git
worktree data and the mailbox stay local; cloning a repository does not clone its mailbox.

0.2 intentionally removes shell `--exec` templates and changes JSONL files from the
primary store to migration/export artifacts. Existing mailboxes are never imported or
modified implicitly. Follow [MIGRATION.md](MIGRATION.md) before switching live agents.

## Development

```bash
python -m pytest tests -q
ruff check .
ruff format --check .
python scripts/benchmark.py --records 100000
```

Tests and benchmarks use disposable repositories. CI additionally builds and installs a
wheel. See [VALIDATION.md](VALIDATION.md) for the scope of verification.

Related projects and standards: [agent-message-queue](https://github.com/avivsinai/agent-message-queue),
[claude-codex-handoff](https://github.com/OpenMOSS/claude-codex-handoff),
[A2A](https://a2a-protocol.org/latest/), [MCP](https://modelcontextprotocol.io),
and [AGENTS.md](https://agents.md/).

MIT license. Copyright Lucienne Swart.
