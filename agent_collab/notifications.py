"""Bounded, retryable notifications; events are data on stdin, never shell templates."""

import contextlib
import json
import math
import os
import signal
import subprocess
import time
from pathlib import Path

from . import safety


def argv(value):
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 32
        or any(not isinstance(v, str) or len(v) > 4096 or "\x00" in v for v in value)
    ):
        raise ValueError("--exec-argv must be a bounded JSON array of strings")
    if not Path(value[0]).is_absolute():
        raise ValueError("notification executable must be an absolute path")
    return value


def run(command, event, root, timeout):
    """The operator selects argv. A fixed event schema is passed as JSON stdin."""
    command = argv(command)
    process = subprocess.Popen(  # noqa: S603 - operator-owned argv; no shell or substitution
        command,
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        process.communicate(json.dumps(event).encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the group, not just the shell/adapter parent; avoid orphaned callbacks.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return "notification timed out"
    return None if process.returncode == 0 else f"notification exited {process.returncode}"


def watch(
    store,
    agent,
    *,
    consumer="watch",
    command=None,
    types=None,
    interval=5,
    timeout=30,
    once=False,
    stream=None,
):
    import sys

    stream = stream or sys.stdout
    command = argv(command)
    types = list(types if types is not None else (safety.WAKE_TYPES if command else safety.TYPES))
    if not types or any(t not in safety.TYPES for t in types):
        raise ValueError("invalid subscription types")
    if not math.isfinite(interval) or not 0.1 <= interval <= 60:
        raise ValueError("interval must be 0.1..60 seconds")
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 300:
        raise ValueError("timeout must be 0.1..300 seconds")
    store.subscribe(
        agent,
        consumer,
        {
            "types": sorted(set(types)),
            "argv": command,
            "workspace": str(store.root),
            "interval": interval,
        },
    )
    while True:
        token, rows = store.lease_notifications(agent, consumer, lease_seconds=timeout + 10)
        if rows:
            messages = [message for _, message in rows]
            for seq, message in rows:
                summary = {k: message[k] for k in ("id", "from", "to", "type")}
                summary.update(seq=seq, claim=message.get("claim", "")[:200])
                print(safety.encode(summary), file=stream, flush=True)
            error = None
            if command:
                # No message claims, evidence or prompts are forwarded to an executable.
                event = {
                    "schema": 2,
                    "agent": agent,
                    "consumer": consumer,
                    "project": store.project_id(),
                    "messages": [{k: m[k] for k in ("id", "from", "type")} for m in messages],
                }
                try:
                    error = run(command, event, store.root, timeout)
                except OSError as exc:
                    error = f"notification launch failed: {type(exc).__name__}"
            store.finish_notifications(token, error=error)
            if error:
                print(
                    safety.encode({"notification_error": error, "consumer": consumer}),
                    file=stream,
                    flush=True,
                )
                if once:
                    return 1
        if once:
            return 0
        time.sleep(interval)
