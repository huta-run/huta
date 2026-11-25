"""notes: a tiny huta example that uses every v1 feature. Python stdlib only.

/healthz      {"ok": true, "sha": RELEASE_SHA}
/             a page (smoke checks look for its title and its CSP header)
/hit          appends one line to /data/hits.<slot>.log, returns the total over all slots
/egress       fetches https://<NOTES_EGRESS_HOST>/ through the egress proxy (HTTPS_PROXY)
/upstream     opens a TCP connection to HUTA_UPSTREAM_<NAME> (NOTES_UPSTREAM names it)
/secret       says whether the NOTES_KEY secret is set (never its value)

Test switch (host env, to watch huta refuse or revert a release):
NOTES_FAIL=cf     every request that came through Cloudflare gets 503: loopback checks pass,
                  public checks fail, so huta switches back (rolled-back)
NOTES_FAIL=root   / answers 500: the smoke check on the new slot fails (nothing-activated)
python app.py rotate    the job: gzip-free rotation, keeps the last 3 rotated files
"""

import glob
import json
import os
import signal
import socket
import sys
import time
import urllib.request
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
        if self.path == "/secret":
            return self.send(200, json.dumps({"NOTES_KEY": bool(os.environ.get("NOTES_KEY"))}))
        return self.send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass


def rotate():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for f in glob.glob(f"{DATA}/hits.*.log"):
        os.rename(f, f"{f}.{stamp}")
    old = sorted(glob.glob(f"{DATA}/hits.*.log.*"))
    for f in old[:-3]:
        os.remove(f)
    print(f"rotated at {stamp}; {len(old[-3:])} rotated files kept")


if __name__ == "__main__":
    if sys.argv[1:] == ["rotate"]:
        rotate()
    else:
        # As PID 1 the process gets no default SIGTERM handling: exit cleanly when stopped.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
