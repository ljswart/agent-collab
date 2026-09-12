"""Guards for the agent mailbox protocol.

Each test pins a defect found during real use, so a refactor cannot quietly
reintroduce it. Everything runs against temporary directories: a test must never
touch a live mailbox.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collab  # noqa: E402


@pytest.fixture
def project(tmp_path):
    """A real git repo with an initialised mailbox."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    collab.init(tmp_path, collab.DEFAULT_AGENTS)
    return tmp_path


def record(agent="claude", **extra):
    return {
        "id": None,
        "ts": "2026-01-01T00:00:00+00:00",
        "from": agent,
        "type": "ping",
        "provenance": {"commit": "abc", "snapshot_sha256": "def"},
        **extra,
    }


def test_init_scaffolds_a_usable_mailbox(project):
    box = collab.mailbox(project)
    assert (box / "claude.outbox.jsonl").exists()
    assert (box / "codex.outbox.jsonl").exists()
    assert (box / "config.json").exists()
    assert (box / "OWNERSHIP.md").exists()
    assert (project / "AGENTS.md").exists()
    assert collab.load_config(project)["agents"] == ["claude", "codex"]


def test_find_root_locates_the_git_toplevel(project):
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    assert collab.find_root(nested).resolve() == project.resolve()


def test_snapshot_sees_untracked_file_contents(project):
    config = collab.load_config(project)
    probe = project / "new_source.py"
    probe.write_text("VERSION = 1\n")
    first = collab.provenance(project, config)["snapshot_sha256"]
    probe.write_text("VERSION = 2  # materially different\n")
    second = collab.provenance(project, config)["snapshot_sha256"]
    assert first != second, "a bare '?? path' hides the bytes that matter"


def test_snapshot_excludes_mailbox_traffic(project):
    config = collab.load_config(project)
    before = collab.provenance(project, config)["snapshot_sha256"]
    collab.append(project, "claude", record(), config)
    after = collab.provenance(project, config)["snapshot_sha256"]
    assert before == after, "sending a message must not stale outstanding approvals"


def test_snapshot_includes_reviewable_source(project):
    config = collab.load_config(project)
    before = collab.provenance(project, config)["snapshot_sha256"]
    (project / "collab" / "OWNERSHIP.md").write_text("# changed\n")
    assert collab.provenance(project, config)["snapshot_sha256"] != before


def test_provenance_files_are_hashed(project):
    box = collab.mailbox(project)
    data = project / "frozen.json"
    data.write_text('{"a":1}')
    (box / "config.json").write_text(
        json.dumps({"agents": ["claude", "codex"], "provenance_files": ["frozen.json"]})
    )
    config = collab.load_config(project)
    first = collab.provenance(project, config)["frozen_sha256"]
    data.write_text('{"a":2}')
    assert collab.provenance(project, config)["frozen_sha256"] != first


def test_ids_are_allocated_under_the_append_lock(project):
    config = collab.load_config(project)
    ids = [
        collab.append(project, "codex", record("codex"), config)["id"] for _ in range(3)
    ]
    assert len(set(ids)) == 3, ids
    written = [
        json.loads(line)["id"]
        for line in collab.outbox(project, "codex").read_text().splitlines()
    ]
    assert written == ids


def test_partial_trailing_record_keeps_complete_ones(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"id":"a"}\n{"id":"b"}\n{"id":')
    assert [r["id"] for r in collab.read_all(path)] == ["a", "b"]


def test_malformed_complete_record_is_reported_not_fatal(tmp_path, capsys):
    path = tmp_path / "t.jsonl"
    path.write_text('{"id":"a"}\n{"bad json"}\n{"id":"c"}\n')
    assert [r["id"] for r in collab.read_all(path)] == ["a", "c"]
    assert "malformed" in capsys.readouterr().err
    with pytest.raises(json.JSONDecodeError):
        collab.read_all(path, strict=True)


def test_incomplete_record_does_not_crash_the_reader(project, capsys):
    config = collab.load_config(project)
    collab.outbox(project, "codex").write_text(
        '{"id":"codex-0001","ts":"t","from":"codex","type":"ping","provenance":{"commit":"a"}}\n'
        '{"id":"codex-0002","type":"ping"}\n'
    )
    collab.show_inbox(project, "claude", config, show_all=True)
    out = capsys.readouterr().out
    assert "codex-0001" in out
    assert "codex-0002" in out
    assert "INCOMPLETE RECORD" in out


def test_append_refuses_incomplete_or_unknown(project):
    config = collab.load_config(project)
    with pytest.raises(ValueError, match="missing"):
        collab.append(project, "claude", {"id": None, "type": "ping"}, config)
    with pytest.raises(ValueError, match=r"missing \['provenance'\]"):
        collab.append(project, "claude", record(provenance={}), config)
    with pytest.raises(ValueError, match="unknown agent"):
        collab.append(project, "mallory", record("mallory"), config)
    assert not (collab.mailbox(project) / "mallory.outbox.jsonl").exists()


def test_cursor_only_advances_over_complete_records(project):
    config = collab.load_config(project)
    collab.append(project, "codex", record("codex"), config)
    assert len(collab.show_inbox(project, "claude", config)) == 1
    assert len(collab.show_inbox(project, "claude", config)) == 0
    collab.append(project, "codex", record("codex"), config)
    assert len(collab.show_inbox(project, "claude", config)) == 1


def test_watch_does_not_consume_the_agents_unread_queue(project, capsys):
    """The watch cursor must be separate from the read cursor.

    A watcher that advanced the read cursor would announce a message and then make
    it invisible to --inbox, so the agent that has to act on it never sees it.
    """
    config = collab.load_config(project)
    collab.append(project, "codex", record("codex"), config)

    fresh = collab.watch(
        project, config, "claude", interval=0.01, once=True, from_start=True
    )
    assert len(fresh) == 1
    assert (collab.mailbox(project) / ".watch.claude.cursor").exists()
    assert not (collab.mailbox(project) / ".claude.cursor").exists()

    capsys.readouterr()
    assert len(collab.show_inbox(project, "claude", config)) == 1, (
        "inbox must still deliver it"
    )


def test_watch_reports_only_new_records(project, capsys):
    config = collab.load_config(project)
    collab.append(project, "codex", record("codex"), config)
    assert (
        len(
            collab.watch(
                project, config, "claude", interval=0.01, once=True, from_start=True
            )
        )
        == 1
    )
    assert collab.watch(project, config, "claude", interval=0.01, once=True) == []
    collab.append(project, "codex", record("codex"), config)
    assert len(collab.watch(project, config, "claude", interval=0.01, once=True)) == 1


def test_watch_exec_substitutes_placeholders(project, tmp_path, capsys):
    config = collab.load_config(project)
    collab.append(project, "codex", record("codex"), config)
    marker = tmp_path / "woken.txt"
    collab.watch(
        project,
        config,
        "claude",
        interval=0.01,
        once=True,
        from_start=True,
        command=f"echo '{{count}} {{agent}} {{ids}}' > {marker}",
    )
    assert marker.read_text().strip() == "1 codex codex-0001"


def test_watch_defers_an_incomplete_tail(project, capsys):
    """A torn append must not be announced as a message."""
    config = collab.load_config(project)
    collab.append(project, "codex", record("codex"), config)
    box = collab.outbox(project, "codex")
    box.write_text(box.read_text() + '{"id":"codex-000')
    fresh = collab.watch(
        project, config, "claude", interval=0.01, once=True, from_start=True
    )
    assert len(fresh) == 1, "only the complete record is announced"


def test_git_failure_fails_closed(tmp_path, monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "fatal: not a git repository"

    def fail(*_args, **_kwargs):
        return Failed()

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="git .* failed"):
        collab._git(tmp_path, "rev-parse", "HEAD")


def test_runtime_state_classification():
    config = {"agents": ["claude", "codex"], "exclude": []}
    for path in (
        "collab/claude.outbox.jsonl",
        "collab/codex.outbox.jsonl",
        "collab/.lock",
        "collab/.claude.cursor",
    ):
        assert collab._is_runtime_state(path, config), path
    for path in ("collab/OWNERSHIP.md", "collab/config.json", "src/thing.py"):
        assert not collab._is_runtime_state(path, config), path


def test_force_actually_overwrites(project):
    """--force was parsed and then dropped on the way to init(); templates never moved."""
    target = project / "AGENTS.md"
    target.write_text("stale\n")
    collab.main(["--root", str(project), "init"])
    assert target.read_text() == "stale\n", "init without --force must not clobber"
    collab.main(["--root", str(project), "init", "--force"])
    assert target.read_text() != "stale\n", "--force must regenerate from the template"


def test_config_requires_exactly_two_agents(project):
    (collab.mailbox(project) / "config.json").write_text(
        json.dumps({"agents": ["solo"]})
    )
    with pytest.raises(ValueError, match="exactly two agents"):
        collab.load_config(project)


def test_end_to_end_round_trip(project, capsys):
    assert (
        collab.main(
            [
                "--root",
                str(project),
                "--from",
                "claude",
                "--type",
                "ping",
                "--claim",
                "hello",
            ]
        )
        == 0
    )
    assert collab.main(["--root", str(project), "--from", "codex", "--inbox"]) == 0
    out = capsys.readouterr().out
    assert "PING" in out and "hello" in out
    assert (
        collab.main(
            [
                "--root",
                str(project),
                "--from",
                "codex",
                "--type",
                "received",
                "--replies-to",
                "claude-0001",
                "--claim",
                "got it",
            ]
        )
        == 0
    )
    assert collab.main(["--root", str(project), "--from", "claude", "--inbox"]) == 0
    assert "RECEIVED" in capsys.readouterr().out
