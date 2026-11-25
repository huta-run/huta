"""A fake host for crash tests: huta's real release and recovery code, with Docker, Caddy,
systemd and the data volume replaced by small models whose state lives in files.

Why files: a crash test runs huta in a forked child that dies with os._exit() right after a
chosen step, so nothing after that point runs (no except, no finally, no cleanup), as with a
kill -9. The parent then works on what the child left on disk: the store, the phase files, the
vhost files and edge journal, and the fake Docker, Caddy and data state.

What the models keep apart, because huta must not confuse them:
- the vhost files on disk and the config Caddy has loaded (a reload applies the files);
- a container's running state and its restart policy (a reboot starts what Docker would);
- data writes: a data container writes when it starts, and two data containers of a
  one-writer target running at once is recorded as an overlap.

Steps are recorded by wrapping huta's own functions in the test (no hooks in production code).
Nothing here needs root, Docker or Caddy. The root-ownership checks are skipped (tests run as an
ordinary user on files they own).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import tempfile
import time as _time
import traceback
import types
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from tests._util import CF_RANGES, etc_copy, load

from huta import cfg, data, edge, jobs, recipe, runtime, store
from huta import logs as L
from huta import store as S
from huta.ext import Cloudflare

CRASH_EXIT = 77
# recipe's own view of the time module: sleeps return at once, everything else is real.
_FAST_TIME = types.SimpleNamespace(**{n: getattr(_time, n) for n in dir(_time)
                                      if not n.startswith("_")})
_FAST_TIME.sleep = lambda _s: None


class FakeHost:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="huta-fakehost-"))
        self.etc = etc_copy()
        platform, self.registry = load(self.etc)
        for d in ("state", "sites", "tls", "caddy-log"):
            (self.root / d).mkdir()
        self.platform = dataclasses.replace(
            platform, state_dir=self.root / "state", caddy_sites=self.root / "sites",
            tls_dir=self.root / "tls", caddy_log_dir=self.root / "caddy-log")
        self.cert = (self.root / "tls" / "test.pem", self.root / "tls" / "test.key")
        for p in self.cert:
            p.write_text("test\n")
        self.crash_after: int | None = None     # set only in a crashing child
        self._save("docker", {"containers": {}, "images": {}, "next": 1})
        self._save("caddy", {"loaded": {}})
        self._save("data", {})
        self._save("ops", [])
        self._save("faults", {})

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.etc, ignore_errors=True)

    # ------------------------------------------------------------ state in files

    def _load(self, name):
        return json.loads((self.root / f"{name}.json").read_text())

    def _save(self, name, value):
        p = self.root / f"{name}.json"
        tmp = p.with_name(f".{name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(value, indent=1, sort_keys=True))
        os.replace(tmp, p)

    def ops(self) -> list[str]:
        return self._load("ops")

    def fault(self, **kw):
        """Make something fail:
        unhealthy=<sha>      health never reports that sha
        public_fail=True     public checks fail
        caddy_invalid=<port> validation refuses any config that routes to that port
        no_cert=True         the certificate check finds no usable pair (edge.cert_ok)
        no_cf=True           Cloudflare's IP-range list can't be fetched
        old_phase=True       phase files lack the previous vhost (as an older huta wrote them)
        migrate_fail=True    the [migrate] command exits non-zero (after writing)
        """
        f = self._load("faults")
        f.update(kw)
        self._save("faults", f)

    def _faults(self):
        return self._load("faults")

    def _op(self, name: str):
        """Record a completed step; die right here if this is the crash point."""
        ops = self._load("ops")
        ops.append(name)
        self._save("ops", ops)
        if self.crash_after is not None and self.crash_after == len(ops):
            os._exit(CRASH_EXIT)

    # ------------------------------------------------------------ images

    def add_image(self, project: str, sha: str) -> str:
        d = self._load("docker")
        iid = "sha256:" + (sha * 2)[:64]
        d["images"][iid] = {"Id": iid, "Architecture": "amd64", "Os": "linux",
                            "Config": {"User": "10001", "Volumes": None, "Labels": {
                                "org.opencontainers.image.revision": sha,
                                "run.huta.project": project}}}
        self._save("docker", d)
        return iid

    # ------------------------------------------------------------ docker

    @staticmethod
    def _match(c, filters):
        for f in filters:
            k, _, v = f.removeprefix("label=").partition("=")
            if v and c["labels"].get(k) != v:
                return False
            if not v and k not in c["labels"]:
                return False
        return True

    def sh(self, cmd, check=True, input_=None, timeout=600):
        assert cmd[0] == "docker", f"fake host: unexpected command {cmd}"
        d = self._load("docker")
        args = cmd[1:]
        if args[:2] == ["image", "inspect"]:
            ref = args[-1]
            if ref not in d["images"]:
                raise recipe.RecipeError(f"no such image {ref}")
            return json.dumps(d["images"][ref])
        if args[0] == "ps":
            every = "-a" in args or "-aq" in args
            filters = [args[i + 1] for i, a in enumerate(args) if a == "--filter"]
            return "\n".join(cid for cid, c in d["containers"].items()
                             if self._match(c, filters) and (every or c["running"]))
        if args[0] == "inspect":
            return json.dumps([{"Id": cid, "Image": d["containers"][cid]["image"],
                                "Config": {"Labels": d["containers"][cid]["labels"]},
                                "State": {"Running": d["containers"][cid]["running"]}}
                               for cid in args[1:]])
        if args[0] == "stop":
            c = d["containers"][args[1]]
            c["running"], c["stopped_by_user"] = False, True
            self._save("docker", d)
            self._op(f"docker stop {c['labels']['run.huta.slot']}")
            return ""
        if args[0] == "start":
            c = d["containers"][args[1]]
            c["running"], c["stopped_by_user"] = True, False
            self._save("docker", d)
            self._started(args[1])
            self._op(f"docker start {c['labels']['run.huta.slot']}")
            return ""
        if args[0] == "update":
            pol = next(a.split("=", 1)[1] for a in args if a.startswith("--restart="))
            c = d["containers"][args[-1]]
            if c["restart"] != pol:
                c["restart"] = pol
                self._save("docker", d)
                self._op(f"restart={pol} {c['labels']['run.huta.slot']}")
            return ""
        if args[0] == "compose":
            return self._compose(d, args)
        if args[:2] == ["rm", "-f"] or args[0] == "kill":
            return ""          # the migrate container: it runs to completion in _docker_run
        raise AssertionError(f"fake host: unexpected docker command {cmd}")

    def _compose(self, d, args):
        project = args[args.index("-p") + 1]
        doc = json.loads(Path(args[args.index("-f") + 1]).read_text())
        verb, svc = args[args.index("-f") + 2], args[-1]
        mine = [cid for cid, c in d["containers"].items()
                if c["labels"].get("com.docker.compose.project") == project and c["service"] == svc]
        if verb == "stop":
            for cid in mine:
                d["containers"][cid]["running"] = False
                d["containers"][cid]["stopped_by_user"] = True
            self._save("docker", d)
            self._op(f"compose stop {svc}")
            return ""
        assert verb == "up" and "--no-deps" in args and "--pull" in args, args
        spec = doc["services"][svc]
        # Recreate as compose does: the old container is removed, then the new one created and
        # started (separate steps; a crash can fall between them).
        for cid in mine:
            del d["containers"][cid]
        if mine:
            self._save("docker", d)
            self._op(f"compose removed {svc}")
        cid = f"c{d['next']:04d}"
        d["next"] += 1
        d["containers"][cid] = {
            "service": svc, "image": spec["image"], "running": True, "stopped_by_user": False,
            "restart": spec.get("restart", "no"),
            "labels": {**spec["labels"], "com.docker.compose.project": project},
            "port": int(spec["ports"][0].split(":")[1]), "data": bool(spec.get("volumes"))}
        self._save("docker", d)
        self._started(cid)
        self._op(f"compose up {svc}")
        return ""

    def _started(self, cid):
        """A data container writes at once; a second running writer is an overlap."""
        d = self._load("docker")
        c = d["containers"][cid]
        if not c.get("data"):
            return
        key = c["labels"]["run.huta.project"] + "/" + c["labels"]["run.huta.target"]
        v = self._load("data")
        e = v.setdefault(key, {"writes": [], "copy": None, "overlaps": []})
        others = [o["labels"]["run.huta.sha"] for ocid, o in d["containers"].items()
                  if ocid != cid and o["running"] and o.get("data")
                  and o["labels"]["run.huta.target"] == c["labels"]["run.huta.target"]
                  and o["labels"]["run.huta.project"] == c["labels"]["run.huta.project"]]
        if others:
            e["overlaps"].append([c["labels"]["run.huta.sha"], *others])
        e["writes"].append(c["labels"]["run.huta.sha"])
        self._save("data", v)

    def reboot(self):
        """Docker after a host reboot: running containers stop; those whose policy says so start
        again (unless-stopped: only if not stopped by hand; always: always)."""
        d = self._load("docker")
        for c in d["containers"].values():
            c["running"] = False
        start = [cid for cid, c in d["containers"].items()
                 if c["restart"] == "always"
                 or (c["restart"] == "unless-stopped" and not c["stopped_by_user"])]
        self._save("docker", d)
        for cid in start:
            d = self._load("docker")
            d["containers"][cid]["running"] = True
            self._save("docker", d)
            self._started(cid)
        self._reload_caddy()       # Caddy loads the files on disk at boot

    def containers(self, t) -> list[dict]:
        return [c for c in self._load("docker")["containers"].values()
                if c["labels"].get("run.huta.target") == t.name
                and c["labels"].get("run.huta.project") == t.project]

    def running(self, t) -> dict[str, str]:
        """slot -> sha of each running container of a target (two for one slot: an error)."""
        out = {}
        for c in self.containers(t):
            if c["running"]:
                slot = c["labels"]["run.huta.slot"]
                assert slot not in out, f"two running containers claim slot {slot}"
                out[slot] = c["labels"]["run.huta.sha"]
        return out

    def data_state(self, t) -> dict:
        return self._load("data").get(f"{t.project}/{t.name}",
                                      {"writes": [], "copy": None, "overlaps": []})

    # ------------------------------------------------------------ caddy

    def disk_routes(self) -> dict:
        """What the vhost files on disk say: host -> upstream port (None: the 503 form)."""
        routes = {}
        for f in sorted(self.platform.caddy_sites.glob("*.caddy")):
            text = f.read_text()
            m = re.search(r"reverse_proxy 127\.0\.0\.1:(\d+)", text)
            first = next((ln for ln in text.splitlines() if ln.endswith("{") and "." in ln), "")
            for host in re.findall(r"[a-z0-9.-]+\.[a-z]{2,}", first):
                routes[host] = int(m.group(1)) if m else None
        return routes

    def loaded_routes(self) -> dict:
        return self._load("caddy")["loaded"]

    def _reload_caddy(self):
        """Caddy applies the vhost files on disk."""
        self._save("caddy", {"loaded": self.disk_routes()})

    def loaded_port(self, host: str) -> int | None:
        return self._load("caddy")["loaded"].get(host)

    def edge_run(self, cmd, env=None, check=True, input_=None):
        assert cmd[:2] == ["systemctl", "reload"], f"fake host: unexpected command {cmd}"
        self._reload_caddy()
        self._op("caddy reloaded (journal still pending)")

        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()

    def validate_caddy(self):
        bad = self._faults().get("caddy_invalid")
        if bad and bad in self.disk_routes().values():
            raise edge.EdgeError(f"caddy validate failed: test fault (port {bad})")
        for f in self.platform.caddy_sites.glob("*.caddy"):
            m = re.search(r"\ttls (\S+) (\S+)", f.read_text())
            if m and not (Path(m.group(1)).is_file() and Path(m.group(2)).is_file()):
                raise edge.EdgeError(f"caddy validate failed: {f.name}: certificate files missing")

    def adapt_hosts(self, path: Path) -> set[str]:
        """`caddy adapt` for a hand-written site: the names before its first '{'."""
        first = next(ln for ln in path.read_text().splitlines() if ln.rstrip().endswith("{"))
        return set(re.findall(r"[a-z0-9.-]+\.[a-z]{2,}", first))

    # ------------------------------------------------------------ health and checks

    def _serving(self, port) -> dict | None:
        for c in self._load("docker")["containers"].values():
            if c["running"] and c["port"] == port:
                return c
        return None

    def wait_health(self, port, path, sha, timeout=30):
        c = self._serving(port)
        if not c or c["labels"]["run.huta.sha"] != sha or self._faults().get("unhealthy") == sha:
            raise recipe.RecipeError(f"127.0.0.1:{port}{path} never reported {sha[:12]}")

    def public_check(self, host, path, sha, timeout=30):
        """What a visitor gets: the config Caddy has loaded, not the file on disk."""
        port = self.loaded_port(host)
        c = self._serving(port) if port else None
        if self._faults().get("public_fail") or not c or c["labels"]["run.huta.sha"] != sha:
            raise recipe.RecipeError(f"https://{host}{path} did not report {sha[:12]}")

    def cert_ok(self, *a, **k):
        return None if self._faults().get("no_cert") else self.cert

    def ip_ranges(self):
        if self._faults().get("no_cf"):
            raise OSError("Cloudflare IP ranges: test fault (network down)")
        return list(CF_RANGES)

    # ------------------------------------------------------------ migrate, restore points

    def docker_run(self, argv, env, timeout):
        """The [migrate] container: it writes to the data (as a migration does), then exits."""
        assert argv[:3] == ["docker", "run", "--rm"] and "none" in argv, argv
        t_key = f"{env['HUTA_PROJECT']}/{env['HUTA_TARGET']}"
        v = self._load("data")
        v.setdefault(t_key, {"writes": [], "copy": None, "overlaps": []})["writes"].append(
            f"migrate:{env['RELEASE_SHA']}")
        self._save("data", v)
        running = [c for c in self._load("docker")["containers"].values() if c["running"]
                   and c.get("data") and f"{c['labels']['run.huta.project']}/"
                   f"{c['labels']['run.huta.target']}" == t_key]
        if running:
            v = self._load("data")
            v[t_key]["overlaps"].append([f"migrate:{env['RELEASE_SHA']}",
                                         *[c["labels"]["run.huta.sha"] for c in running]])
            self._save("data", v)
        self._op("migrate ran")
        if self._faults().get("migrate_fail"):
            return 1, "migration error: test fault"
        return 0, "migrated"

    def pin_copy(self, platform, t, rid):
        v = self._load("data")
        e = v[f"{t.project}/{t.name}"]
        e.setdefault("pins", {})[rid] = list(e["copy"])
        self._save("data", v)
        self._op("restore point pinned")
        return self.root / "pins" / rid, "fp-" + rid

    def fingerprint(self, path):
        if self._faults().get("pin_changed"):
            return "changed"
        return "fp-" + Path(path).name

    def restore_pin(self, platform, project, t, path, fp, log):
        if fp != self.fingerprint(path):
            raise data.DataError("restore point changed")
        v = self._load("data")
        e = v[f"{t.project}/{t.name}"]
        e["writes"] = list(e["pins"][Path(path).name])
        self._save("data", v)
        self._op("data restored from pin")

    def schema(self, t):
        st = self.store()
        try:
            r = st.resource(t.project, t.name, "data")
            return r["schema"] if r else None
        finally:
            st.db.close()

    # ------------------------------------------------------------ data volume

    def copy_consistent(self, platform, t, log, quiet=False):
        v = self._load("data")
        e = v.setdefault(f"{t.project}/{t.name}", {"writes": [], "copy": None, "overlaps": []})
        e["copy"] = list(e["writes"])
        self._save("data", v)
        self._op("data copy")
        return self.root / "backup"

    def restore_copy(self, platform, project, t, log):
        v = self._load("data")
        e = v.get(f"{t.project}/{t.name}")
        if not e or e["copy"] is None:
            raise data.DataError("no copy to restore")
        e["writes"] = list(e["copy"])
        self._save("data", v)
        self._op("data restore")

    # ------------------------------------------------------------ wiring

    @contextmanager
    def patched(self):
        noop = lambda *a, **k: None  # noqa: E731
        real_phase_set, real_phase_clear = recipe._phase_set, recipe._phase_clear
        real_make_live, real_revert_live = S.Store.make_live, S.Store.revert_live
        real_set_state = S.Store.set_state
        real_set_schema = S.Store.set_resource_schema
        real_atomic, real_transact = edge._atomic_write, edge.transact

        def phase_set(platform, t, **rec):
            if self._faults().get("old_phase") and not rec.get("adopting"):
                rec = {**rec, "undo": []}    # as written by huta before it kept the old vhost
            real_phase_set(platform, t, **rec)
            self._op(f"phase {rec.get('stage', 'one-writer')} copied={rec.get('copied')}")

        def phase_clear(platform, t):
            had = recipe._phase_file(platform, t).exists()
            real_phase_clear(platform, t)
            if had:
                self._op("phase cleared")

        def make_live(st, *a, **k):
            real_make_live(st, *a, **k)
            self._op("store live")

        def revert_live(st, *a, **k):
            real_revert_live(st, *a, **k)
            self._op("store reverted")

        def set_state(st, rid, state, *a, **k):
            real_set_state(st, rid, state, *a, **k)
            self._op(f"state {state}")

        def set_schema(st, *a, **k):
            real_set_schema(st, *a, **k)
            self._op(f"schema recorded {a[3]}")

        def atomic_write(path, text, mode):
            real_atomic(path, text, mode)
            p = Path(path)
            if p.parent == self.platform.caddy_sites:
                self._op(f"vhost written {p.name}")
            elif p.suffix == ".pending":
                self._op("edge journal pending")

        def transact(platform, changes):
            undo = real_transact(platform, changes)
            self._op("edge committed")
            return undo

        with ExitStack() as s:
            def patch(obj, name, new):
                s.enter_context(mock.patch.object(obj, name, new))
            for mod in (cfg, store, edge, data):
                patch(mod, "assert_root_dir", noop)
            patch(recipe, "sh", self.sh)
            patch(recipe, "time", _FAST_TIME)
            patch(recipe, "wait_health", self.wait_health)
            patch(recipe, "public_check", self.public_check)
            patch(recipe, "smoke", lambda *a, **k: [])
            patch(recipe, "_egress_sync", noop)
            patch(edge, "_run", self.edge_run)
            patch(edge, "validate_caddy", self.validate_caddy)
            patch(edge, "cert_ok", self.cert_ok)
            patch(edge, "adapt_hosts", self.adapt_hosts)
            patch(edge, "ensure_log_file", noop)
            patch(edge, "_atomic_write", atomic_write)
            patch(edge, "transact", transact)
            patch(L, "collect_quietly", noop)
            patch(L, "slot_state", lambda *a, **k: "")
            patch(jobs, "sync_units", lambda *a, **k: [])
            patch(jobs, "start_after_release", noop)
            patch(runtime, "secret_value", lambda *a: "test-secret")
            patch(data, "ensure", lambda *a, **k: "volume ready")
            patch(data, "thaw", noop)
            patch(data, "backup", lambda *a, **k: "copy made")
            patch(data, "copy_consistent", self.copy_consistent)
            patch(data, "restore_copy", self.restore_copy)
            patch(data, "pin_copy", self.pin_copy)
            patch(data, "fingerprint", self.fingerprint)
            patch(data, "restore_pin", self.restore_pin)
            patch(data, "drop_pin", lambda platform, t, rid: None)
            patch(recipe, "_docker_run", self.docker_run)
            patch(Cloudflare, "ip_ranges", staticmethod(self.ip_ranges))
            patch(Cloudflare, "ensure_host_strict", lambda _self, h: f"{h}: strict")
            patch(recipe, "_phase_set", phase_set)
            patch(recipe, "_phase_clear", phase_clear)
            patch(S.Store, "make_live", make_live)
            patch(S.Store, "revert_live", revert_live)
            patch(S.Store, "set_state", set_state)
            patch(S.Store, "set_resource_schema", set_schema)
            yield

    # ------------------------------------------------------------ releases

    def store(self) -> S.Store:
        with mock.patch.object(store, "assert_root_dir", lambda _p: None):
            return S.Store(self.platform.state_dir)

    def plan(self, project, t, app: dict, sha: str) -> str:
        """An approved release, as cli.cmd_approve leaves it for recipe.run."""
        iid = self.add_image(project.name, sha)
        st = self.store()
        try:
            rid = st.new_release(project=project.name, target=t.name, sha=sha, image_id=iid,
                                 image_tag=f"huta-build/{project.name}:{sha[:12]}",
                                 app_toml=json.dumps(app), plan={}, plan_hash="0" * 64,
                                 built_by="test")
            st.transition(rid, S.PLANNED, S.APPROVED)
        finally:
            st.db.close()
        return rid

    def release(self, project, t, rid) -> str:
        st = self.store()
        try:
            with self.patched():
                return recipe.run(self.platform, st, project, t, rid, self.cert,
                                  list(CF_RANGES), lambda _m: None)
        finally:
            st.db.close()

    def reconcile(self, project, t):
        st = self.store()
        try:
            with self.patched():
                recipe.reconcile(self.platform, st, project, t, lambda _m: None)
        finally:
            st.db.close()

    def in_child(self, fn, crash_after: int | None) -> tuple[int, str]:
        """Run fn() in a forked child that dies right after step `crash_after` (an index into
        ops, 1-based). Returns (exit code, the child's traceback if it raised)."""
        err = self.root / "child-error.txt"
        err.unlink(missing_ok=True)
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                self.crash_after = crash_after
                fn()
            except BaseException:  # noqa: BLE001 — reported through the file and exit status
                err.write_text(traceback.format_exc())
                code = 1
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), (err.read_text() if err.exists() else "")
