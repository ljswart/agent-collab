"""Transactional single-host mailbox. SQLite owns framing, IDs, cursors and leases."""

import contextlib
import hashlib
import os
import re
import sqlite3
import stat
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import provenance as pv
from . import safety

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stats(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 count INTEGER NOT NULL);
INSERT OR IGNORE INTO stats VALUES(1,0);
CREATE TABLE IF NOT EXISTS messages(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 sender TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL, reply_to TEXT,
 payload TEXT NOT NULL CHECK(length(payload)<=65536));
CREATE INDEX IF NOT EXISTS replies ON messages(reply_to);
CREATE INDEX IF NOT EXISTS retained ON messages(kind,created,seq);
CREATE TRIGGER IF NOT EXISTS count_insert AFTER INSERT ON messages BEGIN
 UPDATE stats SET count=count+1 WHERE singleton=1; END;
CREATE TRIGGER IF NOT EXISTS count_delete AFTER DELETE ON messages BEGIN
 UPDATE stats SET count=count-1 WHERE singleton=1; END;
CREATE TABLE IF NOT EXISTS deliveries(
 agent TEXT NOT NULL, seq INTEGER NOT NULL REFERENCES messages(seq) ON DELETE CASCADE,
 read_at REAL, PRIMARY KEY(agent,seq));
CREATE INDEX IF NOT EXISTS unread ON deliveries(agent,seq) WHERE read_at IS NULL;
CREATE TABLE IF NOT EXISTS subscriptions(
 agent TEXT NOT NULL, consumer TEXT NOT NULL, spec TEXT NOT NULL,
 scanned INTEGER NOT NULL DEFAULT 0, active_token TEXT,
 active_until REAL NOT NULL DEFAULT 0, next_run REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(agent,consumer));
CREATE TABLE IF NOT EXISTS notifications(
 agent TEXT NOT NULL, consumer TEXT NOT NULL,
 seq INTEGER NOT NULL REFERENCES messages(seq) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, token TEXT, lease_until REAL NOT NULL DEFAULT 0,
 error TEXT, PRIMARY KEY(agent,consumer,seq));
CREATE INDEX IF NOT EXISTS ready ON notifications(agent,consumer,state,next_attempt,seq);
CREATE TABLE IF NOT EXISTS quarantine(
 digest TEXT PRIMARY KEY, origin TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS claims(
 path TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
 event TEXT NOT NULL, detail TEXT NOT NULL);
PRAGMA user_version=3;
COMMIT;
"""


ADMISSION_SCHEMA = """CREATE TABLE IF NOT EXISTS admissions(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 declaration TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 agent TEXT UNIQUE, permission TEXT, created REAL NOT NULL)"""


class Store:
    def __init__(self, root, *, create=True, read_only=False):
        self.root = pv.find_root(root)
        self.read_only = read_only
        common = pv.common_dir(self.root).resolve()
        # Read only the shared local config, never global/worktree overrides.
        configured = pv.git(
            self.root, "config", "--file", str(common / "config"), "--null", "--list"
        ).split(b"\0")
        paths = [
            v.split(b"\n", 1)[1]
            for v in configured
            if v.startswith(b"agent-collab.statedirectory\n")
        ]
        if len(paths) > 1:
            raise ValueError("stateDirectory must have exactly one value")
        directory = Path(os.fsdecode(paths[0])) if paths else common / "agent-collab"
        if not directory.is_absolute():
            raise ValueError("stateDirectory must be an absolute path")
        if paths and (directory == self.root or self.root in directory.parents):
            raise ValueError("configured stateDirectory must be outside the worktree")
        if not create and not (directory / "mailbox.sqlite3").exists():
            raise ValueError("mailbox is not initialized; run init")
        self.directory = safety.secure_directory(directory, create=create, read_only=read_only)
        self.path = self.directory / "mailbox.sqlite3"
        for suffix in ("", "-journal", "-wal", "-shm"):
            safety.no_symlinks(str(self.path) + suffix)
        flags = os.O_RDONLY if read_only else os.O_RDWR
        if create and not read_only:
            flags |= os.O_CREAT
        fd = os.open(self.path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ValueError(
                    "mailbox database must be a singly linked, owner-controlled regular file"
                )
            if not read_only:
                os.fchmod(fd, 0o600)
            safety.owner_only(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(
            self.path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA trusted_schema=OFF")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and create and not read_only:
            # Rollback journal avoids depending on the host's SQLite WAL patch level.
            self.db.executescript(SCHEMA)
        elif version == 2 and create and not read_only:
            pass  # Only explicit init may upgrade the existing mailbox.
        elif version == 2:
            self.db.close()
            raise ValueError("admission upgrade requires explicit init with existing configuration")
        elif version != 3:
            self.db.close()
            raise ValueError(f"unsupported database schema {version}; no automatic downgrade")

        # External state must be bound explicitly, never silently shared by clones.
        repository = self.db.execute("SELECT value FROM meta WHERE key='repository'").fetchone()
        if repository and repository[0] != str(common):
            self.db.close()
            raise ValueError("mailbox belongs to a different Git common directory")
        if paths and not repository and not create:
            self.db.close()
            raise ValueError(
                "external mailbox needs an explicit repository binding; see MIGRATION.md"
            )

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def settings(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        if row is None:
            raise ValueError("mailbox is not initialized; run init")
        return safety.config(safety.decode(row[0]))

    def initialize(self, value, *, permission=None):
        settings = safety.config(value)
        if permission is not None:
            permission = safety.permission(permission)
        with self.transaction():
            self.db.execute(ADMISSION_SCHEMA)
            self.db.execute("PRAGMA user_version=3")
            old = self.db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
            if old and safety.decode(old[0]) != settings:
                raise ValueError("mailbox already initialized with different configuration")
            self.db.execute(
                "INSERT OR IGNORE INTO meta VALUES('config',?)", (safety.encode(settings),)
            )
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('project',?)", (uuid.uuid4().hex,))
            self.db.execute(
                "INSERT OR IGNORE INTO meta VALUES('repository',?)",
                (str(pv.common_dir(self.root).resolve()),),
            )
            if permission is not None:
                self._audit(
                    "bootstrap_permission", {"permission": permission, "agents": settings["agents"]}
                )
        return settings

    def configure(self, value):
        settings = safety.config(value)
        with self.transaction():
            if set(self.settings()["agents"]) != set(settings["agents"]):
                raise ValueError(
                    "configuration cannot add or remove identities; use admission approval"
                )
            if settings["max_messages"] < self.db.execute("SELECT count FROM stats").fetchone()[0]:
                raise ValueError("capacity cannot be below the retained message count")
            self.db.execute(
                "UPDATE meta SET value=? WHERE key='config'", (safety.encode(settings),)
            )
            self._audit("configure", settings)

    def project_id(self):
        return self.db.execute("SELECT value FROM meta WHERE key='project'").fetchone()[0]

    def register(self, _agent):
        raise ValueError("direct registration is disabled; request-entry and await user approval")

    def _admissions(self):
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admissions'"
        ).fetchone():
            raise ValueError("admission upgrade requires explicit init with existing configuration")

    def request_entry(self, provider, display_name, purpose):
        self._admissions()
        declaration = {}
        for key, value, limit in (
            ("provider", provider, 80),
            ("name", display_name, 120),
            ("purpose", purpose, 2000),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(
                    f"declaration {key} must be nonempty text, at most {limit} characters"
                )
            declaration[key] = value.strip()
        identifier = "request-" + uuid.uuid4().hex
        with self.transaction():
            total, pending = self.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(state='pending'),0) FROM admissions"
            ).fetchone()
            if total >= 10000 or pending >= 128:
                raise ValueError("admission capacity reached (128 pending / 10000 total)")
            self.db.execute(
                "INSERT INTO admissions(id,declaration,created) VALUES(?,?,?)",
                (identifier, safety.encode(declaration), time.time()),
            )
            self._audit("entry_requested", {"id": identifier, **declaration})
        return {"id": identifier, "state": "pending", "declaration": declaration}

    def entry_status(self, identifier):
        self._admissions()
        row = self.db.execute("SELECT * FROM admissions WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("unknown admission request")
        return {**dict(row), "declaration": safety.decode(row["declaration"])}

    def decide_entry(self, identifier, permission, *, approve):
        self._admissions()
        permission = safety.permission(permission)
        with self.transaction():
            request = self.entry_status(identifier)
            if request["state"] != "pending":
                raise ValueError("admission request is already decided")
            agent = None
            if approve:
                settings = self.settings()
                agent = "agent-" + uuid.uuid4().hex
                settings["agents"].append(agent)
                settings = safety.config(settings)
                self.db.execute(
                    "UPDATE meta SET value=? WHERE key='config'", (safety.encode(settings),)
                )
            state = "approved" if approve else "denied"
            self.db.execute(
                "UPDATE admissions SET state=?,agent=?,permission=? WHERE id=?",
                (state, agent, permission.strip(), identifier),
            )
            self._audit(
                "entry_" + state,
                {"id": identifier, "agent": agent, "permission": permission.strip()},
            )
        return self.entry_status(identifier)

    def _audit(self, event, detail):
        self.db.execute(
            "INSERT INTO audit(created,event,detail) VALUES(?,?,?)",
            (time.time(), event, safety.encode(detail)),
        )

    def _agent(self, agent):
        if safety.name(agent) not in self.settings()["agents"]:
            raise ValueError("unknown agent")

    def get(self, identifier):
        row = self.db.execute("SELECT payload FROM messages WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError(f"unknown reply target: {identifier}")
        message = safety.decode(row[0])
        safety.envelope(message, self.settings(), legacy=message.get("legacy") is True)
        if not message.get("legacy") and message.get("project") != self.project_id():
            raise ValueError("message belongs to a different project")
        return message

    def send(self, agent, *, kind="ping", to=None, fields=None, subject=None):
        self._agent(agent)
        settings = self.settings()
        current = pv.provenance(self.root, settings)
        fields = dict(fields or {})
        if set(fields) - {
            "claim",
            "ref",
            "expect",
            "evidence",
            "evidence_kind",
            "task",
            "replies_to",
            "severity",
            "status",
        }:
            raise ValueError("unknown or reserved message field")
        ref = fields.get("ref")
        if isinstance(ref, str) and ref:
            # References identify files in this project. Cite external material as evidence.
            file_ref = re.sub(r":\d+(?::\d+)?$", "", ref)
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", file_ref):
                raise ValueError(
                    "ref must identify this repository; use evidence for external citations"
                )
            candidate = (self.root / file_ref).resolve()
            if not candidate.is_relative_to(self.root.resolve()):
                raise ValueError(
                    "ref points outside this repository; use its own mailbox for review"
                )
        reply = fields.get("replies_to")
        target = self.get(reply) if reply else None
        if target and (agent not in target["to"] or target["from"] == agent):
            raise ValueError("reply must address a message delivered to this agent")
        if kind == "approved":
            if not target or target.get("legacy") or target.get("schema") != 2:
                raise ValueError("approval requires a current-schema proposal")
            if target["type"] not in ("claim", "finding", "handoff"):
                raise ValueError("approval target must be a claim, finding or handoff")
            if subject is None or tuple(subject) != pv.revision(target["provenance"]):
                raise ValueError("approval must explicitly name the proposal commit and snapshot")
            if pv.revision(current) != tuple(subject):
                raise ValueError("stale approval: current worktree does not match reviewed subject")
        recipients = (
            to
            if to is not None
            else ([target["from"]] if target else [a for a in settings["agents"] if a != agent])
        )
        message = {
            "schema": 2,
            "project": self.project_id(),
            "id": f"{agent}-{uuid.uuid4().hex}",
            "ts": datetime.now(UTC).isoformat(),
            "from": agent,
            "to": recipients,
            "type": kind,
            "provenance": current,
            **fields,
        }
        if kind == "approved":
            message["subject"] = {"id": reply, "commit": subject[0], "snapshot_sha256": subject[1]}
        safety.envelope(message, settings)
        with self.transaction():
            self._insert(message)
        return message

    def _insert(self, message):
        settings = self.settings()
        if self.db.execute("SELECT count FROM stats").fetchone()[0] >= settings["max_messages"]:
            raise RuntimeError("mailbox capacity reached; archive traffic or raise max_messages")
        cursor = self.db.execute(
            "INSERT INTO messages(id,sender,kind,created,reply_to,payload) VALUES(?,?,?,?,?,?)",
            (
                message["id"],
                message["from"],
                message["type"],
                time.time(),
                message.get("replies_to"),
                safety.encode(message),
            ),
        )
        self.db.executemany(
            "INSERT INTO deliveries(agent,seq) VALUES(?,?)",
            [(a, cursor.lastrowid) for a in message["to"]],
        )
        return cursor.lastrowid

    def _rows(self, rows):
        settings = self.settings()
        valid = []
        for row in rows:
            try:
                message = safety.decode(row["payload"])
                safety.envelope(message, settings, legacy=message.get("legacy") is True)
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                if self.read_only:
                    raise ValueError(
                        f"invalid stored message at sequence {row['seq']}: {exc}"
                    ) from exc
                self.quarantine(str(row["seq"]), row["payload"].encode(), str(exc))
                continue
            valid.append((row["seq"], message))
        return valid

    def quarantine(self, origin, raw, reason):
        digest = hashlib.sha256(raw).hexdigest()
        count = self.db.execute(
            "INSERT OR IGNORE INTO quarantine VALUES(?,?,?,?)",
            (digest, origin[:1024], reason[:256], time.time()),
        ).rowcount
        if count:
            self._audit(
                "quarantine", {"digest": digest, "origin": origin[:1024], "reason": reason[:256]}
            )

    def inbox(self, agent, *, all_messages=False, limit=safety.MAX_BATCH, after=0):
        self._agent(agent)
        if not 1 <= limit <= safety.MAX_BATCH:
            raise ValueError("invalid batch size")
        sql = """SELECT m.seq,m.payload FROM deliveries d JOIN messages m ON m.seq=d.seq
                 WHERE d.agent=? AND d.seq>?"""
        if not all_messages:
            sql += " AND d.read_at IS NULL"
        rows = self.db.execute(sql + " ORDER BY d.seq LIMIT ?", (agent, after, limit)).fetchall()
        valid = self._rows(rows)
        invalid = {r["seq"] for r in rows} - {seq for seq, _ in valid}
        # Quarantined corruption must not hold the head of the unread queue forever.
        if invalid and not all_messages:
            self.mark_read(agent, invalid)
        return valid

    def mark_read(self, agent, sequences):
        self._agent(agent)
        with self.transaction():
            self.db.executemany(
                "UPDATE deliveries SET read_at=? WHERE agent=? AND seq=?",
                [(time.time(), agent, seq) for seq in sequences],
            )

    def activity(self, *, after=0, limit=safety.MAX_BATCH):
        if not 1 <= limit <= safety.MAX_BATCH:
            raise ValueError("invalid batch size")
        rows = self.db.execute(
            "SELECT seq,payload FROM messages WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)
        ).fetchall()
        self.last_scan = rows[-1]["seq"] if rows else after
        return self._rows(rows)

    def check_approval(self, identifier):
        message = self.get(identifier)
        if message["type"] != "approved" or message.get("legacy") or message.get("schema") != 2:
            raise ValueError("not a current-schema approval")
        target = self.get(message.get("replies_to", ""))
        expected = {
            "id": target["id"],
            "commit": target["provenance"]["commit"],
            "snapshot_sha256": target["provenance"]["snapshot_sha256"],
        }
        if (
            message.get("subject") != expected
            or message["from"] not in target["to"]
            or message["from"] == target["from"]
            or target["type"] not in ("claim", "finding", "handoff")
            or target.get("schema") != 2
            or target.get("legacy")
            or pv.revision(message["provenance"]) != pv.revision(target["provenance"])
        ):
            raise ValueError("approval does not authorize this proposal")
        if pv.revision(pv.provenance(self.root, self.settings())) != pv.revision(
            target["provenance"]
        ):
            raise ValueError("approval is stale for the current worktree")
        return expected

    def subscribe(self, agent, consumer, spec):
        self._agent(agent)
        safety.name(consumer)
        text = safety.encode(spec)
        with self.transaction():
            old = self.db.execute(
                "SELECT spec FROM subscriptions WHERE agent=? AND consumer=?", (agent, consumer)
            ).fetchone()
            if old and old[0] != text:
                raise ValueError(
                    "consumer already has a different subscription; use a new --consumer"
                )
            count = self.db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE agent=?", (agent,)
            ).fetchone()[0]
            if not old and count >= 8:
                raise ValueError("at most eight notification consumers per agent")
            self.db.execute(
                "INSERT OR IGNORE INTO subscriptions(agent,consumer,spec) VALUES(?,?,?)",
                (agent, consumer, text),
            )
            if not old:
                self._audit("subscribe", {"agent": agent, "consumer": consumer, "spec": spec})

    def lease_notifications(self, agent, consumer, *, now=None, lease_seconds=60):
        now = time.time() if now is None else now
        with self.transaction():
            sub = self.db.execute(
                "SELECT * FROM subscriptions WHERE agent=? AND consumer=?", (agent, consumer)
            ).fetchone()
            if sub is None:
                raise ValueError("unknown subscription")
            if sub["active_until"] > now or sub["next_run"] > now:
                return "", []
            spec = safety.decode(sub["spec"])
            rows = self.db.execute(
                """SELECT m.seq,m.payload,m.kind FROM deliveries d
                JOIN messages m ON m.seq=d.seq WHERE d.agent=? AND d.seq>?
                ORDER BY d.seq LIMIT ?""",
                (agent, sub["scanned"], safety.MAX_BATCH),
            ).fetchall()
            for row in rows:
                if row["kind"] in spec["types"]:
                    self.db.execute(
                        "INSERT OR IGNORE INTO notifications(agent,consumer,seq) VALUES(?,?,?)",
                        (agent, consumer, row["seq"]),
                    )
            if rows:
                self.db.execute(
                    "UPDATE subscriptions SET scanned=? WHERE agent=? AND consumer=?",
                    (rows[-1]["seq"], agent, consumer),
                )
            jobs = self.db.execute(
                """SELECT n.seq,m.payload FROM notifications n
                JOIN messages m ON m.seq=n.seq WHERE n.agent=? AND n.consumer=?
                AND n.state IN ('pending','leased') AND n.next_attempt<=?
                AND n.lease_until<=? ORDER BY n.seq LIMIT ?""",
                (agent, consumer, now, now, safety.MAX_BATCH),
            ).fetchall()
            valid = self._rows(jobs)
            for seq in {j["seq"] for j in jobs} - {seq for seq, _ in valid}:
                self.db.execute(
                    "UPDATE notifications SET state='failed',error='invalid record' "
                    "WHERE agent=? AND consumer=? AND seq=?",
                    (agent, consumer, seq),
                )
            token = uuid.uuid4().hex
            if valid:
                self.db.execute(
                    "UPDATE subscriptions SET active_token=?,active_until=?,next_run=? "
                    "WHERE agent=? AND consumer=?",
                    (token, now + lease_seconds, now + spec.get("interval", 0), agent, consumer),
                )
            for seq, _ in valid:
                self.db.execute(
                    """UPDATE notifications SET state='leased',token=?,lease_until=?,
                    attempts=attempts+1 WHERE agent=? AND consumer=? AND seq=?""",
                    (token, now + lease_seconds, agent, consumer, seq),
                )
            if valid:
                self._audit(
                    "notification_attempt",
                    {"agent": agent, "consumer": consumer, "ids": [m["id"] for _, m in valid]},
                )
        return token, valid

    def finish_notifications(self, token, *, error=None, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            jobs = self.db.execute(
                "SELECT * FROM notifications WHERE token=? AND state='leased'", (token,)
            ).fetchall()
            for job in jobs:
                failed = error is not None and job["attempts"] >= 5
                state = "failed" if failed else ("pending" if error is not None else "done")
                self.db.execute(
                    """UPDATE notifications SET state=?,token=NULL,lease_until=0,
                    error=?,next_attempt=? WHERE agent=? AND consumer=? AND seq=? AND token=?""",
                    (
                        state,
                        error[:256] if error else None,
                        now + min(300, 2 ** job["attempts"]) if error else 0,
                        job["agent"],
                        job["consumer"],
                        job["seq"],
                        token,
                    ),
                )
            self.db.execute(
                "UPDATE subscriptions SET active_token=NULL,active_until=0 WHERE active_token=?",
                (token,),
            )
            if jobs and not error:
                self._audit(
                    "notification_completed",
                    {
                        "agent": jobs[0]["agent"],
                        "consumer": jobs[0]["consumer"],
                        "count": len(jobs),
                    },
                )
            if jobs and error:
                self._audit(
                    "notification_failure",
                    {
                        "consumer": jobs[0]["consumer"],
                        "agent": jobs[0]["agent"],
                        "error": error[:256],
                        "count": len(jobs),
                    },
                )

    def retry_failed(self, agent, consumer):
        self._agent(agent)
        safety.name(consumer)
        with self.transaction():
            count = self.db.execute(
                """UPDATE notifications SET state='pending',attempts=0,
                next_attempt=0,lease_until=0,token=NULL
                WHERE agent=? AND consumer=? AND state='failed'""",
                (agent, consumer),
            ).rowcount
            self._audit("retry", {"agent": agent, "consumer": consumer, "count": count})
        return count

    def status(self):
        return {
            "project": self.db.execute("SELECT value FROM meta WHERE key='project'").fetchone()[0],
            "agents": self.settings()["agents"],
            "messages": self.db.execute("SELECT count FROM stats").fetchone()[0],
            "unread": self.db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE read_at IS NULL"
            ).fetchone()[0],
            "notifications": {
                r[0]: r[1]
                for r in self.db.execute("SELECT state,COUNT(*) FROM notifications GROUP BY state")
            },
            "quarantined": self.db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0],
            "active_claims": [
                dict(row)
                for row in self.db.execute(
                    "SELECT * FROM claims WHERE expires>? ORDER BY path LIMIT 100", (time.time(),)
                )
            ],
            "state_directory": str(self.directory),
        }

    def claim(self, agent, path, *, seconds=900):
        self._agent(agent)
        path = safety.relative(path.rstrip("/"))
        if not 1 <= seconds <= 86400:
            raise ValueError("lease duration must be 1..86400 seconds")
        now = time.time()
        with self.transaction():
            rows = self.db.execute("SELECT * FROM claims WHERE expires>?", (now,)).fetchall()
            for row in rows:
                if row["owner"] != agent and (
                    path == row["path"]
                    or path.startswith(row["path"] + "/")
                    or row["path"].startswith(path + "/")
                ):
                    raise ValueError(f"path held by {row['owner']} until {row['expires']}")
            self.db.execute(
                "INSERT INTO claims VALUES(?,?,?) ON CONFLICT(path) "
                "DO UPDATE SET owner=excluded.owner,expires=excluded.expires",
                (path, agent, now + seconds),
            )
            self._audit("claim", {"agent": agent, "path": path, "expires": now + seconds})

    def release(self, agent, path):
        self._agent(agent)
        path = safety.relative(path.rstrip("/"))
        with self.transaction():
            count = self.db.execute(
                "DELETE FROM claims WHERE owner=? AND path=?", (agent, path)
            ).rowcount
            if not count:
                raise ValueError("no matching owned claim")
            self._audit("release", {"agent": agent, "path": path})

    def prune(self, before, archive):
        """Archive before deleting completed batches. Preserve all review/reply evidence."""
        if not archive.is_absolute():
            raise ValueError("archive path must be absolute and outside the repository")
        if archive == self.root or self.root in archive.parents:
            raise ValueError("archive must be outside the repository")
        safety.no_symlinks(archive)
        descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        count = 0
        with os.fdopen(descriptor, "w") as handle, self.transaction():
            rows = self.db.execute(
                """SELECT m.seq,m.payload FROM messages m WHERE created<?
              AND kind='ping'
              AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.seq=m.seq AND d.read_at IS NULL)
              AND NOT EXISTS(SELECT 1 FROM notifications n WHERE n.seq=m.seq AND n.state!='done')
              AND NOT EXISTS(SELECT 1 FROM subscriptions s JOIN deliveries d ON d.agent=s.agent
                             WHERE d.seq=m.seq AND s.scanned<m.seq)
              AND NOT EXISTS(SELECT 1 FROM messages r
                             WHERE r.reply_to=m.id)
              ORDER BY m.seq LIMIT 1000""",
                (before,),
            ).fetchall()
            for row in rows:
                handle.write(row["payload"] + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            # Archive directory entry must also survive before the delete commits.
            parent_fd = os.open(archive.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            for row in rows:
                self.db.execute("DELETE FROM messages WHERE seq=?", (row["seq"],))
            count = len(rows)
            self._audit("prune", {"archive": str(archive), "count": count})
        return count

    def migrate(self, path, agent, *, skip_invalid=False):
        """Explicit one-way import; originals untouched; old approvals cannot authorize."""
        self._agent(agent)
        settings = self.settings()
        imported = 0
        safety.no_symlinks(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle, self.transaction():
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("import must be a regular file")
            number = 0
            while raw := handle.readline(safety.MAX_RECORD + 1):
                number += 1
                try:
                    if len(raw) > safety.MAX_RECORD or not raw.endswith(b"\n"):
                        raise ValueError("oversized or incomplete JSONL record")
                    message = safety.decode(raw)
                    if message.get("from") != agent:
                        raise ValueError("sender does not match imported outbox")
                    message.setdefault("to", [a for a in settings["agents"] if a != agent])
                    message["legacy"] = True
                    prov = message.get("provenance")
                    if (
                        isinstance(prov, dict)
                        and "snapshot_sha256" not in prov
                        and "dirty_sha256" in prov
                    ):
                        prov["snapshot_sha256"] = prov["dirty_sha256"]
                    safety.envelope(message, settings, legacy=True)
                    old = self.db.execute(
                        "SELECT payload FROM messages WHERE id=?", (message["id"],)
                    ).fetchone()
                    if old:
                        if old[0] != safety.encode(message):
                            raise ValueError("conflicting existing message ID")
                        continue
                    self._insert(message)
                    imported += 1
                except (ValueError, TypeError, KeyError, RecursionError) as exc:
                    if not skip_invalid:
                        raise ValueError(
                            f"import line {number}: {exc}; transaction rolled back"
                        ) from exc
                    self.quarantine(f"{path}:{number}", raw, str(exc))
                    if len(raw) > safety.MAX_RECORD and not raw.endswith(b"\n"):
                        # Drain the SAME oversized record in bounded chunks.
                        while chunk := handle.readline(safety.MAX_RECORD + 1):
                            if chunk.endswith(b"\n"):
                                break
            self._audit("import", {"path": str(path), "agent": agent, "count": imported})
        return imported
