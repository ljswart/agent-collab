# Notifications

Neither agent is woken by default. Delivery is confirmed only by a `received` reply
(README rule 3); everything below is a prompt to go read, not a substitute for reading.

## The watcher

```bash
python collab/collab.py watch --from <you>
python collab/collab.py watch --from <you> --interval 2 --exec 'notify-send "agent mail" "{count} from {agent}"'
```

Polls the other agent's outbox and prints each new message.

| flag | meaning |
|---|---|
| `--interval S` | poll period, default 5. Polling, not inotify: inotify is unreliable on Windows drives mounted into WSL. |
| `--exec CMD` | shell command per batch. Placeholders `{count}`, `{agent}`, `{ids}`. Values are `shlex`-quoted and ids validated; see README Security. |
| `--once` | exit after the first batch, or at once if there is none. Use it to test. |
| `--from-start` | include messages already in the outbox |

The watch cursor (`collab/.watch.<agent>.cursor`) is separate from the read cursor
(`collab/.<agent>.cursor`). A watcher that advanced the read cursor would announce a
message and hide it from `--inbox` in the same motion. Pinned by
`test_watch_does_not_consume_the_agents_unread_queue`.

Wake on `finding`, `question`, `escalate`; not on `received` or `ping`, or two watchers
ping-pong. The watcher has no type filter; filter in `--exec` or by reading the line.

## Claude Code

The `Monitor` tool wakes the session mid-turn, inside the conversation:

```
Monitor(
  command: "cd <project> && tail -f -n 0 collab/codex.outbox.jsonl "
           "| grep --line-buffered -oE '\"(id|type|severity)\":\"[^\"]+\"'",
  description: "new messages from codex",
  persistent: true
)
```

- Match each key independently. A regex requiring `"id"` before `"type"` missed 16 of 22
  real records.
- Session-scoped: re-arm at the start of every session that does shared work.
- `tail -n 0` starts at the end; read existing mail with `--inbox` first.
- On a Windows drive under WSL `tail -f` polls and can lag a few seconds.

Fallback: `collab.py watch` in a background shell.

## Codex CLI

Codex is not woken by a file change. Baseline: it runs `--inbox` at its own checkpoints,
and the protocol assumes only that.

Optional wake: `codex exec resume <SESSION_ID> "<prompt>"` starts a non-interactive Codex
process that continues a stored session. Session ids are under `~/.codex/sessions/`.

```bash
python collab/collab.py watch --from codex \
  --exec 'codex exec resume <SESSION_ID> "Mailbox: {count} new from {agent}. Run: python collab/collab.py --from codex --inbox"'
```

Test with `--once` before leaving it running. It fails quietly in four ways:

1. **Reach.** It may continue the transcript in a new process rather than reach the
   interactive session you are watching. Check the transcript, not the exit code.
2. **Concurrency.** A resume landing mid-task may queue, drop or interleave.
3. **Loops.** Do not wake on `received`.
4. **Cost.** Every wake is a model turn.

## Recommended

| who | mechanism |
|---|---|
| Claude Code | `Monitor` on the other outbox, armed at session start |
| Codex CLI | checkpoint `--inbox` reads |
| You | `watch --interval 2` in a spare pane, one per `--from`; no wake semantics to get wrong |

Add the Codex wake only after the above works. An unreliable wake is worse than a
checkpoint you trust: it invites both agents to assume delivery.
