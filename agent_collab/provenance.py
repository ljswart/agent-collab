"""Canonical, full-length worktree fingerprints, separate from shared mailbox state."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import safety

GIT = shutil.which("git") or "git"


def git(root, *args):
    result = subprocess.run(  # noqa: S603 - fixed git executable, literal argv, no shell
        [GIT, *args],
        cwd=root,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def find_root(start=None):
    start = Path(start or os.environ.get("COLLAB_ROOT") or Path.cwd()).absolute()
    return Path(os.fsdecode(git(start, "rev-parse", "--show-toplevel").removesuffix(b"\n")))


def common_dir(root):
    raw = Path(os.fsdecode(git(root, "rev-parse", "--git-common-dir").removesuffix(b"\n")))
    return (raw if raw.is_absolute() else Path(root) / raw).absolute()


def runtime(path, settings):
    return (
        path in settings["exclude"]
        or path == "collab/.lock"
        or path in {f"collab/{agent}.outbox.jsonl" for agent in settings["agents"]}
        or path
        in {
            p
            for agent in settings["agents"]
            for p in (f"collab/.{agent}.cursor", f"collab/.watch.{agent}.cursor")
        }
    )


def snapshot(root, settings, depth=0):
    if depth > 8:
        raise ValueError("submodule nesting exceeds snapshot limit")
    flags = git(root, "ls-files", "-v", "-z").split(b"\0")
    if any(item and (item[:1].islower() or item[:1] == b"S") for item in flags):
        raise ValueError("snapshot refuses assume-unchanged or skip-worktree index entries")
    if git(root, "ls-files", "--unmerged", "-z"):
        raise ValueError("snapshot refuses unresolved merge conflicts")
    changed = git(root, "diff", "--name-only", "-z", "HEAD").split(b"\0")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    paths = sorted({os.fsdecode(p) for p in changed + untracked if p})
    entries = []
    for path in paths:
        if runtime(path, settings):
            continue
        target = Path(root) / path
        try:
            if target.is_dir() and not target.is_symlink():
                # Git lists a changed gitlink as a directory, not its descendant files.
                sub = provenance(
                    target, {"exclude": [], "provenance_files": [], "agents": []}, depth + 1
                )
                kind, digest = "submodule", sub
            else:
                kind, digest = safety.file_digest(root, path)
        except FileNotFoundError:
            kind, digest = "deleted", None
        entries.append([path, kind, digest])
    return entries


def provenance(root, settings, depth=0):
    """Reject observable concurrent mutation; integration must recheck this fingerprint."""
    commit = git(root, "rev-parse", "HEAD").strip().decode("ascii")
    first = snapshot(root, settings, depth)
    inputs = {
        p: safety.file_digest(root, p, regular_only=True)[1] for p in settings["provenance_files"]
    }
    second = snapshot(root, settings, depth)
    again = {
        p: safety.file_digest(root, p, regular_only=True)[1] for p in settings["provenance_files"]
    }
    if (
        first != second
        or inputs != again
        or git(root, "rev-parse", "HEAD").strip().decode() != commit
    ):
        raise ValueError("worktree changed while hashing; review an immutable revision")
    policy = {k: settings[k] for k in ("exclude", "provenance_files")}
    canonical = json.dumps(
        {"entries": first, "inputs": inputs, "policy": policy},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "commit": commit,
        "snapshot_sha256": hashlib.sha256(canonical).hexdigest(),
        "uncommitted_paths": len(first),
        "clean": not first,
        "inputs_sha256": inputs,
    }


def revision(value):
    return value["commit"], value["snapshot_sha256"]
