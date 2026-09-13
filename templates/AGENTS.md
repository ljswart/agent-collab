# Agent collaboration

Use agent-collab 0.2 and assign each instance a unique registered ID.
Read the project's AGENTS.md and the framework README.md/SECURITY.md.

- Check `agent-collab --from ID --inbox` at checkpoints and reply `received` explicitly.
- Send evidence, inspect it before execution, and reproduce in temporary repositories.
- Use explicit recipients; omission broadcasts to other registered instances.
- Claim paths with `claim-path`, renew leases, and coordinate handoffs before releasing.
- Work in separate Git worktrees; state is shared through their Git common directory.
- Approvals identify an existing proposal, full commit and snapshot. Run `check-approval`
  on the integrator's fixed revision before integration. Do not treat receipt as approval.
- Show disputes, verdicts and integration decisions to the user. Agreement is not evidence.
- All participants share one trusted OS owner. The mailbox is not an isolation boundary.
