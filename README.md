# agent-collab

A durable, local evidence exchange for cooperating coding agents. Give each instance
its own ID, point it at the project's `AGENTS.md`, and use the same CLI from Claude Code,
Codex, or another agent that can run commands. Multiple instances of one provider work too.

Version 0.2 is an alpha for **up to 32 admitted instances on one Linux/WSL host**, under
one trusted OS owner. It is not an isolation boundary between malicious processes.
Read [SECURITY.md](SECURITY.md) before choosing a deployment model.

```bash
# In a Git project with an initial commit, after installing the package:
agent-collab init
agent-collab request-entry --provider "Claude Code" --display-name "Builder" --purpose "Implement"
# The user approves the returned request; see Quickstart for the complete admission flow.
# Use the assigned IDs below in place of these illustrative names.

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
ownership leases in `<git-common-dir>/agent-collab/` by default. A shared local Git setting,
`agent-collab.stateDirectory`, can select an absolute directory on a local Linux filesystem
outside the worktree; see [MIGRATION.md](MIGRATION.md) before moving existing state. Separate Git worktrees automatically
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

An instance must declare its provider, display name and purpose with `request-entry` and
wait for explicit user permission. Approval assigns its ID. Agents must not self-approve
or approve peers without the user's decision for that request. See the admission workflow
in [QUICKSTART.md](QUICKSTART.md); permission records are visible in the audit.

1. **Evidence before agreement.** Findings and claims need evidence. Inspect commands
   before running them; never execute an evidence field automatically. Reproduce in a
   disposable repository. Record actual output and uncertainty.
2. **Receipt is separate from approval.** `received` confirms reading, `reproduced` records
   a checked result, `disputed` records disagreement, and `approved` authorizes a specific
   proposal under the cooperating-agent protocol. Reading an inbox is not a receipt reply.
3. **Approvals identify the reviewed subject.** An approval requires an existing proposal,
   its full commit and snapshot, and a matching current worktree. Run `check-approval`
   immediately before integration on a fixed revision. Review in your own checkout: `--root`
   selects the tree fingerprinted, so pointing at the proposer's tree is not an independent
   checkout check. It rejects stale approvals. This
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

## Things that went wrong

These were observed failures; the last column describes the current rule or implementation.

| What happened | Response |
|---|---|
| Fingerprints missed new-file contents, unusual names or executable bits | Hash bytes and types; use NUL-delimited Git paths |
| Sending changed the reviewed snapshot | Keep mailbox state outside tracked files |
| Concurrent senders reused IDs; torn appends damaged following records | UUID IDs and transactional SQLite |
| One malformed record hid valid traffic | Validate bounded records; inbox quarantines corruption; read-only exports fail visibly |
| Tests wrote to a peer's outbox or restored a live mailbox | Reproduce only in disposable repositories |
| `--force` was parsed but never forwarded | Exercise CLI flags end to end |
| A watcher consumed the agent's unread queue | Separate delivery and notification state |
| A monitor regex missed 16 of 22 records because key order differed | Parse JSON |
| Shell templates interpolated untrusted identifiers | Literal argv and JSON stdin; no shell templates |
| `chmod` silently left the live Windows-mounted store at 777 | Recheck directory/file modes; keep state on Linux storage |
| An approval fingerprinted the proposer's worktree | Reviewer uses their own checkout at the proposed revision |
| Two reviewer sessions shared one identity and duplicated findings | Each instance declares itself, obtains user approval and receives a unique ID |

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
