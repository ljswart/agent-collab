# Project integration roles

Record the coordinator, integrator, reviewers and authorized commit identity here.
Actual path claims live in the transactional store; this document is a human-readable
role assignment, not the lock authority.

```bash
agent-collab claim-path --from YOUR_ID --path src --lease-seconds 900
agent-collab release-path --from YOUR_ID --path src
agent-collab activity --events
```

Renew claims before expiry. Discuss a handoff, release your lease, and require the recipient
to confirm it acquired the new lease. Separate worktrees share message and claim state.
The integrator checks an explicit approval on the fixed revision before integrating.
A user override must be recorded transparently; it is never backdated as agent approval.
