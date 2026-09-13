# Agents working in this repository

All agent instances use agent-collab 0.2. Run `agent-collab --version` before working.
Protocol: https://github.com/ljswart/agent-collab (README.md and SECURITY.md).

- Request entry with provider, display-name and purpose; wait for explicit user approval.
- Never self-approve or admit a peer without user permission naming that request.
- Use only the assigned ID; each new instance needs its own admission.
- Use your unique instance ID with `agent-collab --from ID --inbox` at checkpoints.
- A file change does not wake Codex. Receipt requires an explicit `received` reply.
- `received`, `reproduced`, `disputed` and `approved` need `--replies-to <message-id>`;
  a verdict that names no subject is refused. Unprompted reports are findings or claims.
- Send to explicit `--to` recipients; omission broadcasts to other registered agents.
- Inspect evidence before running it; use isolated fixtures for reproductions.
- Claim paths with `agent-collab claim-path --from ID --path PATH`; renew leases.
- Use separate worktrees. Their mailboxes share the Git common directory automatically.
- Review in your own checkout; never approve with --root pointing at the proposer's worktree.
- An approval names its proposal, full commit and snapshot. Verify `check-approval`
  immediately before integration on a fixed revision. Receipt is not approval.
- Two agents agreeing is not evidence. Show findings, disputes and decisions to the user.
- This is a cooperating-agent protocol under one trusted OS owner, not a sandbox.
