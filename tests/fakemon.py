"""Fake monitoring services for tests: healthchecks (API v3 and pings), Bugsink (canonical API)
and Matomo (HTTP API), answering as the versions in docs/PLAN-monitoring.md's API survey do.

They answer at the HTTP level: `FakeMonitoring.handle(method, url, headers, body)` returns
(status, headers, body bytes), so huta's real clients run against them with only the opener
replaced. State lives in one JSON file (crash tests fork, and the parent reads what the child
left), and every create is counted, so a test can say "made once".

Faults (`fault(service, kind)`): "down" (connection refused), "slow" (a timeout), "oversize"
(a body larger than any client accepts), "redirect" (a 302 to elsewhere), "garbage" (not JSON),
"lost-create" (the create is committed, then the connection drops before the answer).

Every value here is made up.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

HC_KEY = "hc-test-key"
BUGSINK_TOKEN = "bugsink-test-token"
MATOMO_TOKEN = "matomo-test-token"
BUGSINK_BASE = "https://bugs.example.org"
BUGSINK_TEAM = "11111111-2222-3333-4444-555555555555"
HC_PING = "https://hc.example.org/ping/"

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class Down(OSError):
    """The service doesn't answer (connection refused, or the answer is lost)."""


class Slow(TimeoutError):
    pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _json(status: int, value, extra: dict | None = None):
    return status, {"Content-Type": "application/json", **(extra or {})}, json.dumps(value).encode()


class FakeMonitoring:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            self._save({"hc": {"checks": {}, "pings": []},
                        "bugsink": {"projects": {}, "issues": {}, "events": [], "next_id": 1},
                        "matomo": {"sites": {}, "next_id": 1},
                        "creates": {"hc": 0, "bugsink": 0, "matomo": 0},
                        "faults": {}})

    # ------------------------------------------------------------ state

    def _load(self) -> dict:
        return json.loads(self.path.read_text())

    def _save(self, s: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        tmp.replace(self.path)

    def state(self) -> dict:
        return self._load()

    def creates(self, service: str) -> int:
        return self._load()["creates"][service]

    def fault(self, service: str, kind: str | None) -> None:
        s = self._load()
        if kind:
            s["faults"][service] = kind
        else:
            s["faults"].pop(service, None)
        self._save(s)

    # Test helpers that play the part of an app or a human.

    def add_event(self, project_id: int, title: str, ts: float) -> None:
        """An error event reaches Bugsink: grouped into an issue by title."""
        s = self._load()
        b = s["bugsink"]
        key = f"{project_id}:{title}"
        iss = b["issues"].get(key)
        if iss is None:
            n = sum(1 for i in b["issues"].values() if i["project"] == project_id) + 1
            iss = {"id": str(_uuid.uuid4()), "friendly_id": f"P-{n}", "project": project_id,
                   "digest_order": n, "first_seen": ts, "last_seen": ts,
                   "calculated_type": "Error", "calculated_value": title, "transaction": "",
                   "digested_event_count": 0, "stored_event_count": 0, "is_resolved": False,
                   "is_resolved_by_next_release": False, "is_muted": False}
            b["issues"][key] = iss
        iss["last_seen"] = max(iss["last_seen"], ts)
        iss["first_seen"] = min(iss["first_seen"], ts)
        iss["digested_event_count"] += 1
        iss["stored_event_count"] += 1
        b["events"].append({"id": str(_uuid.uuid4()), "issue": iss["id"], "timestamp": ts,
                            "digest_order": iss["digested_event_count"],
                            "event_id": _uuid.uuid4().hex, "grouping": 1,
                            "project": project_id})
        self._save(s)

    def add_project(self, name: str) -> int:
        """A project made by hand in Bugsink (for adoption and ambiguity tests)."""
        s = self._load()
        pid = self._bugsink_create(s, name)
        self._save(s)
        return pid

    def add_site(self, url: str) -> int:
        s = self._load()
        m = s["matomo"]
        sid = m["next_id"]
        m["next_id"] += 1
        m["sites"][str(sid)] = {"idsite": sid, "urls": [url]}
        self._save(s)
        return sid

    def set_check(self, uuid: str, status: str, ts: float) -> None:
        """A check changes state (healthchecks' own clock), with a flip."""
        s = self._load()
        c = s["hc"]["checks"][uuid]
        if c["status"] != status:
            c.setdefault("flips", []).append({"ts": ts, "up": 1 if status == "up" else 0})
        c["status"] = status
        self._save(s)

    # ------------------------------------------------------------ HTTP

    def handle(self, method: str, url: str, headers: dict, body: bytes | None,
               timeout: float = 10):
        u = urllib.parse.urlsplit(url)
        service = self._service(u)
        s = self._load()
        f = s["faults"].get(service)
        if f == "down":
            raise Down("connection refused")
        if f == "slow":
            raise Slow("timed out")
        if f == "oversize":
            return 200, {"Content-Type": "application/json"}, b"[" + b"0," * (5 << 20) + b"0]"
        if f == "redirect":
            return 302, {"Location": "https://elsewhere.example.org/"}, b""
        if f == "garbage":
            return 200, {"Content-Type": "text/html"}, b"<html>oops</html>"
        h = {k.lower(): v for k, v in headers.items()}
        out = getattr(self, f"_{service}")(s, method, u, h, body or b"")
        created = s.pop("_created", False)
        self._save(s)
        if f == "lost-create" and created:
            raise Down("connection reset after the request was sent")
        return out

    @staticmethod
    def _service(u) -> str:
        if u.path.startswith(("/api/v3/", "/ping/")):
            return "hc"
        if u.path.startswith("/api/canonical/0/"):
            return "bugsink"
        if u.path == "/index.php":
            return "matomo"
        raise Down(f"nothing listens for {u.path}")

    # ---- healthchecks

    def _hc(self, s, method, u, h, body):
        hc = s["hc"]
        m = re.fullmatch(rf"/ping/({UUID_RE})(?:/(start|fail|log|\d+))?", u.path)
        if m:
            c = hc["checks"].get(m.group(1))
            if c is None:
                return 404, {}, b"not found"
            action = m.group(2) or "0"
            if action.isdigit() and int(action) > 255:
                return 400, {}, b"invalid url format"
            rid = urllib.parse.parse_qs(u.query).get("rid", [None])[0]
            hc["pings"].append({"uuid": m.group(1), "action": action, "rid": rid})
            new = {"start": c["status"], "log": c["status"], "fail": "down"}.get(
                action, "up" if action == "0" else "down")
            if action == "start":
                new = "started"
            if new != c["status"] and new in ("up", "down"):
                c.setdefault("flips", []).append({"ts": time.time(), "up": 1 if new == "up" else 0})
            c["status"] = new
            c["n_pings"] += 1
            return 200, {"Content-Type": "text/plain"}, b"OK"
        if h.get("x-api-key") != HC_KEY:
            return _json(401, {"error": "missing api key"})
        if u.path == "/api/v3/checks/" and method == "GET":
            tag = urllib.parse.parse_qs(u.query).get("tag", [None])[0]
            rows = [c for c in hc["checks"].values() if tag is None or tag in c["tags"].split()]
            return _json(200, {"checks": [self._hc_dict(c) for c in rows]})
        if u.path == "/api/v3/checks/" and method == "POST":
            try:
                spec = json.loads(body)
            except ValueError:
                return _json(400, {"error": "could not parse request body"})
            name = spec.get("name")
            if not isinstance(name, str) or len(name) > 100:
                return _json(400, {"error": "name: bad"})
            for k in ("timeout", "grace"):
                if k in spec and not (isinstance(spec[k], int) and 60 <= spec[k] <= 31536000):
                    return _json(400, {"error": f"{k}: out of range"})
            found = None
            if spec.get("unique") == ["name"]:
                found = next((c for c in hc["checks"].values() if c["name"] == name), None)
            created = found is None
            if created:
                code = str(_uuid.uuid4())
                found = {"uuid": code, "name": name, "status": "new", "n_pings": 0,
                         "channels": "", "tags": "", "desc": "", "grace": 3600,
                         "timeout": 86400, "flips": []}
                hc["checks"][code] = found
                s["creates"]["hc"] += 1
                s["_created"] = True
            for k in ("tags", "desc", "grace", "channels"):
                if k in spec:
                    found[k] = spec[k]
            if "schedule" in spec:
                found["schedule"], found["tz"] = spec["schedule"], spec.get("tz", "UTC")
                found.pop("timeout", None)
            elif "timeout" in spec:
                found["timeout"] = spec["timeout"]
                found.pop("schedule", None)
            return _json(201 if created else 200, self._hc_dict(found))
        m = re.fullmatch(rf"/api/v3/checks/({UUID_RE})(/pause|/resume|/flips/)?", u.path)
        if m:
            c = hc["checks"].get(m.group(1))
            if c is None:
                return _json(404, {"error": "not found"})
            what = m.group(2)
            if what == "/pause" and method == "POST":
                c["status"] = "paused"
                return _json(200, self._hc_dict(c))
            if what == "/resume" and method == "POST":
                c["status"] = "new"
                return _json(200, self._hc_dict(c))
            if what == "/flips/" and method == "GET":
                q = urllib.parse.parse_qs(u.query)
                start = float(q.get("start", ["0"])[0])
                fl = [x for x in c.get("flips", []) if x["ts"] >= start]
                return _json(200, {"flips": [{"timestamp": _iso(x["ts"]), "up": x["up"]}
                                             for x in reversed(fl)]})
            if what is None and method == "GET":
                return _json(200, self._hc_dict(c))
        return _json(404, {"error": "not found"})

    @staticmethod
    def _hc_dict(c: dict) -> dict:
        d = {"name": c["name"], "slug": "", "tags": c["tags"], "desc": c["desc"],
             "grace": c["grace"], "n_pings": c["n_pings"], "status": c["status"],
             "started": c["status"] == "started", "last_ping": None, "next_ping": None,
             "manual_resume": False, "methods": "", "uuid": c["uuid"],
             "ping_url": HC_PING + c["uuid"],
             "update_url": f"https://hc.example.org/api/v3/checks/{c['uuid']}",
             "pause_url": f"https://hc.example.org/api/v3/checks/{c['uuid']}/pause",
             "resume_url": f"https://hc.example.org/api/v3/checks/{c['uuid']}/resume",
             "channels": c["channels"], "badge_url": "https://hc.example.org/b/2/x.svg",
             "subject": "", "subject_fail": "", "start_kw": "", "success_kw": "",
             "failure_kw": "", "filter_subject": False, "filter_body": False,
             "filter_http_body": False, "filter_default_fail": False}
        if "schedule" in c:
            d["schedule"], d["tz"] = c["schedule"], c["tz"]
        else:
            d["timeout"] = c["timeout"]
        return d

    # ---- Bugsink

    def _bugsink_create(self, s, name: str) -> int:
        b = s["bugsink"]
        pid = b["next_id"]
        b["next_id"] += 1
        b["projects"][str(pid)] = {"id": pid, "team": BUGSINK_TEAM, "name": name,
                                   "slug": re.sub(r"[^a-z0-9-]", "-", name.lower())[:50],
                                   "key": _uuid.uuid4().hex}
        return pid

    def _bugsink(self, s, method, u, h, body):
        if h.get("authorization") != f"Bearer {BUGSINK_TOKEN}":
            return _json(401, {"detail": "Authentication credentials were not provided."})
        b = s["bugsink"]
        path = u.path[len("/api/canonical/0/"):]
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if path == "projects/" and method == "GET":
            rows = sorted(b["projects"].values(), key=lambda p: p["name"])
            return _json(200, {"next": None, "previous": None,
                               "results": [self._project(p) for p in rows]})
        if path == "projects/" and method == "POST":
            try:
                d = json.loads(body)
            except ValueError:
                return _json(400, {"detail": "JSON parse error"})
            if d.get("team") != BUGSINK_TEAM:
                return _json(400, {"team": ["Invalid pk - object does not exist."]})
            if any(p["name"] == d.get("name") for p in b["projects"].values()):
                return _json(400, {"name": ["project with this name already exists."]})
            self._bugsink_create(s, d["name"])
            s["creates"]["bugsink"] += 1
            s["_created"] = True
            # The create answer has no dsn (Bugsink 2.2).
            return _json(201, {"id": None, "team": BUGSINK_TEAM, "name": d["name"],
                               "visibility": "team_members", "alert_on_new_issue": True,
                               "alert_on_regression": True, "alert_on_unmute": True,
                               "retention_max_event_count": 10000})
        m = re.fullmatch(r"projects/(\d+)/", path)
        if m and method == "GET":
            p = b["projects"].get(m.group(1))
            return _json(200, self._project(p)) if p else _json(404, {"detail": "Not found."})
        if path == "teams/" and method == "GET":
            return _json(200, {"next": None, "previous": None,
                               "results": [{"id": BUGSINK_TEAM, "name": "team",
                                            "visibility": "joinable"}]})
        if path == "issues/" and method == "GET":
            if "project" not in q:
                return _json(400, {"project": ["This field is required."]})
            rows = [i for i in b["issues"].values() if str(i["project"]) == q["project"]]
            rows.sort(key=lambda i: i["last_seen"], reverse=q.get("order") == "desc")
            return _json(200, {"next": None, "previous": None, "results": [
                {**i, "first_seen": _iso(i["first_seen"]), "last_seen": _iso(i["last_seen"])}
                for i in rows]})
        if path == "events/" and method == "GET":
            if "issue" not in q:
                return _json(400, {"issue": ["This field is required."]})
            rows = [e for e in b["events"] if e["issue"] == q["issue"]]
            rows.sort(key=lambda e: e["digest_order"], reverse=q.get("order", "desc") == "desc")
            return _json(200, {"next": None, "previous": None, "results": [
                {**e, "timestamp": _iso(e["timestamp"]), "ingested_at": _iso(e["timestamp"]),
                 "digested_at": _iso(e["timestamp"])}
                for e in rows]})
        return _json(404, {"detail": "Not found."})

    @staticmethod
    def _project(p: dict) -> dict:
        host = urllib.parse.urlsplit(BUGSINK_BASE).hostname
        return {"id": p["id"], "team": p["team"], "name": p["name"], "slug": p["slug"],
                "dsn": f"https://{p['key']}@{host}/{p['id']}",
                "digested_event_count": 0, "stored_event_count": 0, "alert_on_new_issue": True,
                "alert_on_regression": True, "alert_on_unmute": True,
                "visibility": "team_members", "retention_max_event_count": 10000}

    # ---- Matomo

    def _matomo(self, s, method, u, h, body):
        if h.get("x-forwarded-proto") != "https":
            return 302, {"Location": "https://stats.example.org/index.php"}, b""
        if method != "POST":
            return _json(200, {"result": "error", "message": "token_auth in a GET is refused"})
        f = {k: v for k, v in urllib.parse.parse_qsl(body.decode())}
        if f.get("token_auth") != MATOMO_TOKEN:
            return _json(401, {"result": "error",
                               "message": "You can't access this resource as it requires view "
                                          "access for at least one website."})
        m = s["matomo"]
        meth = f.get("method")
        if meth == "SitesManager.getSitesIdFromSiteUrl":
            url = f.get("url", "")
            return _json(200, [{"idsite": str(x["idsite"])} for x in m["sites"].values()
                               if url in x["urls"]])
        if meth == "SitesManager.addSite":
            urls = [v for k, v in sorted(f.items()) if k.startswith("urls[")]
            if not f.get("siteName") or not urls:
                return _json(200, {"result": "error", "message": "siteName and urls are required"})
            sid = m["next_id"]
            m["next_id"] += 1
            m["sites"][str(sid)] = {"idsite": sid, "urls": urls, "name": f["siteName"]}
            s["creates"]["matomo"] += 1
            s["_created"] = True
            return _json(200, {"value": sid})
        return _json(200, {"result": "error", "message": f"method {meth} not known"})
