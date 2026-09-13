# Security

## Supported trust boundary

agent-collab 0.2 is for cooperating agents controlled by one trusted OS owner on a single
Linux/WSL host. Message content and imported records are treated as untrusted input.
This protects against malformed records, accidental corruption and unintended execution
through data interpolation. It does not isolate mutually malicious processes sharing an
OS account, filesystem, Git credentials or model credentials.

Any process running as the owner can modify the database or source, invoke the CLI using
another registered identity, or commit directly. Message IDs are routing identifiers,
not authenticated principals. Audit events, path leases and approval checks coordinate
cooperating participants; they are not tamper-proof or an authorization boundary over Git.

For hostile-agent or multi-tenant deployments, use a separately designed broker and
integration service under separate principals, with access controls agents cannot bypass.
This release does not implement or advertise that deployment model. Shared secrets or
signatures stored where all agents can read them do not provide that isolation.

## Input, execution and filesystem handling

- Agent names and recipients are bounded and validated before initialization writes.
- New messages require schema 2, typed fields and full provenance. Inputs are capped at
  64 KiB; duplicate JSON keys, malformed UTF-8, non-finite values and invalid envelopes
  are rejected. Legacy import is explicit and can quarantine invalid records.
- Evidence is data. The tool never runs it. Human-facing JSON escapes terminal controls.
- Notification argv is operator-selected and literal; events use JSON stdin. There is no
  shell interpolation. Callback timeouts kill the process group; retries and failures are
  visible. Review callback code, since it runs with the owner's permissions.
- State lives outside tracked project files under the Git common directory. The state
  directory is owner-only; database creation and scaffold writes reject symlink targets.
  Scaffold traversal uses directory descriptors and no-follow operations. These defenses
  do not promise protection against a hostile same-UID process racing filesystem changes.
- Snapshots include file type/mode, symlink target and bytes, NUL-delimited filenames,
  submodule state and full-path provenance inputs. Missing declared inputs fail closed.
  Double collection detects observable concurrent changes; use immutable review revisions.

## Persistence and supported platforms

SQLite rollback journaling, `synchronous=FULL`, bounded reads and short write transactions
replace JSONL append framing and count-based IDs. A failed transaction is not acknowledged.
External notification effects are at-least-once, so callers must handle duplicates.

The supported environment is local Linux/WSL with POSIX filesystem safety primitives.
Native Windows fails explicitly. Network filesystems are unsupported. Mounted Windows
project paths are not a qualified database deployment; keep the repository/common Git
directory on the WSL Linux filesystem for the tested configuration. No claim is made
about unlimited agent count or history: see VALIDATION.md.

Mailbox state, exports, callback arguments, evidence and audit events may contain private
project information. Do not put credentials in messages or callback arguments. Git does
not automatically include the database; manually exported files can still be committed.

## Reporting and releases

0.2 is a development alpha, not a completed independent security certification. Report
exploitable findings privately to the maintainer at **ljswart@me.com**. Once GitHub private
vulnerability reporting is enabled for the public repository, that is also an appropriate
channel. Do not post exploit details in public issues before coordinated remediation.

Security fixes target the latest 0.2 revision. The earlier JSONL/shell-template implementation
is superseded and should not be advertised as secure. No automatic publication or upgrade
of existing live mailboxes is performed. Pin reviewed commits or wheel artifacts and
follow MIGRATION.md when changing versions.
