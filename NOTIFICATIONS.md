# Notifications

By default neither agent knows a message has arrived. This document sets up a real wake
for each, and explains which parts are reliable.

The shared mechanism is one command:

```bash
python collab/collab.py watch --from <you>
```

It polls the *other* agent's outbox and prints each new message as it lands. Add
`--exec` to run something when mail arrives — that is how a wake is built.

```bash
python collab/collab.py watch --from claude --exec 'notify-send "mail from {agent}: {ids}"'
```

Placeholders: `{ids}`, `{count}`, `{agent}`.

---

## The one thing that would silently break this

The watch cursor (`collab/.watch.<agent>.cursor`) is **separate** from the read cursor
(`collab/.<agent>.cursor`).

If a watcher advanced the read cursor, it would announce a message and in the same
motion make it invisible to `--inbox` — so the agent that has to act on it would never
see it. Pinned by `test_watch_does_not_consume_the_agents_unread_queue`.

The practical consequence: **being woken and reading your mail are separate steps.** A
wake tells you to run `--inbox`; it is not a substitute for it.

---

## Claude Code

Claude Code can be woken mid-turn by its own `Monitor` tool, which is better than an
external watcher because the message arrives inside the conversation:

```
Monitor(
  command: "cd <project> && tail -f -n 0 collab/codex.outbox.jsonl "
           "| grep --line-buffered -oE '\"(id|type|severity)\":\"[^\"]+\"'",
  description: "new messages from codex in collab mailbox",
  persistent: true
)
```

**Do not write a regex that assumes key order.** The first version of this line required
`"id"` to appear before `"type"`. In a real mailbox 16 of 22 records serialised them the
other way round, and the watch silently missed every one. Match each key independently,
or better, use `collab.py watch`, which parses the JSON instead of pattern-matching it.

Limits worth knowing:

- It is **session-scoped**. It dies with the session and must be re-armed. Arm it early,
  as part of picking up shared work.
- `tail -f -n 0` starts from the end, so messages already sitting in the outbox are not
  announced — read those with `--inbox` first.
- On a Windows drive mounted into WSL, `tail -f` uses polling internally and can lag a
  few seconds. That is fine for this purpose.

If the tool is unavailable, fall back to the shared watcher in a background shell.

---

## Codex CLI

Codex is **not** woken by a file change. It reads at checkpoints in its own work. There
are two levels of setup.

### Level 1 — checkpoints (works today, no setup)

Codex checks its mailbox at natural breaks:

```bash
python collab/collab.py --from codex --inbox
```

This is the baseline and it is what the protocol assumes. **Never treat a message as
delivered until it is answered with `received`.**

### Level 2 — a real wake via `codex exec resume`

`codex exec resume <SESSION_ID> "<prompt>"` injects a turn into an existing session, so
a watcher can wake Codex when mail arrives:

```bash
python collab/collab.py watch --from codex \
  --exec 'codex exec resume <SESSION_ID> "Mailbox: {count} new message(s) from {agent} ({ids}). Run: python collab/collab.py --from codex --inbox"'
```

Find the session id from `~/.codex/sessions`, or have the Codex session print its own id
once and record it in `collab/config.json`.

Before relying on this, test it deliberately, because several things can go wrong and
all of them fail quietly:

1. **Does resume actually reach the running session,** or does it start a detached one
   whose output nobody reads? Verify by sending a ping and checking the session
   transcript, not just the exit code.
2. **Concurrency.** A resume that lands while Codex is mid-task may be queued, dropped,
   or may interleave. Establish which before depending on it.
3. **Loops.** If waking Codex causes it to send a message, and Codex's watcher wakes
   Claude, and so on, two agents can ping-pong. Wake on `finding`, `question` and
   `escalate`; do not wake on `received`.
4. **Cost.** Every wake is a model turn. A noisy filter is a recurring bill.

Run it with `--once` first and confirm the behaviour before leaving it running.

---

## A human in a terminal

The most reliable option, and the one to start with:

```bash
python collab/collab.py watch --from claude --interval 2
```

Leave it in a spare pane. It shows the traffic between both agents live, with no wake
semantics to get wrong. Use it while testing Level 2 so you can see what the automation
is actually doing.

Desktop notification on WSL/Linux:

```bash
python collab/collab.py watch --from claude \
  --exec 'notify-send "agent mail" "{count} from {agent}: {ids}"'
```

---

## Choosing a filter

Waking on every message is usually wrong. The messages worth interrupting for are
`finding`, `question` and `escalate`; `received` and `ping` are not.

The watcher does not filter by type yet — filter in the `--exec` command, or read the
printed line and decide. If you find yourself wanting a real filter, that is a good
signal to add `--types` rather than to widen the wake.

---

## Recommended setup

| who | mechanism | effort |
|---|---|---|
| Claude Code | `Monitor` on the other outbox, armed at session start | one line |
| Codex CLI | checkpoint `--inbox` reads | none |
| You | `watch` in a spare terminal pane | one line |

Add the `codex exec resume` wake only once the above is working and you have tested the
four failure modes. A wake that fires unreliably is worse than a checkpoint you trust,
because it invites both agents to assume delivery.
