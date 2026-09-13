# Quickstart

## Install a fixed version

Use Python 3.11+ on Linux/WSL and a virtual environment. From a checked-out, reviewed
revision of this repository:

```bash
python -m pip install .
agent-collab --version
```

Pin the commit or wheel artifact when deploying. 0.2.0 is the package's development
version; these instructions do not imply it has been published to PyPI. For development,
`python /path/to/agent-collab/collab.py ...` also works. Project shims import the installed
package and contain no developer-specific absolute path.

## Initialize a project

The project must have an initial Git commit. Each running agent instance needs a unique
ID, even when several use the same model/provider.

```bash
agent-collab init --agents claude-builder codex-reviewer claude-tests
agent-collab register --from codex-second-reviewer
agent-collab status
```

Names are 1–48 ASCII letters/digits/underscores/hyphens, starting with a letter or digit;
they are not paths or display names. The supported limit is 32 registered instances.

`init` preserves existing instructions. It creates `AGENTS.md`, a `CLAUDE.md` import when
absent, and a portable `collab/collab.py` shim. If your existing instruction files contain
custom content, incorporate the generated protocol instructions without discarding that
content. `--force` explicitly regenerates framework instructions and the shim; it does
not overwrite an existing `CLAUDE.md`.

Tell each agent its ID and ask it to read `AGENTS.md`. Codex reads that file directly;
Claude Code uses `CLAUDE.md`, which should include `@AGENTS.md`. Both can use the same CLI.
The baseline is checkpoint inbox reads; automatic notifications are optional.

Separate worktrees share the store through Git's common directory automatically:

```bash
git worktree add ../project-review -b review/task
# Run agent-collab from either worktree. Sending captures THAT worktree's revision.
```

## Messages and approvals

```bash
agent-collab --from claude-builder --to codex-reviewer --type claim \
  --claim 'Ready for review' --evidence 'pytest -q'
agent-collab --from codex-reviewer --inbox
agent-collab --from codex-reviewer --type received --replies-to <proposal-id>

# After reviewing and testing the exact proposal revision:
agent-collab --from codex-reviewer --type approved --replies-to <proposal-id> \
  --commit <full-commit> --snapshot <full-snapshot-sha256>

# The integrator checks on the exact worktree to be integrated:
agent-collab check-approval --id <approval-id>
```

Replies must name a message delivered to the sender. An approval targets a `claim`,
`finding`, or `handoff`; arbitrary messages and imported legacy approvals cannot authorize
integration. Changing the worktree makes an approval stale. Reissue the proposal and
approval after substantive changes. Do not modify reviewable files between checking and
integration; the tool does not enforce Git permissions.

`--inbox` prints up to 100 unread messages and marks those displayed as read. `--all`
prints history without consuming it. Continue paging with the last returned `seq` through
`--after`; no records returned means there is no eligible record in that page. Invalid
persisted records are quarantined and removed from the unread head so later reads progress.
Send explicit `received` messages to confirm receipt to other participants.

Optional send fields: `--severity P1|P2|P3`, `--ref`, `--expect`, `--task`,
`--evidence-kind command|citation`, `--status open|fixed|withdrawn`, and `--evidence-file`.
Findings and claims require evidence. `--ref` identifies a file inside this repository,
optionally with a line suffix; external citations belong in evidence. Review another
repository through that repository's own mailbox. Evidence is data and is never run.

## Ownership and operator visibility

```bash
agent-collab claim-path --from claude-builder --path src --lease-seconds 900
agent-collab release-path --from claude-builder --path src
agent-collab activity --follow
agent-collab activity --events --follow
```

A claim covers a path and its descendants. Renew with the same command before it expires;
lease durations are 1–86,400 seconds. Release and subsequent acquisition are separate
transactions; arrange a handoff with the peer and verify the new owner acquired the lease.
A lease is coordination, not an OS filesystem lock.

## Configuration, storage and retention

`init --config /absolute/config.json` accepts:

```json
{"agents":["claude-builder","codex-reviewer"],
 "provenance_files":["data/manifest.json"],
 "exclude":[],
 "max_messages":100000}
```

Configuration is stored in the shared database; editing an old `collab/config.json` does
not change it. `configure --config FILE` validates an explicit replacement. Existing
agent IDs cannot be removed; capacity cannot fall below retained message count. Exclusions
are exact repository-relative paths and are themselves bound into the snapshot policy.
Do not exclude code that approval is supposed to cover.

The configured capacity may be 1–1,000,000 messages; load evidence currently extends to
100,000. Capacity is a message-count bound, not a disk-space reservation. A full database
or disk returns an error; it does not acknowledge a send. The store uses SQLite rollback
journaling with `synchronous=FULL` and local filesystem locking. Do not change its journal
mode or share it over a network filesystem. Native Windows is not supported.

`export` streams all message records. For conservative retention:

```bash
agent-collab prune --before <unix-timestamp> --archive /absolute/new-archive.jsonl
```

Each prune archives at most 1,000 completed, unreferenced `ping` messages before deleting
them. Unread messages, pending notification work, unscanned subscriptions, replies, and
review evidence remain. Existing archive files are never overwritten. The archive and
its directory are synced before deletion commits. Disk-full or rollback can leave an
archive containing messages still retained; duplicates are safe and identifiable by ID.
Administrative audit history remains. For research/approval history growth, export and
raise capacity deliberately within the supported configuration range.

Stop clients before backing up the entire state directory, or use SQLite's online backup
API. Database files, journals, logs and exports may contain sensitive project information.
`status` prints the state location; ordinary Git commits do not include that directory.
