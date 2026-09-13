"""Bounded input and POSIX filesystem primitives. No shell execution."""

import contextlib
import hashlib
import json
import os
import re
import stat
from pathlib import Path

MAX_RECORD = 65536
MAX_BATCH = 100
MAX_AGENTS = 32
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}")
MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
TYPES = (
    "ping",
    "finding",
    "claim",
    "question",
    "handoff",
    "escalate",
    "received",
    "reproduced",
    "disputed",
    "approved",
)
# A verdict that names no subject is unreadable evidence: a 10-agent trial produced
# ten verdicts with no reply target, so nothing linked a result to the claim it judged.
RESPONSE_TYPES = ("received", "reproduced", "disputed", "approved")
WAKE_TYPES = ("finding", "claim", "question", "handoff", "escalate")


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("agent/consumer names must be 1-48 ASCII letters, digits, _ or -")
    return value


def relative(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("expected a nonempty repository-relative POSIX path")
    parts = value.split("/")
    if any(p in ("", ".", "..", ".git") for p in parts):
        raise ValueError("path must stay within the repository")
    return value


def encode(value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(data.encode()) > MAX_RECORD:
        raise ValueError(f"message exceeds {MAX_RECORD} bytes")
    return data


def decode(data):
    if len(data) > MAX_RECORD:
        raise ValueError("record too large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"non-finite JSON value: {value}")

    try:
        result = json.loads(data, object_pairs_hook=unique, parse_constant=reject)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("invalid or excessively nested JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("message must be a JSON object")
    return result


def config(value):
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    if set(value) - {"agents", "provenance_files", "exclude", "max_messages"}:
        raise ValueError("unknown configuration key")
    agents = value.get("agents", [])
    if not isinstance(agents, list) or not 0 <= len(agents) <= MAX_AGENTS:
        raise ValueError(f"configure between 0 and {MAX_AGENTS} agent instances")
    if len({name(agent) for agent in agents}) != len(agents):
        raise ValueError("agent names must be unique")
    result = {"agents": agents}
    for key in ("provenance_files", "exclude"):
        paths = value.get(key, [])
        if not isinstance(paths, list) or len(paths) > 256:
            raise ValueError(f"{key} must be a bounded list")
        result[key] = sorted({relative(p) for p in paths})
    capacity = value.get("max_messages", 100000)
    if type(capacity) is not int or not 1 <= capacity <= 1000000:
        raise ValueError("max_messages must be between 1 and 1000000")
    result["max_messages"] = capacity
    return result


def permission(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("record the user's explicit permission/rejection (1-2000 characters)")
    return value.strip()


def envelope(message, settings, *, legacy=False):
    encode(message)
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    for key in ("id", "ts", "from", "type", "provenance"):
        if key not in message:
            raise ValueError(f"missing {key}")
    if not isinstance(message["id"], str) or not MESSAGE_ID.fullmatch(message["id"]):
        raise ValueError("invalid message ID")
    if name(message["from"]) not in settings["agents"]:
        raise ValueError("unknown agent")
    if message["type"] not in TYPES:
        raise ValueError("unknown message type")
    if not isinstance(message["ts"], str) or not 1 <= len(message["ts"]) <= 64:
        raise ValueError("invalid timestamp")
    recipients = message.get("to")
    if (
        not isinstance(recipients, list)
        or not recipients
        or any(not isinstance(a, str) or a not in settings["agents"] for a in recipients)
        or len(set(recipients)) != len(recipients)
        or message["from"] in recipients
    ):
        raise ValueError("to must name distinct other registered agents")
    for key in ("claim", "ref", "expect", "evidence", "task", "replies_to", "status"):
        if key in message and not isinstance(message[key], str):
            raise ValueError(f"{key} must be text")
    if "severity" in message and message["severity"] not in ("P1", "P2", "P3"):
        raise ValueError("invalid severity")
    if "evidence_kind" in message and message["evidence_kind"] not in ("command", "citation"):
        raise ValueError("invalid evidence kind")
    prov = message["provenance"]
    if not isinstance(prov, dict):
        raise ValueError("provenance must be an object")
    for key, length in (("commit", 40), ("snapshot_sha256", 64)):
        text = prov.get(key)
        if legacy and key == "snapshot_sha256" and text is None:
            # Historical review-only records may have no whole-tree fingerprint.
            # They remain legacy audit evidence and can never authorize integration.
            continue
        if not isinstance(text, str) or not text:
            raise ValueError(f"missing provenance {key}")
        if not legacy and not re.fullmatch(rf"[0-9a-f]{{{length}}}", text):
            raise ValueError(f"invalid provenance {key}")
    if not legacy and message.get("schema") != 2:
        raise ValueError("unsupported message schema")
    if not legacy and (
        not isinstance(message.get("project"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", message["project"])
    ):
        raise ValueError("missing or invalid project identity")
    if not legacy and message["type"] in ("finding", "claim") and not message.get("evidence"):
        raise ValueError("a finding or claim needs evidence")
    return message


def display(value):
    """Keep terminal controls and multiline impersonation out of event summaries."""
    return json.dumps(value, ensure_ascii=True)


def no_symlinks(path):
    """Validate existing components, used inside the trusted-owner state directory."""
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ValueError(f"refusing symlink: {component}")
    return path


def secure_directory(path, *, create=True, read_only=False):
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("agent-collab 0.2 supports Linux/WSL POSIX filesystems only")
    path = no_symlinks(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("state directory must be owned by the current user")
    if not read_only:
        path.chmod(0o700)
    owner_only(path.stat().st_mode)
    return path


def owner_only(mode):
    if stat.S_IMODE(mode) & 0o077:
        raise ValueError(
            "mailbox requires owner-only permissions; chmod did not establish them. "
            "Use a local Linux filesystem; see MIGRATION.md for state relocation"
        )


def safe_write(root, relative_path, data, *, overwrite=False):
    """Write scaffold files via directory descriptors; never follow planted symlinks."""
    parts = relative(relative_path).split("/")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, mode=0o755, dir_fd=directory)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_TRUNC if overwrite else os.O_EXCL
        try:
            descriptor = os.open(parts[-1], flags, 0o644, dir_fd=directory)
        except FileExistsError:
            info = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("scaffold target is not a regular file") from None
            return False
        with os.fdopen(descriptor, "wb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("scaffold target is not a regular file")
            handle.write(data.encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory)
        return True
    finally:
        os.close(directory)


def file_digest(root, path, *, regular_only=False):
    """Hash bytes/type/mode without following symlinks, using bounded memory."""
    parts = relative(path).split("/")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            if regular_only:
                raise ValueError("provenance input must be a regular file")
            return "symlink", hashlib.sha256(
                os.fsencode(os.readlink(parts[-1], dir_fd=directory))
            ).hexdigest()
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as handle:
            start = os.fstat(handle.fileno())
            if not stat.S_ISREG(start.st_mode):
                raise ValueError("snapshot target is not a regular file")
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            end = os.fstat(handle.fileno())
        if (start.st_ino, start.st_size, start.st_mtime_ns, start.st_ctime_ns) != (
            end.st_ino,
            end.st_size,
            end.st_mtime_ns,
            end.st_ctime_ns,
        ):
            raise ValueError("file changed while hashing; retry on a fixed revision")
        return "executable" if start.st_mode & 0o111 else "file", digest
    finally:
        os.close(directory)


def render(value):
    """Output wrappers can exceed the input record cap by their small fixed metadata."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
