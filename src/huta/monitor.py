"""Monitoring hooks (docs/PLAN-monitoring.md): small clients for healthchecks, Bugsink and
Matomo, and `ensure`, which makes the remote resources a target needs once and finds them again
by name.

Every answer from those services is untrusted: strict shapes, ids matched by regex, a wall-clock
deadline and a size cap on every call, no proxy and no redirects. A failure here is an event
(`monitor-fail`), never a release outcome.

What is kept where:
- `monitors` rows in huta.db: kind, remote name, remote id (a Bugsink project id, a Matomo site
  id, a healthchecks check uuid) and the spec last sent.
- A check's uuid is a capability (anyone who has it can ping the check), so it is also written
  to secrets/_monitor/CAPABILITIES before its row; the redactor reads that file with the other
  secrets, so no process prints one.
- The Bugsink DSN is a project secret, HUTA_SENTRY_DSN_<TARGET>.

Remote names: <prefix>_<project>_<target>[_<job>]. Names of projects, targets and jobs can't hold
"_", so a name maps back to one target. Longer than 100 characters (healthchecks' limit): cut,
plus 8 hex of its sha256.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

from . import runtime as R
from .cfg import UUID_RE, ConfigError, Platform, Project, Target, read_secret, redact, remember_secret

LIMIT = 8 << 20             # bytes of one answer
CALL_TIMEOUT = 10           # seconds per call
ENSURE_BUDGET = 30          # seconds for one ensure, all calls together
NAME_MAX = 100
CAPS_DIR = "_monitor"       # secrets/_monitor/CAPABILITIES (not a project: names start a-z)
CAPS_NAME = "CAPABILITIES"
UPTIME_TIMEOUT, UPTIME_GRACE = 300, 900
ENSURE_KINDS = ("errors", "analytics", "uptime")   # jobs: milestone 3
PLAN_KINDS = ("errors", "analytics")               # the kinds that change the container
DSN_KEY_RE = r"[0-9a-f]{32}"


class MonitorError(Exception):
    """A short reason, safe to record (no URL, no token, no answer body)."""


# ------------------------------------------------------------------ HTTP

def _transport(method: str, url: str, headers: dict, body: bytes | None,
               timeout: float) -> tuple[int, dict, bytes]:
    """One request: (status, headers, body). No proxy, no redirect (a 3xx comes back as its
    status), at most LIMIT + 1 bytes read, and the whole exchange — connect, headers, body —
    within `timeout`. Socket timeouts only bound each wait, so a server dripping bytes could
    hold a read for ever: the request runs in a daemon thread and is abandoned at the deadline
    (codex review of milestone 1, #2)."""
    import threading
    box: list = []

    def run():
        try:
            box.append(("ok", _transport_once(method, url, headers, body, timeout)))
        except BaseException as e:  # noqa: BLE001 — handed to the caller
            box.append(("err", e))
    th = threading.Thread(target=run, daemon=True, name="huta-monitor-http")
    th.start()
    th.join(timeout)
    if not box:
        raise TimeoutError("answer too slow")
    kind, v = box[0]
    if kind == "err":
        raise v
    return v


def _transport_once(method: str, url: str, headers: dict, body: bytes | None,
                    timeout: float) -> tuple[int, dict, bytes]:
    from .recipe import _OPENER
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        r = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        r = e
    with r:
        out = b""
        while len(out) <= LIMIT:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("answer too slow")
            try:
                r.fp.raw._sock.settimeout(left)       # type: ignore[attr-defined]
            except AttributeError:
                pass
            chunk = r.read(65536)
            if not chunk:
                break
            out += chunk
        return r.status if hasattr(r, "status") else r.code, dict(r.headers or {}), out


transport = _transport      # tests put the fake services here


def _call(method: str, url: str, headers: dict, body: bytes | None, deadline: float,
          ok: tuple = (200, 201)):
    """(status, parsed JSON). Raises MonitorError with a short reason."""
    left = min(CALL_TIMEOUT, deadline - time.monotonic())
    if left <= 0.5:
        raise MonitorError("out of time")
    try:
        status, _h, raw = transport(method, url, headers, body, left)
    except Exception as e:  # noqa: BLE001 — refused, reset, timed out, a bad status line …
        raise MonitorError(f"no answer ({type(e).__name__})") from None
    if 300 <= status < 400:
        raise MonitorError(f"redirected ({status}); a loopback Matomo with force_ssl needs "
                           "X-Forwarded-Proto in its proxy_scheme_headers" if "index.php" in url
                           else f"redirected ({status})")
    if len(raw) > LIMIT:
        raise MonitorError("answer too large")
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        raise MonitorError(f"answer is not JSON (status {status})") from None
    if status not in ok:
        raise MonitorError(f"status {status}")
    return status, data


def _deadline(budget: float = ENSURE_BUDGET) -> float:
    return time.monotonic() + budget


# ------------------------------------------------------------------ names

def _cut(name: str) -> str:
    if len(name) <= NAME_MAX:
        return name
    return name[:NAME_MAX - 9] + "-" + hashlib.sha256(name.encode()).hexdigest()[:8]


def name(platform: Platform, t: Target, job: str | None = None) -> str:
    base = f"{platform.monitoring['prefix']}_{t.project}_{t.name}"
    return _cut(base + (f"_{job}" if job else ""))


def self_name(platform: Platform) -> str:
    return _cut(f"{platform.monitoring['prefix']}__self_{platform.host}")


def dsn_secret(t: Target) -> str:
    return "HUTA_SENTRY_DSN_" + t.name.upper().replace("-", "_")


# ------------------------------------------------------------------ healthchecks

def _hc_headers(platform: Platform) -> dict:
    hc = platform.monitoring["healthchecks"]
    h = {"X-Api-Key": platform.secret("HEALTHCHECKS_API_KEY"),
         "Content-Type": "application/json", "Accept": "application/json"}
    if hc.get("host"):
        h["Host"] = hc["host"]
    return h


def hc_upsert(platform: Platform, spec: dict, deadline: float) -> dict:
    """Find-or-create a check by name. Returns {"uuid", "status"} from a checked answer."""
    hc = platform.monitoring["healthchecks"]
    _s, d = _call("POST", f"{hc['url']}/api/v3/checks/", _hc_headers(platform),
                  json.dumps({**spec, "unique": ["name"]}).encode(), deadline)
    if not isinstance(d, dict) or d.get("name") != spec["name"]:
        raise MonitorError("healthchecks answered with another check or no check")
    u = d.get("uuid")
    if not isinstance(u, str) or not UUID_RE.match(u):
        raise MonitorError("healthchecks gave no check id (is the API key read-only?)")
    status = d.get("status")
    return {"uuid": u, "status": status if status in HC_STATES else "?"}


HC_STATES = ("new", "up", "grace", "down", "paused", "started")


def hc_find(platform: Platform, check_name: str, tag: str, deadline: float) -> str | None:
    """The uuid of the check called check_name among those tagged `tag`, or None."""
    hc = platform.monitoring["healthchecks"]
    _s, d = _call("GET", f"{hc['url']}/api/v3/checks/?{urllib.parse.urlencode({'tag': tag})}",
                  _hc_headers(platform), None, deadline, ok=(200,))
    if not isinstance(d, dict) or not isinstance(d.get("checks"), list):
        raise MonitorError("healthchecks answered with no check list")
    found = []
    for c in d["checks"]:
        if not (isinstance(c, dict) and isinstance(c.get("name"), str)):
            raise MonitorError("healthchecks listed a check huta can't read")
        if c["name"] == check_name:
            u = c.get("uuid")
            if not isinstance(u, str) or not UUID_RE.match(u):
                raise MonitorError("healthchecks gave no check id (is the API key read-only?)")
            found.append(u)
    if len(found) > 1:
        raise MonitorError(f"ambiguous: {len(found)} healthchecks checks are called {check_name}")
    return found[0] if found else None


def hc_pause(platform: Platform, uuid: str, deadline: float) -> None:
    if not UUID_RE.match(uuid):
        raise MonitorError("bad check id")
    hc = platform.monitoring["healthchecks"]
    _call("POST", f"{hc['url']}/api/v3/checks/{uuid}/pause", _hc_headers(platform), b"",
          deadline)


def ping_url(platform: Platform, uuid: str, suffix: str = "") -> str:
    """Built from the configured ping base and a checked uuid; a URL from an answer is never
    used."""
    if not UUID_RE.match(uuid):
        raise MonitorError("bad check id")
    if suffix and not re.fullmatch(r"start|fail|log|\d{1,3}", suffix):
        raise MonitorError("bad ping suffix")
    return f"{platform.monitoring['healthchecks']['ping']}/{uuid}" + (f"/{suffix}" if suffix else "")


# ------------------------------------------------------------------ Bugsink

def _bs(platform: Platform, method: str, path: str, deadline: float, params: dict | None = None,
        body: dict | None = None, ok=(200, 201)):
    c = platform.crashes
    url = f"{c['url']}/api/canonical/0/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    h = {"Authorization": "Bearer " + platform.secret("BUGSINK_TOKEN"), "Accept": "application/json"}
    if c.get("host"):
        h["Host"] = c["host"]
    data = None
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    return _call(method, url, h, data, deadline, ok)


def _cursor(nxt) -> str | None:
    """The cursor of a `next` link, and nothing else of it: the link's host is never called."""
    if not isinstance(nxt, str):
        return None
    c = urllib.parse.parse_qs(urllib.parse.urlsplit(nxt).query).get("cursor", [None])[0]
    return c if isinstance(c, str) and re.fullmatch(r"[A-Za-z0-9_=%+/-]{1,512}", c) else None


def _project_of(p) -> dict | None:
    if not isinstance(p, dict):
        return None
    pid, pname = p.get("id"), p.get("name")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 1 << 31
            and isinstance(pname, str)):
        return None
    return {"id": pid, "name": pname, "dsn": p.get("dsn")}


def bs_find(platform: Platform, pname: str, deadline: float) -> dict | None:
    """The project called pname ({"id", "name", "dsn"}), or None. At most 20 pages of 250."""
    found, cursor = [], None
    for _ in range(20):
        _s, page = _bs(platform, "GET", "projects/", deadline,
                       {"cursor": cursor} if cursor else None)
        if not isinstance(page, dict) or not isinstance(page.get("results"), list):
            raise MonitorError("Bugsink answered with no project list")
        for p in page["results"]:
            q = _project_of(p)
            if q is None:
                # A row huta can't read might be the one it is looking for: refuse rather than
                # create a second (codex review of milestone 1, #3).
                raise MonitorError("Bugsink listed a project huta can't read")
            if q["name"] == pname:
                found.append(q)
        nxt = page.get("next")
        if nxt is None:
            break
        cursor = _cursor(nxt)
        if not cursor:
            raise MonitorError("Bugsink's next page link has no cursor huta can read")
    else:
        raise MonitorError("Bugsink has too many projects to list")
    if len(found) > 1:
        raise MonitorError(f"ambiguous: {len(found)} Bugsink projects are called {pname}")
    return found[0] if found else None


def bs_get(platform: Platform, pid: int, deadline: float) -> dict:
    _s, p = _bs(platform, "GET", f"projects/{int(pid)}/", deadline)
    q = _project_of(p)
    if not q or q["id"] != pid:
        raise MonitorError(f"Bugsink has no project {pid}")
    return q


def bs_create(platform: Platform, pname: str, deadline: float) -> dict:
    """Create, then find by name (Bugsink's create answer carries no DSN). A name taken
    meanwhile (another create, or one whose answer was lost) is found the same way."""
    team = platform.monitoring["bugsink"]["team"]
    try:
        _bs(platform, "POST", "projects/", deadline, body={"team": team, "name": pname},
            ok=(201,))
    except MonitorError as e:
        if "status 400" not in str(e):
            raise
    p = bs_find(platform, pname, deadline)
    if p is None:
        raise MonitorError("Bugsink refused to create the project (is the team id right?)")
    return p


def check_dsn(platform: Platform, dsn, pid: int) -> str:
    host = re.escape(platform.monitoring["bugsink"]["dsn_host"])
    if not isinstance(dsn, str) or not re.fullmatch(rf"https://{DSN_KEY_RE}@{host}/{pid}", dsn):
        raise MonitorError("Bugsink's DSN is not https://<key>@<dsn_host>/<project id> "
                           "(check Bugsink's BASE_URL against [monitoring.bugsink] dsn_host)")
    return dsn


# ------------------------------------------------------------------ Matomo

def _mt(platform: Platform, method: str, params: dict, deadline: float):
    m = platform.monitoring["matomo"]
    form = {"module": "API", "format": "json", "method": method, **params,
            "token_auth": platform.secret("MATOMO_TOKEN")}
    h = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
         "X-Forwarded-Proto": "https"}
    if m.get("host"):
        h["Host"] = m["host"]
    try:
        _s, d = _call("POST", f"{m['url']}/index.php", h, urllib.parse.urlencode(form).encode(),
                      deadline)
    except MonitorError as e:
        raise MonitorError(f"Matomo: {e}") from None
    if isinstance(d, dict) and d.get("result") == "error":
        raise MonitorError("Matomo refused the call (token or access)")
    return d


def mt_find(platform: Platform, url: str, deadline: float) -> int | None:
    d = _mt(platform, "SitesManager.getSitesIdFromSiteUrl", {"url": url}, deadline)
    if not isinstance(d, list):
        raise MonitorError("Matomo answered with no site list")
    if not all(isinstance(x, dict) and re.fullmatch(r"[1-9]\d{0,9}", str(x.get("idsite", "")))
               for x in d):
        raise MonitorError("Matomo listed a site huta can't read")
    ids = sorted({int(x["idsite"]) for x in d})
    if len(ids) > 1:
        raise MonitorError(f"ambiguous: {len(ids)} Matomo sites have {url}; set matomo = <id>")
    return ids[0] if ids else None


def mt_add(platform: Platform, site_name: str, urls: list[str], deadline: float) -> int:
    params = {"siteName": site_name, **{f"urls[{i}]": u for i, u in enumerate(urls)}}
    d = _mt(platform, "SitesManager.addSite", params, deadline)
    v = d.get("value") if isinstance(d, dict) else d
    if not (isinstance(v, int) and not isinstance(v, bool) and 0 < v < 1 << 31):
        raise MonitorError("Matomo answered with no site id")
    return v


# ------------------------------------------------------------------ capabilities

def _caps_path(platform: Platform):
    return platform.secrets_dir / CAPS_DIR / CAPS_NAME


def remember_capability(platform: Platform, uuid: str) -> None:
    """Add a check uuid to the capabilities file (before its row is written), so every process
    redacts it. Old ones stay: logs written before may hold them."""
    if not UUID_RE.match(uuid):
        raise MonitorError("bad check id")
    p = _caps_path(platform)
    have = set(read_secret(p).split()) if p.exists() else set()
    if uuid not in have:
        R._write_secret(p.parent, CAPS_NAME, "\n".join(sorted(have | {uuid})))
    remember_secret(uuid)


# ------------------------------------------------------------------ ensure

@contextmanager
def _ensure_lock(platform: Platform, wait: float = ENSURE_BUDGET):
    """One ensure on the host at a time (a tick, a plan and the operator may race). Not in
    locks/: install.sh reads a held lock there as a release at work."""
    p = platform.state_dir / "monitor-ensure.lock"
    fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    raise MonitorError("another ensure is running") from None
                time.sleep(0.5)
        yield
    finally:
        os.close(fd)


def ensure(platform: Platform, st, project: Project, t: Target, kinds=ENSURE_KINDS,
           log=lambda _m: None) -> dict[str, str]:
    """Make what the target's monitor grants and `kinds` names exist, once. Returns kind →
    "ok" or a short reason. Never raises: each failure is an event `monitor-fail`."""
    out: dict[str, str] = {}
    want = [k for k in kinds if k in t.monitor and k in ENSURE_KINDS]
    if not want or not platform.monitoring:
        return out
    deadline = _deadline()
    try:
        with _ensure_lock(platform):
            for kind in want:
                try:
                    {"errors": _ensure_errors, "analytics": _ensure_analytics,
                     "uptime": _ensure_uptime}[kind](platform, st, project, t, deadline)
                    out[kind] = "ok"
                except Exception as e:  # noqa: BLE001 — monitoring never fails its caller
                    out[kind] = _fail(st, t, kind, e, log)
    except Exception as e:  # noqa: BLE001 — the lock file, the store: never the caller's problem
        for kind in want:
            out.setdefault(kind, _fail(st, t, kind, e, log))
    return out


def _fail(st, t: Target, kind: str, e: Exception, log) -> str:
    """Record a failure; recording may fail too (a locked or full database), and that is not
    the caller's problem either."""
    try:
        why = redact(str(e))[:300] if isinstance(e, (MonitorError, ConfigError)) else type(e).__name__
    except Exception:  # noqa: BLE001
        why = type(e).__name__
    for f in (lambda: st.event("monitor-fail", f"{kind}: {why}", None, t.project, t.name),
              lambda: log(f"monitor {kind}: {why}")):
        try:
            f()
        except Exception:  # noqa: BLE001
            pass
    return why


def _ensure_errors(platform, st, project, t, deadline) -> None:
    if not platform.monitoring.get("bugsink"):
        raise MonitorError("no [monitoring.bugsink] on this host")
    n = name(platform, t)
    row = st.monitor(project.name, t.name, "errors", n)
    secret = dsn_secret(t)
    have = R.secret_dir(platform, project.name) / secret
    if row and have.exists():
        return
    if row:                                       # the secret file went: repair it
        p = bs_get(platform, int(row["remote_id"]), deadline)
    elif t.crashes:                               # the registry names the project: adopt it
        p = bs_get(platform, t.crashes, deadline)
    else:
        p = bs_find(platform, n, deadline) or bs_create(platform, n, deadline)
    dsn = check_dsn(platform, p["dsn"], p["id"])
    if not row:
        st.set_monitor(project.name, t.name, "errors", n, str(p["id"]), None)
    if not have.exists() or read_secret(have) != dsn:
        R.secret_set(platform, project.name, secret, dsn)
    st.event("monitor", f"errors: Bugsink project {p['id']}", None, project.name, t.name)


def _ensure_analytics(platform, st, project, t, deadline) -> None:
    n = name(platform, t)
    if st.monitor(project.name, t.name, "analytics", n):
        return
    if t.matomo is not None:
        sid = t.matomo
    else:
        if not platform.monitoring.get("matomo"):
            raise MonitorError("no [monitoring.matomo] on this host")
        urls = [f"https://{h}" for h in t.hosts]
        sid = mt_find(platform, urls[0], deadline) or mt_add(platform, n, urls, deadline)
    st.set_monitor(project.name, t.name, "analytics", n, str(sid), None)
    st.event("monitor", f"analytics: Matomo site {sid}", None, project.name, t.name)


def _ensure_uptime(platform, st, project, t, deadline) -> None:
    if not platform.monitoring.get("healthchecks"):
        raise MonitorError("no [monitoring.healthchecks] on this host")
    n = name(platform, t)
    spec = {"name": n, "timeout": UPTIME_TIMEOUT, "grace": UPTIME_GRACE,
            "tags": f"{platform.monitoring['prefix']} {project.name} {t.name} uptime",
            "desc": f"huta probes https://{t.hosts[0]} for {project.name}/{t.name}"}
    row = st.monitor(project.name, t.name, "uptime", n)
    if row and json.loads(row["spec"] or "{}") == spec:
        return
    # channels only when the check is new remotely, not merely new to huta: after a create whose
    # answer was lost, the operator may have changed them since (codex review of milestone 1, #4).
    exists = row is not None or hc_find(platform, n, platform.monitoring["prefix"], deadline)
    first = {} if exists else {"channels": platform.monitoring["healthchecks"]["channels"]}
    c = hc_upsert(platform, {**spec, **first}, deadline)
    remember_capability(platform, c["uuid"])
    st.set_monitor(project.name, t.name, "uptime", n, c["uuid"], spec)
    st.event("monitor", "uptime: healthchecks check made", None, project.name, t.name)


# ------------------------------------------------------------------ status

def status_lines(platform: Platform, st, project: Project, t: Target) -> list[str]:
    """What is wired up, for `huta monitor status`. Check uuids are capabilities: never shown."""
    if not platform.monitoring:
        return ["monitoring is off on this host (no [monitoring] in huta.toml)"]
    if not t.monitor:
        return [f"{project.name}/{t.name} has no monitor in the host registry"]
    rows = {r["kind"]: r for r in st.monitors(project.name, t.name)}
    out = []
    for kind in t.monitor:
        r = rows.get(kind)
        if kind == "jobs":
            out.append("jobs: planned (milestone 3)")
        elif r is None:
            last = st.target_events(project.name, t.name, ("monitor-fail",), limit=20)
            why = next((e["msg"] for e in last if e["msg"].startswith(f"{kind}:")), "")
            out.append(f"{kind}: not made yet" + (f" (last failure: {why[len(kind) + 2:]})" if why else ""))
        elif kind == "errors":
            ok = (R.secret_dir(platform, project.name) / dsn_secret(t)).exists()
            out.append(f"errors: Bugsink project {r['remote_id']} ({r['name']}), DSN in secret "
                       f"{dsn_secret(t)}" + ("" if ok else " (missing: huta monitor ensure)"))
        elif kind == "analytics":
            out.append(f"analytics: Matomo site {r['remote_id']} ({r['name']})")
        else:
            out.append(f"{kind}: healthchecks check {r['name']} ({r['state']})")
    return out
