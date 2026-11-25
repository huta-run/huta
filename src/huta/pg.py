"""Postgres per target (DESIGN-v2 §4): one database server per target, run by huta in its own
compose project, outside the release lifecycle (a release never recreates it).

- Its data lives on its own loop-mounted volume (the target's name plus "-pg"), PGDATA in a
  subdirectory; the image is pinned by digest in huta.toml ([postgres.images]).
- Three internal networks, no route out and not in the egress policy: slot a's components join
  ...pga, slot b's ...pgb, the migration ...pgm. Postgres is on all three as "postgres".
- Roles: huta_owner (huta's: setup, dumps, restores), migrate (owns the schema; only the
  [migrate] step gets it) and app (data only, by default privileges migrate grants). Passwords are
  generated here and kept as root-only secrets of the project, so logs redact them.
- Backups are pg_dump -Fc files rotated like data copies (current/, previous/, last.json) under
  the "-pg" name; a migration pins one as its restore point.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from . import runtime as R
from .cfg import Platform, Project, Target

DB, OWNER = "app", "huta_owner"
ROLES = ("owner", "migrate", "app")
PGDATA = "/var/lib/postgresql/data"
DUMP = "app.dump"


class PgError(Exception):
    pass


# ------------------------------------------------------------------ names

def compose_name(t: Target) -> str:
    return f"huta_{t.project}_{t.name}_pg"


def network(t: Target, which: str) -> str:
    """which: "a", "b" (a slot's) or "m" (the migration's)."""
    return f"{compose_name(t)}{which}"


def bridge(t: Target, which: str) -> str:
    return "dp-" + hashlib.sha1(f"{t.project}/{t.name}/pg/{which}".encode()).hexdigest()[:12]


def volume_target(t: Target) -> Target:
    """The target as data.py sees the database's volume and its backups: "<name>-pg"."""
    return dataclasses.replace(t, name=f"{t.name}-pg", data=t.postgres)


def secret_name(t: Target, role: str) -> str:
    return f"HUTA_PG_{t.name.upper().replace('-', '_')}_{role.upper()}"


def wants(app: dict) -> bool:
    return (app.get("state") or {}).get("kind") == "postgres"


# ------------------------------------------------------------------ secrets

PASSWORD_RE = re.compile(r"[A-Za-z0-9_.~-]{16,128}")


def password(platform: Platform, project: Project, t: Target, role: str, create: bool = False) -> str:
    """A role's password. huta makes them (hex); one set by hand must stay plain, because it goes
    into SQL and into a URL (anything else is refused, never quoted around)."""
    name = secret_name(t, role)
    try:
        value = R.secret_value(platform, project.name, name)
    except Exception:  # noqa: BLE001 — missing: make it (root only, like every secret)
        if not create:
            raise
    else:
        if not PASSWORD_RE.fullmatch(value):
            raise PgError(f"secret {name} must be 16..128 letters, digits or _.~- "
                          "(huta generates one if it is missing)")
        return value
    value = secrets.token_hex(24)
    R.secret_set(platform, project.name, name, value)
    return value


def database_url(platform: Platform, project: Project, t: Target, role: str,
                 with_secrets: bool = True) -> str:
    import urllib.parse
    pw = password(platform, project, t, role) if with_secrets else R.SECRET_PLACEHOLDER
    user = OWNER if role == "owner" else role
    if with_secrets:
        pw = urllib.parse.quote(pw, safe="")
    return f"postgresql://{user}:{pw}@postgres:5432/{DB}"


# ------------------------------------------------------------------ compose

def compose_doc(platform: Platform, project: Project, t: Target, with_secrets: bool = True) -> dict:
    from . import data as D
    _img, where = D.data_paths(platform, volume_target(t))
    image = platform.postgres_images[t.postgres["version"]]
    h = R.hardening(project, t, t.postgres.get("limits") or {"memory": "1g", "cpus": 2,
                                                             "pids": 512, "tmp": "64m"})
    pw = password(platform, project, t, "owner") if with_secrets else R.SECRET_PLACEHOLDER
    nets = {}
    for which in ("a", "b", "m"):
        nets[f"pg{which}"] = {
            "name": network(t, which), "internal": True,
            "driver_opts": {"com.docker.network.bridge.name": bridge(t, which),
                            "com.docker.network.bridge.enable_icc": "true"}}
    return {
        "name": compose_name(t),
        "services": {"postgres": {
            "image": image, "pull_policy": "never",
            **h,
            "tmpfs": [h["tmpfs"][0], "/var/run/postgresql:size=1m,mode=1777"],
            "shm_size": "256m",
            "restart": "unless-stopped", "stop_grace_period": "60s",
            "environment": R.compose_escape({
                "POSTGRES_USER": OWNER, "POSTGRES_PASSWORD": pw, "POSTGRES_DB": DB,
                # One data directory per version: a major upgrade starts a new cluster next to
                # the old one, which stays until the operator removes it.
                "PGDATA": f"{PGDATA}/pgdata-{t.postgres['version']}"}),
            "labels": {"run.huta.project": project.name, "run.huta.target": t.name,
                       "run.huta.pg": "1"},
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            "volumes": [{"type": "bind", "source": str(where), "target": PGDATA,
                         "bind": {"create_host_path": False}}],
            "networks": {n: {"aliases": ["postgres"]} for n in nets},
        }},
        "networks": nets,
    }


def _compose_file(platform: Platform, t: Target) -> Path:
    d = platform.state_dir / "compose" / t.project / f"{t.name}-pg"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d / "compose.yaml"


def _sh(cmd, check=True, input_=None, timeout=600, stdout=None) -> str:
    from .recipe import sh
    if stdout is None:
        return sh(cmd, check=check, input_=input_, timeout=timeout)
    r = subprocess.run(cmd, stdout=stdout, stderr=subprocess.PIPE, timeout=timeout, cwd="/")
    if check and r.returncode != 0:
        raise PgError(f"{cmd[0]} {cmd[1]}: {r.stderr.decode(errors='replace').strip()[-300:]}")
    return ""


def container(t: Target) -> str | None:
    out = _sh(["docker", "ps", "-aq", "--no-trunc",
               "--filter", f"label=com.docker.compose.project={compose_name(t)}",
               "--filter", "label=run.huta.pg=1"], check=False).split()
    return out[0] if out else None


def running(t: Target) -> bool:
    cid = container(t)
    if not cid:
        return False
    return _sh(["docker", "inspect", "--format", "{{.State.Running}}", cid],
               check=False).strip() == "true"


def _psql(t: Target, sql: str, timeout: float = 120, db: str = DB) -> str:
    """SQL on stdin (passwords never on a command line), as the owner over the socket."""
    cid = container(t)
    return _sh(["docker", "exec", "-i", cid, "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", OWNER,
                "-d", db], input_=sql, timeout=timeout)


def _wait_ready(t: Target, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cid = container(t)
        if cid and subprocess.run(["docker", "exec", cid, "pg_isready", "-U", OWNER, "-d", DB],
                                  capture_output=True, timeout=30, cwd="/").returncode == 0:
            return
        time.sleep(1)
    raise PgError(f"postgres of {t.project}/{t.name} did not get ready in {int(timeout)} s")


def ensure(platform: Platform, st, project: Project, t: Target, log) -> str:
    """Volume, image, container, roles: create or repair, never recreate a running server
    unless its definition changed. Idempotent. Hold the target lock."""
    from . import data as D
    from .recipe import edge
    if not t.postgres:
        raise PgError(f"{project.name}/{t.name} has no postgres in the host registry")
    said = [D.ensure(platform, project, volume_target(t))]
    for role in ROLES:
        password(platform, project, t, role, create=True)
    image = platform.postgres_images[t.postgres["version"]]
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                      timeout=60, cwd="/").returncode != 0:
        _sh(["docker", "pull", "-q", image], timeout=1800)
        said.append(f"pulled {image.split('@')[0]}")
    f = _compose_file(platform, t)
    doc = json.dumps(compose_doc(platform, project, t), indent=2) + "\n"
    changed = not f.exists() or f.read_text() != doc
    if changed and f.exists():
        # Never silently: a new image, version or limits recreate the server (a new version
        # starts an empty cluster). That is an upgrade plan of its own.
        raise PgError(f"the postgres definition of {project.name}/{t.name} changed (image, "
                      f"version or limits): plan it with huta postgres {project.name} {t.name} "
                      "upgrade")
    if changed:
        edge._atomic_write(f, doc, 0o600)
    if changed or not running(t):
        _sh(["docker", "compose", "-p", compose_name(t), "-f", str(f), "up", "-d", "--pull",
             "never"], timeout=600)
        said.append("postgres started")
    _wait_ready(t)
    _roles(platform, project, t)
    st.event("postgres", ", ".join(said), None, project.name, t.name)
    return ", ".join(said)


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _roles(platform: Platform, project: Project, t: Target) -> None:
    """migrate owns the schema; app may only read and write rows (DESIGN-v2 §4)."""
    m = password(platform, project, t, "migrate")
    a = password(platform, project, t, "app")
    _psql(t, f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'migrate') THEN
    CREATE ROLE migrate LOGIN PASSWORD {_q(m)};
  ELSE ALTER ROLE migrate LOGIN PASSWORD {_q(m)}; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN
    CREATE ROLE app LOGIN PASSWORD {_q(a)};
  ELSE ALTER ROLE app LOGIN PASSWORD {_q(a)}; END IF;
END $$;
GRANT CONNECT ON DATABASE {DB} TO migrate, app;
""")
    _psql(t, _SCHEMA_GRANTS)


_SCHEMA_GRANTS = """
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO migrate;
GRANT USAGE ON SCHEMA public TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE migrate IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE migrate IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app;
"""


# ------------------------------------------------------------------ dumps (backups, restore points)

def _dump_to(t: Target, dest: Path, timeout: float = 3 * 3600) -> None:
    cid = container(t)
    if not cid:
        raise PgError("postgres is not there")
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        _sh(["docker", "exec", cid, "pg_dump", "-U", OWNER, "-Fc", "-d", DB], stdout=out,
            timeout=timeout)
        out.flush()
        os.fsync(out.fileno())


def copy(platform: Platform, t: Target, log, quiet: bool = False) -> Path:
    """A new dump generation, published only when complete (current/ previous/ as data.py)."""
    from . import data as D
    vt = volume_target(t)
    base = D.backup_dir(platform, vt)
    for d in (platform.backup["dir"], base.parent, base):
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    D.recover_publish(base)
    nxt = base / "next"
    if nxt.exists():
        D._rmtree(nxt)
    nxt.mkdir(mode=0o700)
    t0 = time.monotonic()
    _dump_to(t, nxt / DUMP)
    return D._publish(base, nxt, vt, t0, 0.0, log)


def backup(platform: Platform, t: Target, st, log, do_ship: bool = True) -> str:
    """Scheduled backup: a dump, shipped like a data copy."""
    from . import data as D
    if not t.postgres or t.postgres.get("backup") == "off":
        return "backups are off for this database"
    vt = volume_target(t)
    copy(platform, t, log)
    stamp = D.backup_dir(platform, vt) / "last.json"
    rec = {"ts": time.time(), "shipped": "not shipped"}
    try:
        if do_ship:
            rec["shipped"] = D.ship(platform, vt, log)
            rec["shipped_ts"] = time.time() if rec["shipped"] == "ok" else None
    except D.DataError as e:
        rec["shipped"] = "failed"
        raise PgError(f"dumped locally, but {e}") from None
    finally:
        old = D.last_backup(platform, vt) or {}
        if rec.get("shipped_ts") is None and old.get("shipped_ts"):
            rec["shipped_ts"] = old["shipped_ts"]
        D._write(stamp, json.dumps(rec) + "\n")
        os.chmod(stamp, 0o600)
        st.event("backup", f"database dump ok, ship {rec['shipped']}", None, t.project, t.name)
    return f"database dump ok, ship {rec['shipped']}"


def _restore_file(platform: Platform, project: Project, t: Target, dump: Path, log) -> None:
    """Put a dump back exactly, and atomically: the dump is restored into a new database; only
    when that has fully worked is it swapped in for the old one, both renames in one
    transaction. So objects a failed migration added are gone (pg_restore --clean only drops
    what the dump has), and a restore that fails leaves the database as it was. Nothing may be
    connected but huta."""
    cid = container(t)
    if not cid or not dump.is_file() or dump.is_symlink():
        raise PgError("postgres is not there, or the dump is missing")
    new, old = f"{DB}_restore", f"{DB}_old"
    _psql(t, f"DROP DATABASE IF EXISTS {new};", db="postgres")
    _psql(t, f"DROP DATABASE IF EXISTS {old};", db="postgres")
    _psql(t, f"CREATE DATABASE {new} OWNER {OWNER};", db="postgres")
    try:
        _psql(t, _SCHEMA_GRANTS, db=new)
        with open(dump, "rb") as f:
            r = subprocess.run(["docker", "exec", "-i", cid, "pg_restore", "-U", OWNER, "-d", new,
                                "--exit-on-error", "--single-transaction"],
                               stdin=f, capture_output=True, timeout=6 * 3600, cwd="/")
        if r.returncode != 0:
            raise PgError(f"pg_restore: {r.stderr.decode(errors='replace').strip()[-300:]}")
    except Exception:
        _psql(t, f"DROP DATABASE IF EXISTS {new};", db="postgres")
        raise
    _psql(t, f"""
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = {_q(DB)};
BEGIN;
ALTER DATABASE {DB} RENAME TO {old};
ALTER DATABASE {new} RENAME TO {DB};
COMMIT;
""", db="postgres")
    _psql(t, f"DROP DATABASE IF EXISTS {old};", db="postgres")
    _roles(platform, project, t)
    log(f"database restored from {dump.parent.name}/{dump.name}")


def restore_copy(platform: Platform, project: Project, t: Target, log) -> None:
    from . import data as D
    base = D.backup_dir(platform, volume_target(t))
    D.recover_publish(base)
    _restore_file(platform, project, t, base / "current" / DUMP, log)


def pin_copy(platform: Platform, t: Target, rid: str) -> tuple[Path, str]:
    from . import data as D
    return D.pin_copy(platform, volume_target(t), rid)


def restore_pin(platform: Platform, project: Project, t: Target, path: Path, fp: str, log) -> None:
    from . import data as D
    vt = volume_target(t)
    if path.parent != D.pins_dir(platform, vt) or not D.PIN_RE.fullmatch(path.name):
        raise PgError(f"{path} is not one of this database's restore points")
    if D.fingerprint(path) != fp:
        raise PgError(f"restore point {path.name} changed since it was pinned; refusing it")
    _restore_file(platform, project, t, path / DUMP, log)


# ------------------------------------------------------------------ upgrades (infra plans)

def pending(platform: Platform, project: Project, t: Target) -> dict | None:
    """What an upgrade would change, or None: the definition on disk against the registry's."""
    f = _compose_file(platform, t)
    if not f.exists():
        return None
    old = json.loads(f.read_text())["services"]["postgres"]
    new = compose_doc(platform, project, t)["services"]["postgres"]
    if old == new:
        return None
    pgdata = lambda s: next((v for k, v in s["environment"].items() if k == "PGDATA"), "")  # noqa: E731
    return {"from_image": old["image"], "to_image": new["image"],
            "new_cluster": pgdata(old) != pgdata(new),
            "from_limits": [old.get(k) for k in ("mem_limit", "cpus", "pids_limit")],
            "to_limits": [new.get(k) for k in ("mem_limit", "cpus", "pids_limit")]}


def _pgdata(doc: dict) -> str:
    return doc["services"]["postgres"]["environment"].get("PGDATA", "")


def upgrade(platform: Platform, st, project: Project, t: Target, pid: str, log) -> None:
    """An approved upgrade plan. Hold the target lock. The app stops (nothing writes), the
    database is dumped and the dump pinned, the new definition starts (restored from the dump
    when it is a new cluster), the app starts again. Any failure puts the old definition back
    and starts the app on it. A phase record lets reconcile do the same after a crash."""
    from . import data as D
    from . import recipe as RC
    f = _compose_file(platform, t)
    old_doc = f.read_text()
    change = pending(platform, project, t)
    if change is None:
        raise PgError("nothing to upgrade")
    if RC.jobs_running(t):
        raise PgError("a job of this target is running; upgrade when it has finished")
    new_pgdata = _pgdata(compose_doc(platform, project, t)).rsplit("/", 1)[-1] \
        if change["new_cluster"] else None
    from . import data as D
    _img, where = D.data_paths(platform, volume_target(t))
    if new_pgdata and (where / new_pgdata).exists():
        raise PgError(f"{where / new_pgdata} already exists (an earlier attempt?): remove it first")
    RC._phase_set(platform, t, v=RC.PHASE_VERSION, stage="pg-upgrade", rid=pid, idle=None,
                  old_doc=old_doc, step="stopping", new_pgdata=new_pgdata)
    for slot in RC.SLOTS:
        RC._stop_members(t, slot)
        if any(c["running"] for c in RC.slot_members(t).get(slot, {}).values()):
            raise PgError(f"slot {slot} would not stop: not upgrading")
    dump_dir = copy(platform, t, log, quiet=True)
    path, fp = D.pin_copy(platform, volume_target(t), pid)
    st.db.execute("INSERT INTO restore_points (id, project, target, kind, release_id, path,"
                  " fingerprint, schema, created, state) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (pid, project.name, t.name, "postgres", pid, str(path), fp,
                   RC.schema_now(st, t, "postgres"), time.time(), "kept"))
    RC._phase_set(platform, t, v=RC.PHASE_VERSION, stage="pg-upgrade", rid=pid, idle=None,
                  old_doc=old_doc, step="switching", dump=str(dump_dir / DUMP),
                  new_pgdata=new_pgdata)
    try:
        new_doc = json.dumps(compose_doc(platform, project, t), indent=2) + "\n"
        from .recipe import edge
        edge._atomic_write(f, new_doc, 0o600)
        image = platform.postgres_images[t.postgres["version"]]
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                          timeout=60, cwd="/").returncode != 0:
            _sh(["docker", "pull", "-q", image], timeout=1800)
        _sh(["docker", "compose", "-p", compose_name(t), "-f", str(f), "up", "-d", "--pull",
             "never", "--force-recreate"], timeout=600)
        _wait_ready(t, 300)
        _roles(platform, project, t)
        if change["new_cluster"]:
            _restore_file(platform, project, t, dump_dir / DUMP, log)
    except Exception as e:
        log(f"upgrade failed ({type(e).__name__}: {e}); putting the old server back")
        revert_upgrade(platform, project, t, old_doc, log, new_pgdata)
        RC._phase_clear(platform, t)
        raise PgError(f"upgrade failed and was undone: {e}") from None
    RC._phase_clear(platform, t)
    st.event("postgres", f"upgraded: {change['from_image']} → {change['to_image']}", pid,
             project.name, t.name)
    log("postgres upgraded; the app starts again")


def revert_upgrade(platform: Platform, project: Project, t: Target, old_doc: str, log,
                   new_pgdata: str | None = None) -> None:
    """Back to the definition before the upgrade: its image, its data directory (untouched).
    A new cluster's directory the attempt made goes, so a later attempt starts clean."""
    from .recipe import edge
    from . import data as D
    f = _compose_file(platform, t)
    edge._atomic_write(f, old_doc, 0o600)
    _sh(["docker", "compose", "-p", compose_name(t), "-f", str(f), "up", "-d", "--pull", "never",
         "--force-recreate"], timeout=600)
    _wait_ready(t, 300)
    if new_pgdata and re.fullmatch(r"pgdata-[a-z0-9][a-z0-9.-]{0,30}", new_pgdata) \
            and new_pgdata != _pgdata(json.loads(old_doc)).rsplit("/", 1)[-1]:
        _img, where = D.data_paths(platform, volume_target(t))
        p = where / new_pgdata
        if p.is_dir() and not p.is_symlink():
            subprocess.run(["rm", "-rf", "--one-file-system", "--", str(p)], capture_output=True,
                           timeout=3600, cwd="/")
    log("the old postgres is back")
