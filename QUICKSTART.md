# Quickstart

## Install

Clone this repository anywhere; `collab.py` runs from the clone. In the root of the
project the agents share (a git repository with at least one commit):

```bash
python /path/to/agent-collab/collab.py init                   # agents: claude, codex
python /path/to/agent-collab/collab.py init --agents alice bob
```

`init` creates `collab/` and `AGENTS.md` and does not overwrite existing files; `--force`
regenerates them from `templates/`. The shim `collab/collab.py`, `collab/README.md` and
`AGENTS.md` embed the absolute path of your clone, so committing them publishes that path.

Mailbox traffic can be kept out of git; the reviewed snapshot excludes it either way:

```gitignore
collab/*.outbox.jsonl
collab/.lock
collab/.*.cursor
```

## Point the agents at the protocol

Codex CLI reads `AGENTS.md` from the project root. Claude Code reads `CLAUDE.md`, not
`AGENTS.md`: add the line `@AGENTS.md` to `CLAUDE.md`, or `ln -s AGENTS.md CLAUDE.md`.

Then tell each agent once:

> We share this repo with another coding agent. Read `AGENTS.md` and the protocol it
> links before shared work. Check your mailbox at checkpoints with
> `python collab/collab.py --from <you> --inbox`. Claim paths in `collab/OWNERSHIP.md`
> before editing shared code. Read the other agent's evidence commands before running
> them. Only `approved` naming an exact commit and snapshot authorises integration.

## CLI

```bash
python collab/collab.py [init|status|watch] [--from AGENT] [flags]
```

| flag | meaning |
|---|---|
| `--from AGENT` | who you are; or set `COLLAB_AGENT` |
| `--root DIR` | project root; default is the git top level of cwd, or `COLLAB_ROOT` |
| `--inbox [--all]` | unread mail from the other agent; `--all` prints history. Both advance the read cursor. |
| `--type T` | send; T in `ping finding claim question handoff escalate received reproduced disputed approved` |
| `--severity P1/P2/P3` `--ref PATH:LINE` `--claim` `--expect` | message fields |
| `--evidence CMD` / `--evidence-file PATH` | required for `finding` and `claim` |
| `--evidence-kind command/citation` | default `command` |
| `--replies-to ID` `--task ID` | threading |
| `--status open/fixed/withdrawn` | default `open` |
| `status` | message counts and current provenance |
| `watch` | `--interval S` `--exec CMD` `--once` `--from-start`; see [NOTIFICATIONS.md](NOTIFICATIONS.md) |

Provenance is attached to every message automatically. Outside a git repository, or in
one with no commits, sending fails with a git error rather than sending without it.
Message examples: [README.md](README.md).

## Configuration

`collab/config.json`, all keys optional:

| key | example | meaning |
|---|---|---|
| `agents` | `["claude", "codex"]` | exactly two; they set the outbox filenames |
| `provenance_files` | `["data/frozen/manifest.json"]` | hashed into every message as `<stem>_sha256`, so a claim names the data it was measured on |
| `exclude` | `["notes/scratch.md"]` | extra repo-relative paths left out of the reviewed snapshot |

## Tests

```bash
python -m pytest /path/to/agent-collab/tests -q     # 21 tests; temporary repos only
```
