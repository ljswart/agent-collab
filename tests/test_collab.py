"""Release regressions. Every filesystem mutation is in a disposable Git repository."""

import io
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_collab import notifications, provenance, safety  # noqa: E402
from agent_collab.cli import initialize, main  # noqa: E402
from agent_collab.store import Store  # noqa: E402


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def git_init(root):
    root.mkdir(exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "core.filemode", "true")
    (root / "seed.txt").write_text("seed\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")


@pytest.fixture
def project(tmp_path):
    git_init(tmp_path)
    initialize(tmp_path, {"agents": ["claude", "codex", "claude-reviewer"]})
    return tmp_path


@pytest.fixture
def store(project):
    with Store(project) as value:
        yield value


def old_record(**fields):
    return {
        "id": "claude-0001",
        "ts": "fixture",
        "from": "claude",
        "type": "ping",
        "provenance": {"commit": "abc", "snapshot_sha256": "def"},
        **fields,
    }


def test_install_scaffold_is_portable_and_preserves_existing(project):
    assert str(project) not in (project / "collab/collab.py").read_text()
    assert (project / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    (project / "AGENTS.md").write_text("custom")
    initialize(project, {"agents": ["claude", "codex", "claude-reviewer"]})
    assert (project / "AGENTS.md").read_text() == "custom"
    initialize(project, {"agents": ["claude", "codex", "claude-reviewer"]}, force=True)
    assert "check-approval" in (project / "AGENTS.md").read_text()


@pytest.mark.parametrize(
    "agents",
    [
        ["../../escaped", "codex"],
        ["$(touch owned)", "codex"],
        ["a\n", "codex"],
        ["a", "a"],
        "ab",
        [1, "codex"],
        ["x" * 49, "codex"],
        ["/absolute/escaped", "codex"],
    ],
)
def test_names_rejected_before_writes(tmp_path, agents):
    git_init(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        initialize(tmp_path, {"agents": agents})
    assert not (tmp_path / "collab").exists()
    assert not (tmp_path / ".git/agent-collab").exists()
    assert not (tmp_path / "owned").exists()


def test_three_agents_routing_and_independent_inboxes(store):
    first = store.send("claude", to=["codex"])
    assert [m["id"] for _, m in store.inbox("codex")] == [first["id"]]
    assert store.inbox("claude-reviewer") == []
    second = store.send("codex")
    assert [m["id"] for _, m in store.inbox("claude-reviewer")] == [second["id"]]
    store.mark_read("codex", [seq for seq, _ in store.inbox("codex")])
    assert not store.inbox("codex")
    assert store.inbox("claude-reviewer")


def test_sender_and_reserved_fields_cannot_be_forged(store):
    with pytest.raises(ValueError, match="reserved"):
        store.send("claude", fields={"from": "codex"})
    with pytest.raises(ValueError, match="unknown agent"):
        store.send("mallory")
    with pytest.raises(ValueError, match="evidence"):
        store.send("claude", kind="finding")
    assert store.status()["messages"] == 0


def test_bounded_messages_and_backpressure(project):
    with Store(project) as store:
        settings = store.settings()
        settings["max_messages"] = 1
        store.db.execute("UPDATE meta SET value=? WHERE key='config'", (safety.encode(settings),))
        with pytest.raises(ValueError, match="exceeds"):
            store.send("claude", fields={"claim": "x" * safety.MAX_RECORD})
        store.send("claude")
        with pytest.raises(RuntimeError, match="capacity"):
            store.send("codex")
        assert store.status()["messages"] == 1


def test_snapshot_tracks_newline_and_non_ascii_filename_bytes(project, store):
    for filename in ("odd\nname.py", 'quote".py', os.fsdecode(b"byte-\xff.py")):
        path = project / filename
        path.write_text("a")
        first = provenance.provenance(project, store.settings())
        path.write_text("b")
        second = provenance.provenance(project, store.settings())
        assert first != second
        assert len(second["snapshot_sha256"]) == 64


def test_snapshot_tracks_mode_and_symlink_target(project, store):
    path = project / "seed.txt"
    path.write_text("changed")
    first = provenance.provenance(project, store.settings())
    path.chmod(0o755)
    second = provenance.provenance(project, store.settings())
    assert first != second
    (project / "same.txt").write_text("changed")
    link = project / "link"
    link.symlink_to("seed.txt")
    first = provenance.provenance(project, store.settings())
    link.unlink()
    link.symlink_to("same.txt")
    assert first != provenance.provenance(project, store.settings())


def test_snapshot_excludes_delivery_state_but_includes_instructions(store):
    before = provenance.provenance(store.root, store.settings())
    store.send("claude")
    store.mark_read("codex", [seq for seq, _ in store.inbox("codex")])
    assert before == provenance.provenance(store.root, store.settings())
    (store.root / "AGENTS.md").write_text("changed")
    assert before != provenance.provenance(store.root, store.settings())


def test_provenance_inputs_fail_closed_and_preserve_same_stem(project, store):
    settings = store.settings()
    settings["provenance_files"] = ["one/manifest.json", "two/manifest.json"]
    for parent in ("one", "two"):
        (project / parent).mkdir()
        (project / parent / "manifest.json").write_text(parent)
    value = provenance.provenance(project, settings)
    assert len(value["inputs_sha256"]) == 2
    (project / "two/manifest.json").unlink()
    with pytest.raises(FileNotFoundError):
        provenance.provenance(project, settings)


def test_observable_concurrent_mutation_fails_closed(project, store, monkeypatch):
    real = provenance.snapshot
    counter = 0

    def changing(*args, **kwargs):
        nonlocal counter
        counter += 1
        (project / "seed.txt").write_text(str(counter))
        return real(*args, **kwargs)

    monkeypatch.setattr(provenance, "snapshot", changing)
    with pytest.raises(ValueError, match="changed while hashing"):
        store.send("claude")
    assert store.status()["messages"] == 0


def test_worktrees_share_delivery_but_keep_own_provenance(project, store, tmp_path):
    other = tmp_path / "other"
    git(project, "worktree", "add", "-b", "other", str(other))
    (other / "seed.txt").write_text("other revision")
    git(other, "add", "seed.txt")
    git(other, "commit", "-qm", "other revision")
    with Store(other) as peer:
        assert peer.directory == store.directory
        message = peer.send("codex", to=["claude"])
    received = store.inbox("claude")[0][1]
    assert received["id"] == message["id"]
    assert received["provenance"]["commit"] == git(other, "rev-parse", "HEAD").strip().decode()
    assert received["provenance"]["commit"] != git(project, "rev-parse", "HEAD").strip().decode()


def test_reply_and_approval_require_existing_explicit_matching_subject(store):
    with pytest.raises(ValueError, match="unknown reply target"):
        store.send("codex", kind="approved", fields={"replies_to": "claude-9999"})
    proposal = store.send(
        "claude", kind="claim", to=["codex"], fields={"claim": "ready", "evidence": "pytest -q"}
    )
    fields = {"replies_to": proposal["id"]}
    with pytest.raises(ValueError, match="explicitly"):
        store.send("codex", kind="approved", fields=fields)
    with pytest.raises(ValueError, match="delivered"):
        store.send("claude-reviewer", kind="approved", fields=fields)
    subject = provenance.revision(proposal["provenance"])
    approved = store.send("codex", kind="approved", fields=fields, subject=subject)
    assert store.check_approval(approved["id"])["id"] == proposal["id"]
    (store.root / "seed.txt").write_text("moved")
    with pytest.raises(ValueError, match="stale"):
        store.send("codex", kind="approved", fields=fields, subject=subject)
    with pytest.raises(ValueError, match="stale"):
        store.check_approval(approved["id"])


def test_verdicts_must_name_the_message_they_answer(store):
    """Ten of ten verdicts in a 10-agent trial carried no reply target, so nothing in the
    store linked a result to the claim it judged. An unprompted report is a finding."""
    for kind in safety.RESPONSE_TYPES:
        with pytest.raises(ValueError, match=f"{kind} answers a specific message"):
            store.send("codex", kind=kind, to=["claude"], fields={"claim": "agreement"})
    for kind in ("ping", "finding", "claim", "question", "handoff", "escalate"):
        fields = {"claim": "unprompted", "evidence": "pytest -q"}
        assert store.send("codex", kind=kind, to=["claude"], fields=fields)["type"] == kind


def test_generated_instructions_carry_every_enforced_rule(project):
    """The AGENTS.md an agent actually reads is generated from cli.PROTOCOL. Editing the
    unused templates/ copy changed nothing, which is how this rule first shipped invisible."""
    generated = (project / "AGENTS.md").read_text()
    assert "--replies-to <message-id>" in generated
    for kind in safety.RESPONSE_TYPES:
        assert f"`{kind}`" in generated, kind


def test_a_verdict_reaches_the_agent_whose_work_it_judges(store):
    """The auditors addressed every verdict to the orchestrator alone, so the agent whose
    numbers were disputed was never told."""
    proposal = store.send(
        "claude",
        kind="claim",
        to=["codex", "claude-reviewer"],
        fields={"claim": "sum=0.0104", "evidence": "sha256sum BTCUSDT-fundingRate-2025-07.zip"},
    )
    verdict = store.send(
        "codex",
        kind="disputed",
        to=["claude-reviewer"],
        fields={"replies_to": proposal["id"], "claim": "ground truth is 0.0073"},
    )
    assert verdict["to"] == ["claude", "claude-reviewer"]
    assert [m["id"] for _, m in store.inbox("claude")] == [verdict["id"]]
    assert store.get(verdict["replies_to"])["id"] == proposal["id"]


@pytest.mark.parametrize(
    "bad", ["null\n", "[]\n", '{"bad":\n', "\ufffd\n", '{"id":"a","id":"b"}\n', '{"a":NaN}\n']
)
def test_invalid_import_quarantines_without_hiding_valid_records(store, tmp_path, bad):
    path = tmp_path / "old.jsonl"
    path.write_text(bad + json.dumps(old_record()) + "\n")
    assert store.migrate(path, "claude", skip_invalid=True) == 1
    assert store.inbox("codex")[0][1]["id"] == "claude-0001"
    assert store.status()["quarantined"] == 1


def test_import_sender_mismatch_and_poison_fields_are_rejected(store, tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps(old_record(**{"from": "codex"})) + "\n")
    with pytest.raises(ValueError, match="sender"):
        store.migrate(path, "claude")
    path.write_text(json.dumps(old_record(provenance=None)) + "\n")
    with pytest.raises(ValueError, match="provenance"):
        store.migrate(path, "claude")
    assert store.status()["messages"] == 0


def test_torn_import_rolls_back_or_quarantines_and_cannot_corrupt_next_send(store, tmp_path):
    path = tmp_path / "old.jsonl"
    original = json.dumps(old_record()) + '\n{"id":'
    path.write_text(original)
    with pytest.raises(ValueError, match="rolled back"):
        store.migrate(path, "claude")
    assert store.status()["messages"] == 0
    assert store.migrate(path, "claude", skip_invalid=True) == 1
    next_message = store.send("claude")
    assert len(store.inbox("codex")) == 2
    assert next_message["id"] != "claude-0001"
    assert path.read_text() == original
    assert store.migrate(path, "claude", skip_invalid=True) == 0


def test_corrupt_database_payload_is_quarantined_and_queue_progresses(store):
    first = store.send("claude")
    second = store.send("claude")
    store.db.execute("UPDATE messages SET payload='null' WHERE id=?", (first["id"],))
    assert [m["id"] for _, m in store.inbox("codex")] == [second["id"]]
    assert store.status()["quarantined"] == 1


def test_oversized_import_drains_one_record_with_bounded_reads(store, tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text("x" * (safety.MAX_RECORD * 3) + "\n" + json.dumps(old_record()) + "\n")
    assert store.migrate(path, "claude", skip_invalid=True) == 1
    assert store.status()["quarantined"] == 1


def test_legacy_approval_is_audit_only(store, tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps(old_record(type="approved", replies_to="codex-9999")) + "\n")
    store.migrate(path, "claude")
    with pytest.raises(ValueError, match="current-schema"):
        store.check_approval("claude-0001")


def test_json_schema_rejects_wrong_field_types(store):
    with pytest.raises(ValueError, match="claim must be text"):
        store.send("claude", fields={"claim": ["not text"]})
    with pytest.raises(ValueError, match="to must"):
        store.send("claude", to=["codex", "codex"])


def test_scaffold_symlink_does_not_overwrite_external_file(tmp_path):
    root = tmp_path / "repo"
    git_init(root)
    outside = tmp_path / "outside"
    outside.write_text("keep")
    (root / "AGENTS.md").symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        initialize(root, {"agents": ["claude", "codex"]}, force=True)
    assert outside.read_text() == "keep"


def test_state_database_symlink_is_rejected(project, tmp_path):
    with Store(project) as store:
        path = store.path
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("keep")
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        Store(project)
    assert outside.read_text() == "keep"


def test_legacy_cursor_symlink_is_never_written(store, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("0")
    (store.root / "collab/.codex.cursor").symlink_to(outside)
    store.send("claude")
    store.mark_read("codex", [seq for seq, _ in store.inbox("codex")])
    assert outside.read_text() == "0"


def test_notification_command_is_literal_argv_and_structured_stdin(store, tmp_path):
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import pathlib,sys\npathlib.Path(sys.argv[1]).write_text(sys.stdin.read())\n"
    )
    output = tmp_path / "event.json"
    store.send(
        "claude", kind="finding", fields={"claim": "$(touch pwned)", "evidence": "echo safe"}
    )
    assert (
        notifications.watch(
            store,
            "codex",
            once=True,
            command=[sys.executable, str(adapter), str(output)],
            stream=io.StringIO(),
        )
        == 0
    )
    event = json.loads(output.read_text())
    assert event["messages"][0]["from"] == "claude"
    assert "claim" not in event["messages"][0]
    assert not (tmp_path / "pwned").exists()
    assert store.inbox("codex"), "notification must not consume inbox"


def test_shell_template_rejected_even_when_previously_quoted(project):
    assert (
        main(["--root", str(project), "watch", "--from", "codex", "--exec", 'echo "{agent}"']) == 2
    )


def test_notification_failure_retries_and_becomes_visible_failed_state(store):
    store.send("claude", kind="question")
    spec = {"types": ["question"]}
    store.subscribe("codex", "test", spec)
    now = time.time()
    for attempt in range(5):
        token, jobs = store.lease_notifications("codex", "test", now=now + attempt * 400)
        assert len(jobs) == 1
        store.finish_notifications(token, error="exit 17", now=now + attempt * 400)
    assert store.status()["notifications"] == {"failed": 1}
    assert store.retry_failed("codex", "test") == 1
    token, jobs = store.lease_notifications("codex", "test", now=now + 2000)
    assert len(jobs) == 1
    store.finish_notifications(token)
    assert store.status()["notifications"] == {"done": 1}


def test_notification_expired_lease_recovers_and_stale_completion_cannot_ack(store):
    store.send("claude", kind="question")
    store.subscribe("codex", "test", {"types": ["question"]})
    now = time.time()
    old_token, first = store.lease_notifications("codex", "test", now=now)
    assert first
    assert store.lease_notifications("codex", "test", now=now + 1)[1] == []
    new_token, retry = store.lease_notifications("codex", "test", now=now + 61)
    assert retry
    store.finish_notifications(old_token)
    assert store.status()["notifications"] == {"leased": 1}
    store.finish_notifications(new_token)
    assert store.status()["notifications"] == {"done": 1}


def test_notification_type_filter_prevents_ack_loops_and_subscriptions_are_independent(store):
    ping = store.send("claude")
    store.send("codex", kind="received", fields={"replies_to": ping["id"]})
    question = store.send("codex", kind="question")
    for consumer in ("desktop", "agent"):
        store.subscribe("claude", consumer, {"types": list(safety.WAKE_TYPES)})
        _, rows = store.lease_notifications("claude", consumer)
        assert [m["id"] for _, m in rows] == [question["id"]]
    with pytest.raises(ValueError, match="different subscription"):
        store.subscribe("claude", "desktop", {"types": ["received"]})


def test_notifier_timeout_is_recorded_and_not_consumed(store):
    store.send("claude", kind="question")
    result = notifications.watch(
        store,
        "codex",
        once=True,
        timeout=0.1,
        command=[sys.executable, "-c", "import time; time.sleep(20)"],
        stream=io.StringIO(),
    )
    assert result == 1
    assert store.status()["notifications"] == {"pending": 1}


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_invalid_poll_intervals_fail(store, interval):
    with pytest.raises(ValueError, match="interval"):
        notifications.watch(store, "codex", once=True, interval=interval)


def test_path_lease_conflicts_renewal_and_release_are_transactional(store):
    store.claim("claude", "src")
    with pytest.raises(ValueError, match="held"):
        store.claim("codex", "src/file.py")
    store.claim("claude", "src", seconds=1800)
    with pytest.raises(ValueError, match="owned"):
        store.release("codex", "src")
    store.release("claude", "src")
    store.claim("codex", "src/file.py")
    assert store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 4


def worker(root, agent, count):
    with Store(root) as store:
        for _ in range(count):
            store.send(agent, to=["codex"])


def test_32_concurrent_agent_instances_have_no_lost_or_duplicate_messages(tmp_path):
    git_init(tmp_path)
    agents = ["codex", *[f"claude-{i}" for i in range(31)]]
    initialize(tmp_path, {"agents": agents})
    processes = [
        multiprocessing.Process(target=worker, args=(tmp_path, agent, 2)) for agent in agents[1:]
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        if process.is_alive():
            process.kill()
            process.join()
        assert process.exitcode == 0
    with Store(tmp_path) as store:
        rows = store.inbox("codex")
        assert len(rows) == len({m["id"] for _, m in rows}) == 62
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def crash_worker(root, pipe):
    with Store(root) as store:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.execute(
            "INSERT INTO messages(id,sender,kind,created,payload) "
            "VALUES('crash','claude','ping',0,'{}')"
        )
        pipe.send(True)
        time.sleep(60)


def test_killed_writer_rolls_back_and_preserves_acknowledged_message(project):
    with Store(project) as store:
        before = store.send("claude")
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=crash_worker, args=(project, child))
    process.start()
    try:
        assert parent.poll(10)
        assert parent.recv()
    finally:
        process.kill()
        process.join(10)
        parent.close()
        child.close()
    with Store(project) as store:
        after = store.send("claude")
        assert [m["id"] for _, m in store.inbox("codex")] == [before["id"], after["id"]]
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_storage_failure_does_not_acknowledge_send(store):
    # Exercise a real SQLITE_FULL response, including SQLite's automatic rollback.
    pages = store.db.execute("PRAGMA page_count").fetchone()[0]
    store.db.execute(f"PRAGMA max_page_count={pages}")
    with pytest.raises(sqlite3.OperationalError, match="full"):
        store.send("claude", fields={"claim": "x" * 60000})
    assert store.status()["messages"] == 0
    assert not store.db.in_transaction


def test_prune_archives_before_removal_and_preserves_unread_or_referenced(store, tmp_path):
    first = store.send("claude", to=["codex"])
    store.mark_read("codex", [seq for seq, _ in store.inbox("codex")])
    second = store.send("claude", to=["codex"])
    archive = tmp_path.parent / (tmp_path.name + "-archive.jsonl")
    try:
        assert store.prune(time.time() + 1, archive) == 1
        assert json.loads(archive.read_text())["id"] == first["id"]
        assert store.inbox("codex")[0][1]["id"] == second["id"]
        with pytest.raises(FileExistsError):
            store.prune(time.time() + 1, archive)
    finally:
        archive.unlink(missing_ok=True)


def test_cli_roundtrip_activity_export_and_all_does_not_consume(project, capsys):
    capsys.readouterr()
    base = ["--root", str(project)]
    assert main([*base, "--from", "claude", "--type", "ping", "--to", "codex"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert main([*base, "--from", "codex", "--inbox", "--all"]) == 0
    assert sent["id"] in capsys.readouterr().out
    assert main([*base, "--from", "codex", "--inbox"]) == 0
    assert sent["id"] in capsys.readouterr().out
    assert main([*base, "--from", "codex", "--inbox"]) == 0
    assert not capsys.readouterr().out
    assert main([*base, "activity"]) == 0
    assert sent["id"] in capsys.readouterr().out
    assert main([*base, "export"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == sent["id"]


def test_git_failure_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="git .* failed"):
        provenance.find_root(tmp_path)


def test_native_windows_does_not_silently_disable_safety(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(RuntimeError, match="Linux/WSL"):
        safety.secure_directory(tmp_path)


def test_import_capacity_failure_is_not_quarantined_as_bad_input(store, tmp_path):
    settings = store.settings()
    settings["max_messages"] = 1
    store.configure(settings)
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps(old_record()) + "\n" + json.dumps(old_record(id="claude-0002")) + "\n"
    )
    with pytest.raises(RuntimeError, match="capacity"):
        store.migrate(path, "claude", skip_invalid=True)
    assert store.status()["messages"] == 0


def test_read_only_export_reports_corruption_without_mutating_store(store, capsys):
    seed = store.send("claude")
    with store.transaction():
        for index in range(100):
            store._insert({**seed, "id": f"invalid-{index}"})
        store.db.execute("UPDATE messages SET payload='null'")
    store.send("claude")
    before = store.path.read_bytes()
    capsys.readouterr()
    assert main(["--root", str(store.root), "export"]) == 2
    assert "invalid stored message" in capsys.readouterr().err
    assert store.path.read_bytes() == before
    assert store.status()["quarantined"] == 0


def test_one_callback_batch_per_consumer_and_rate_gate(store):
    seed = store.send("claude", kind="question")
    with store.transaction():
        for index in range(101):
            store._insert({**seed, "id": f"bulk-{index}"})
    store.subscribe("codex", "test", {"types": ["question"], "interval": 5})
    now = time.time()
    token, first = store.lease_notifications("codex", "test", now=now)
    assert len(first) == 100
    assert store.lease_notifications("codex", "test", now=now + 1)[1] == []
    store.finish_notifications(token, now=now + 1)
    assert store.lease_notifications("codex", "test", now=now + 2)[1] == []
    assert len(store.lease_notifications("codex", "test", now=now + 6)[1]) == 2


def test_inbox_lookup_uses_unread_index(store):
    plan = [
        row[3]
        for row in store.db.execute("""EXPLAIN QUERY PLAN
        SELECT m.seq,m.payload FROM deliveries d JOIN messages m ON m.seq=d.seq
        WHERE d.agent='codex' AND d.seq>0 AND d.read_at IS NULL ORDER BY d.seq LIMIT 100""")
    ]
    assert any("USING INDEX unread" in line for line in plan)
    assert all("SCAN m" not in line and "SCAN d" not in line for line in plan)


def test_prune_does_not_delete_messages_awaiting_subscription_scan(store, tmp_path):
    store.subscribe("codex", "test", {"types": ["ping"]})
    store.send("claude", to=["codex"])
    store.mark_read("codex", [seq for seq, _ in store.inbox("codex")])
    archive = tmp_path.parent / (tmp_path.name + "-pending.jsonl")
    try:
        assert store.prune(time.time() + 1, archive) == 0
        assert store.status()["messages"] == 1
    finally:
        archive.unlink(missing_ok=True)


def test_configure_cannot_remove_historical_identities(store):
    settings = store.settings()
    settings["agents"].remove("claude")
    with pytest.raises(ValueError, match="cannot add or remove"):
        store.configure(settings)


def test_submodule_dirty_contents_change_snapshot(project, store, tmp_path):
    child = tmp_path / "child-source"
    git_init(child)
    git(project, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "module")
    git(project, "add", ".gitmodules", "module")
    git(project, "commit", "-qm", "submodule")
    (project / "module/seed.txt").write_text("changed one")
    first = provenance.provenance(project, store.settings())
    (project / "module/seed.txt").write_text("changed two")
    assert first != provenance.provenance(project, store.settings())


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flags_cannot_hide_modified_code(project, store, flag):
    git(project, "update-index", flag, "seed.txt")
    (project / "seed.txt").write_text("hidden change")
    with pytest.raises(ValueError, match="index entries"):
        store.send("claude")


def test_unknown_cursor_named_source_is_not_excluded(project, store):
    path = project / "collab/.hidden.cursor"
    path.write_text("one")
    first = provenance.provenance(project, store.settings())
    path.write_text("two")
    assert provenance.provenance(project, store.settings()) != first


def test_pre_rename_and_review_only_legacy_provenance_are_preserved(store, tmp_path):
    path = tmp_path / "old.jsonl"
    records = [
        old_record(provenance={"commit": "abc", "dirty_sha256": "old"}),
        old_record(id="claude-0002", provenance={"commit": "abc", "reviewed_file_sha256": "file"}),
    ]
    original = "".join(json.dumps(record) + "\n" for record in records)
    path.write_text(original)
    assert store.migrate(path, "claude") == 2
    assert store.get("claude-0001")["provenance"]["snapshot_sha256"] == "old"
    assert store.get("claude-0002")["provenance"]["reviewed_file_sha256"] == "file"
    assert "snapshot_sha256" not in store.get("claude-0002")["provenance"]
    assert store.migrate(path, "claude") == 0
    assert path.read_text() == original


def test_missing_new_schema_snapshot_is_not_treated_as_legacy(store):
    message = store.send("claude")
    del message["provenance"]["snapshot_sha256"]
    store.db.execute(
        "UPDATE messages SET payload=? WHERE id=?", (safety.encode(message), message["id"])
    )
    assert store.inbox("codex") == []
    assert store.status()["quarantined"] == 1


@pytest.mark.parametrize(
    "reference", ["../other/code.py:12", "/outside/code.py", "C:\\other\\code.py"]
)
def test_cross_repository_references_cannot_receive_misleading_provenance(store, reference):
    with pytest.raises(ValueError, match="repository"):
        store.send("claude", kind="claim", fields={"ref": reference, "evidence": "pytest -q"})


def test_approval_lookup_rejects_another_project_identity(store):
    message = store.send("claude", kind="claim", fields={"evidence": "pytest -q"})
    message["project"] = "0" * 32
    store.db.execute(
        "UPDATE messages SET payload=? WHERE id=?", (safety.encode(message), message["id"])
    )
    with pytest.raises(ValueError, match="different project"):
        store.get(message["id"])


@pytest.mark.parametrize("command", ["status", "activity", "export", "entry-status"])
def test_reads_do_not_initialize_missing_mailbox(tmp_path, command):
    git_init(tmp_path)
    assert main(["--root", str(tmp_path), command]) == 2
    assert not (tmp_path / ".git/agent-collab").exists()


def test_directory_chmod_noop_is_rejected(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    monkeypatch.setattr(Path, "chmod", lambda *_a, **_k: None)
    with pytest.raises(ValueError, match="owner-only"):
        safety.secure_directory(directory)


def test_database_chmod_noop_is_rejected(project, monkeypatch):
    path = project / ".git/agent-collab/mailbox.sqlite3"
    path.chmod(0o666)
    monkeypatch.setattr(os, "fchmod", lambda *_a: None)
    with pytest.raises(ValueError, match="owner-only"):
        Store(project)


def test_linux_state_permissions_and_read_only_operations(store):
    import stat

    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.send("claude")
    before = store.path.read_bytes()
    for command in ("status", "activity", "export"):
        assert main(["--root", str(store.root), command]) == 0
    assert main(["--root", str(store.root), "--from", "codex", "--inbox", "--all"]) == 0
    assert before == store.path.read_bytes()


def test_release_normalizes_same_path_as_claim(store):
    store.claim("codex", "tests///")
    store.release("codex", "tests///")
    assert store.status()["active_claims"] == []
    with pytest.raises(ValueError, match="within"):
        store.release("codex", "../tests/")


def test_external_state_shared_across_worktrees_and_rejects_other_clone(tmp_path):
    root = tmp_path / "repo"
    git_init(root)
    external = tmp_path / "state"
    git(root, "config", "--local", "agent-collab.stateDirectory", str(external))
    initialize(root, {"agents": ["claude", "codex"]})
    assert not (root / ".git/agent-collab").exists()
    git(root, "add", ".")
    git(root, "commit", "-qm", "instructions")
    peer = tmp_path / "peer"
    git(root, "worktree", "add", "--detach", str(peer))
    with Store(root) as first, Store(peer) as second:
        sent = first.send("claude")
        assert second.inbox("codex")[0][1]["id"] == sent["id"]
        assert first.path == second.path
    unrelated = tmp_path / "unrelated"
    git_init(unrelated)
    git(unrelated, "config", "--local", "agent-collab.stateDirectory", str(external))
    with pytest.raises(ValueError, match="different Git common"):
        Store(unrelated)


@pytest.mark.parametrize("path", ["relative", "inside"])
def test_external_state_rejects_unsafe_location(tmp_path, path):
    git_init(tmp_path)
    value = path if path == "relative" else str(tmp_path / "state")
    git(tmp_path, "config", "--local", "agent-collab.stateDirectory", value)
    with pytest.raises(ValueError, match="stateDirectory"):
        Store(tmp_path)
    assert not (tmp_path / "state").exists()


def test_unapproved_agent_cannot_send_read_or_claim(store):
    request = store.request_entry("Claude Code", "Reviewer", "Review storage")
    assert request["state"] == "pending"
    for action in (
        lambda: store.send(request["id"]),
        lambda: store.inbox(request["id"]),
        lambda: store.claim(request["id"], "tests"),
        lambda: store.register("new-agent"),
    ):
        with pytest.raises(ValueError, match="unknown agent|registration is disabled"):
            action()
    assert store.status()["messages"] == 0


def test_admission_requires_permission_and_assigns_distinct_ids(store):
    first = store.request_entry("Codex", "Reviewer", "Review")
    second = store.request_entry("Codex", "Reviewer", "Review")
    with pytest.raises(ValueError, match="permission"):
        store.decide_entry(first["id"], "", approve=True)
    approved = store.decide_entry(first["id"], "User approved request in session", approve=True)
    other = store.decide_entry(second["id"], "User approved second request", approve=True)
    assert approved["agent"] != other["agent"]
    assert approved["declaration"]["provider"] == "Codex"
    assert approved["agent"].startswith("agent-")
    store.send(approved["agent"], to=["codex"])
    assert store.inbox("codex")
    with pytest.raises(ValueError, match="already decided"):
        store.decide_entry(first["id"], "Again", approve=True)


def test_denied_request_cannot_be_approved(store):
    request = store.request_entry("Claude", "Builder", "Build")
    denied = store.decide_entry(request["id"], "User declined", approve=False)
    assert denied["agent"] is None
    with pytest.raises(ValueError, match="already decided"):
        store.decide_entry(request["id"], "Retry", approve=True)


def test_configuration_cannot_bypass_admission(store):
    settings = store.settings()
    settings["agents"].append("unapproved")
    with pytest.raises(ValueError, match="admission"):
        store.configure(settings)
    assert "unapproved" not in store.settings()["agents"]


def test_pending_admission_capacity_and_missing_declaration(store):
    with pytest.raises(ValueError, match="provider"):
        store.request_entry("", "A", "B")
    for _ in range(128):
        store.request_entry("Codex", "A", "B")
    with pytest.raises(ValueError, match="capacity"):
        store.request_entry("Codex", "A", "B")


def test_admission_at_agent_limit_rolls_back(store):
    for i in range(29):
        request = store.request_entry("Codex", str(i), "Test")
        store.decide_entry(request["id"], "Fixture approval", approve=True)
    request = store.request_entry("Codex", "Overflow", "Test")
    with pytest.raises(ValueError, match="32"):
        store.decide_entry(request["id"], "Fixture approval", approve=True)
    assert store.entry_status(request["id"])["state"] == "pending"
    assert len(store.settings()["agents"]) == 32


def test_cli_default_init_admits_nobody_and_complete_admission(tmp_path, capsys):
    git_init(tmp_path)
    assert main(["--root", str(tmp_path), "init"]) == 0
    with Store(tmp_path) as store:
        assert store.settings()["agents"] == []
    capsys.readouterr()
    base = ["--root", str(tmp_path)]
    assert (
        main(
            base
            + [
                "request-entry",
                "--provider",
                "Codex",
                "--display-name",
                "Reviewer",
                "--purpose",
                "Review",
            ]
        )
        == 0
    )
    request = json.loads(capsys.readouterr().out)
    assert main(base + ["approve-entry", "--id", request["id"]]) == 2
    capsys.readouterr()
    assert (
        main(
            base
            + [
                "approve-entry",
                "--id",
                request["id"],
                "--permission",
                "User explicitly approved this fixture request",
            ]
        )
        == 0
    )
    admitted = json.loads(capsys.readouterr().out)
    assert admitted["state"] == "approved"
    assert main(base + ["entry-status", "--id", request["id"]]) == 0


def test_cli_bootstrap_requires_explicit_user_permission(tmp_path):
    git_init(tmp_path)
    assert main(["--root", str(tmp_path), "init", "--agents", "codex"]) == 2
    assert not (tmp_path / ".git/agent-collab").exists()


def test_old_store_requires_explicit_upgrade_preserving_identity_and_messages(store):
    sent = store.send("claude")
    project = store.project_id()
    settings = store.settings()
    store.db.execute("DROP TABLE admissions")
    store.db.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError, match="explicit init"):
        Store(store.root, create=False)
    initialize(store.root, settings)
    with Store(store.root, create=False) as updated:
        assert updated.project_id() == project
        assert updated.get(sent["id"]) == sent
        assert updated.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert updated.request_entry("Codex", "Reviewer", "Review")["state"] == "pending"
