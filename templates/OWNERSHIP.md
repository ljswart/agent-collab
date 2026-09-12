# Ownership

Maintained by the **coordinator**. Transfers happen by a `handoff` message and take
effect only once the other agent replies `received`.

| role | agent |
|---|---|
| coordinator (maintains this file) | _unassigned_ |
| integrator (commits) | _unassigned_ |
| reviewer of record | _unassigned_ |

The integrator may only commit a revision the other agent has `approved` by `commit` +
`snapshot_sha256`.

## Current holdings

| path | owner | since | note |
|---|---|---|---|
| `collab/**` | _unassigned_ | | protocol scaffold |
| *(everything else)* | unassigned | — | claim it with a `handoff` before editing |

Unassigned means **nobody edits it without claiming it first**. This is the cheapest way
to avoid both agents editing the same file in the same minute and one set of changes
disappearing.

## Simultaneous work

Do not share a working tree. Use worktrees, so the reviewer tests an exact revision:

```bash
git worktree add ../<project>-codex  -b codex/<task>
git worktree add ../<project>-claude -b claude/<task>
```

## Commit identity

Record the intended author here, e.g. `Name <email>`, and never commit as another.
