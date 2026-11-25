"""The request format dev sends to the host, the same for both transports (pull and push).

A request is one small JSON object. Everything in it is untrusted: the host validates it here,
then checks every claim itself (sha on main, manifest from git, image id and labels). Who sent
it is never taken from the request: pull knows its container, push learns the box from the
kernel (the socket peer's uid).

  {"v": 1, "id": "<16 hex>", "op": "submit", "project": "hello", "target": "prod",
   "sha": "<40 hex>", "image": "huta-build/hello:<sha12>"[, "digest": "sha256:<64 hex>"]}
  {"v": 1, "id": "...", "op": "status", "project": "hello", "target": "prod"}
  {"v": 1, "id": "...", "op": "log", "release": "<8 hex>"}
  {"v": 1, "id": "...", "op": "jobs", "project": "hello", "target": "prod"}   (read-only)
  {"v": 1, "id": "...", "op": "logs", "project": "hello", "target": "prod"[, "since": "1h",
   "until": "...", "grep": "...", "tail": 500, "slot": "live", "release": "<8 hex>",
   "job": "<name>"]}                                                (read-only, redacted)
  {"v": 1, "id": "...", "op": "egress-log", "project": "hello", "target": "prod"[, "since", "until"]}
  {"v": 1, "id": "...", "op": "traffic", "project": "hello", "target": "prod"[, "since", "until"]}
  {"v": 1, "id": "...", "op": "crashes", "project": "hello", "target": "prod"[, "since"]}
  {"v": 1, "id": "...", "op": "diag", "project": "hello", "target": "prod", "diag": "<name>"}
  {"v": 1, "id": "...", "op": "credentials"}                       (push only)
  {"v": 1, "id": "...", "op": "client"}                            (the client bundle, for self-update)
  {"v": 1, "id": "...", "op": "secret", "project": "hello", "name": "API_TOKEN", "value": "..."}
                       (stages a new value; it goes live only with a release the owner approves)

Every answer carries "huta": the host's installed revision, so a client can tell it is outdated.
"""

from __future__ import annotations

import json
import re

MAX_REQUEST = 16 * 1024
OPS = {"submit", "status", "log", "jobs", "logs", "egress-log", "traffic", "crashes", "diag",
       "credentials", "client", "secret"}
MAX_SECRET = 8 * 1024    # characters; the client also checks the encoded request fits MAX_REQUEST
NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
FIELDS = {
    "submit": {"project", "target", "sha", "image"},
    "status": {"project", "target"},
    "log": {"release"},
    "jobs": {"project", "target"},   # the live release's jobs and recent run outcomes
    "logs": {"project", "target"},   # the target's container logs, redacted (logs.py)
    "egress-log": {"project", "target"},   # the egress daemon's refusals (observe.py)
    "traffic": {"project", "target"},      # edge summary: status classes per minute, 5xx
    "crashes": {"project", "target"},      # Bugsink issues of the target
    "diag": {"project", "target", "diag"},   # run a diagnostic app.toml declares (diag.py)
    "credentials": set(),          # push only: the box's own registry login
    "client": set(),               # the client this host would install (huta self-update)
    "secret": {"project", "name", "value"},   # stage a value for a secret the registry names
}
OPTIONAL = {"submit": {"digest"},  # push: the manifest digest the box pushed to the registry
            "logs": {"since", "until", "grep", "tail", "slot", "release", "job"},
            "egress-log": {"since", "until"}, "traffic": {"since", "until"},
            "crashes": {"since"}}
WHEN = r"\d{1,6}[smhd]|\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?)?Z?"   # = logs.WHEN_PATTERN
PUSH_ONLY = {"credentials"}
ID = re.compile(r"[0-9a-f]{16}")


class RequestError(Exception):
    pass


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise RequestError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _str(d: dict, k: str, pattern: str) -> None:
    if k in d and not (isinstance(d[k], str) and re.fullmatch(pattern, d[k])):
        raise RequestError(f"bad {k}")


def parse(raw: bytes) -> dict:
    if len(raw) > MAX_REQUEST:
        raise RequestError("request too large")
    try:
        d = json.loads(raw, object_pairs_hook=_no_dupes)
    except RequestError:
        raise
    except (ValueError, RecursionError):
        raise RequestError("request is not JSON")
    if not isinstance(d, dict) or type(d.get("v")) is not int or d["v"] != 1:
        raise RequestError("request must be an object with v = 1")
    if not (isinstance(d.get("id"), str) and ID.fullmatch(d["id"])):
        raise RequestError("id must be 16 hex characters")
    op = d.get("op")
    if not isinstance(op, str) or op not in OPS:
        raise RequestError(f"op must be one of {sorted(OPS)}")
    want = FIELDS[op] | {"v", "id", "op"}
    extra = set(d) - want - OPTIONAL.get(op, set())
    missing = want - set(d)
    if extra or missing:
        raise RequestError(f"fields for {op}: missing {sorted(missing)}, unexpected {sorted(extra)}")
    _str(d, "project", NAME.pattern)
    _str(d, "target", NAME.pattern)
    _str(d, "sha", r"[0-9a-f]{40}")
    if "image" in d:
        _str(d, "image", rf"huta-build/{re.escape(d['project'])}:[0-9a-f]{{12}}")
    _str(d, "release", r"[0-9a-f]{8}")
    _str(d, "digest", r"sha256:[0-9a-f]{64}")
    _str(d, "name", r"[A-Z][A-Z0-9_]{0,63}")
    _str(d, "since", WHEN)
    _str(d, "until", WHEN)
    _str(d, "slot", r"a|b|live|previous")
    _str(d, "job", NAME.pattern)
    _str(d, "diag", NAME.pattern)
    # A plain substring, matched after redaction: no pattern language to hang or probe with.
    _str(d, "grep", r"[^\x00-\x1f\x7f]{1,200}")
    if "tail" in d and not (type(d["tail"]) is int and 1 <= d["tail"] <= 5000):
        raise RequestError("tail must be 1..5000")
    if "value" in d:
        # Never quote the value in an error: errors are logged and answered.
        # One line, no control characters: job output is redacted line by line, so a
        # multi-line value could pass through in pieces (grok secrets #5). A multi-line secret
        # (a PEM key) is set by the operator on the host.
        v = d["value"]
        if (not isinstance(v, str) or not v.strip() or len(v) > MAX_SECRET
                or re.search(r"[\x00-\x1f\x7f]", v)):
            raise RequestError(f"value must be one line of at most {MAX_SECRET} characters, "
                               "without control characters (a multi-line secret is set by the "
                               "operator on the host)")
    return d
