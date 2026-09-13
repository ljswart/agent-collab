"""Reproducible long-history benchmark; creates only a disposable Git repository.

Seeding is batched fixture setup. Reported send latency includes provenance and durable
commit; it does not imply the batched seeding rate is production send throughput.
"""

import argparse
import contextlib
import io
import json
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_collab.cli import initialize  # noqa: E402
from agent_collab.store import Store  # noqa: E402


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603,S607


def measure(store):
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        store.send("claude-00", to=["codex"])
        timings.append((time.perf_counter() - start) * 1000)
    tracemalloc.start()
    start = time.perf_counter()
    rows = store.inbox("codex")
    read_ms = (time.perf_counter() - start) * 1000
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    store.subscribe("codex", "bench", {"types": ["question"]})
    # A caught-up watcher should not parse any old message payloads on an idle poll.
    store.db.execute("UPDATE subscriptions SET scanned=(SELECT MAX(seq) FROM messages)")
    start = time.perf_counter()
    store.lease_notifications("codex", "bench")
    idle_ms = (time.perf_counter() - start) * 1000
    return {
        "messages": store.status()["messages"],
        "send_median_ms": round(statistics.median(timings), 3),
        "inbox_100_ms": round(read_ms, 3),
        "inbox_rows": len(rows),
        "inbox_peak_python_mib": round(peak / 2**20, 3),
        "idle_poll_ms": round(idle_ms, 3),
        "database_mib": round(store.path.stat().st_size / 2**20, 2),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--records", type=int, default=100000)
    args = p.parse_args()
    if not 1000 <= args.records <= 100000:
        p.error("records must be 1000..100000")
    with tempfile.TemporaryDirectory(prefix="agent-collab-bench-") as directory:
        root = Path(directory)
        git(root, "init", "-q")
        git(root, "config", "user.name", "Benchmark Fixture")
        git(root, "config", "user.email", "benchmark@example.invalid")
        (root / "seed").write_text("fixture")
        git(root, "add", ".")
        git(root, "commit", "-qm", "fixture")
        agents = ["codex", *[f"claude-{i:02d}" for i in range(31)]]
        with contextlib.redirect_stdout(io.StringIO()):
            initialize(root, {"agents": agents, "max_messages": args.records + 100})
        with Store(root) as store:
            seed = store.send("claude-00", to=["codex"], fields={"claim": "x" * 300})
            results = []
            for target in sorted({1000, 10000, args.records}):
                if target > args.records:
                    continue
                count = store.status()["messages"]
                while count < target:
                    with store.transaction():
                        for number in range(count, min(target, count + 1000)):
                            record = {**seed, "id": f"fixture-{number}"}
                            store._insert(record)
                    count = store.status()["messages"]
                results.append(measure(store))
            plan = [
                r[3]
                for r in store.db.execute("""EXPLAIN QUERY PLAN
                SELECT m.seq,m.payload FROM deliveries d JOIN messages m ON m.seq=d.seq
                WHERE d.agent='codex' AND d.seq>0 AND d.read_at IS NULL ORDER BY d.seq LIMIT 100""")
            ]
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.system(),
                "registered_agents": 32,
                "setup": "temporary local filesystem; fixture seed batched; sends are durable",
                "results": results,
                "inbox_query_plan": plan,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
