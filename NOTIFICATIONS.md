# Notifications

The reliable baseline for both Claude Code and Codex is an explicit inbox read at natural
checkpoints. A background process printing a line does not prove the intended conversation
received it. Only a `received` message confirms receipt under this protocol.

## Watch a routed inbox

```bash
agent-collab watch --from codex-reviewer --consumer terminal
agent-collab watch --from codex-reviewer --consumer terminal --once
```

The watcher queries a shared SQLite store, not JSONL files or filesystem events. It works
across project worktrees. New subscriptions start at the beginning of retained routed
history, so starting a watcher does not silently skip an existing message. A paid model
callback can incur charges for each historical batch on first start; inspect the backlog
and set a spending limit before enabling it. Reads are
bounded to 100 candidate messages per poll. `--once` performs one bounded poll; if the
first page contains only filtered messages, subsequent polls continue from that page.

Each agent can have up to eight independently named notification consumers. Inbox reads
and notification state are independent. Two processes using the same consumer cannot
lease overlapping batches or launch simultaneous callbacks for that consumer. A crashed
watcher's lease expires and its work becomes eligible again.

## Safe external callbacks

Shell `--exec` templates were removed. Use a JSON array of **literal arguments**; the
executable must be an absolute path. The callback receives one JSON event on stdin.
Nothing is substituted into argv and no shell is invoked.

```bash
agent-collab watch --from codex-reviewer --consumer desktop \
  --exec-argv '["/absolute/venv/bin/python","/absolute/notify.py"]'
```

Example `notify.py`, using a separately installed desktop notifier:

```python
import json
import subprocess
import sys

payload = json.load(sys.stdin)
subprocess.run(
    ["/usr/bin/notify-send", "Agent mailbox", f"{len(payload['messages'])} new messages"],
    check=True,
)
```

The event contains `schema`, `project`, receiving `agent`, `consumer`, and `messages` with
only `id`, `from`, and `type`. Claims and evidence never become executable arguments or
model prompts automatically. Callback files and their configuration are operator-owned
code and must be reviewed; intentionally invoking an interpreter can execute whatever
that operator configures.

Callback subscriptions default to `finding`, `claim`, `question`, `handoff`, and `escalate`.
Use `--types` for an explicit selection. `received`, `approved`, and `ping` do not wake a
model by default. Terminal-only watchers display every message type. Consumer settings
are pinned: changing argv, workspace, interval or types requires a new consumer name.

The poll interval is 0.1–60 seconds, default 5. A shared consumer rate gate coalesces work
into at most one batch per interval. The callback timeout is 0.1–300 seconds, default 30;
timeout terminates its process group. Failed launches/nonzero exits/timeouts remain pending
with exponential backoff, then become visibly failed after five attempts.

```bash
agent-collab status
agent-collab activity --events --follow
agent-collab retry --from codex-reviewer --consumer desktop
```

`--once` returns nonzero on a callback failure. Success marks notification complete only
after the callback exits successfully. A crash between the external side effect and the
commit can cause a retry, so callbacks must deduplicate by message ID. Completion records
an exit status, not proof that a human or model read the message.

## Claude Code and Codex

Both providers work through the CLI without a provider API. Give multiple sessions distinct
IDs; do not let separate instances share an inbox identity.

For Claude, import `AGENTS.md` from `CLAUDE.md`. A session-specific monitor may watch the
terminal consumer output, but availability and re-arming depend on the installed Claude
client. The framework does not assume a monitor is present or scrape JSON with regex.

For Codex, use `AGENTS.md` and checkpoint inbox reads. The installed CLI's
`codex exec resume --help` documents a non-interactive resume command, but launching it
can create a separate process rather than wake the interactive UI. Do not assume safe
concurrent access to an active conversation. No automatic model resume is enabled by
this package, and no permission-bypass flags are supplied.

An operator can implement a callback that calls the chosen provider, with explicit session
routing, deduplication, a busy-session policy and a spending limit. Before enabling that
integration, demonstrate a real `received` reply in the intended session, including during
busy work and after restart. Automated tests validate the callback transport using local
executables; they do not certify delivery inside live provider conversations.
