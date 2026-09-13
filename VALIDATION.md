# Validation of the 0.2 development candidate

This records measured behavior, not an independent security certification. All tests use
disposable repositories. No live mailbox migration or automatic provider resume was run.

## Correctness and fault handling

The regression suite covers the public-release audit findings and additional review:

- Invalid agent names, traversal, duplicate identities and reserved sender fields.
- Routed broadcast/direct messages and independent inboxes across more than two agents.
- Shared delivery across Git worktrees with each worktree's own revision provenance.
- Full snapshot changes for unusual filenames, executable mode, symlink targets,
  submodule contents and named inputs; missing inputs and concealed Git index entries
  fail closed, as do observable concurrent changes.
- Approval target existence, explicit subjects, recipient eligibility and stale rejection.
- Malformed/oversized JSON, duplicate keys, wrong field types, incomplete legacy tails,
  corrupt persisted payloads, migration rollback/idempotence and historical provenance.
- Literal notification argv, JSON stdin, independent inbox state, retry/backoff, terminal
  failures, expired leases, stale completion, acknowledgement filters, rate gates and timeouts.
- Path-lease conflicts/renewal/release and conservative archive-before-delete retention.
- 32 registered instances with 31 simultaneous sending processes: 62 new messages,
  no loss or duplicate IDs, and successful SQLite integrity check.
- A writer killed inside a transaction: rollback succeeded and preceding acknowledged
  messages remained available alongside later sends.
- Real SQLITE_FULL via a page limit: no send acknowledgement and no partial message.

The real legacy communication files were read into disposable snapshots. Strict import
initially rejected the historical fingerprint variants; the corrected importer preserved
all 58 records in the recorded snapshot (32 Claude, 26 Codex), including review-only
provenance. These are private project records and are not bundled in this repository.
Synthetic regressions cover the same variants. Counts increase as those live agents talk.

## Long-history benchmark

Run `python scripts/benchmark.py --records 100000`. Fixture seeding is batched and is not
reported as send throughput. Each measured send includes worktree fingerprinting and a
separate durable transaction. Inbox timings use tracemalloc and a 100-record page. Idle
poll timings represent a consumer already caught up with history. The script reports the
actual query plan to verify indexed lookup.

The latest recorded run is in [validation/load-results.json](validation/load-results.json).
It uses Python 3.12.3, SQLite 3.45.1 and a temporary local Linux filesystem, with 32 registered
instances. This is one host and a small source fixture, not a guarantee for every repository
or storage device. Large worktrees and provenance inputs add hashing cost independently
of mailbox history. Memory numbers are Python allocations, not total process RSS.

The important property is bounded inbox pages and indexed incremental polling. Historic
JSONL readers parsed the entire log; the new reader's query plan searches the unread index
and retrieves messages by primary key. Administrative full exports and maintenance are
explicit operations and naturally take time proportional to the work requested.

## Packaging and CI

CI covers Python 3.11, 3.12 and 3.13 on Ubuntu, Ruff lint/format checks, and wheel build/install.
Action dependencies are pinned to exact commits with read-only repository permissions.
Local verification also installs the wheel into a fresh virtual environment and exercises
the CLI from a separate temporary Git repository, without the source checkout on sys.path.

## Support limits

The initial supported configuration is a local Linux/WSL filesystem with at most 32
registered instances, 8 consumers per instance, 64 KiB per message and 100 records per
read/poll. Default retention capacity is 100,000; configuration allows up to 1,000,000 but
that upper capacity has not been load-qualified. Multi-host, network-filesystem, native
Windows and mutually hostile-agent deployments are not supported.

Provider-specific active-session wake delivery is not qualified by these tests. Local
callback transport is tested, but actual delivery inside a Claude/Codex conversation must
be confirmed with a receipt, including busy-session and restart behavior. Checkpoint inbox
reads are the supported baseline and require no automatic model invocation.

A targeted Git-history scan checks private-key blocks and common GitHub/OpenAI/AWS token
patterns; it is not a comprehensive secret scanner. Publication remains a separate step,
and the repository's private status is retained during development.
