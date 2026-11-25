"""The `web` recipe: one HTTP component on two loopback slots behind the generated vhost.

check image → start the idle slot (no traffic reaches it) → verify /healthz reports the new sha
→ switch the vhost to it (edge transaction) → stop the old slot → public check.

Truth after a crash: the slot the installed vhost routes to. reconcile() makes Docker and the
store agree with it before every action (codex #3).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import edge
from . import logs as L
from . import runtime as R
from . import store as S
from .cfg import Platform, Project, Target
from .ext import UA
from . import spec as SP

TAG_RE = re.compile(r"^huta/[a-z][a-z0-9-]*:[0-9a-f]{12}-[0-9a-f]{12}$")
ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SLOTS = ("a", "b")
MAX_BODY = 64 * 1024


class RecipeError(Exception):
    pass


def sh(cmd: list[str], check=True, input_=None, timeout: float = 600) -> str:
    """Every helper command has a deadline (transport review #5)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, input=input_, cwd="/",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RecipeError(f"{' '.join(cmd[:3])}…: no result after {int(timeout)} s")
    if check and r.returncode != 0:
        raise RecipeError(f"{' '.join(cmd[:3])}…: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


# ------------------------------------------------------------------ names (unambiguous)
# Project and target names can't contain "_", so "_" separates them without collisions
# (codex #15: "foo-bar"+"prod" vs "foo"+"bar-prod").

def compose_name(t: Target) -> str:
    return f"huta_{t.project}_{t.name}"


NET_GEN = 2   # bump when the network's options change: compose won't alter a live network, so a
              # new generation is created next to the old one and the next deploy moves onto it


def network_name(t: Target) -> str:
    return f"{compose_name(t)}_n{NET_GEN}"


def bridge_name(t: Target) -> str:
    return "dp-" + hashlib.sha1(f"{t.project}/{t.name}/{NET_GEN}".encode()).hexdigest()[:12]


def service(slot: str) -> str:
    return f"slot-{slot}"   # independent of component names (codex #4)


# ------------------------------------------------------------------ images

def image_facts(ref: str) -> dict:
    d = json.loads(sh(["docker", "image", "inspect", "--format", "{{json .}}", ref]))
    cfg = d.get("Config") or {}
    return {"id": d["Id"], "user": cfg.get("User") or "", "labels": cfg.get("Labels") or {},
            "arch": d.get("Architecture"), "os": d.get("Os"), "volumes": cfg.get("Volumes") or {}}


def check_image(image_id: str, sha: str, project: str) -> None:
    """Checks the immutable image ID; tags are only names for people (codex #9)."""
    if not ID_RE.match(image_id):
        raise RecipeError(f"bad image id {image_id!r}")
    f = image_facts(image_id)
    if f["id"] != image_id:
        raise RecipeError(f"image {image_id[:19]} not found as itself")
    if f["os"] != "linux" or f["arch"] != "amd64":
        raise RecipeError(f"{image_id[:19]}: {f['os']}/{f['arch']}, expected linux/amd64")
    if f["user"].split(":")[0] in ("", "0", "root"):
        raise RecipeError(f"{image_id[:19]}: image runs as root (USER {f['user'] or 'unset'})")
    if f["labels"].get("org.opencontainers.image.revision") != sha:
        raise RecipeError(f"{image_id[:19]}: revision label does not match {sha[:12]}")
    if f["labels"].get("run.huta.project") != project:
        raise RecipeError(f"{image_id[:19]}: run.huta.project label is not {project!r}")
    if f["volumes"]:
        # Docker would give each VOLUME an anonymous writable volume: storage outside the
        # read-only root and the data quota (codex design review #17).
        raise RecipeError(f"{image_id[:19]}: the image declares VOLUME {sorted(f['volumes'])}; "
                          "remove it (persistent data is the target's data volume)")


# ------------------------------------------------------------------ compose (generated)

def effective(platform: Platform, project: Project, t: Target, app: dict) -> dict:
    """Everything the host config and manifest decide about how a release runs. Stored in the
    plan and recomputed at approval: if it differs, the plan is stale (codex #6). Secret
    values are never in it, only names."""
    comp = SP.public(app)
    return {"uid": project.uid, "slots": list(t.slots), "hosts": t.hosts, "egress": t.egress,
            "limits": t.limits, "cloudflare": t.cloudflare,
            "container_port": int(comp.get("port", 8080)),
            "health": comp.get("health", "/healthz"),
            "drain": t.drain,
            "env": dict(sorted(t.env.items())), "secrets": list(t.secrets),
            "job_secrets": dict(t.job_secrets),
            "upstreams": {u: platform.upstreams[u] for u in t.upstreams},
            "data": ({**t.data, "mount": R.data_mount(app),
                      "concurrent_safe": R.concurrent_safe(app, t)} if t.data else None),
            "jobs": app.get("jobs", []), "smoke": app.get("smoke", []),
            "diag": app.get("diag", []),
            "adopt": t.adopt, "access_log": t.access_log,
            # v2 keys appear only when set, so a v1 target's dict is unchanged (tests/golden).
            **({"edge": t.edge} if t.edge else {}),
            **({"health_timeout": t.health_timeout} if t.health_timeout != 30 else {}),
            **({"migrate": app["migrate"], "migrate_secrets": list(t.migrate_secrets)}
               if app.get("migrate") else {}),
            **({"schema": app["state"]["schema"], "runs_on": app["state"]["runs_on"]}
               if (app.get("state") or {}).get("schema") else {}),
            "compose": compose_doc(platform, project, t, app,
                                   {s: ("<image>", "<sha>") for s in SLOTS}, with_secrets=False)}


def compose_doc(platform: Platform, project: Project, t: Target, app: dict,
                images: dict[str, tuple[str, str]], with_secrets: bool = True) -> dict:
    """images: slot -> (image id, sha). Generated from the schema; repo compose is never used."""
    comp = SP.public(app)
    mount = R.data_mount(app)
    services = {}
    for slot, port in zip(SLOTS, t.slots):
        image, sha = images[slot]
        svc = {
            "image": image,
            "pull_policy": "never",
            **R.hardening(project, t),
            "ports": [f"127.0.0.1:{port}:{int(comp.get('port', 8080))}"],
            "restart": "unless-stopped",
            "stop_grace_period": "10s",
            # Pinned, not the host's default: huta logs reads these files (logs.py).
            "logging": L.SLOT_LOGGING,
            "environment": R.compose_escape(R.environment(platform, project, t, sha, slot,
                                                          with_secrets)),
            "labels": {"run.huta.project": project.name, "run.huta.target": t.name,
                       "run.huta.slot": slot, "run.huta.sha": sha},
            "networks": [f"n{NET_GEN}"],
        }
        if R.needs_gateway(t):
            svc["extra_hosts"] = [f"{R.EGRESS_HOST}:host-gateway"]
        if mount:
            _img, where = R.data_paths(platform, t)
            svc["volumes"] = [{"type": "bind", "source": str(where), "target": mount,
                               "bind": {"create_host_path": False}}]
        services[service(slot)] = svc
    # A named key per generation, never `default`: compose treats `default` as the project's own
    # network and tries to delete and recreate it when its options change, which fails while the
    # live slot is attached (found 2026-11-09). A new key is simply created alongside.
    return {
        "name": compose_name(t),
        "services": services,
        "networks": {f"n{NET_GEN}": {
            "name": network_name(t),
            # ICC off: the new (untrusted) slot can't reach the live slot over the bridge
            # (grok #2). Egress and host services are blocked by the host firewall, which
            # accepts nothing from dp-* bridges except the egress daemon's ports.
            "driver_opts": {"com.docker.network.bridge.name": bridge_name(t),
                            "com.docker.network.bridge.enable_icc": "false"},
        }},
    }


def compose_file(platform: Platform, t: Target) -> Path:
    d = platform.state_dir / "compose" / t.project / t.name
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d / "compose.yaml"


def write_compose(platform: Platform, t: Target, doc: dict) -> None:
    edge._atomic_write(compose_file(platform, t), json.dumps(doc, indent=2) + "\n", 0o600)


def compose(platform: Platform, t: Target, *args: str) -> str:
    if "up" in args:
        # `up` may recreate a slot container, and its Docker log goes with it.
        L.collect_quietly(platform, t)
    return sh(["docker", "compose", "-p", compose_name(t), "-f", str(compose_file(platform, t)), *args])


def slot_containers(t: Target) -> dict[str, dict]:
    """What Docker actually runs for this target, by slot."""
    ids = sh(["docker", "ps", "-aq", "--no-trunc",
              "--filter", f"label=run.huta.project={t.project}",
              "--filter", f"label=run.huta.target={t.name}"]).split()
    res = {}
    if not ids:
        return res
    for c in json.loads(sh(["docker", "inspect", *ids])):
        labels = (c.get("Config") or {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != compose_name(t):
            continue  # same labels but another compose project: not ours to manage
        slot = labels.get("run.huta.slot")
        if slot in SLOTS:
            running = bool((c.get("State") or {}).get("Running"))
            if slot in res and res[slot]["running"] and not running:
                continue   # two containers claim the slot: the running one is what matters
            res[slot] = {"id": c["Id"], "running": running,
                         "image": c.get("Image"), "sha": labels.get("run.huta.sha")}
    return res


# ------------------------------------------------------------------ health

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise urllib.error.HTTPError(a[0].full_url, a[2], "redirect refused", a[4], None)


# No proxies, ever: an http_proxy in the environment must not answer the health check that
# authorises a traffic switch (grok review #4). No redirects either.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _get_json(url: str, timeout: float, headers=None) -> dict:
    """GET with a wall-clock deadline that also covers a slowly dribbled body (grok #6)."""
    deadline = time.monotonic() + max(0.5, timeout)
    req = urllib.request.Request(url, headers=headers or {})
    with _OPENER.open(req, timeout=max(0.5, timeout)) as r:
        body = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("health answer too slow")
            try:
                r.fp.raw._sock.settimeout(left)       # type: ignore[attr-defined]
            except AttributeError:
                pass
            chunk = r.read(4096)
            if not chunk:
                break
            body += chunk
            if len(body) > MAX_BODY:
                raise ValueError("health body too large")
    return json.loads(body)


def wait_health(port: int, path: str, sha: str, timeout: float = 30) -> None:
    """The image is untrusted: no redirects, capped body, a real total deadline (codex #12)."""
    deadline = time.monotonic() + timeout
    last = "no answer"
    while (left := deadline - time.monotonic()) > 0:
        try:
            body = _get_json(f"http://127.0.0.1:{port}{path}", min(3, left))
            if body.get("ok") is True and body.get("sha") == sha:
                return
            last = f"reports ok={body.get('ok')!r} sha={str(body.get('sha'))[:12]}"
        except Exception as e:  # noqa: BLE001 — any failure is "not ready yet"
            last = type(e).__name__
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise RecipeError(f"127.0.0.1:{port}{path} never reported {sha[:12]} ({last})")


SMOKE_BODY = 1 << 20


def http_check(url: str, check: dict, host_header: str | None, timeout: float = 10) -> str | None:
    """One [[smoke]] check. None if it passed, else what was wrong. The app is untrusted: a real
    wall-clock deadline for headers and body (each socket read is bounded by what is left),
    no redirects, no proxies, the response always closed (codex code review #11)."""
    headers = {"User-Agent": UA, "Accept-Encoding": "identity", **check.get("request_headers", {})}
    if host_header:
        headers.update({"Host": host_header, "X-Forwarded-Proto": "https"})
    else:
        headers["Cache-Control"] = "no-cache"
    deadline = time.monotonic() + timeout
    resp = None
    try:
        try:
            resp = _OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as e:
            resp = e
        status = resp.code
        hdrs = resp.headers
        body = b""
        fp = getattr(resp, "fp", None)
        if fp is not None and check.get("contains"):
            while len(body) <= SMOKE_BODY:
                left = deadline - time.monotonic()
                if left <= 0:
                    return "answer too slow"
                try:
                    resp.fp.raw._sock.settimeout(left)       # type: ignore[attr-defined]
                except AttributeError:
                    pass
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                body += chunk
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:160]
    finally:
        try:
            if resp is not None:
                resp.close()
        except Exception:  # noqa: BLE001
            pass
    if status != check["status"]:
        return f"status {status}, expected {check['status']}"
    for name, want in check.get("headers", {}).items():
        got = ", ".join(hdrs.get_all(name) or []) if hdrs else ""
        if want not in got:
            return f"header {name} does not contain {want!r} (got {got[:80]!r})"
    for name in check.get("headers_absent", []):
        got = hdrs.get_all(name) if hdrs else None
        if got:
            return f"header {name} must be absent (got {', '.join(got)[:80]!r})"
    if check.get("contains") and check["contains"].encode() not in body[:SMOKE_BODY]:
        return f"body does not contain {check['contains']!r}"
    return None


def smoke(base: str, checks: list[dict], host_header: str | None) -> list[str]:
    """Every check against base; the list of failures (empty: all passed)."""
    out = []
    for c in checks:
        why = http_check(base + c["path"], c, host_header)
        if why:
            out.append(f"{c['path']}: {why}")
    return out


def public_check(host: str, path: str, sha: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while (left := deadline - time.monotonic()) > 0:
        try:
            # Cloudflare answers 403 to Python's default User-Agent (2026-11-09).
            body = _get_json(f"https://{host}{path}", min(10, left),
                             {"Cache-Control": "no-cache", "User-Agent": UA,
                              "Accept-Encoding": "identity"})
            if body.get("sha") == sha:
                return
            last = f"reports {str(body.get('sha'))[:12]}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"[:120]
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RecipeError(f"https://{host}{path}: {last}")


# ------------------------------------------------------------------ one-writer phase marker
# A one-writer release stops the live slot before the new one starts. What reconcile must undo
# after a crash depends on how far it got, so it is written down first (codex code review #1).

def _phase_file(platform: Platform, t: Target) -> Path:
    d = platform.state_dir / "phases"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / f"{t.project}--{t.name}.json"


def _phase_set(platform: Platform, t: Target, **rec) -> None:
    edge._atomic_write(_phase_file(platform, t), json.dumps(rec) + "\n", 0o600)


# Phase records: v1 ones have no "v" and a stage of None (one writer, before the switch),
# "switching" or "checks". A record this huta can't resolve is never cleared: a human looks
# (DESIGN-v2 §0). Later milestones add their stages here together with what reconcile does.
PHASE_VERSION = 2
KNOWN_STAGES = {None, "switching", "checks",
                "migrating", "migrated",      # DESIGN-v2 §2: exclusive migrations
                "restoring"}                  # DESIGN-v2 §2: restore plans


def _phase_get(platform: Platform, t: Target) -> dict | None:
    f = _phase_file(platform, t)
    try:
        text = f.read_text()
    except FileNotFoundError:
        return None
    try:
        rec = json.loads(text)
        if not isinstance(rec, dict):
            raise ValueError("not an object")
    except ValueError as e:
        raise RecipeError(f"{f} is unreadable ({e}): needs a human") from None
    return rec


def _phase_unresolvable(phase: dict | None) -> str | None:
    if not phase:
        return None
    v = phase.get("v", 1)
    if not isinstance(v, int) or v > PHASE_VERSION:
        return f"phase record version {v!r} is newer than this huta"
    if phase.get("stage") not in KNOWN_STAGES:
        return f"phase stage {phase.get('stage')!r} is unknown to this huta"
    return None


def _phase_clear(platform: Platform, t: Target) -> None:
    _phase_file(platform, t).unlink(missing_ok=True)


def stopped_marker(platform: Platform, t: Target) -> Path:
    """`huta stop` was run: reconcile must not start the routed slot again."""
    d = platform.state_dir / "phases"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / f"{t.project}--{t.name}.stopped"


def import_marker(platform: Platform, t: Target) -> Path:
    d = platform.state_dir / "phases"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / f"{t.project}--{t.name}.import"


# ------------------------------------------------------------------ reconcile

def reconcile(platform: Platform, st: S.Store, project: Project, t: Target, log) -> None:
    """Make the store and Docker agree with what the vhost routes to. Hold the target lock.
    Starts only the routed slot of the live release (if it was stopped); stops only slots
    nothing routes to; thaws the data volume (a backup interrupted while it was frozen)."""
    if t.kind == "gateway":
        return       # nothing runs; its edge changes are journaled like every vhost change
    if t.data:
        from .data import thaw
        thaw(R.data_paths(platform, t)[1])
    with edge.edge_lock(platform):
        restored = edge.recover(platform)
        for msg in restored:
            st.event("recovered", msg, None, project.name, t.name)
            log(msg)
        port = edge.routed_port(platform, t)
    why = ("interrupted during the switch; edge restored to the previous release" if restored
           else "interrupted before the switch")
    routed = dict(zip(t.slots, SLOTS)).get(port) if port else None
    cons = slot_containers(t)
    live = st.live(project.name, t.name)

    if import_marker(platform, t).exists():
        raise RecipeError("a data import was interrupted: run huta data import again (nothing "
                          "of this target starts until it completes)")
    try:
        why_not = _phase_unresolvable(_phase_get(platform, t))
    except RecipeError as e:
        why_not = str(e)
    if why_not:
        st.event("needs-human", f"reconcile stopped: {why_not}", None, project.name, t.name)
        raise RecipeError(f"{why_not}; the phase file is kept: needs a human")
    ph0 = _phase_get(platform, t)
    if ph0 and ph0.get("stage") == "restoring":
        # An approved restore was interrupted: do it again (it is idempotent), then the target
        # stays stopped as the plan said. The plan is recorded as applied.
        log("reconcile: finishing an interrupted restore")
        _finish_restore(platform, st, project, t, ph0, log)
        p = st.plan_row(ph0["rid"])
        if p and p["state"] == S.APPROVED:
            st.plan_transition(ph0["rid"], S.APPROVED, "applied",
                               "data restored (finished by reconcile); the target is stopped")
    # Plans (not releases) of this target that were approved but never recorded an outcome: the
    # process died. A restore that got as far as restoring counts as applied; anything else is
    # failed (an edge change was undone by the edge journal, so plan it again).
    for p in st.db.execute("SELECT id, kind FROM plans WHERE project=? AND target=? AND state=?",
                           (project.name, t.name, S.APPROVED)).fetchall():
        done = p["kind"] == "restore" and st.db.execute(
            "SELECT 1 FROM events WHERE release_id=? AND kind='restored'", (p["id"],)).fetchone()
        if done:
            st.plan_transition(p["id"], S.APPROVED, "applied", "data restored (recorded by "
                               "reconcile); the target is stopped")
        else:
            st.plan_transition(p["id"], S.APPROVED, "failed", "interrupted before it finished "
                               "(reconcile); plan it again")

    if routed is None:
        if live and edge.adopt_pending(platform, t) is not None:
            # An adopted site's revert put the old file back but died before recording it.
            # Stop first, record, then clear the phase: a crash in between repeats this branch.
            for slot, c in cons.items():
                if c["running"]:
                    stop_container(c["id"])
            st.revert_live(live["release_id"], None, None,
                           "reverted to the site's old config (finished by reconcile)")
            _phase_clear(platform, t)
            log("reconcile: finished an interrupted revert to the adopted site's old config")
            return
        if live:
            # The site answers 503 while the store believes something is live: stop here
            # instead of reporting success (grok #8).
            st.event("needs-human", "store has a live release but the vhost routes nowhere",
                     live["release_id"], project.name, t.name)
            raise RecipeError("a release is live but the vhost routes nowhere: needs a human "
                              "(huta edge --apply, approved, re-routes it)")
        for slot, c in cons.items():
            if c["running"] and not live:
                stop_container(c["id"])
                log(f"reconcile: stopped unrouted slot {slot}")
        phase = _phase_get(platform, t)
        if phase and phase.get("copied"):
            # A first release that copied the data (to migrate it) died before its switch:
            # nothing else ran since the copy, so putting it back loses nothing.
            sh(["docker", "rm", "-f", migrate_name(t)], check=False)
            from .data import restore_copy
            restore_copy(platform, project, t, log)
            _reset_schema(st, t, phase)
            _phase_set(platform, t, **{**phase, "copied": False, "restored": True,
                                       "migrated": False})
        for r in st.in_state(project.name, t.name, S.RUNNING, S.APPROVED):
            st.set_state(r["id"], S.NOTHING_ACTIVATED, msg="interrupted before any traffic")
        # A first release (or an adoption) that died before its switch: nothing ran with data
        # (no copy is taken without a live release), so the phase has nothing left to undo. Left
        # in place it would block `huta data import` (crash tests).
        _phase_clear(platform, t)
        return

    c = cons.get(routed)
    phase = _phase_get(platform, t)
    if phase and phase.get("idle") != routed:
        # A one-writer release died before its switch. Stop the new slot (proven) before the
        # old one may run, put the pre-release copy back if the new slot could have written,
        # then the routed slot starts below. A migration that may still run goes first.
        if phase.get("migrated") or phase.get("stage") in ("migrating", "migrated"):
            sh(["docker", "rm", "-f", migrate_name(t)], check=False)
        for slot, oc in cons.items():
            if slot != routed and oc["running"]:
                stop_container(oc["id"])
                if slot_containers(t).get(slot, {}).get("running"):
                    raise RecipeError(f"slot {slot} would not stop: needs a human")
                log(f"reconcile: stopped the interrupted release's slot {slot}")
        if phase.get("copied"):
            from .data import restore_copy
            restore_copy(platform, project, t, log)
            _reset_schema(st, t, phase)
            # Restored: never again, even if starting the old slot fails (codex final #1).
            _phase_set(platform, t, **{**phase, "copied": False, "restored": True,
                                       "migrated": False})
        _phase_clear(platform, t)
        cons = slot_containers(t)
        c = cons.get(routed)
    elif phase and phase.get("stage") in ("switching", "checks") and phase.get("idle") == routed:
        # Died during the public checks: run them now, and revert or hand over as run() would.
        # The other slot is not touched before that (it may be what we go back to).
        log("reconcile: finishing the public checks of an interrupted release")
        if phase.get("stage") == "switching":
            # The switch went through but wasn't recorded: record it, then check.
            prev_live = st.live(project.name, t.name)
            if not prev_live or prev_live["release_id"] != phase["rid"]:
                st.make_live(phase["rid"], routed, phase.get("prev"),
                             f"live on slot {routed} (switch finished by reconcile)")
            stopped_marker(platform, t).unlink(missing_ok=True)   # as run(): a release starts it
            _phase_set(platform, t, **{**phase, "stage": "checks", "copied": False})
        out = after_switch(platform, st, project, t, log)
        if out is not None:
            log(f"reconcile: {phase['rid']} ended {out}")
            # The phase is gone: one more pass puts slots, policies and timers in line with
            # whatever now serves (a revert moved the route; needs-human keeps it).
            return reconcile(platform, st, project, t, log)
        cons = slot_containers(t)
        c = cons.get(routed)
    elif phase and phase.get("stage") is None:
        _phase_clear(platform, t)            # it switched; the new slot is the routed one
    stopped = stopped_marker(platform, t).exists()   # `huta stop`: leave the slot stopped
    if not stopped and c and not c["running"] and live and live["slot"] == routed:
        # A non-concurrent release stops the old slot before the switch; if huta died there,
        # the routed slot is still the one that should serve: start it again.
        start_container(c["id"])
        st.event("recovered", f"started the routed slot {routed} again", live["release_id"],
                 project.name, t.name)
        log(f"reconcile: started the routed slot {routed} again")
        c["running"] = True
    if not c or (not c["running"] and not stopped):
        st.event("needs-human", f"vhost routes to slot {routed} but its container is not running",
                 None, project.name, t.name)
        raise RecipeError(f"slot {routed} is routed but not running: needs a human")
    # Which release is that container? Match by image id and sha, in ANY state: a release that
    # failed after the switch (needs-human) is still the one serving, and must be adoptable
    # (grok #1). Prefer the live pointer, then the most recent attempt.
    candidates = st.db.execute(
        "SELECT * FROM releases WHERE project=? AND target=? AND image_id=? AND sha=?"
        " ORDER BY created DESC", (project.name, t.name, c["image"], c["sha"])).fetchall()
    serving = next((r for r in candidates if live and r["id"] == live["release_id"]), None) or \
        next((r for r in candidates if r["state"] in (S.RUNNING, S.LIVE, S.NEEDS_HUMAN)), None) or \
        (candidates[0] if candidates else None)
    if serving is None:
        raise RecipeError(f"slot {routed} runs an image no release owns: needs a human")
    keep = live and live["release_id"] == serving["id"] and serving["state"] == S.NEEDS_HUMAN
    if not keep and (not live or live["release_id"] != serving["id"] or serving["state"] != S.LIVE):
        prev = live["release_id"] if live else None
        note = (" (interrupted after the switch: its public checks did not run)"
                if serving["state"] == S.RUNNING else "")
        # A revert interrupted after the vhost went back: the failed release must not become
        # `retired` (huta rollback's target; grok #12). Its new state goes in the same
        # transaction as the live pointer, so a crash can't leave it retired (crash tests).
        failed = bool(prev and prev != serving["id"] and st.db.execute(
            "SELECT 1 FROM events WHERE release_id=? AND kind='public-fail'", (prev,)).fetchone())
        st.make_live(serving["id"], routed, prev,
                     f"reconciled: slot {routed} serves this release{note}",
                     retire_as=S.ROLLED_BACK if failed else S.RETIRED,
                     retire_msg="reverted (finished by reconcile)")
        log(f"reconcile: {serving['id']} is live on slot {routed}")
    for r in st.in_state(project.name, t.name, S.RUNNING, S.APPROVED):
        if r["id"] != serving["id"]:
            st.set_state(r["id"], S.NOTHING_ACTIVATED, msg=why)
    for slot, oc in cons.items():
        if slot != routed and oc["running"]:
            stop_container(oc["id"])
            log(f"reconcile: stopped unrouted slot {slot}")
    for slot, oc in slot_containers(t).items():
        if slot != routed and not oc["running"]:
            _idle_policy(t, slot)
    # The timers follow the live release (a crash after the switch could leave them stale).
    try:
        from . import jobs as J
        live = st.live(project.name, t.name)
        app_live = json.loads(st.release(live["release_id"])["app_toml"]) if live else {}
        for m in J.sync_units(t, app_live.get("jobs", [])):
            log(f"reconcile: {m}")
    except Exception as e:  # noqa: BLE001
        log(f"WARNING reconcile: timers not updated: {e}")


# ------------------------------------------------------------------ migrations (DESIGN-v2 §2)

def schema_now(st: S.Store, t: Target):
    """The schema the target's data is at, or None (never versioned)."""
    r = st.resource(t.project, t.name, "data")
    return r["schema"] if r else None


def migration(st: S.Store, t: Target, app: dict) -> dict | None:
    """What this release does to the data schema: None (nothing to record), or
    {"from": S, "to": M, "run": bool}. Raises RecipeError for a release that can't run on the
    target's data: a lower schema (that is a restore plan), no schema on versioned data, or a
    schema change without [migrate]."""
    st_cfg = app.get("state") or {}
    m = st_cfg.get("schema")
    s = schema_now(st, t)
    if m is None:
        if s is not None:
            raise RecipeError(f"the target's data is at schema {s}, and this release declares "
                              "none ([state] schema): it can't tell whether its code runs on it")
        return None
    if s is not None and m < s:
        if s in (st_cfg.get("runs_on") or []):
            return {"from": s, "to": s, "run": False}   # older code that runs on newer data
        raise RecipeError(f"this release is for schema {m}, and the data is at {s}: going back "
                          f"needs a restore (huta restore {t.project} {t.name} --to <release>), "
                          "never a migration down")
    if s == m:
        return {"from": s, "to": m, "run": False}
    if not app.get("migrate"):
        if s is None:
            return {"from": None, "to": m, "run": False}     # starts versioning: nothing to run
        raise RecipeError(f"this release moves the data from schema {s} to {m} but has no "
                          "[migrate] to do it")
    return {"from": s, "to": m, "run": True}


def migrate_name(t: Target) -> str:
    return f"huta-migrate-{t.project}-{t.name}"


def _docker_run(argv: list[str], env: dict, timeout: float) -> tuple[int, str]:
    """Run a one-off container; values reach it by name from this call's environment, never on
    the command line."""
    r = subprocess.run(argv, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", **env},
                       capture_output=True, text=True, timeout=timeout, cwd="/")
    return r.returncode, (r.stdout + r.stderr)


def run_migrate(platform: Platform, st: S.Store, project: Project, t: Target, app: dict,
                rel, mig: dict, log) -> None:
    """The release's [migrate] command, once, in its own container: the release's named image,
    the target's limits and data volume, no network, only the registry's migrate_secrets
    (DESIGN-v2 §2). Raises RecipeError on failure or timeout."""
    from .cfg import redact
    m = app["migrate"]
    image = st.images(rel).get(m["image"], rel["image_id"])
    name = migrate_name(t)
    env = {"HUTA_PROJECT": project.name, "HUTA_TARGET": t.name, "RELEASE_SHA": rel["sha"],
           "HUTA_SCHEMA_FROM": str(mig["from"] or 0), "HUTA_SCHEMA_TO": str(mig["to"])}
    for n in t.migrate_secrets:
        env[n] = R.secret_value(platform, project.name, n)
    labels = {"run.huta.project": project.name, "run.huta.target": t.name,
              "run.huta.migrate": rel["id"], "run.huta.sha": rel["sha"]}
    sh(["docker", "rm", "-f", name], check=False)
    argv = ["docker", "run", "--rm", *R.docker_run_args(platform, project, t, app, "none", env,
                                                       name, labels), image, *m["cmd"]]
    log(f"migrating the data: schema {mig['from'] if mig['from'] is not None else 'unversioned'} "
        f"→ {mig['to']} ({' '.join(m['cmd'])}, at most {m['timeout']})")
    try:
        rc, out = _docker_run(argv, env, m["timeout_s"])
    except subprocess.TimeoutExpired:
        sh(["docker", "rm", "-f", name], check=False)
        raise RecipeError(f"the migration did not finish within {m['timeout']}") from None
    tail = redact(out)[-1500:]
    st.event("migrate", f"exit {rc}: {tail}" if tail else f"exit {rc}", rel["id"],
             project.name, t.name)
    if rc != 0:
        raise RecipeError(f"the migration failed (exit {rc}): {tail[-300:]}")
    log(f"migration done: the data is at schema {mig['to']}")


def restore_point(platform: Platform, st: S.Store, project: Project, t: Target, pid: str,
                  plan: dict, log) -> None:
    """Put a pinned copy back (an approved restore plan, DESIGN-v2 §2): mark the target stopped
    (so nothing starts it on data it may not run on), stop every slot and any migration, restore,
    record the point's schema. The phase lets reconcile finish it after a crash. Hold the target
    lock."""
    from . import data as D
    if jobs_running(t):
        raise RecipeError("a job of this target is running; restore when it has finished")
    _phase_set(platform, t, v=PHASE_VERSION, stage="restoring", rid=pid, idle=None,
               point=plan["point"], path=plan["path"], fingerprint=plan["fingerprint"],
               schema=plan["schema"])
    _finish_restore(platform, st, project, t, _phase_get(platform, t), log)


def _finish_restore(platform, st, project, t, phase: dict, log) -> None:
    from pathlib import Path as _P
    from . import data as D
    stopped_marker(platform, t).write_text(f"stopped for restore {phase['rid']}\n")
    sh(["docker", "rm", "-f", migrate_name(t)], check=False)
    for slot, c in slot_containers(t).items():
        if c["running"]:
            stop_container(c["id"])
        if slot_containers(t).get(slot, {}).get("running"):
            raise RecipeError(f"slot {slot} would not stop: not restoring")
    D.restore_pin(platform, project, t, _P(phase["path"]), phase["fingerprint"], log)
    st.set_resource_schema(t.project, t.name, "data", phase["schema"], phase["rid"])
    st.event("restored", f"data restored to restore point {phase['point']}; the target is "
             "stopped until a release starts it", phase["rid"], t.project, t.name)
    _phase_clear(platform, t)


def prune_restore_points(platform: Platform, st: S.Store, t: Target, log) -> None:
    """Keep the newest restore points (at least 2, else keep_releases); drop older ones. Never
    fails a release."""
    from . import data as D
    keep = max(2, t.keep_releases)
    rows = st.db.execute("SELECT id FROM restore_points WHERE project=? AND target=? AND"
                         " state='kept' ORDER BY created DESC", (t.project, t.name)).fetchall()
    for r in rows[keep:]:
        try:
            D.drop_pin(platform, t, r["id"])
            st.db.execute("UPDATE restore_points SET state='dropped' WHERE id=?", (r["id"],))
            log(f"restore point {r['id']} dropped (keeping the newest {keep})")
        except Exception as e:  # noqa: BLE001
            log(f"WARNING restore point {r['id']} not dropped: {e}")


def _reset_schema(st: S.Store, t: Target, phase: dict) -> None:
    """A migration that may have run is undone by a restore: the recorded schema goes back."""
    if phase.get("migrated") or phase.get("stage") in ("migrating", "migrated"):
        st.set_resource_schema(t.project, t.name, "data", phase.get("schema_from"),
                               phase.get("rid"))


# ------------------------------------------------------------------ run

def _egress_sync(platform: Platform, log) -> None:
    """The egress daemon learns the subnets of networks that now exist."""
    from . import egress
    from .cfg import load_registry
    try:
        if egress.sync(platform, load_registry(platform=platform)):
            log("egress policy updated")
    except Exception as e:  # noqa: BLE001 — never blocks a release; the app sees refusals
        log(f"WARNING egress policy not updated: {type(e).__name__}: {e}")


def _bring_back(platform, t, slot: str, port: int, health: str, sha: str, log) -> None:
    """Start the old slot again (one-writer releases stop it first) and wait for it. Its own
    stopped container is started as it was: recreating it from the current host config could
    change what it runs with (env, secrets) at the worst moment."""
    c = slot_containers(t).get(slot)
    if c and c.get("sha") == sha:
        start_container(c["id"])
    else:
        compose(platform, t, "up", "-d", "--no-deps", "--pull", "never", service(slot))
    wait_health(port, health, sha, timeout=60)
    log(f"old slot {slot} is serving again")


def run(platform: Platform, st: S.Store, project: Project, t: Target, rid: str, cert, cf_ranges,
        log) -> str:
    """Hold the target lock. The release is already APPROVED → RUNNING by the caller.

    Concurrent (stateless, or data with concurrent_safe): start the idle slot → health → smoke
    on loopback → switch → public checks (old slot still running) → drain → stop old.
    Not concurrent (data, the default): backup → stop the old slot → start the new one → health
    → smoke → switch → public checks. A failure before the switch brings the old slot back
    (nothing-activated); after it, the vhost goes back to the old slot (rolled-back)."""
    from . import data as D
    from . import jobs as J
    rel = st.release(rid)
    app = json.loads(rel["app_toml"])
    health = SP.health(app)
    checks = app.get("smoke", [])
    check_image(rel["image_id"], rel["sha"], project.name)
    has_data = bool(R.data_mount(app))
    if has_data and not t.data:
        raise RecipeError("app.toml mounts data, but the host registry gives this target none")
    live = st.live(project.name, t.name)
    port_of = dict(zip(SLOTS, t.slots))
    cur = None
    mig = migration(st, t, app)
    migrating = bool(mig and mig["run"])
    # A migration runs exclusive: no release writes while it runs (DESIGN-v2 §2).
    concurrent = R.concurrent_safe(app, t) and not migrating
    if live:
        cur = st.release(live["release_id"])
        # Overlap needs both releases to allow it (codex code review #4).
        concurrent = concurrent and R.concurrent_safe(json.loads(cur["app_toml"]), t)
        live_slot = live["slot"]
        idle = "b" if live_slot == "a" else "a"
        images = {live_slot: (cur["image_id"], cur["sha"]), idle: (rel["image_id"], rel["sha"])}
    else:
        live_slot, idle = None, "a"
        images = {s: (rel["image_id"], rel["sha"]) for s in SLOTS}
    adopting = not live and edge.adopt_pending(platform, t) is not None
    # A stopped target (huta stop, or a restore) stays stopped if this release fails: the old
    # slot may not belong on the data any more.
    was_stopped = stopped_marker(platform, t).exists()
    st.set_state(rid, S.RUNNING, slot=idle, msg=f"starting slot {idle} on 127.0.0.1:{port_of[idle]}")

    if has_data:
        try:
            log(f"data: {D.ensure(platform, project, t)}")
            if live and concurrent and t.data["backup"] != "off":
                # A local copy only: the release must not wait on (or fail by) off-site shipping.
                log(f"backup before the release: {D.backup(platform, t, st, log, do_ship=False)}")
        except D.DataError as e:
            st.set_state(rid, S.NOTHING_ACTIVATED, msg=f"data not ready, nothing changed: {e}")
            return S.NOTHING_ACTIVATED
    write_compose(platform, t, compose_doc(platform, project, t, app, images))

    # 1. Start and verify the new slot. The vhost does not route to it, so no traffic yet.
    stopped_old = copied = migrated = False
    failing = "new slot failed before any traffic"
    mig_rec = {"schema_from": mig["from"]} if migrating else {}
    try:
        if (live and not concurrent) or migrating:
            if jobs_running(t):
                raise RecipeError("a job of this target is running (one writer at a time); "
                                  "submit again when it has finished")
            _phase_set(platform, t, rid=rid, old=live_slot, idle=idle, copied=False, **mig_rec)
            if live:
                log(f"one writer at a time: stopping slot {live_slot} first (the site answers "
                    "502 until the switch)")
                stopped_old = True    # from here, any failure brings the old slot back (grok #11)
                _stop_strict(platform, t, live_slot)
                st.event("stopped-old", f"slot {live_slot} stopped before starting {idle}", rid,
                         project.name, t.name)
            # Taken after the stop, so restoring it after a failed start loses nothing.
            D.copy_consistent(platform, t, log, quiet=True)
            copied = True
            _phase_set(platform, t, rid=rid, old=live_slot, idle=idle, copied=True, **mig_rec)
        if migrating:
            path, fp = D.pin_copy(platform, t, rid)
            st.db.execute(
                "INSERT INTO restore_points (id, project, target, kind, release_id, path,"
                " fingerprint, schema, created, state) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, project.name, t.name, "data", rid, str(path), fp, mig["from"],
                 time.time(), "kept"))
            st.event("restore-point", f"pinned before migrating (schema {mig['from']}): {rid}",
                     rid, project.name, t.name)
            _phase_set(platform, t, v=PHASE_VERSION, stage="migrating", rid=rid, old=live_slot,
                       idle=idle, copied=True, migrated=True, **mig_rec)
            migrated, failing = True, "the migration failed"
            run_migrate(platform, st, project, t, app, rel, mig, log)
            failing = "new slot failed before any traffic"
            st.set_resource_schema(project.name, t.name, "data", mig["to"], rid)
            _phase_set(platform, t, v=PHASE_VERSION, stage="migrated", rid=rid, old=live_slot,
                       idle=idle, copied=True, migrated=True, **mig_rec)
        compose(platform, t, "up", "-d", "--no-deps", "--pull", "never", "--force-recreate",
                service(idle))
        _egress_sync(platform, log)
        log(f"slot {idle} started, verifying {health} reports {rel['sha'][:12]} (no traffic yet)")
        wait_health(port_of[idle], health, rel["sha"], timeout=t.health_timeout)
        bad = smoke(f"http://127.0.0.1:{port_of[idle]}", checks, t.hosts[0])
        if bad:
            raise RecipeError("smoke on the new slot failed: " + "; ".join(bad[:5]))
        if checks:
            log(f"smoke on the new slot: {len(checks)} checks passed")
    except (RecipeError, D.DataError) as e:
        return _abort_before_switch(platform, st, project, t, app, rid, images, idle, live_slot,
                                    stopped_old, cur, health, port_of, f"{failing}: {e}", log,
                                    copied, mig_rec if migrated else None, was_stopped)

    # 2. Switch traffic: one validated, graceful caddy reload (an adopted site's old file goes
    #    in the same transaction). The intent is recorded first, with what puts the previous
    #    config back (the exact files Caddy served), so a crash at any point after this is
    #    finished by reconcile: by the route actually installed (final reviews: codex #3,
    #    grok #2). A revert replays those files: it needs no certificate check and no call to
    #    Cloudflare (crash-harness reviews).
    undo_pre = []
    if live and not adopting:
        undo_pre = [[str(edge.vhost_path(platform, t)), edge.installed(platform, t)]]
    if adopting:
        vp, fp = edge.vhost_path(platform, t), edge.adopt_pending(platform, t)
        undo_pre = [[str(fp), fp.read_text()]]
        if vp != fp:
            undo_pre.insert(0, [str(vp), vp.read_text() if vp.exists() else None])
    phase_rec = dict(rid=rid, stage="switching", idle=idle, old=live_slot,
                     prev=live["release_id"] if live else None, concurrent=concurrent,
                     stopped_old=stopped_old, copied=copied, adopting=adopting, undo=undo_pre,
                     **({"migrated": True, **mig_rec} if migrated else {}))
    _phase_set(platform, t, **phase_rec)
    try:
        with edge.edge_lock(platform):
            msg, undo = edge.change_vhost(
                platform, t, edge.render_vhost(platform, t, cf_ranges, cert, port_of[idle]),
                adopt=adopting)
            log(msg)
    except edge.EdgeError as e:
        return _abort_before_switch(platform, st, project, t, app, rid, images, idle, live_slot,
                                    stopped_old, cur, health, port_of,
                                    f"switch refused, old config still serving: {e}", log, copied,
                                    mig_rec if migrated else None, was_stopped)
    st.make_live(rid, idle, live["release_id"] if live else None, f"live on slot {idle}")
    stopped_marker(platform, t).unlink(missing_ok=True)     # a release starts it again
    if mig and not mig["run"] and mig["from"] != mig["to"]:
        st.set_resource_schema(project.name, t.name, "data", mig["to"], rid)   # starts versioning
    if migrating:
        prune_restore_points(platform, st, t, log)
    # Until its public checks have passed, the release is not finished (grok #2).
    _phase_set(platform, t, **{**phase_rec, "stage": "checks", "copied": False})
    if adopting:
        # huta edge left the SSL mode alone while the old file served; now the origin is huta's
        # (an Origin CA certificate), so the per-host strict rule goes on.
        from .ext import Cloudflare
        for h in t.hosts:
            try:
                log(f"{h}: {Cloudflare(platform).ensure_host_strict(h)}")
            except Exception as e:  # noqa: BLE001
                log(f"WARNING {h}: SSL strict rule not set: {e}")

    # 3. Public checks while the old slot can still take the traffic back.
    outcome = after_switch(platform, st, project, t, log)
    if outcome is not None:
        return outcome

    # 4. Drain and stop the old slot.
    if live_slot and not stopped_old:
        time.sleep(t.drain)
        _stop_quietly(platform, t, live_slot)
    log(f"live on slot {idle}; old slot {live_slot or '-'} stopped")

    # 5. Jobs and the backup timer follow the live release.
    try:
        for m in J.sync_units(t, app.get("jobs", [])):
            log(m)
    except Exception as e:  # noqa: BLE001 — the release is live; the units are fixable later
        st.event("units-fail", f"timers not updated: {e}", rid, project.name, t.name)
        log(f"WARNING timers not updated: {e} (huta units {project.name} {t.name} retries)")
    else:
        try:
            J.start_after_release(t, app.get("jobs", []), st, rid, log)
        except Exception as e:  # noqa: BLE001 — belt and braces: the release stays live
            log(f"WARNING after-release jobs not started: {e}")
    return S.LIVE


def after_switch(platform, st, project, t, log) -> str | None:
    """Public checks for the release recorded in the phase file (stage "checks"), and what a
    failure means. None: passed (the phase file is cleared). Otherwise the outcome. Used by
    run() and by reconcile after a crash in this step."""
    ph = _phase_get(platform, t)
    rid = ph["rid"]
    rel = st.release(rid)
    app = json.loads(rel["app_toml"])
    health = SP.health(app)
    checks = app.get("smoke", [])
    problems = []
    for host in t.hosts:
        try:
            public_check(host, health, rel["sha"])
            log(f"public check ok: https://{host}{health}")
        except RecipeError as e:
            problems.append(str(e))
    if not problems and checks:
        problems = smoke(f"https://{t.hosts[0]}", checks, None)
        if not problems:
            log(f"public smoke: {len(checks)} checks passed")
    if not problems:
        st.event("public-ok", f"public checks passed for {', '.join(t.hosts)}", rid,
                 project.name, t.name)
        _phase_clear(platform, t)
        return None
    why = "; ".join(problems[:5])
    st.event("public-fail", why, rid, project.name, t.name)
    port_of = dict(zip(SLOTS, t.slots))
    prev = ph.get("prev")
    if (prev and ph.get("concurrent")) or ph.get("adopting"):
        live = {"release_id": prev, "slot": ph.get("old")} if prev else None
        cur = st.release(prev) if prev else None
        undo = [(Path(p), txt) for p, txt in ph.get("undo", [])]
        out = _revert(platform, st, project, t, rid, live, cur, ph["idle"], ph.get("old"),
                      ph.get("stopped_old"), undo, bool(ph.get("adopting")),
                      port_of, health, why, log)
        _phase_clear(platform, t)
        return out
    if prev:
        # One writer at a time: the new release has written the data and taken traffic, so
        # going back to the old code automatically could run it on data it doesn't know
        # (design reviews: codex #5, grok #13). A human decides.
        msg = (f"live, but its public checks failed: {why}. Not switched back automatically "
               f"(the new release has written the data); huta rollback {project.name} "
               f"{t.name} plans the previous image (data is not restored)")
    else:
        msg = f"first release is live but its public checks failed: {why}"
    st.set_state(rid, S.NEEDS_HUMAN, msg=msg)
    log(f"WARNING {msg}")
    _phase_clear(platform, t)
    return S.NEEDS_HUMAN


def _abort_before_switch(platform, st, project, t, app, rid, images, idle, live_slot, stopped_old,
                         cur, health, port_of, why, log, copied=False, mig_rec=None,
                         was_stopped=False) -> str:
    try:
        st.event("slot-state", L.slot_state(t, idle), rid, project.name, t.name)
    except Exception:  # noqa: BLE001 — a note for the human, never the outcome
        pass
    if mig_rec is not None:
        sh(["docker", "rm", "-f", migrate_name(t)], check=False)   # a migration still running
    if stopped_old or copied:
        try:
            _stop_strict(platform, t, idle)     # never two writers on non-concurrent data
        except RecipeError as e2:
            st.set_state(rid, S.NEEDS_HUMAN, msg=f"{why}; and the new slot would not stop: {e2}")
            return S.NEEDS_HUMAN
    else:
        _stop_quietly(platform, t, idle)
    L.collect_quietly(platform, t, log)      # the failed slot's output, readable at once
    if live_slot:
        # The old slot runs with its own manifest (port, mount, health), not the failed one's
        # (codex code review #2).
        old_app = json.loads(cur["app_toml"])
        health = SP.health(old_app)
        write_compose(platform, t, compose_doc(platform, project, t, old_app,
                                               {s: images[live_slot] for s in SLOTS}))
    if stopped_old or copied:
        try:
            if copied:
                from . import data as D
                D.restore_copy(platform, project, t, log)
                ph = _phase_get(platform, t) or {}
                if mig_rec is not None:
                    _reset_schema(st, t, {**ph, "migrated": True, **mig_rec, "rid": rid})
                _phase_set(platform, t, **{**ph, "copied": False, "restored": True,
                                           "migrated": False})
            if stopped_old and not was_stopped:
                _bring_back(platform, t, live_slot, port_of[live_slot], health, cur["sha"], log)
            _phase_clear(platform, t)
        except Exception as e2:  # noqa: BLE001 — any failure here needs a human
            st.set_state(rid, S.NEEDS_HUMAN,
                         msg=f"{why}; and the old state did not come back: {e2}")
            return S.NEEDS_HUMAN
    _phase_clear(platform, t)
    st.set_state(rid, S.NOTHING_ACTIVATED, msg=why)
    return S.NOTHING_ACTIVATED


def _previous_vhost(platform, t, port: int) -> str:
    """For a phase written before huta kept the previous vhost: the installed one, pointed back
    at the old slot. Only that one line changes; anything unexpected is refused."""
    cur = edge.installed(platform, t)
    if not cur or len(edge.UPSTREAM_RE.findall(cur)) != 1:
        raise RecipeError("can't tell what the previous vhost was")
    return edge.UPSTREAM_RE.sub(f"reverse_proxy 127.0.0.1:{port}", cur)


def _revert(platform, st, project, t, rid, live, cur, idle, live_slot, stopped_old, undo, adopting,
            port_of, health, why, log) -> str:
    """Post-switch checks failed: put the previous config back, stop the new slot."""
    log(f"public checks failed ({why}); reverting")
    try:
        if live:
            if stopped_old:
                # Never two writers on non-concurrent data: the new slot stops (and is proven
                # stopped) before the old one starts; the site answers 502 in between.
                _stop_strict(platform, t, idle)
                _bring_back(platform, t, live_slot, port_of[live_slot], health, cur["sha"], log)
            with edge.edge_lock(platform):
                if undo:
                    edge.transact(platform, undo)   # the files Caddy served before the switch
                    log(f"vhost back on slot {live_slot}, caddy reloaded")
                else:
                    log(edge.change_vhost(platform, t,
                                          _previous_vhost(platform, t, port_of[live_slot]))[0])
            st.revert_live(rid, live["release_id"], live_slot,
                           f"reverted to {live['release_id']} on slot {live_slot}: {why}")
        else:
            with edge.edge_lock(platform):
                edge.transact(platform, undo)
            log("adopted site's old config restored")
            st.revert_live(rid, None, None, f"reverted to the site's old config: {why}")
    except Exception as e:  # noqa: BLE001 — whatever it was, the outcome is recorded
        st.set_state(rid, S.NEEDS_HUMAN, msg=f"checks failed ({why}) and the revert failed: "
                                              f"{type(e).__name__}: {e}")
        return S.NEEDS_HUMAN
    _stop_quietly(platform, t, idle)
    return S.ROLLED_BACK


def stop_container(cid: str) -> None:
    """Stop a slot and drop its restart policy: an idle slot is kept (so it can come back exactly
    as it was) but must not look like a container that ought to be running."""
    sh(["docker", "stop", cid])
    sh(["docker", "update", "--restart=no", cid], check=False)


def start_container(cid: str) -> None:
    sh(["docker", "update", "--restart=unless-stopped", cid], check=False)
    sh(["docker", "start", cid])


def _idle_policy(t: Target, slot: str) -> None:
    c = slot_containers(t).get(slot)
    if c and not c["running"]:
        sh(["docker", "update", "--restart=no", c["id"]], check=False)


def jobs_running(t: Target) -> list[str]:
    return sh(["docker", "ps", "-q", "--filter", f"label=run.huta.project={t.project}",
               "--filter", f"label=run.huta.target={t.name}", "--filter", "label=run.huta.job"]).split()


def _stop_strict(platform, t, slot: str) -> None:
    """Stop a slot and prove it is stopped (before another writer may start)."""
    compose(platform, t, "stop", service(slot))
    c = slot_containers(t).get(slot)
    if c and c["running"]:
        raise RecipeError(f"slot {slot} is still running after stop")
    _idle_policy(t, slot)


def _stop_quietly(platform, t, slot):
    try:
        compose(platform, t, "stop", service(slot))
        _idle_policy(t, slot)
    except RecipeError:
        pass


def prune_images(st: S.Store, project: Project, t: Target) -> list[str]:
    """Untag old images of this target. Protects every image a live release of any target,
    an unfinished release, or the newest keep_releases releases still refer to (codex #10)."""
    protect = set()
    for r in st.db.execute("SELECT r.image_id FROM live l JOIN releases r ON r.id = l.release_id"):
        protect.add(r["image_id"])
    for r in st.db.execute("SELECT image_id FROM releases WHERE state IN (?,?,?)",
                           (S.PLANNED, S.APPROVED, S.RUNNING)):
        protect.add(r["image_id"])
    rows = st.db.execute("SELECT * FROM releases WHERE project=? AND target=? ORDER BY created DESC",
                         (project.name, t.name)).fetchall()
    # Count only releases that actually went live, so abandoned plans can't push the rollback
    # target out of the window (grok #7); and always keep the newest retired one.
    seen: list[str] = []
    for r in rows:
        if r["state"] in (S.LIVE, S.RETIRED) and r["image_id"] not in seen:
            seen.append(r["image_id"])
    protect.update(seen[:max(2, t.keep_releases)])
    removed = []
    for r in rows:
        if r["image_id"] in protect or not TAG_RE.match(r["image_tag"]):
            continue
        if subprocess.run(["docker", "image", "rm", r["image_tag"]], capture_output=True).returncode == 0:
            removed.append(r["image_tag"])
    return removed
