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


def hc_find(platform: Platform, check_name: str, deadline: float) -> str | None:
    """The uuid of the check called check_name, or None. All the key's checks are listed, as
    the upsert matches by name across all of them (a tag filter could miss one whose tags the
    operator changed)."""
    hc = platform.monitoring["healthchecks"]
    _s, d = _call("GET", f"{hc['url']}/api/v3/checks/", _hc_headers(platform), None, deadline,
                  ok=(200,))
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
        if not isinstance(page, dict) or not isinstance(page.get("results"), list) \
                or "next" not in page:
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
    # Writes the DSN secret: callers hold the project's secrets lock (plan_release's callers
    # do; huta monitor ensure takes it).
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


def site_file(platform: Platform, t: Target):
    """The target's Matomo site id, for the container's environment, which is built without the
    store (runtime.environment): <state_dir>/monitor/<p>--<t>.json."""
    return platform.state_dir / "monitor" / f"{t.project}--{t.name}.json"


def site_id(platform: Platform, t: Target) -> int | None:
    try:
        v = json.loads(site_file(platform, t).read_text()).get("matomo_site")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _write_site(platform: Platform, t: Target, sid: int) -> None:
    d = site_file(platform, t).parent
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = d / f".{t.project}--{t.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps({"matomo_site": sid}).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, site_file(platform, t))


def _ensure_analytics(platform, st, project, t, deadline) -> None:
    n = name(platform, t)
    row = st.monitor(project.name, t.name, "analytics", n)
    if row and site_id(platform, t) == int(row["remote_id"]) \
            and (t.matomo is None or t.matomo == int(row["remote_id"])):
        return
    if row and (t.matomo is None or t.matomo == int(row["remote_id"])):
        _write_site(platform, t, int(row["remote_id"]))    # the file went: repair it
        return
    if t.matomo is not None:
        sid = t.matomo
    else:
        if not platform.monitoring.get("matomo"):
            raise MonitorError("no [monitoring.matomo] on this host")
        urls = [f"https://{h}" for h in t.hosts]
        sid = mt_find(platform, urls[0], deadline) or mt_add(platform, n, urls, deadline)
    st.set_monitor(project.name, t.name, "analytics", n, str(sid), None)
    _write_site(platform, t, sid)
    st.event("monitor", f"analytics: Matomo site {sid}", None, project.name, t.name)


def _ensure_uptime(platform, st, project, t, deadline) -> None:
    if not platform.monitoring.get("healthchecks"):
        return                    # huta probes by itself; there is just no check to feed
    n = name(platform, t)
    spec = {"name": n, "timeout": UPTIME_TIMEOUT, "grace": UPTIME_GRACE,
            "tags": f"{platform.monitoring['prefix']} {project.name} {t.name} uptime",
            "desc": f"huta probes https://{t.hosts[0]} for {project.name}/{t.name}"}
    row = st.monitor(project.name, t.name, "uptime", n)
    if row and json.loads(row["spec"] or "{}") == spec:
        return
    # channels only when the check is new remotely, not merely new to huta: after a create whose
    # answer was lost, the operator may have changed them since (codex review of milestone 1, #4).
    exists = row is not None or hc_find(platform, n, deadline)
    first = {} if exists else {"channels": platform.monitoring["healthchecks"]["channels"]}
    c = hc_upsert(platform, {**spec, **first}, deadline)
    remember_capability(platform, c["uuid"])
    st.set_monitor(project.name, t.name, "uptime", n, c["uuid"], spec)
    st.event("monitor", "uptime: healthchecks check made", None, project.name, t.name)


# ------------------------------------------------------------------ env

def env_for(platform: Platform, project: Project, t: Target, app: dict | None, sha: str,
            with_secrets: bool) -> tuple[dict[str, str], list[str]]:
    """The variables a container gets from monitoring, and why any it asked for are missing.
    Only what the repo asks for ([monitor] env) and the registry grants (monitor.errors,
    monitor.analytics); a name the registry already sets wins. The values come from what ensure
    made at planning time; nothing here calls a service."""
    asks = set(((app or {}).get("monitor") or {}).get("env", []))
    env: dict[str, str] = {}
    missing: list[str] = []
    if not asks or not t.monitor:
        return env, missing
    taken = set(t.env) | set(t.secrets)
    if "sentry" in asks:
        if "errors" not in t.monitor:
            missing.append("sentry: the host registry doesn't grant monitor.errors")
        elif "SENTRY_DSN" in taken:
            missing.append("sentry: SENTRY_DSN is set by the host registry; huta adds nothing")
        elif not (R.secret_dir(platform, project.name) / dsn_secret(t)).is_file():
            missing.append("sentry: no Bugsink DSN yet (it comes with the next release after "
                           "huta monitor ensure succeeds)")
        else:
            env["SENTRY_DSN"] = (R.secret_value(platform, project.name, dsn_secret(t))
                                 if with_secrets else R.SECRET_PLACEHOLDER)
            for k, v in (("SENTRY_RELEASE", sha), ("SENTRY_ENVIRONMENT", t.name)):
                if k not in taken:
                    env[k] = v
    if "matomo" in asks:
        sid = t.matomo if t.matomo is not None else site_id(platform, t)
        if "analytics" not in t.monitor:
            missing.append("matomo: the host registry doesn't grant monitor.analytics")
        elif "MATOMO_SITE_ID" in taken:
            missing.append("matomo: MATOMO_SITE_ID is set by the host registry; huta adds nothing")
        elif sid is None:
            missing.append("matomo: no Matomo site yet")
        else:
            env["MATOMO_SITE_ID"] = str(sid)
    return env, missing


MONITOR_VARS = ("SENTRY_DSN", "SENTRY_RELEASE", "SENTRY_ENVIRONMENT", "MATOMO_SITE_ID")


def effective(platform: Platform, project: Project, t: Target, app: dict) -> dict:
    """The plan's view (`effective["monitor"]`): kinds, the variables injected (names only),
    what is missing and why, the egress it adds."""
    env, missing = env_for(platform, project, t, app, "<sha>", with_secrets=False)
    return {"kinds": sorted(t.monitor), "env": sorted(env), "missing": missing,
            "egress": list(t.monitor_egress) if "SENTRY_DSN" in env else [],
            **({"watch": dict(sorted(t.watch.items()))} if t.watch else {})}


def secret_names(platform: Platform, project: Project, t: Target, app: dict) -> list[str]:
    """The generated secrets a release reads (the DSN), for the plan's secret_versions: approval
    refuses a plan whose DSN changed since (codex review of milestone 2, #2)."""
    env, _m = env_for(platform, project, t, app, "", with_secrets=False)
    return [dsn_secret(t)] if "SENTRY_DSN" in env else []


# ------------------------------------------------------------------ status

def ready(platform: Platform, st, project: Project, t: Target) -> tuple[bool, str]:
    """Whether every source the target's watch needs is there (docs/PLAN-monitoring.md §7):
    configured, its remote resource made, and (errors) the live release got the DSN. Resource
    existence is the minimum; plan 3 may ask for more."""
    from . import watch as W
    if not platform.monitoring:
        return False, "monitoring is off on this host"
    if not t.watch:
        return False, f"{project.name}/{t.name} has no watch"
    live = st.live(project.name, t.name)
    if not live:
        return False, "nothing is live"
    have = W.sources(platform, project, t, st.release(live["release_id"]))
    rows = {r["kind"]: r for r in st.monitors(project.name, t.name)}
    if "errors" in t.monitor:
        if not (platform.crashes or {}).get("url"):
            return False, "errors: no [crashes] url to read Bugsink from"
        if not (("errors" in rows and rows["errors"]["remote_id"]) or t.crashes):
            return False, "errors: the Bugsink project isn't made yet"
        if "errors" not in have:
            return False, "errors: the live release didn't get SENTRY_DSN"
    if "uptime" in t.monitor and platform.monitoring.get("healthchecks") \
            and "uptime" not in rows:
        return False, "uptime: the healthchecks check isn't made yet"
    return True, ""


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
    if "uptime" in t.monitor and "uptime" not in rows and not platform.monitoring.get("healthchecks"):
        out = [x for x in out if not x.startswith("uptime:")] + [
            "uptime: probed by huta only (no [monitoring.healthchecks])"]
    if "uptime" in t.monitor:
        ring = samples(platform, t)
        if ring:
            last = ring[-1]
            when = time.strftime("%H:%M:%S", time.localtime(last.get("ts", 0)))
            bad = sum(1 for x in ring if not x.get("ok"))
            out.append(f"probe: last {when} " + ("ok" if last.get("ok") else
                                                  f"FAILED ({last.get('why', '')})")
                       + f"; {bad} of the last {len(ring)} failed")
        else:
            out.append("probe: no sample yet (huta-monitor.timer runs every minute)")
    return out


# ------------------------------------------------------------------ the tick (milestone 4)
#
# huta-monitor.timer runs `huta monitor tick` every minute: one tick at a time (a lock outside
# locks/), no target lock taken or waited for, 45 s for everything. It probes the public health
# URL of every target with monitor.uptime, keeps the last samples itself, and pings the target's
# healthchecks check when one is configured. Targets are taken in turn, so a slow one can't
# starve the rest.

TICK_BUDGET = 45
PROBE_TIMEOUT = 5
PING_TIMEOUT = 5
RESERVE = 2                # seconds kept for writing the tick's state at the end
HEALTH_MAX = 64 << 10      # bytes of a health answer (recipe.MAX_BODY)
RING = 60
ENSURE_RETRY = 3600        # seconds between tick attempts to make a missing check


def samples_file(platform: Platform, t: Target):
    return platform.state_dir / "monitor" / f"{t.project}--{t.name}.probe.json"


def samples(platform: Platform, t: Target) -> list[dict]:
    """The last RING probe samples, oldest first; a missing or broken file is an empty one."""
    try:
        v = json.loads(samples_file(platform, t).read_text())
    except (OSError, ValueError):
        return []
    return [s for s in v if isinstance(s, dict)] if isinstance(v, list) else []


def _write_json(path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(value).encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _snapshot(platform: Platform, st, t: Target):
    """What must not change around a probe: the live release and its episode, and whether a
    release is at work (a phase file) or the target is stopped. None: don't probe now."""
    from .recipe import _phase_file, stopped_marker
    live = st.live(t.project, t.name)
    if not live or _phase_file(platform, t).exists() or stopped_marker(platform, t).exists():
        return None
    return (live["release_id"], live["since"])


def probe_host(host: str, path: str, sha: str, timeout: float = PROBE_TIMEOUT) -> str | None:
    """One request to https://<host><path>: None if it answered ok with the live sha, else a
    short reason. Through the public edge, as a visitor would reach it."""
    from .ext import UA
    try:
        status, _h, raw = transport("GET", f"https://{host}{path}",
                                    {"User-Agent": UA, "Cache-Control": "no-cache",
                                     "Accept-Encoding": "identity"}, None, timeout)
    except Exception as e:  # noqa: BLE001
        return f"no answer ({type(e).__name__})"
    if status != 200:
        return f"status {status}"
    if len(raw) > HEALTH_MAX:
        return "answer too large"
    try:
        body = json.loads(raw)
    except ValueError:
        return "answer is not JSON"
    if not isinstance(body, dict) or body.get("ok") is not True:
        return "not ok"
    if body.get("sha") != sha:
        return f"reports {str(body.get('sha'))[:12]}"
    return None


_NO_TIME = "\0no time"


def probe_target(platform: Platform, st, project: Project, t: Target, log=lambda _m: None,
                 end: float | None = None):
    """Probe every host of the target once, within `end` (monotonic). Returns the sample, or
    None when it was skipped (nothing live, a release at work, stopped, any of that changed
    during the probe, or not every host could be asked in time)."""
    end = time.monotonic() + TICK_BUDGET if end is None else end
    from . import spec as SP
    before = _snapshot(platform, st, t)
    if before is None:
        return None
    rel = st.release(before[0])
    app = json.loads(rel["app_toml"])
    path = SP.public(app).get("health", "/healthz")
    # Every host at once: a target's probe takes one PROBE_TIMEOUT whatever its number of
    # hosts, so no target can outgrow the tick (codex review of milestone 4, #1).
    from concurrent.futures import ThreadPoolExecutor
    def one(h):
        left = min(PROBE_TIMEOUT, end - time.monotonic())    # a queued host gets what's left
        return _NO_TIME if left < 1 else probe_host(h, path, rel["sha"], left)
    with ThreadPoolExecutor(max_workers=min(8, len(t.hosts))) as pool:
        got = list(pool.map(one, t.hosts))
    if _NO_TIME in got:
        return None                              # incomplete: not a sample
    whys = [f"{h}: {w}" for h, w in zip(t.hosts, got) if w]
    if _snapshot(platform, st, t) != before:
        return None                              # a release started or ended meanwhile
    sample = {"ts": time.time(), "release": before[0], "since": before[1], "ok": not whys,
              "why": redact("; ".join(whys))[:300]}
    old = samples(platform, t)
    _write_json(samples_file(platform, t), (old + [sample])[-RING:])
    prev = old[-1] if old else None
    if prev is not None and prev.get("ok") != sample["ok"]:
        st.event("probe-up" if sample["ok"] else "probe-down",
                 "public health answers again" if sample["ok"] else sample["why"],
                 before[0], project.name, t.name)
        log(f"{project.name}/{t.name}: probe {'up' if sample['ok'] else 'down: ' + sample['why']}")
    return sample


def _ping(platform: Platform, uuid: str, suffix: str, end: float) -> bool:
    """A ping, only if the tick has time for it."""
    left = min(PING_TIMEOUT, end - time.monotonic())
    if left < 1:
        return False
    try:
        status, _h, _b = transport("GET", ping_url(platform, uuid, suffix), {}, None, left)
        return status == 200
    except Exception:  # noqa: BLE001
        return False


@contextmanager
def _tick_lock(platform: Platform):
    """One tick at a time; a tick that finds the lock held yields False and does nothing."""
    p = platform.state_dir / "monitor.lock"
    fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def tick(platform: Platform, registry: dict, st, log=lambda _m: None) -> dict:
    """One minute's work. Returns {"probed", "skipped", "left"} counts. Never raises for a
    target: each one's failure is logged and the next goes on."""
    out = {"probed": 0, "skipped": 0, "left": 0}
    if not platform.monitoring:
        return out
    with _tick_lock(platform) as mine:
        if not mine:
            out["busy"] = 1
            return out
        end = time.monotonic() + TICK_BUDGET - RESERVE
        state_path = platform.state_dir / "monitor" / "tick.json"
        try:
            state = json.loads(state_path.read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        todo = sorted(((p, t) for p in registry.values() for t in p.targets.values()
                       if t.kind != "gateway" and "uptime" in t.monitor),
                      key=lambda pt: (pt[0].name, pt[1].name))
        if todo:
            k = int(state.get("cursor", 0)) % len(todo)
            todo = todo[k:] + todo[:k]
        hc = platform.monitoring.get("healthchecks")
        for i, (project, t) in enumerate(todo):
            if end - time.monotonic() < PROBE_TIMEOUT + 1:
                out["left"] = len(todo) - i
                # The rest go first next time (at least one step, so nothing sticks).
                state["cursor"] = int(state.get("cursor", 0)) + max(i, 1)
                break
            try:
                s = probe_target(platform, st, project, t, log, end)
                out["probed" if s else "skipped"] += 1
                if s and hc:
                    row = st.monitor(project.name, t.name, "uptime", name(platform, t))
                    if row is None:
                        key = f"ensure:{project.name}/{t.name}"
                        if time.time() - float(state.get(key, 0)) > ENSURE_RETRY \
                                and end - time.monotonic() > ENSURE_BUDGET + PROBE_TIMEOUT:
                            state[key] = time.time()
                            ensure(platform, st, project, t, kinds=("uptime",), log=log)
                            row = st.monitor(project.name, t.name, "uptime", name(platform, t))
                    if row is not None:
                        _ping(platform, row["remote_id"], "" if s["ok"] else "fail", end)
            except Exception as e:  # noqa: BLE001 — one target never stops the others
                log(f"{project.name}/{t.name}: probe failed: {type(e).__name__}")
        else:
            state["cursor"] = int(state.get("cursor", 0)) + 1
        # Watch windows (§6): after the probes, whose samples they read.
        try:
            from . import watch as W
            W.tick(platform, registry, st, end, log)
        except Exception as e:  # noqa: BLE001
            log(f"watch: {type(e).__name__}")
        if hc and hc.get("self") and time.monotonic() < end:
            try:
                _self_ping(platform, st, state, end, log)
            except Exception as e:  # noqa: BLE001
                log(f"self check: {type(e).__name__}")
        _write_json(state_path, state)
    return out


SELF_PROJECT, SELF_TARGET = "_host", "self"


def _self_ping(platform: Platform, st, state: dict, end: float, log) -> None:
    """huta's own check: 'the tick ran'. Made once (find-or-create), then pinged every tick."""
    n = self_name(platform)
    row = st.monitor(SELF_PROJECT, SELF_TARGET, "self", n)
    if row is None:
        if time.time() - float(state.get("ensure:self", 0)) < ENSURE_RETRY \
                or end - time.monotonic() < 15:
            return
        state["ensure:self"] = time.time()
        budget = min(15, end - time.monotonic() - PING_TIMEOUT)
        deadline = time.monotonic() + budget
        spec = {"name": n, "timeout": 300, "grace": 600, "tags": platform.monitoring["prefix"],
                "desc": f"huta's monitor tick on {platform.host}"}
        # Under ensure's lock, as every capability write is (codex review of milestone 4, #2).
        with _ensure_lock(platform, wait=max(0.5, budget / 2)):
            row = st.monitor(SELF_PROJECT, SELF_TARGET, "self", n)
            if row is None:
                exists = hc_find(platform, n, deadline)
                first = {} if exists else {"channels": platform.monitoring["healthchecks"]["channels"]}
                c = hc_upsert(platform, {**spec, **first}, deadline)
                remember_capability(platform, c["uuid"])
                st.set_monitor(SELF_PROJECT, SELF_TARGET, "self", n, c["uuid"], spec)
                row = st.monitor(SELF_PROJECT, SELF_TARGET, "self", n)
    _ping(platform, row["remote_id"], "", end)
