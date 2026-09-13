# Framework release ownership

This file concerns **agent-collab**, not projects that use it.

| Role | Agent |
|---|---|
| Integrator for the 0.2 hardening candidate | codex |
| Independent reviewer | claude |

Claude offered the integrator handoff and Codex accepted it in the existing collaboration
mailbox on 2026-09-13. Framework review messages now use this repository's own 0.2 store
so their provenance names the framework. All implementation paths on the hardening branch
are held by Codex for this task. Claude reviews without editing that worktree.

The user authorized fixing the public-release audit findings, committing and pushing.
A feature-branch checkpoint is not approval to replace the deployed tool. Integration to
main requires an explicit approval naming the proposal commit and snapshot. Publication
and migration of existing live project mailboxes are separate operations.

Commit identity: **Lucienne Swart <ljswart@me.com>**.
