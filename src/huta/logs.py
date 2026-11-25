"""Container logs, for the operator and a target's dev boxes (`huta logs`).

Capture. Slots log to Docker's json-file driver, pinned in the generated compose file
(SLOT_LOGGING), so it never depends on the host's default. `huta logs-collect` (huta-logs.timer,
every minute) reads what each slot container wrote since the last run and appends it to
<state_dir>/logs/<p>/<t>/<YYYY-MM-DDTHH>.jsonl, one file per UTC hour. huta also collects a
target right before it recreates a slot (recipe.compose "up"), so a release or a restart doesn't
lose the old container's last lines, and a slot that never became healthy keeps its output
(its stopped container stays until the next release recreates it). Jobs log to no driver; their
output reaches this store (and the journal) only through the job runner's redactor.

Redaction is a gate at ingest: a line is checked against the project's `log_drop` (raw text,
lowercase) and dropped, or redacted (cfg.redact: every live and recently retired secret, and
the forms `cfg.needles` derives) before it is written. Nothing unredacted is stored. Reading
redacts again (values learned since), then filters, then cuts to the answer size.

With [logs] syslog = true the host also gets each stored line (after the gate) through syslog,
ident huta/<p>/<t>, facility daemon: for rsyslog forwarding or a log shipper. Capture stays
stdout/stderr: a container has no /dev/log.

Retention per target: `logs.days` (hour files older than that go) and `logs.max` (oldest hour
files go until the target fits); then [logs] max_total for all targets together (oldest first).

Each record: {"t": "2026-11-25T10:00:00.123456Z", "s": "o"|"e", "slot": "a", "rel": "<8 hex>",
"sha": "<12 hex>", "l": "<text>"}, or with "job": "<name>" instead of "slot" for job output.

Crash safety: the cursor (container id → file inode and offset) is written after the lines, so
a crash between the two repeats at most one run's lines; it never loses or exposes any.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cfg import ConfigError, Platform, Project, Target, redact

SLOT_LOGGING = {"driver": "json-file",
                "options": {"max-size": "10m", "max-file": "3", "compress": "false"}}
LINE_MAX = 8192            # characters kept of one line (after redaction)
PARTIAL_MAX = 64 * 1024    # a line without a newline is cut here (redacted whole, then cut)
READ_MAX = 64 << 20        # bytes read per container per run; the rest waits for the next run
ANSWER_MAX = 900_000       # bytes of JSON-encoded text in one answer (transports cap at 1 MiB)
TAIL_DEFAULT, TAIL_MAX = 500, 5000
SCAN_SECONDS = 20          # a read stops scanning after this and says so
HOUR_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d)\.jsonl$")
CID_RE = re.compile(r"^[0-9a-f]{64}$")


def log_dir(platform: Platform, project: str, target: str | None = None) -> Path:
    d = platform.state_dir / "logs" / project
    return d / target if target else d


def _root_dirs(platform: Platform, t: Target) -> Path:
    """<state_dir>/logs/<p>/<t>, root 0700 all the way (state_dir is root's)."""
    d = platform.state_dir
    for part in ("logs", t.project, t.name):
        d = d / part
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
            raise ConfigError(f"{d} must be a root-owned directory with mode 0700")
    return d


class _Lock:
    """One collector per target at a time (the timer and a release's flush)."""

    def __init__(self, d: Path, wait: float = 120):
        self.fd = os.open(d / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise RuntimeError(f"{d}: log collector busy")
                time.sleep(0.2)

    def close(self):
        os.close(self.fd)


# ------------------------------------------------------------------ timestamps

def norm_time(s: str) -> str | None:
    """Docker's RFC3339Nano (trailing zeros trimmed, any offset) → fixed UTC microseconds, so
    records sort as strings."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", s or "")
    if not m:
        return None
    frac = (m.group(2) or "").ljust(6, "0")[:6]
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}.{frac}" + ("+00:00" if m.group(3) == "Z"
                                                              else m.group(3)))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_when(v: str, now: float | None = None) -> float:
    """'90s', '15m', '1h', '2d' (ago) or a UTC time '2026-11-25', '2026-11-25T10:00[:00][Z]'."""
    now = time.time() if now is None else now
    m = re.fullmatch(r"(\d{1,6})([smhd])", v)
    if m:
        return now - int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)(?:[T ](\d\d:\d\d(?::\d\d)?))?Z?", v)
    if m:
        try:
            dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2) or '00:00'}+00:00")
        except ValueError:
            raise ConfigError(f"bad time {v!r}") from None
        return dt.timestamp()
    raise ConfigError(f"bad time {v!r}: use 90s, 15m, 1h, 2d or a UTC time like 2026-11-25T10:00")


WHEN_PATTERN = r"\d{1,6}[smhd]|\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?)?Z?"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ------------------------------------------------------------------ writing

def gate(text: str, drop: tuple) -> str | None:
    """The log gate: None when a log_drop string is in the raw line, else redacted and cut."""
    low = text.lower()
    if any(d in low for d in drop):
        return None
    text = redact(text)
    if len(text) > LINE_MAX:
        text = text[:LINE_MAX] + f" ‹line cut at {LINE_MAX} characters›"
    return text


class Writer:
    """Appends records to the target's hour files. Gate first: drop, redact, cut."""

    def __init__(self, platform: Platform, t: Target, drop: tuple):
        self.dir = _root_dirs(platform, t)
        self.drop = drop
        self.syslog = None
        if platform.logs_syslog:
            import syslog
            syslog.openlog(f"huta/{t.project}/{t.name}", 0, syslog.LOG_DAEMON)
            self.syslog = syslog
        self.files: dict[str, int] = {}
        self.written = self.dropped = 0

    def gate(self, text: str) -> str | None:
        out = gate(text, self.drop)
        if out is None:
            self.dropped += 1
        return out

    def put(self, rec: dict, text: str) -> None:
        line = self.gate(text)
        if line is None:
            return
        self.raw(rec, line)

    def raw(self, rec: dict, line: str) -> None:
        """A record whose text already passed the gate (or is huta's own note)."""
        hour = rec["t"][:13]
        fd = self.files.get(hour)
        if fd is None:
            fd = os.open(self.dir / f"{hour}.jsonl",
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            self.files[hour] = fd
        os.write(fd, (json.dumps({**rec, "l": line}, ensure_ascii=False) + "\n").encode())
        self.written += 1
        if self.syslog:
            # Only what passed the gate; the journal keeps its own time, so the line's goes in.
            who = f"job:{rec['job']}" if "job" in rec else f"slot-{rec.get('slot', '?')}"
            self.syslog.syslog(self.syslog.LOG_NOTICE if rec.get("s") == "e" else
                               self.syslog.LOG_INFO,
                               f"{rec['t']} {who} {rec.get('rel') or '-'} {line}")

    def close(self) -> None:
        for fd in self.files.values():
            os.fsync(fd)
            os.close(fd)
        self.files = {}


class JobLog:
    """A job's output, line by line, from the job runner (its relay thread)."""

    def __init__(self, platform: Platform, project: Project, t: Target, job: str, rid: str,
                 sha: str):
        self.w = Writer(platform, t, project.log_drop)
        self.base = {"job": job, "rel": rid, "sha": sha[:12]}

    def line(self, text: str) -> None:
        try:
            self.w.put({"t": now_iso(), "s": "o", **self.base}, text)
        except OSError:
            pass                       # a full disk must not fail the job

    def close(self) -> None:
        try:
            self.w.close()
        except OSError:
            pass


# ------------------------------------------------------------------ collecting

def _docker_inspect(t: Target) -> list[dict]:
    r = subprocess.run(["docker", "ps", "-aq", "--no-trunc",
                        "--filter", f"label=run.huta.project={t.project}",
                        "--filter", f"label=run.huta.target={t.name}"],
                       capture_output=True, text=True, timeout=60, cwd="/")
    ids = [i for i in r.stdout.split() if CID_RE.match(i)]
    if r.returncode != 0 or not ids:
        return []
    r = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True, timeout=60,
                       cwd="/")
    try:
        return json.loads(r.stdout) if r.returncode == 0 else []
    except ValueError:
        return []


def _release_of(st, t: Target, sha: str, created: str) -> str:
    """The release a container ran: the newest of this target's releases with its sha that was
    made before the container was created."""
    if st is None:
        return ""
    rows = st.db.execute("SELECT id, created FROM releases WHERE project=? AND target=? AND sha=?"
                         " ORDER BY created DESC", (t.project, t.name, sha)).fetchall()
    c = norm_time(created)
    cts = datetime.fromisoformat(c.replace("Z", "+00:00")).timestamp() if c else time.time()
    for r in rows:
        if r["created"] <= cts + 5:
            return r["id"]
    return rows[-1]["id"] if rows else ""


def _log_files(path: str, cid: str) -> list[tuple[str, int, int]]:
    """Docker's files for one container, oldest first: (path, inode, size)."""
    if not (path.startswith("/") and path.endswith(f"/containers/{cid}/{cid}-json.log")
            and "/../" not in path):
        return []
    out = []
    for p in [f"{path}.{i}" for i in range(9, 0, -1)] + [path]:
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == 0:
            out.append((p, st.st_ino, st.st_size))
    return out


def _read_container(w: Writer, files, cur: dict | None, base: dict) -> dict:
    """Append what the container wrote after the cursor. Returns the new cursor."""
    idx, off, lost = 0, 0, cur is not None
    if cur:
        for i, (_p, ino, size) in enumerate(files):
            if ino == cur.get("ino") and size >= cur.get("off", 0):
                idx, off, lost = i, cur["off"], False
                break
    if lost:
        w.raw({"t": now_iso(), "s": "e", **base},
              "‹huta: earlier lines lost: Docker rotated its log before huta read it›")
    bufs = {"o": "", "e": ""}
    last_t = {"o": now_iso(), "e": now_iso()}
    commit = {"ino": files[idx][1], "off": off} if files else (cur or {})
    budget = READ_MAX
    for i in range(idx, len(files)):
        path, ino, _size = files[i]
        start = off if i == idx else 0
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.lseek(fd, start, os.SEEK_SET)
            pos, pending = start, b""
            while budget > 0:
                chunk = os.read(fd, min(1 << 20, budget))
                if not chunk:
                    break
                budget -= len(chunk)
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    pos += len(raw) + 1
                    try:
                        e = json.loads(raw)
                        text, s = e["log"], {"stdout": "o", "stderr": "e"}[e["stream"]]
                        ts = norm_time(e.get("time", "")) or now_iso()
                        if not isinstance(text, str):
                            raise TypeError
                    except (ValueError, KeyError, TypeError):
                        continue
                    last_t[s] = ts
                    bufs[s] += text
                    if text.endswith("\n") or len(bufs[s]) > PARTIAL_MAX:
                        cut = not text.endswith("\n")
                        w.put({"t": ts, "s": s, **base}, bufs[s].rstrip("\n")
                              + (" ‹no newline within 64 KiB›" if cut else ""))
                        bufs[s] = ""
                    if not bufs["o"] and not bufs["e"]:
                        commit = {"ino": ino, "off": pos}
        finally:
            os.close(fd)
        if budget <= 0:
            break
        if not pending and not bufs["o"] and not bufs["e"] and i + 1 < len(files):
            # This file is done: the cursor moves to the next one, so Docker deleting this
            # one later isn't mistaken for lost lines.
            commit = {"ino": files[i + 1][1], "off": 0}
    return commit


def collect(platform: Platform, project: Project, t: Target, st=None) -> str:
    """Read every slot container of the target since the last run. Returns a short summary."""
    d = _root_dirs(platform, t)
    lock = _Lock(d)
    w = Writer(platform, t, project.log_drop)
    try:
        cpath = d / ".cursor.json"
        try:
            cursors = json.loads(cpath.read_text())
        except (OSError, ValueError):
            cursors = {}
        from .recipe import compose_name
        seen = set()
        for c in _docker_inspect(t):
            labels = (c.get("Config") or {}).get("Labels") or {}
            cid = c.get("Id", "")
            if (labels.get("com.docker.compose.project") != compose_name(t)
                    or labels.get("run.huta.slot") not in ("a", "b") or not CID_RE.match(cid)
                    or (c.get("HostConfig") or {}).get("LogConfig", {}).get("Type") != "json-file"):
                continue
            seen.add(cid)
            sha = labels.get("run.huta.sha", "")
            base = {"slot": labels["run.huta.slot"], "sha": sha[:12],
                    **({"comp": labels["run.huta.component"]}
                       if labels.get("run.huta.component") else {}),
                    "rel": _release_of(st, t, sha, c.get("Created", ""))}
            files = _log_files(c.get("LogPath") or "", cid)
            if files:
                cursors[cid] = {**_read_container(w, files, cursors.get(cid), base),
                                "seen": int(time.time())}
        w.close()
        # Cursors of containers gone for a while go too.
        cursors = {k: v for k, v in cursors.items()
                   if k in seen or time.time() - v.get("seen", 0) < 8 * 86400}
        tmp = d / ".cursor.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, json.dumps(cursors).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, cpath)
        trim(d, t.logs["days"], t.logs["max"])
    finally:
        w.close()
        lock.close()
    return f"{t.project}/{t.name}: {w.written} lines" + (f", {w.dropped} dropped (log_drop)"
                                                        if w.dropped else "")


def collect_quietly(platform: Platform, t: Target, log=None) -> None:
    """Before a slot is recreated: best effort, never the action's outcome."""
    try:
        from . import store as S
        from .cfg import load_project
        project = load_project(t.project)
        collect(platform, project, project.targets.get(t.name, t), S.Store(platform.state_dir))
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"WARNING logs not collected before the recreate: {type(e).__name__}: {e}")


def _hours(d: Path) -> list[tuple[str, Path, int]]:
    out = []
    try:
        entries = list(os.scandir(d))
    except FileNotFoundError:
        return out
    for e in entries:
        m = HOUR_RE.match(e.name)
        if m and e.is_file(follow_symlinks=False):
            out.append((m.group(1), Path(e.path), e.stat(follow_symlinks=False).st_size))
    return sorted(out)


def trim(d: Path, days: int, max_bytes: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H")
    hours = _hours(d)
    keep = []
    for h, p, size in hours:
        if h < cutoff:
            p.unlink(missing_ok=True)
        else:
            keep.append((h, p, size))
    total = sum(s for *_x, s in keep)
    for h, p, size in keep[:-1]:            # the current hour stays
        if total <= max_bytes:
            break
        p.unlink(missing_ok=True)
        total -= size


def trim_total(platform: Platform) -> None:
    """[logs] max_total over every target's logs: the oldest hours go first."""
    root = platform.state_dir / "logs"
    allh = []
    for pd in (root.iterdir() if root.is_dir() else []):
        for td in (pd.iterdir() if pd.is_dir() and not pd.is_symlink() else []):
            if td.is_dir() and not td.is_symlink():
                allh += _hours(td)
    total = sum(s for *_x, s in allh)
    for _h, p, size in sorted(allh):
        if total <= platform.logs_max_total:
            break
        p.unlink(missing_ok=True)
        total -= size


# ------------------------------------------------------------------ reading

def query(platform: Platform, st, project: Project, t: Target, since: str | None = None,
          until: str | None = None, grep: str | None = None, tail: int | None = None,
          slot: str | None = None, release: str | None = None, job: str | None = None,
          box: str | None = None, component: str | None = None) -> dict:
    """Matching lines, newest last, within ANSWER_MAX. box: who asks (None: the operator); a box
    is held to the target's logs.boxes / logs.jobs."""
    if box is not None:
        if not t.logs["boxes"]:
            raise ConfigError(f"{t.project}/{t.name}: the host doesn't let boxes read its logs "
                              "(logs.boxes = false)")
        if job and not t.logs["jobs"]:
            raise ConfigError(f"{t.project}/{t.name}: job output is not readable from a box "
                              "(the host sets logs.jobs = true to allow it)")
    now = time.time()
    t0 = parse_when(since, now) if since else now - 3600
    t1 = parse_when(until, now) if until else now + 60
    tail = TAIL_DEFAULT if tail is None else tail
    if not 1 <= tail <= TAIL_MAX:
        raise ConfigError(f"tail must be 1..{TAIL_MAX}")
    if slot in ("live", "previous"):
        live = st.live(t.project, t.name)
        if not live:
            raise ConfigError("nothing is live")
        slot = live["slot"] if slot == "live" else ("b" if live["slot"] == "a" else "a")
    lo, hi = _iso(t0), _iso(t1)
    needle = grep.lower() if grep else None
    d = log_dir(platform, t.project, t.name)
    hours = _hours(d)
    oldest = hours[0][0] if hours else None
    lines: deque = deque(maxlen=tail)
    matched, slow = 0, False
    deadline = time.monotonic() + SCAN_SECONDS
    for h, p, _size in hours:
        if h < lo[:13] or h > hi[:13]:
            continue
        if time.monotonic() > deadline:
            slow = True
            break
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue                        # trimmed meanwhile
        with os.fdopen(fd, "rb") as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                    ts, text = r["t"], r["l"]
                except (ValueError, KeyError, TypeError):
                    continue
                if not (lo <= ts <= hi):
                    continue
                if job:
                    if r.get("job") != job:
                        continue
                elif "job" in r:
                    continue
                if slot and r.get("slot") != slot:
                    continue
                if component and r.get("comp") != component:
                    continue
                if release and r.get("rel") != release:
                    continue
                # Gate again: values learned and drop patterns added since the line was stored.
                low = text.lower()
                if any(x in low for x in project.log_drop):
                    continue
                text = redact(text)
                if needle and needle not in text.lower():
                    continue
                matched += 1
                who = f"job:{r['job']}" if "job" in r else f"slot-{r.get('slot', '?')}"
                lines.append(f"{ts} {who} {r.get('rel') or '-':<8} {'err' if r.get('s') == 'e' else 'out'} {text}")
    head = (f"{t.project}/{t.name} logs {lo[:19]}Z .. {hi[:19]}Z (UTC)"
            + (f", job {job}" if job else f", slot {slot}" if slot else "")
            + (f", release {release}" if release else "") + (f", grep {grep!r}" if grep else ""))
    extra = [f"‹oldest kept log hour: {oldest}:00Z›"] if oldest and lo[:13] < oldest else []
    return answer(head, lines, matched, tail, slow, extra)


def answer(head: str, lines, matched: int, tail: int, slow: bool, extra=()) -> dict:
    """Newest lines win the answer budget; every cut is said, never silent."""
    out, size = [], 0
    for line in reversed(lines):
        n = len(json.dumps(line)) + 1
        if size + n > ANSWER_MAX:
            break
        out.append(line)
        size += n
    out.reverse()
    notes = []
    if matched > len(out):
        notes.append(f"‹{matched - len(out)} earlier matching lines not shown "
                     f"({'tail ' + str(tail) if len(out) == len(lines) else 'answer size limit'}); "
                     "narrow it with --since, --until or --grep›")
    if slow:
        notes.append(f"‹stopped scanning after {SCAN_SECONDS} s; narrow the time range›")
    notes += list(extra)
    return {"text": "\n".join([head, *notes, *(out or ["(no matching lines)"])]),
            "lines": len(out), "matched": matched, "truncated": matched > len(out) or slow}


def slot_state(t: Target, slot: str) -> str:
    """One line per container of a slot: for the release's events when it failed to start."""
    from .recipe import compose_name
    out = []
    for c in _docker_inspect(t):
        labels = (c.get("Config") or {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") == compose_name(t) \
                and labels.get("run.huta.slot") == slot:
            s = c.get("State") or {}
            comp = labels.get("run.huta.component", "")
            parts = [f"slot {slot}{' ' + comp if comp else ''}: {s.get('Status', '?')}"]
            if not s.get("Running"):
                parts.append(f"exit code {s.get('ExitCode')}")
            if s.get("OOMKilled"):
                parts.append("OOM-killed")
            parts.append(f"restarts {c.get('RestartCount', 0)}")
            if s.get("Error"):
                parts.append(f"docker: {str(s['Error'])[:200]}")
            out.append(", ".join(parts))
    if not out:
        return f"slot {slot}: no container"
    return "; ".join(out) + f" · its output: huta logs {t.project} {t.name} --slot {slot}"
