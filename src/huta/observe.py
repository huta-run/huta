"""Read-only views of a target that come from outside its containers, for the operator and the
target's dev boxes. Every answer is bounded in time and size, redacted, and holds no client
addresses, paths, queries, cookies or payloads.

- egress_log: the egress daemon's refusals for the target (from its journal): per minute, the
  host:port as the daemon logged it (names off the allowlist are masked there), the reason and
  a count.
- traffic: an edge summary from Caddy's access log of the target's hosts: per minute, requests
  by status class; each 5xx with its time, status, duration and method. Nothing else of the
  request is read out.
- crashes: Bugsink issues of the target's Bugsink project ([crashes] in huta.toml, `crashes` on
  the target), with the newest event's release and stack trace.
- legacy_logs: the journal of a project's service that huta doesn't run ([legacy.<name>]),
  redacted with that service's own env file as well. Nothing is published when the file can't
  be read.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import stat
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

from . import logs as L
from .cfg import ConfigError, Platform, Project, Target, redact

STREAM_MAX = 64 << 20      # bytes read from journalctl or a log file per answer
SECONDS = 20               # scanning time per answer
EGRESS_UNIT = "huta-egress.service"


def _window(since: str | None, until: str | None, default: int = 3600,
            days: int = 8) -> tuple[float, float]:
    now = time.time()
    t0 = L.parse_when(since, now) if since else now - default
    t1 = L.parse_when(until, now) if until else now + 60
    if t1 - t0 > days * 86400 + 120:
        raise ConfigError(f"at most {days} days at a time")
    return t0, t1


def _minute(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _journal(unit: str, t0: float, t1: float):
    """(unix time, message) of a unit's journal, oldest first, bounded."""
    p = subprocess.Popen(["journalctl", "-u", unit, "-o", "json", "--no-pager", "--utc",
                          "--since", f"@{int(t0)}", "--until", f"@{int(t1) + 1}"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd="/")
    deadline, got = time.monotonic() + SECONDS, 0
    try:
        for raw in p.stdout:
            got += len(raw)
            if got > STREAM_MAX or time.monotonic() > deadline:
                yield None, None          # the caller says it stopped early
                return
            try:
                e = json.loads(raw)
                msg = e.get("MESSAGE")
                if isinstance(msg, list):  # journald's form for non-UTF-8 bytes
                    msg = bytes(x & 0xFF for x in msg if isinstance(x, int)).decode(errors="replace")
                if not isinstance(msg, str):
                    continue
                yield int(e["__REALTIME_TIMESTAMP"]) / 1e6, msg
            except (ValueError, KeyError, TypeError):
                continue
    finally:
        p.kill()
        p.wait(timeout=10)


# ------------------------------------------------------------------ egress refusals

REFUSED = re.compile(r"^refused (\S+)(?: \([^)]*\))?: (.+)$")
UNREACH = re.compile(r"^(\S+) → upstream (\S+) unreachable: (\w+)$")


def egress_log(platform: Platform, project: Project, t: Target, since=None, until=None) -> dict:
    who = f"{t.project}/{t.name}"
    t0, t1 = _window(since, until)
    counts: Counter = Counter()
    slow = False
    for ts, msg in _journal(EGRESS_UNIT, t0, t1):
        if ts is None:
            slow = True
            break
        m = REFUSED.match(msg)
        if m and m.group(1) == who:
            why = m.group(2)
        else:
            m = UNREACH.match(msg)
            if not (m and m.group(1) == who):
                continue
            why = f"upstream {m.group(2)} unreachable: {m.group(3)}"
        counts[(_minute(ts), redact(why)[:300])] += 1
    rows = [f"{mi}  {n:>5}  {why}" for (mi, why), n in sorted(counts.items())]
    head = (f"{who} egress refusals {_minute(t0)} .. {_minute(t1)} (UTC): minute, count, "
            "what was refused and why (names off the allowlist are masked by the daemon)")
    return L.answer(head, rows, len(rows), len(rows) or 1, slow)


# ------------------------------------------------------------------ edge summary

def _caddy_files(platform: Platform, host: str, t0: float) -> list[Path]:
    """The host's access log and its rolled files that may hold lines after t0, oldest first."""
    d = platform.caddy_log_dir
    out = []
    try:
        entries = list(os.scandir(d))
    except FileNotFoundError:
        return out
    for e in entries:
        if not (e.name == f"{host}.log" or (e.name.startswith(f"{host}-")
                                            and re.fullmatch(re.escape(host) + r"-[0-9T:.-]+(-size)?\.log(\.gz)?", e.name))):
            continue
        st = e.stat(follow_symlinks=False)
        if stat.S_ISREG(st.st_mode) and st.st_mtime >= t0:
            out.append((st.st_mtime, Path(e.path)))
    return [p for _m, p in sorted(out)]


def traffic(platform: Platform, project: Project, t: Target, since=None, until=None) -> dict:
    if not t.access_log:
        raise ConfigError(f"{t.project}/{t.name} has no access log (access_log = false)")
    t0, t1 = _window(since, until)
    per: dict[str, Counter] = {}
    fives: list[str] = []
    slow, got = False, 0
    deadline = time.monotonic() + SECONDS
    for host in t.hosts:
        for p in _caddy_files(platform, host, t0):
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            f = os.fdopen(fd, "rb")
            with (gzip.open(f) if p.name.endswith(".gz") else f) as fh:
                for raw in fh:
                    got += len(raw)
                    if got > STREAM_MAX * 4 or time.monotonic() > deadline:
                        slow = True
                        break
                    try:
                        e = json.loads(raw)
                        ts, status = float(e["ts"]), int(e["status"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if not t0 <= ts <= t1:
                        continue
                    mi = _minute(ts)
                    per.setdefault(mi, Counter())[f"{status // 100}xx"] += 1
                    if status >= 500:
                        method = (e.get("request") or {}).get("method", "?")
                        dur = e.get("duration")
                        fives.append(f"{datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%dT%H:%M:%S}Z "
                                     f"{status} {float(dur) * 1000 if isinstance(dur, (int, float)) else 0:.0f} ms "
                                     f"{method if method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS') else '?'}")
            if slow:
                break
    rows = [f"{mi}  " + "  ".join(f"{k} {c[k]}" for k in ("2xx", "3xx", "4xx", "5xx") if c[k])
            for mi, c in sorted(per.items())]
    fives.sort()
    lines = rows + (["5xx responses (time, status, duration, method):", *fives[-500:]]
                    if fives else ["no 5xx responses"])
    head = (f"{t.project}/{t.name} edge {_minute(t0)} .. {_minute(t1)} (UTC), "
            f"{', '.join(t.hosts)}: requests per minute by status class (502: the slot didn't "
            "answer). No addresses, paths or queries.")
    return L.answer(head, lines, len(lines), len(lines), slow)


# ------------------------------------------------------------------ crashes (Bugsink)

def _bugsink(platform: Platform, path: str, params: dict) -> dict:
    from .recipe import _OPENER      # no proxies, no redirects
    c = platform.crashes
    url = f"{c['url']}/api/canonical/0/{path}?{urllib.parse.urlencode(params)}"
    h = {"Authorization": "Bearer " + platform.secret("BUGSINK_TOKEN"), "Accept": "application/json"}
    if c.get("host"):
        h["Host"] = c["host"]
    with _OPENER.open(urllib.request.Request(url, headers=h), timeout=10) as r:
        body = r.read(8 << 20)
    return json.loads(body)


def crashes(platform: Platform, project: Project, pid: int | None, label: str, since=None,
            limit: int = 20) -> dict:
    if not platform.crashes:
        raise ConfigError("this host has no [crashes] in huta.toml")
    if pid is None:
        raise ConfigError(f"{label} has no Bugsink project (crashes = <id> in the host registry)")
    t0, _t1 = _window(since, None, default=86400, days=90)   # few issues, cheap to list
    issues, cursor, deadline = [], None, time.monotonic() + SECONDS
    while len(issues) < limit and time.monotonic() < deadline:
        page = _bugsink(platform, "issues/", {"project": pid, "sort": "last_seen", "order": "desc",
                                              **({"cursor": cursor} if cursor else {})})
        done = False
        for i in page.get("results", []):
            seen = _ts(i.get("last_seen"))
            if seen is None or seen < t0:
                done = True
                break
            issues.append(i)
            if len(issues) >= limit:
                break
        nxt = page.get("next")
        if done or not nxt:
            break
        cursor = urllib.parse.parse_qs(urllib.parse.urlparse(nxt).query).get("cursor", [None])[0]
        if not cursor:
            break
    blocks = []
    for i in issues:
        lines = [f"{i.get('calculated_type') or '?'}: {i.get('calculated_value') or ''}"[:500],
                 f"  count {i.get('digested_event_count')} · first {i.get('first_seen')} · "
                 f"last {i.get('last_seen')}" + (" · resolved" if i.get("is_resolved") else "")
                 + (" · muted" if i.get("is_muted") else "")]
        if time.monotonic() < deadline:
            try:
                ev = _bugsink(platform, "events/", {"issue": i["id"], "order": "desc"})
                first = (ev.get("results") or [None])[0]
                if first:
                    d = _bugsink(platform, f"events/{urllib.parse.quote(str(first['id']))}/", {})
                    data = d.get("data") or {}
                    lines.append(f"  release {str(data.get('release') or '-')[:80]} · "
                                 f"environment {str(data.get('environment') or '-')[:40]}")
                    st = str(d.get("stacktrace_md") or "")[:4000]
                    lines += [f"  {x}" for x in st.splitlines()[:80]]
            except Exception as e:  # noqa: BLE001 — the issue line is still worth giving
                lines.append(f"  (newest event not readable: {type(e).__name__})")
        blocks.append(lines)
    out = []
    for b in blocks:
        for x in b:
            low = x.lower()
            if any(d in low for d in project.log_drop):
                continue
            out.append(redact(x)[:L.LINE_MAX])
        out.append("")
    head = (f"{label} crashes: Bugsink issues last seen since {_minute(t0)} (UTC), newest first, "
            f"at most {limit}")
    return L.answer(head, out or ["(none)"], len(out), len(out) or 1,
                    time.monotonic() >= deadline)


def _ts(v) -> float | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ legacy units

def _env_needles(path: Path) -> None:
    """Every value of the service's env file becomes a redaction needle. Refuses (so nothing is
    shown) when the file isn't a plain readable file."""
    from . import runtime as R
    from .cfg import remember_secret
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise ConfigError(f"{path} is not readable ({e.strerror}); its logs are not shown") from None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > 1 << 20:
            raise ConfigError(f"{path} is not a plain file; its logs are not shown")
        vals = R.parse_env_file(f.read())
    redact("")                     # load the cache, so remember_secret adds to it
    for v in vals.values():
        remember_secret(v)


def legacy_logs(platform: Platform, project: Project, name: str, since=None, until=None,
                grep=None, tail=None) -> dict:
    lg = project.legacy[name]
    _env_needles(lg["env_file"])
    t0, t1 = _window(since, until)
    tail = L.TAIL_DEFAULT if tail is None else tail
    if not 1 <= tail <= L.TAIL_MAX:
        raise ConfigError(f"tail must be 1..{L.TAIL_MAX}")
    needle = grep.lower() if grep else None
    lines: deque = deque(maxlen=tail)
    matched, slow = 0, False
    for ts, msg in _journal(lg["unit"], t0, t1):
        if ts is None:
            slow = True
            break
        low = msg.lower()
        if any(d in low for d in project.log_drop):
            continue
        text = redact(msg)
        if len(text) > L.LINE_MAX:
            text = text[:L.LINE_MAX] + " ‹line cut›"
        if needle and needle not in text.lower():
            continue
        matched += 1
        lines.append(f"{datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%dT%H:%M:%S.%f}Z {name} {text}")
    head = (f"{project.name}/{name} logs ({lg['unit']}, not run by huta) {_minute(t0)} .. "
            f"{_minute(t1)} (UTC)" + (f", grep {grep!r}" if grep else ""))
    return L.answer(head, lines, matched, tail, slow)
