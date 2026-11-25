"""SQLite event log and release table. The event log is append-only and the source of truth."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .cfg import assert_root_dir, redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, target TEXT NOT NULL, sha TEXT NOT NULL,
  image_id TEXT NOT NULL, image_tag TEXT NOT NULL, app_toml TEXT NOT NULL,
  plan TEXT NOT NULL, plan_hash TEXT NOT NULL, built_by TEXT NOT NULL,
  state TEXT NOT NULL, slot TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, release_id TEXT,
  project TEXT, target TEXT, kind TEXT NOT NULL, msg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, transport TEXT NOT NULL, box TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  release_id TEXT PRIMARY KEY, approve_hash TEXT NOT NULL, reject_hash TEXT NOT NULL,
  expires REAL NOT NULL, chat INTEGER, message_id INTEGER
);
CREATE TABLE IF NOT EXISTS tg_dm (
  release_id TEXT PRIMARY KEY, chat INTEGER NOT NULL, message_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_messages (
  release_id TEXT PRIMARY KEY, chat INTEGER NOT NULL, message_id INTEGER NOT NULL, text TEXT
);
CREATE TABLE IF NOT EXISTS live (
  project TEXT NOT NULL, target TEXT NOT NULL, release_id TEXT NOT NULL, slot TEXT NOT NULL,
  since REAL NOT NULL, PRIMARY KEY (project, target)
);
"""

# Release states. Terminal outcomes are the three of DESIGN.md §3 plus rejected/superseded.
PLANNED, APPROVED, RUNNING, LIVE = "planned", "approved", "running", "live"
NOTHING_ACTIVATED, ROLLED_BACK, NEEDS_HUMAN = "nothing-activated", "rolled-back", "needs-human"
REJECTED, SUPERSEDED, RETIRED = "rejected", "superseded", "retired"


class Store:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert_root_dir(self.dir)
        os.chmod(self.dir, 0o700)
        (self.dir / "locks").mkdir(mode=0o700, exist_ok=True)
        old_umask = os.umask(0o077)   # the db, its -wal and -shm are root-only
        try:
            self.db = sqlite3.connect(self.dir / "huta.db", isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
        finally:
            os.umask(old_umask)
        for suffix in ("", "-wal", "-shm"):
            p = self.dir / f"huta.db{suffix}"
            if p.exists():
                os.chmod(p, 0o600)

    def event(self, kind: str, msg: str, release_id=None, project=None, target=None):
        self.db.execute(
            "INSERT INTO events (ts, release_id, project, target, kind, msg) VALUES (?,?,?,?,?,?)",
            (time.time(), release_id, project, target, kind, redact(msg)[:2000]))

    def first_time(self, request_id: str, transport: str, box: str) -> bool:
        """Record a request id; False if it was seen before (a retry must not plan twice)."""
        cur = self.db.execute("INSERT OR IGNORE INTO requests (id, ts, transport, box)"
                              " VALUES (?,?,?,?)", (request_id, time.time(), transport, box))
        return cur.rowcount == 1

    def transition(self, rid: str, frm: str, to: str, msg: str = "") -> None:
        """Move a release from one state to another only if it is still in `frm`; otherwise
        refuse. Makes approve/reject/expire races impossible (codex #14)."""
        cur = self.db.execute("UPDATE releases SET state=?, updated=? WHERE id=? AND state=?",
                              (to, time.time(), rid, frm))
        if cur.rowcount != 1:
            now = self.release(rid)["state"]
            raise RuntimeError(f"release {rid} is {now}, not {frm}")
        r = self.release(rid)
        self.event(to, msg or to, rid, r["project"], r["target"])

    def make_live(self, rid: str, slot: str, retire: str | None, msg: str) -> None:
        """Live pointer, new state and the retired predecessor change in one transaction."""
        r = self.release(rid)
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO live (project, target, release_id, slot, since) VALUES (?,?,?,?,?)"
                " ON CONFLICT(project, target) DO UPDATE SET release_id=excluded.release_id,"
                " slot=excluded.slot, since=excluded.since",
                (r["project"], r["target"], rid, slot, now))
            self.db.execute("UPDATE releases SET state=?, slot=?, updated=? WHERE id=?",
                            (LIVE, slot, now, rid))
            if retire and retire != rid:
                self.db.execute("UPDATE releases SET state=?, updated=? WHERE id=?",
                                (RETIRED, now, retire))
            self.db.execute(
                "INSERT INTO events (ts, release_id, project, target, kind, msg) VALUES (?,?,?,?,?,?)",
                (now, rid, r["project"], r["target"], LIVE, redact(msg)))

    def revert_live(self, rid: str, back_to: str | None, slot: str | None, msg: str) -> None:
        """A release that went live and failed its checks: the live pointer goes back to the
        release that served before (or nowhere, for an adopted site), in one transaction."""
        r = self.release(rid)
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if back_to:
                self.db.execute("UPDATE live SET release_id=?, slot=?, since=? WHERE project=?"
                                " AND target=?", (back_to, slot, now, r["project"], r["target"]))
                self.db.execute("UPDATE releases SET state=?, slot=?, updated=? WHERE id=?",
                                (LIVE, slot, now, back_to))
            else:
                self.db.execute("DELETE FROM live WHERE project=? AND target=?",
                                (r["project"], r["target"]))
            self.db.execute("UPDATE releases SET state=?, updated=? WHERE id=?",
                            (ROLLED_BACK, now, rid))
            self.db.execute(
                "INSERT INTO events (ts, release_id, project, target, kind, msg) VALUES (?,?,?,?,?,?)",
                (now, rid, r["project"], r["target"], ROLLED_BACK, redact(msg)[:2000]))

    def target_events(self, project: str, target: str, kinds: tuple, limit=20):
        q = ",".join("?" * len(kinds))
        return self.db.execute(
            f"SELECT * FROM events WHERE project=? AND target=? AND kind IN ({q})"
            " ORDER BY id DESC LIMIT ?", (project, target, *kinds, limit)).fetchall()

    def in_state(self, project: str, target: str, *states: str):
        q = ",".join("?" * len(states))
        return self.db.execute(
            f"SELECT * FROM releases WHERE project=? AND target=? AND state IN ({q})"
            " ORDER BY created", (project, target, *states)).fetchall()

    def new_release(self, **f) -> str:
        rid = secrets.token_hex(4)
        now = time.time()
        self.db.execute(
            "INSERT INTO releases (id, project, target, sha, image_id, image_tag, app_toml, plan,"
            " plan_hash, built_by, state, slot, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, f["project"], f["target"], f["sha"], f["image_id"], f["image_tag"],
             f["app_toml"], json.dumps(f["plan"]), f["plan_hash"], f["built_by"],
             PLANNED, None, now, now))
        self.event("planned", f"{f['project']}/{f['target']} {f['sha'][:12]}", rid,
                   f["project"], f["target"])
        return rid

    def release(self, rid: str):
        r = self.db.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()
        if r is None:
            raise KeyError(f"no release {rid}")
        return r

    def set_state(self, rid: str, state: str, slot=None, msg=""):
        r = self.release(rid)
        self.db.execute("UPDATE releases SET state=?, slot=COALESCE(?, slot), updated=? WHERE id=?",
                        (state, slot, time.time(), rid))
        self.event(state, msg or state, rid, r["project"], r["target"])

    def live(self, project: str, target: str):
        return self.db.execute("SELECT * FROM live WHERE project=? AND target=?",
                               (project, target)).fetchone()

    def set_live(self, project: str, target: str, rid: str, slot: str):
        self.db.execute(
            "INSERT INTO live (project, target, release_id, slot, since) VALUES (?,?,?,?,?)"
            " ON CONFLICT(project, target) DO UPDATE SET release_id=excluded.release_id,"
            " slot=excluded.slot, since=excluded.since",
            (project, target, rid, slot, time.time()))

    def releases(self, project=None, limit=20):
        q, a = "SELECT * FROM releases", ()
        if project:
            q, a = q + " WHERE project=?", (project,)
        return self.db.execute(q + " ORDER BY created DESC LIMIT ?", a + (limit,)).fetchall()

    def events(self, rid=None, limit=50):
        q, a = "SELECT * FROM events", ()
        if rid:
            q, a = q + " WHERE release_id=?", (rid,)
        return self.db.execute(q + " ORDER BY id DESC LIMIT ?", a + (limit,)).fetchall()

    def acquire(self, project: str, target: str, wait: float = 0) -> int:
        """Take the target lock; returns its fd (os.close releases it)."""
        p = self.dir / "locks" / f"{project}--{target}.lock"
        fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise RuntimeError(f"{project}/{target} is busy (another action holds the lock)")
                time.sleep(1)

    @contextmanager
    def lock(self, project: str, target: str, wait: float = 0):
        """One action per target at a time. wait: seconds to wait for it (0: fail at once)."""
        fd = self.acquire(project, target, wait)
        try:
            yield
        finally:
            os.close(fd)
