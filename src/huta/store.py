"""SQLite event log and release table. The event log is append-only and the source of truth."""

from __future__ import annotations

import fcntl
import hashlib
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

# The database's own version (PRAGMA user_version). Migrations run forward only, each in one
# transaction; a database newer than this code is refused, never misread (DESIGN-v2 §0).
# 0 = a database from before versioning, which is version 1's tables.
SCHEMA_VERSION = 4
MIGRATIONS: dict[int, str] = {1: SCHEMA, 2: """
ALTER TABLE releases ADD COLUMN images TEXT;
ALTER TABLE releases ADD COLUMN schema INTEGER;
CREATE TABLE resources (
  project TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
  schema INTEGER, release_id TEXT, updated REAL NOT NULL,
  PRIMARY KEY (project, target, kind)
);
CREATE TABLE restore_points (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
  release_id TEXT NOT NULL, path TEXT NOT NULL, fingerprint TEXT NOT NULL, schema INTEGER,
  created REAL NOT NULL, state TEXT NOT NULL
);
CREATE TABLE plans (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
  plan TEXT NOT NULL, plan_hash TEXT NOT NULL, state TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL
)
""", 3: """
CREATE TABLE external_deploys (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, target TEXT NOT NULL,
  name TEXT NOT NULL, kind TEXT NOT NULL, release_id TEXT NOT NULL, deployment TEXT NOT NULL,
  previous TEXT, content TEXT, created REAL NOT NULL
)
""", 4: """
CREATE TABLE monitors (
  project TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
  remote_id TEXT NOT NULL, spec TEXT, state TEXT NOT NULL, updated REAL NOT NULL,
  PRIMARY KEY (project, target, kind, name)
);
CREATE TABLE watches (
  release_id TEXT NOT NULL, since REAL NOT NULL, project TEXT NOT NULL, target TEXT NOT NULL,
  opened REAL NOT NULL, until REAL NOT NULL, state TEXT NOT NULL, reasons TEXT,
  coverage TEXT, signals TEXT, last_tick REAL, policy TEXT, notified TEXT,
  button_hash TEXT, button_target TEXT, button_expires REAL, chat INTEGER, message_id INTEGER,
  action TEXT, acked REAL, acked_by TEXT,
  PRIMARY KEY (release_id, since)
)
"""}
# What each version added: 1, the v1 tables; 2, release image maps and schemas, resources
# (a target's data volume or database, with its schema), restore points pinned per release,
# and plans that are not releases (infra: edge, gateways, Postgres; restore); 3, deployments
# published to Cloudflare by releases (DESIGN-v2 §5); 4, monitoring: the remote checks, projects
# and sites made for targets, and watch windows (docs/PLAN-monitoring.md).


class StoreError(Exception):
    pass


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
            self._migrate()
        finally:
            os.umask(old_umask)
        for suffix in ("", "-wal", "-shm"):
            p = self.dir / f"huta.db{suffix}"
            if p.exists():
                os.chmod(p, 0o600)

    def version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        seen = self.version()
        if seen > SCHEMA_VERSION:
            self.db.close()
            raise StoreError(
                f"huta.db is version {seen}, and this huta knows up to "
                f"{SCHEMA_VERSION}: it was written by a newer huta. Install that huta again, or "
                "restore the database copy taken before the upgrade.")
        for n in range(seen + 1, SCHEMA_VERSION + 1):
            # One writer at a time; another huta may have migrated while this one waited.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.version() < n:
                    for stmt in _statements(MIGRATIONS[n]):
                        self.db.execute(stmt)
                    self.db.execute(f"PRAGMA user_version = {n}")
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

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

    def make_live(self, rid: str, slot: str, retire: str | None, msg: str,
                  retire_as: str = RETIRED, retire_msg: str = "") -> None:
        """Live pointer, new state and the predecessor's new state change in one transaction.
        retire_as: the predecessor's state, RETIRED unless it failed (ROLLED_BACK: a revert
        finished by reconcile, so it never becomes huta rollback's target)."""
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
                                (retire_as, now, retire))
                if retire_as != RETIRED:
                    self.db.execute(
                        "INSERT INTO events (ts, release_id, project, target, kind, msg)"
                        " VALUES (?,?,?,?,?,?)", (now, retire, r["project"], r["target"],
                                                 retire_as, redact(retire_msg or retire_as)))
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
        rid = self.new_id()
        now = time.time()
        self.db.execute(
            "INSERT INTO releases (id, project, target, sha, image_id, image_tag, app_toml, plan,"
            " plan_hash, built_by, state, slot, created, updated, images, schema)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, f["project"], f["target"], f["sha"], f["image_id"], f["image_tag"],
             f["app_toml"], json.dumps(f["plan"]), f["plan_hash"], f["built_by"],
             PLANNED, None, now, now,
             json.dumps(f["images"]) if f.get("images") else None, f.get("schema")))
        self.event("planned", f"{f['project']}/{f['target']} {f['sha'][:12]}", rid,
                   f["project"], f["target"])
        return rid

    def new_id(self) -> str:
        """8 hex, unused by any release or plan (both are approved by id)."""
        while True:
            i = secrets.token_hex(4)
            if not self.db.execute("SELECT 1 FROM releases WHERE id=? UNION ALL"
                                   " SELECT 1 FROM plans WHERE id=?", (i, i)).fetchone():
                return i

    def images(self, r) -> dict:
        """A release's image map (name → id). Rows from before maps: the only image."""
        if r["images"]:
            return json.loads(r["images"])
        names = list(json.loads(r["app_toml"]).get("images", {})) or ["web"]
        return {names[0]: r["image_id"]}

    # ---------------------------------------------------------------- plans (not releases)

    def new_plan(self, project: str, target: str, kind: str, plan: dict) -> str:
        pid = self.new_id()
        now = time.time()
        blob = json.dumps(plan, sort_keys=True)
        self.db.execute("INSERT INTO plans (id, project, target, kind, plan, plan_hash, state,"
                        " created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                        (pid, project, target, kind, blob,
                         hashlib.sha256(blob.encode()).hexdigest(), PLANNED, now, now))
        self.event("planned", f"{kind} plan for {project}/{target}", pid, project, target)
        return pid

    def plan_row(self, pid: str):
        return self.db.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()

    def plan_transition(self, pid: str, frm: str, to: str, msg: str = "") -> None:
        cur = self.db.execute("UPDATE plans SET state=?, updated=? WHERE id=? AND state=?",
                              (to, time.time(), pid, frm))
        if cur.rowcount != 1:
            now = self.plan_row(pid)
            raise RuntimeError(f"plan {pid} is {now['state'] if now else 'unknown'}, not {frm}")
        r = self.plan_row(pid)
        self.event(to, msg or to, pid, r["project"], r["target"])

    # ---------------------------------------------------------------- monitors

    def monitors(self, project: str, target: str | None = None) -> list:
        if target is None:
            return self.db.execute("SELECT * FROM monitors WHERE project=? ORDER BY target, kind,"
                                   " name", (project,)).fetchall()
        return self.db.execute("SELECT * FROM monitors WHERE project=? AND target=?"
                               " ORDER BY kind, name", (project, target)).fetchall()

    def monitor(self, project: str, target: str, kind: str, name: str):
        return self.db.execute("SELECT * FROM monitors WHERE project=? AND target=? AND kind=?"
                               " AND name=?", (project, target, kind, name)).fetchone()

    def set_monitor(self, project: str, target: str, kind: str, name: str, remote_id: str,
                    spec: dict | None, state: str = "active") -> None:
        self.db.execute(
            "INSERT INTO monitors (project, target, kind, name, remote_id, spec, state, updated)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project, target, kind, name) DO UPDATE SET"
            " remote_id=excluded.remote_id, spec=excluded.spec, state=excluded.state,"
            " updated=excluded.updated",
            (project, target, kind, name, remote_id,
             json.dumps(spec, sort_keys=True) if spec is not None else None, state, time.time()))

    # ---------------------------------------------------------------- resources

    def resource(self, project: str, target: str, kind: str):
        return self.db.execute("SELECT * FROM resources WHERE project=? AND target=? AND kind=?",
                               (project, target, kind)).fetchone()

    def set_resource_schema(self, project: str, target: str, kind: str, schema, rid) -> None:
        self.db.execute(
            "INSERT INTO resources (project, target, kind, schema, release_id, updated)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(project, target, kind) DO UPDATE SET"
            " schema=excluded.schema, release_id=excluded.release_id, updated=excluded.updated",
            (project, target, kind, schema, rid, time.time()))

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


def _statements(script: str) -> list[str]:
    """A migration's statements, run one by one inside the caller's transaction
    (executescript would commit it)."""
    return [s.strip() for s in script.split(";") if s.strip()]


def busy(state_dir) -> list[str]:
    """What an install must not interrupt: a release or recovery in progress. Used by
    install.sh before it replaces the code (DESIGN-v2 §0). Reads only."""
    d = Path(state_dir)
    out = []
    phases = d / "phases"
    if phases.is_dir():
        out += [f"phase file {p.name}: a release or recovery was interrupted (run huta reconcile)"
                for p in sorted(phases.glob("*.json"))]
    db = d / "huta.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for r in con.execute("SELECT id, project, target, state FROM releases"
                                 " WHERE state IN (?, ?)", (APPROVED, RUNNING)):
                out.append(f"release {r[0]} of {r[1]}/{r[2]} is {r[3]}")
            if con.execute("SELECT name FROM sqlite_master WHERE name='plans'").fetchone():
                for r in con.execute("SELECT id, project, target, kind FROM plans WHERE state=?",
                                     (APPROVED,)):
                    out.append(f"{r[3]} plan {r[0]} of {r[1]}/{r[2]} is being applied")
        finally:
            con.close()
    locks = d / "locks"
    if locks.is_dir():
        for p in sorted(locks.glob("*.lock")):
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                out.append(f"{p.stem.replace('--', '/')} is locked: an action is running")
            finally:
                os.close(fd)
    return out
