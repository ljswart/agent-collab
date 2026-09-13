# Migrating a 0.1 JSONL mailbox to 0.2

This is an explicit storage/CLI upgrade. Stop old writers and watchers first so no message
can be sent to a transport the other agent no longer reads. Preserve the old clone/version
and mailbox files for rollback. Do not upgrade a shared tool clone underneath active agents.

1. Install a wheel or reviewed checkout of 0.2 in a dedicated Python environment.
2. Run `agent-collab init --config /path/to/config.json`. The old `collab/config.json` is
   compatible when it contains `agents`, `provenance_files`, and `exclude`; agent names
   must meet the new bounded-ID rules. Retain all historical sender names during import.
3. Import each old outbox explicitly, using the correct sender:

   ```bash
   agent-collab migrate --from claude --path collab/claude.outbox.jsonl
   agent-collab migrate --from codex --path collab/codex.outbox.jsonl
   ```

   Strict import rolls the entire transaction back on an invalid or incomplete record.
   Inspect the failure; use `--skip-invalid` only when you deliberately want invalid
   records quarantined by digest, origin and reason. Original files remain untouched.
   Re-importing identical records is idempotent. Conflicting IDs are rejected in strict
   mode. Imported approvals are historical evidence and cannot authorize integration.
4. Verify `status`, `activity`, per-agent inbox contents and an `export`. Old cursor files
   are not imported: history is initially unread so the migration cannot silently assume
   a record was received. Send fresh receipts as needed. All default-broadcast legacy
   records are routed to the other currently registered agents unless they have explicit
   recipient fields.
5. Regenerate the portable project shim with `init --force` only after reviewing any
   existing AGENTS.md customization; that flag regenerates AGENTS.md too. Alternatively,
   keep custom instruction files and change them to use the installed `agent-collab` CLI.
   Ensure existing `CLAUDE.md` imports `@AGENTS.md`.
6. Replace old watchers with `watch --consumer NAME`. `--exec` shell strings and old
   `{ids}`/`{agent}` placeholders are rejected. Callbacks now use `--exec-argv` and JSON stdin.
7. Point all worktrees at the same installed version. Send a fresh ping, verify it in the
   intended peer inbox, and require a `received` reply before declaring the migration done.

To roll back before new 0.2 traffic, stop 0.2 clients and restore the pinned old tool and
instructions; original outboxes have not changed. If new 0.2 traffic exists, export and
preserve it first. Routing/schema/approval semantics differ, so do not silently re-import
that export into old append-only files or claim lossless downgrade.

The Git common directory identifies one project. Separate clones do not automatically
share state. Cross-host routing and automatic active-session wake-ups are outside 0.2.
