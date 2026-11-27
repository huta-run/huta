"""notes: a tiny huta example that uses every v1 feature. Python stdlib only.

/healthz      {"ok": true, "sha": RELEASE_SHA}
/             a page (smoke checks look for its title and its CSP header)
/hit          appends one line to /data/hits.<slot>.log, returns the total over all slots
/egress       fetches https://<NOTES_EGRESS_HOST>/ through the egress proxy (HTTPS_PROXY)
/upstream     opens a TCP connection to HUTA_UPSTREAM_<NAME> (NOTES_UPSTREAM names it)
/secret       says whether the NOTES_KEY secret is set (never its value)
/error        sends one test error to Bugsink (SENTRY_DSN, through the egress proxy), tagged
              with SENTRY_RELEASE and SENTRY_ENVIRONMENT. Loopback only: a request that came
              through Cloudflare gets 404, so the public can't fill Bugsink. Ten of them in a
              release's watch window make it degraded (a way to try ↩️ Roll back).

Test switch (host env, to watch huta refuse or revert a release):
NOTES_FAIL=cf     every request that came through Cloudflare gets 503: loopback checks pass,
                  public checks fail, so huta switches back (rolled-back)
NOTES_FAIL=root   / answers 500: the smoke check on the new slot fails (nothing-activated)
python app.py rotate    the job: gzip-free rotation, keeps the last 3 rotated files
python app.py sanitize  the [[snapshot]] "hits": reads the backup copy at /data and writes only
                        counts (hits per file, no line of the logs) to /snapshot/hits.json
python app.py verify    its check: /snapshot holds exactly hits.json, and it is only numbers
"""

import glob
import json
import os
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("NOTES_DATA", "/data")
SHA = os.environ.get("RELEASE_SHA", "unknown")
SLOT = os.environ.get("HUTA_SLOT", "x")


def hits() -> int:
    n = 0
    for f in glob.glob(f"{DATA}/hits.*.log"):
        with open(f) as fh:
            n += sum(1 for _ in fh)
    return n


class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json", extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        fail = os.environ.get("NOTES_FAIL", "")
        if fail == "cf" and self.headers.get("CF-Connecting-IP"):
            return self.send(503, json.dumps({"error": "NOTES_FAIL=cf"}))
        if fail == "root" and self.path == "/":
            return self.send(500, json.dumps({"error": "NOTES_FAIL=root"}))
        if self.path == "/healthz":
            return self.send(200, json.dumps({"ok": True, "sha": SHA}))
        if self.path == "/":
            return self.send(200, f"<!doctype html><title>notes</title><p>notes {SHA[:12]}</p>",
                             "text/html; charset=utf-8",
                             {"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"})
        if self.path == "/hit":
            with open(f"{DATA}/hits.{SLOT}.log", "a") as f:
                f.write(f"{time.time()}\n")
            return self.send(200, json.dumps({"hits": hits(), "slot": SLOT}))
        if self.path == "/egress":
            host = os.environ.get("NOTES_EGRESS_HOST", "example.org")
            try:
                req = urllib.request.Request(f"https://{host}/",
                                             headers={"User-Agent": "huta-notes-example/1"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    return self.send(200, json.dumps({"host": host, "status": r.status}))
            except Exception as e:  # noqa: BLE001
                return self.send(502, json.dumps({"host": host, "error": f"{type(e).__name__}: {e}"}))
        if self.path == "/upstream":
            name = os.environ.get("NOTES_UPSTREAM", "")
            hp = os.environ.get(f"HUTA_UPSTREAM_{name.upper().replace('-', '_')}", "")
            try:
                h, _, p = hp.rpartition(":")
                socket.create_connection((h, int(p)), timeout=5).close()
                return self.send(200, json.dumps({"upstream": name, "reachable": True}))
            except Exception as e:  # noqa: BLE001
                return self.send(502, json.dumps({"upstream": name, "error": f"{type(e).__name__}"}))
        if self.path == "/error" and not self.headers.get("CF-Connecting-IP"):
            return self.send(*send_test_error())
        if self.path == "/secret":
            return self.send(200, json.dumps({"NOTES_KEY": bool(os.environ.get("NOTES_KEY"))}))
        return self.send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass


def send_test_error() -> tuple[int, str]:
    """One event in the Sentry store format, sent with the standard library (no SDK)."""
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn:
        return 503, json.dumps({"error": "no SENTRY_DSN (app.toml asks; the host must grant)"})
    u = urllib.parse.urlsplit(dsn)
    project = u.path.rstrip("/").rsplit("/", 1)[-1]
    url = f"{u.scheme}://{u.hostname}{f':{u.port}' if u.port else ''}/api/{project}/store/"
    event = {"event_id": uuid.uuid4().hex, "timestamp": time.time(), "platform": "python",
             "level": "error", "logger": "notes",
             "release": os.environ.get("SENTRY_RELEASE", SHA),
             "environment": os.environ.get("SENTRY_ENVIRONMENT", ""),
             "exception": {"values": [{"type": "NotesTestError",
                                       "value": "a test error from notes /error"}]}}
    req = urllib.request.Request(url, data=json.dumps(event).encode(), method="POST", headers={
        "Content-Type": "application/json",
        "X-Sentry-Auth": f"Sentry sentry_version=7, sentry_key={u.username}, "
                         "sentry_client=huta-notes-example/1"})
    try:   # urllib takes the route from HTTPS_PROXY, which huta sets
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200, json.dumps({"sent": event["event_id"], "status": r.status,
                                    "release": event["release"]})
    except Exception as e:  # noqa: BLE001
        return 502, json.dumps({"error": f"{type(e).__name__}: {e}"[:300]})


def rotate():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for f in glob.glob(f"{DATA}/hits.*.log"):
        os.rename(f, f"{f}.{stamp}")
    old = sorted(glob.glob(f"{DATA}/hits.*.log.*"))
    for f in old[:-3]:
        os.remove(f)
    print(f"rotated at {stamp}; {len(old[-3:])} rotated files kept")


def sanitize():
    out = os.environ.get("HUTA_SNAPSHOT_OUT", "/snapshot")
    counts = {}
    for f in sorted(glob.glob(f"{DATA}/hits.*.log*")):
        try:
            with open(f, "rb") as fh:
                counts[os.path.basename(f)] = sum(1 for _ in fh)
        except OSError:
            pass
    with open(f"{out}/hits.json", "w") as fh:
        json.dump({"files": counts, "total": sum(counts.values())}, fh, indent=1, sort_keys=True)
    print(f"{len(counts)} files, {sum(counts.values())} hits")


def verify():
    """The whole result must be counts: one file, exactly these keys, names of hit logs only,
    whole numbers, and a total that adds up."""
    import re
    out = os.environ.get("HUTA_SNAPSHOT_OUT", "/snapshot")
    if os.listdir(out) != ["hits.json"]:
        sys.exit("unexpected files in the snapshot")
    def no_dupes(pairs):
        if len({k for k, _v in pairs}) != len(pairs):
            sys.exit("hits.json repeats a key")       # json.load would keep only the last
        return dict(pairs)
    with open(f"{out}/hits.json") as fh:
        d = json.load(fh, object_pairs_hook=no_dupes)
    files = d.get("files") if isinstance(d, dict) else None
    ok = (isinstance(d, dict) and set(d) == {"files", "total"} and isinstance(files, dict)
          and all(re.fullmatch(r"hits\.[a-z]\.log(\.\d{8}-\d{6})?", k)
                  and type(v) is int and v >= 0 for k, v in files.items())
          and type(d["total"]) is int and d["total"] == sum(files.values()))
    if not ok:
        sys.exit("hits.json holds more than counts")
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["rotate"]:
        rotate()
    elif sys.argv[1:] == ["sanitize"]:
        sanitize()
    elif sys.argv[1:] == ["verify"]:
        verify()
    else:
        # As PID 1 the process gets no default SIGTERM handling: exit cleanly when stopped.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
