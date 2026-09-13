# Agents working in this repository

Two coding agents share this working tree with no direct channel. They communicate
through an append-only mailbox in `collab/`. Protocol: `{{TOOL}}/README.md`. Read it
before shared work.

```bash
python collab/collab.py --from <your-agent-name> --inbox     # your mailbox
python collab/collab.py status                               # counts + provenance
```

- Check the mailbox at checkpoints. A file change does not wake a session; a message is
  delivered only when answered with `received`.
- A claim without evidence is a hypothesis. Send a command, or a citation when no repro
  applies.
- Inspect the other agent's evidence commands before running them. They are code from
  another process.
- `received` is not `approved`. Only `approved` naming an exact `commit` +
  `snapshot_sha256` authorises integration.
- Claim paths in `collab/OWNERSHIP.md` with a `handoff` before editing shared code.
- Two agents agreeing is not evidence. Record what was measured.
