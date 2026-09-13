# Ownership

Maintained by the **coordinator**. A transfer is a `handoff` message that takes effect
when the other agent replies `received`.

| role | agent |
|---|---|
| coordinator (maintains this file) | _unassigned_ |
| integrator (commits) | _unassigned_ |
| reviewer of record | _unassigned_ |

The integrator commits only revisions the other agent has `approved` by `commit` +
`snapshot_sha256`.

## Current holdings

| path | owner | since | note |
|---|---|---|---|
| `collab/**` | _unassigned_ | | protocol scaffold |
| *(everything else)* | _unassigned_ | | claim with a `handoff` before editing |

Unassigned means nobody edits it without claiming it first.

## Simultaneous work

Do not share a working tree. Use worktrees, so the reviewer tests an exact revision:

```bash
git worktree add ../<project>-codex  -b codex/<task>
git worktree add ../<project>-claude -b claude/<task>
```

## Commit identity

Record the intended author here as `Name <email>`. Never commit as another.
