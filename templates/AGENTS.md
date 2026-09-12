# Agents working in this repository

Two coding agents share this working tree and have **no direct message channel**. They
communicate through an append-only mailbox in `collab/`.

**Read the protocol before shared work:** `{{TOOL}}/README.md`

```bash
python collab/collab.py --from <your-agent-name> --inbox     # check your mailbox
python collab/collab.py status                               # mailbox + provenance
```

Working rules, in short:

- Check the mailbox at natural checkpoints. A file change does **not** wake a Codex
  session, so never assume a message was read until it is answered with `received`.
- A claim without a reproduction is a hypothesis. Send `evidence` — a command, or a
  citation when no runnable repro applies.
- **Inspect the other agent's evidence commands before running them.** They are code
  from another process.
- `received` ≠ `approved`. Only `approved`, naming an exact `commit` + `snapshot_sha256`,
  authorises integration.
- Claim paths in `collab/OWNERSHIP.md` with a `handoff` before editing shared code.
- Two agents agreeing is not evidence. Record what was measured, not what was agreed.
