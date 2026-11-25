"""Monitoring hooks (docs/PLAN-monitoring.md).

Milestone 0: the planned keys are refused by name, and the fake services in tests/fakemon.py
answer with the fields the real ones have (tests/fixtures/monitor/shapes.json), so the clients
built later are tested against the right shapes.
"""

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._util import FIXTURES, ROOT, etc_copy, load
from tests import fakemon as FM

from huta.cfg import ConfigError

SHAPES = json.loads((FIXTURES / "monitor" / "shapes.json").read_text())


class PlannedKeysRefused(unittest.TestCase):
    def setUp(self):
        self.etc = etc_copy()

    def tearDown(self):
        shutil.rmtree(self.etc, ignore_errors=True)

    def refused(self, path: Path, add: str, pattern: str):
        path.write_text(path.read_text() + add)
        with self.assertRaisesRegex(ConfigError, pattern):
            load(self.etc)

    def test_still_planned(self):
        self.refused(self.etc / "huta.toml",
                     '\n[monitoring]\n[monitoring.watch]\nwindow = "15m"\n',
                     r"\[monitoring.watch\] is planned")

    def test_target_watch(self):
        f = self.etc / "projects.d" / "hello.toml"
        head, sep, tail = f.read_text().partition("[targets.prod]\n")
        f.write_text(head + sep + 'watch = { window = "15m" }\n' + tail)
        with self.assertRaisesRegex(ConfigError, "watch is planned"):
            load(self.etc)

    def test_component_key(self):
        f = self.etc / "projects.d" / "hello.toml"
        self.refused(f, "\n[targets.prod.components.web]\nmonitor_env = true\n",
                     "monitor_env is planned")


class FakeShapes(unittest.TestCase):
    """The fakes answer as the real services do (field names, statuses, the quirks the plan
    relies on)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="huta-fakemon-"))
        self.fm = FM.FakeMonitoring(self.dir / "mon.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, method, url, headers=None, body=None):
        status, _h, raw = self.fm.handle(method, url, headers or {}, body)
        return status, (json.loads(raw) if raw and _h.get("Content-Type") == "application/json"
                        else raw)

    def hc(self, spec):
        return self.call("POST", "http://127.0.0.1:8000/api/v3/checks/",
                         {"X-Api-Key": FM.HC_KEY}, json.dumps(spec).encode())

    def test_healthchecks_upsert(self):
        st, a = self.hc({"name": "huta_notes_prod_rotate", "timeout": 3600, "grace": 600,
                         "channels": "*", "unique": ["name"]})
        self.assertEqual(st, 201)
        self.assertEqual(set(a), set(SHAPES["hc_check"]) | set(SHAPES["hc_check_simple"]))
        st, b = self.hc({"name": "huta_notes_prod_rotate", "schedule": "*-*-* 03:00:00",
                         "tz": "UTC", "unique": ["name"]})
        self.assertEqual(st, 200)
        self.assertEqual(b["uuid"], a["uuid"])
        self.assertEqual(set(b), set(SHAPES["hc_check"]) | set(SHAPES["hc_check_schedule"]))
        self.assertEqual(b["channels"], "*")          # kept: not sent the second time
        self.assertEqual(self.fm.creates("hc"), 1)
        self.assertEqual(self.hc({"name": "x", "timeout": 30})[0], 400)
        st, _ = self.call("POST", "http://127.0.0.1:8000/api/v3/checks/", {"X-Api-Key": "no"},
                          b"{}")
        self.assertEqual(st, 401)

    def test_healthchecks_pings_and_flips(self):
        _st, a = self.hc({"name": "c", "timeout": 3600, "unique": ["name"]})
        base = FM.HC_PING + a["uuid"]
        self.assertEqual(self.call("GET", base + "/start")[0], 200)
        self.assertEqual(self.call("GET", base + "/0")[0], 200)
        self.assertEqual(self.call("GET", base + "/1")[0], 200)
        self.assertEqual(self.call("GET", base + "/256")[0], 400)
        st, f = self.call("GET", f"http://127.0.0.1:8000/api/v3/checks/{a['uuid']}/flips/?start=0",
                          {"X-Api-Key": FM.HC_KEY})
        self.assertEqual(set(f), set(SHAPES["hc_flips"]))
        self.assertEqual([x["up"] for x in f["flips"]], [0, 1])      # newest first
        self.assertEqual(set(f["flips"][0]), set(SHAPES["hc_flip"]))
        st, p = self.call("POST", f"http://127.0.0.1:8000/api/v3/checks/{a['uuid']}/pause",
                          {"X-Api-Key": FM.HC_KEY})
        self.assertEqual(p["status"], "paused")

    def test_bugsink(self):
        auth = {"Authorization": f"Bearer {FM.BUGSINK_TOKEN}"}
        api = "http://127.0.0.1:8000/api/canonical/0/"
        st, made = self.call("POST", api + "projects/", auth,
                             json.dumps({"team": FM.BUGSINK_TEAM, "name": "huta_notes_prod"}).encode())
        self.assertEqual(st, 201)
        self.assertEqual(set(made), set(SHAPES["bugsink_project_created"]))
        self.assertNotIn("dsn", made)
        st, again = self.call("POST", api + "projects/", auth,
                              json.dumps({"team": FM.BUGSINK_TEAM, "name": "huta_notes_prod"}).encode())
        self.assertEqual(st, 400)
        st, page = self.call("GET", api + "projects/", auth)
        self.assertEqual(set(page), set(SHAPES["bugsink_page"]))
        p = page["results"][0]
        self.assertEqual(set(p), set(SHAPES["bugsink_project"]))
        self.assertRegex(p["dsn"], rf"^https://[0-9a-f]{{32}}@bugs\.example\.org/{p['id']}$")
        st, teams = self.call("GET", api + "teams/", auth)
        self.assertEqual(set(teams["results"][0]), set(SHAPES["bugsink_team"]))
        self.fm.add_event(p["id"], "boom", 1000.0)
        self.fm.add_event(p["id"], "boom", 1010.0)
        st, iss = self.call("GET", api + f"issues/?project={p['id']}&sort=last_seen&order=desc", auth)
        i = iss["results"][0]
        self.assertEqual(set(i), set(SHAPES["bugsink_issue"]))
        st, ev = self.call("GET", api + f"events/?issue={i['id']}&order=desc", auth)
        self.assertEqual(len(ev["results"]), 2)
        self.assertEqual(set(ev["results"][0]), set(SHAPES["bugsink_event"]))
        self.assertEqual(self.call("GET", api + "events/", auth)[0], 400)   # issue required
        self.assertEqual(self.call("GET", api + "projects/")[0], 401)
        self.assertEqual(self.fm.creates("bugsink"), 1)

    def test_matomo(self):
        def m(form, proto="https"):
            from urllib.parse import urlencode
            h = {"Content-Type": "application/x-www-form-urlencoded"}
            if proto:
                h["X-Forwarded-Proto"] = proto
            return self.call("POST", "http://127.0.0.1:8081/index.php", h,
                             urlencode({"module": "API", "format": "json", **form}).encode())
        st, _ = m({"method": "SitesManager.addSite"}, proto=None)
        self.assertEqual(st, 302)                       # force_ssl without the header
        st, e = m({"method": "SitesManager.addSite", "token_auth": "wrong"})
        self.assertEqual(st, 401)
        self.assertEqual(set(e), set(SHAPES["matomo_error"]))
        st, ids = m({"method": "SitesManager.getSitesIdFromSiteUrl", "url": "https://n.example.org",
                     "token_auth": FM.MATOMO_TOKEN})
        self.assertEqual(ids, [])
        st, v = m({"method": "SitesManager.addSite", "siteName": "huta_notes_prod",
                   "urls[0]": "https://n.example.org", "token_auth": FM.MATOMO_TOKEN})
        self.assertEqual(set(v), set(SHAPES["matomo_added"]))
        st, ids = m({"method": "SitesManager.getSitesIdFromSiteUrl", "url": "https://n.example.org",
                     "token_auth": FM.MATOMO_TOKEN})
        self.assertEqual(ids, [{"idsite": str(v["value"])}])
        self.assertEqual(set(ids[0]), set(SHAPES["matomo_site_ids"]))
        st, e = m({"method": "Nope.nope", "token_auth": FM.MATOMO_TOKEN})
        self.assertEqual((st, e["result"]), (200, "error"))   # errors can come with 200

    def test_faults(self):
        self.fm.fault("hc", "down")
        with self.assertRaises(FM.Down):
            self.hc({"name": "c", "timeout": 3600})
        self.fm.fault("hc", "lost-create")
        with self.assertRaises(FM.Down):
            self.hc({"name": "c", "timeout": 3600, "unique": ["name"]})
        self.assertEqual(self.fm.creates("hc"), 1)      # committed, answer lost
        self.fm.fault("hc", None)
        self.assertEqual(self.hc({"name": "c", "timeout": 3600, "unique": ["name"]})[0], 200)
        for kind, status in (("redirect", 302), ("oversize", 200), ("garbage", 200)):
            self.fm.fault("bugsink", kind)
            with self.subTest(kind=kind):
                self.assertEqual(self.fm.handle("GET", "http://127.0.0.1:8000/api/canonical/0/projects/",
                                                {}, None)[0], status)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------------ milestone 1

MONITORING = '''
[crashes]
url = "http://127.0.0.1:8000"
host = "bugs.example.org"

[monitoring]
prefix = "huta"
[monitoring.healthchecks]
url = "http://127.0.0.1:8000"
ping = "https://hc.example.org/ping"
[monitoring.bugsink]
dsn_host = "bugs.example.org"
team = "11111111-2222-3333-4444-555555555555"
[monitoring.matomo]
url = "http://127.0.0.1:8081"
host = "stats.example.org"
'''


def _no_uid_root_dir(d):
    d.mkdir(mode=0o700, parents=True, exist_ok=True)


def _plain_read_secret(p, remember=True):
    return Path(p).read_text().strip()


class Config(unittest.TestCase):
    def setUp(self):
        self.etc = etc_copy()

    def tearDown(self):
        shutil.rmtree(self.etc, ignore_errors=True)

    def platform(self, extra: str):
        from huta import cfg
        (self.etc / "huta.toml").write_text((self.etc / "huta.toml").read_text() + extra)
        with mock.patch.object(cfg, "assert_root_dir", lambda _p: None):
            return cfg.load_platform(self.etc)

    def registry(self, platform):
        from huta import cfg
        with mock.patch.object(cfg, "assert_root_dir", lambda _p: None):
            return cfg.load_registry(self.etc, platform)

    def set_target(self, line: str):
        f = self.etc / "projects.d" / "hello.toml"
        head, sep, tail = f.read_text().partition("[targets.prod]\n")
        f.write_text(head + sep + line + "\n" + tail)

    def test_parsed(self):
        m = self.platform(MONITORING).monitoring
        self.assertEqual(m["prefix"], "huta")
        self.assertEqual(m["healthchecks"]["channels"], "*")
        self.assertEqual(m["healthchecks"]["ping"], "https://hc.example.org/ping")
        self.assertEqual(m["bugsink"]["dsn_host"], "bugs.example.org")
        self.assertEqual(m["matomo"]["host"], "stats.example.org")

    def test_off_without_table(self):
        self.assertEqual(self.platform("").monitoring, {})

    def test_bad_values(self):
        for old, new, pattern in (
                ('url = "http://127.0.0.1:8081"', 'url = "http://stats.example.org"', "url must be"),
                ('ping = "https://hc.example.org/ping"', 'ping = "https://u:p@hc.example.org/ping"',
                 "ping must be"),
                ('team = "11111111-2222-3333-4444-555555555555"', 'team = "7"', "team must be"),
                ('dsn_host = "bugs.example.org"', 'dsn_host = "10.0.0.1"', "dsn_host"),
                ('prefix = "huta"', 'prefix = "a_b"', "prefix"),
                ('[monitoring.matomo]', '[monitoring.matomo]\ntoken = "x"', "accepts url and host")):
            with self.subTest(new=new):
                etc = self.etc
                self.etc = etc_copy()
                try:
                    with self.assertRaisesRegex(ConfigError, pattern):
                        self.platform(MONITORING.replace(old, new))
                finally:
                    shutil.rmtree(self.etc, ignore_errors=True)
                    self.etc = etc

    def test_bugsink_needs_crashes(self):
        no_crashes = MONITORING.split("[monitoring]", 1)[1]
        with self.assertRaisesRegex(ConfigError, r"needs \[crashes\]"):
            self.platform("\n[monitoring]" + no_crashes)

    def test_target_monitor(self):
        self.set_target("monitor = { uptime = true, errors = true, analytics = false }\nmatomo = 7")
        with self.assertRaisesRegex(ConfigError, "matomo = <id> needs monitor.analytics"):
            self.registry(self.platform(MONITORING))

    def test_kind_needs_host_table(self):
        self.set_target("monitor = { errors = true }")
        only_hc = MONITORING.split("[monitoring.bugsink]")[0]
        with self.assertRaisesRegex(ConfigError, r"monitor.errors needs \[monitoring.bugsink\]"):
            self.registry(self.platform(only_hc))

    def test_target_values(self):
        self.set_target("monitor = { uptime = true, jobs = false }")
        from huta import cfg
        with mock.patch.object(cfg, "assert_root_dir", lambda _p: None):
            t = cfg.load_project("hello", self.etc).targets["prod"]
        self.assertEqual(t.monitor, {"uptime": True})
        for bad in ("monitor = { uptime = 1 }", "monitor = { logs = true }", "monitor = {}",
                    "matomo = 0", "matomo = true"):
            with self.subTest(bad=bad):
                f = self.etc / "projects.d" / "hello.toml"
                text = f.read_text()
                f.write_text(text.replace("monitor = { uptime = true, jobs = false }", bad))
                with self.assertRaises(ConfigError), \
                        mock.patch.object(cfg, "assert_root_dir", lambda _p: None):
                    cfg.load_project("hello", self.etc)
                f.write_text(text)


class _EnsureBase(unittest.TestCase):
    """A host with all three services (fakes) and the hello target monitored."""

    def setUp(self):
        from huta import cfg, monitor as M, runtime, store
        self.dir = Path(tempfile.mkdtemp(prefix="huta-mon-"))
        self.etc = etc_copy()
        (self.etc / "huta.toml").write_text((self.etc / "huta.toml").read_text() + MONITORING)
        f = self.etc / "projects.d" / "hello.toml"
        head, sep, tail = f.read_text().partition("[targets.prod]\n")
        f.write_text(head + sep + "monitor = { uptime = true, errors = true, analytics = true }\n"
                     + tail)
        plat, reg = load(self.etc)
        self.platform = dataclasses.replace(plat, state_dir=self.dir / "state",
                                            secrets_dir=self.etc / "secrets")
        self.project = reg["hello"]
        self.t = self.project.targets["prod"]
        pdir = self.etc / "secrets" / "_platform"
        pdir.mkdir(parents=True)
        for n, v in (("HEALTHCHECKS_API_KEY", FM.HC_KEY), ("BUGSINK_TOKEN", FM.BUGSINK_TOKEN),
                     ("MATOMO_TOKEN", FM.MATOMO_TOKEN)):
            (pdir / n).write_text(v)
        self.fm = FM.FakeMonitoring(self.dir / "mon.json")
        self.patches = [mock.patch.object(M, "transport", self.fm.handle),
                        mock.patch.object(M, "read_secret", _plain_read_secret),
                        mock.patch.object(cfg, "read_secret", _plain_read_secret),
                        mock.patch.object(runtime, "_root_dir", _no_uid_root_dir),
                        mock.patch.object(store, "assert_root_dir", lambda _p: None),
                        mock.patch.object(cfg, "_SECRETS", [])]
        for p in self.patches:
            p.start()
        self.st = store.Store(self.platform.state_dir)
        self.M = M

    def tearDown(self):
        self.st.db.close()
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.etc, ignore_errors=True)

    def ensure(self, **k):
        return self.M.ensure(self.platform, self.st, self.project, self.t, **k)

    def dsn_file(self):
        return self.etc / "secrets" / "hello" / "HUTA_SENTRY_DSN_PROD"



class Ensure(_EnsureBase):
    """monitor.ensure against the fake services."""

    def test_twice_makes_once(self):
        self.assertEqual(self.ensure(), {"errors": "ok", "analytics": "ok", "uptime": "ok"})
        self.assertEqual(self.ensure(), {"errors": "ok", "analytics": "ok", "uptime": "ok"})
        for svc in ("hc", "bugsink", "matomo"):
            self.assertEqual(self.fm.creates(svc), 1, svc)
        rows = {r["kind"]: r for r in self.st.monitors("hello", "prod")}
        self.assertEqual(set(rows), {"errors", "analytics", "uptime"})
        self.assertEqual(rows["errors"]["name"], "huta_hello_prod")
        proj = next(iter(self.fm.state()["bugsink"]["projects"].values()))
        self.assertEqual(proj["name"], "huta_hello_prod")
        self.assertRegex(self.dsn_file().read_text(),
                         rf"^https://[0-9a-f]{{32}}@bugs\.example\.org/{proj['id']}$")
        sites = self.fm.state()["matomo"]["sites"]
        self.assertEqual([s["urls"] for s in sites.values()], [[f"https://{h}" for h in self.t.hosts]])
        check = next(iter(self.fm.state()["hc"]["checks"].values()))
        self.assertEqual(check["channels"], "*")
        self.assertEqual(rows["uptime"]["remote_id"], check["uuid"])

    def test_api_down_is_an_event(self):
        for svc in ("hc", "bugsink", "matomo"):
            self.fm.fault(svc, "down")
        res = self.ensure()
        self.assertEqual(set(res), {"errors", "analytics", "uptime"})
        self.assertTrue(all(v != "ok" for v in res.values()))
        fails = [e["msg"] for e in self.st.events(limit=10) if e["kind"] == "monitor-fail"]
        self.assertEqual(len(fails), 3)
        self.assertFalse(self.st.monitors("hello", "prod"))
        for svc in ("hc", "bugsink", "matomo"):
            self.fm.fault(svc, None)
        self.assertEqual(set(self.ensure().values()), {"ok"})

    def test_lost_create_answer(self):
        for svc in ("hc", "bugsink", "matomo"):
            self.fm.fault(svc, "lost-create")
        res = self.ensure()
        self.assertTrue(all(v != "ok" for v in res.values()), res)
        for svc in ("hc", "bugsink", "matomo"):
            self.fm.fault(svc, None)
        self.assertEqual(set(self.ensure().values()), {"ok"})
        for svc in ("hc", "bugsink", "matomo"):
            self.assertEqual(self.fm.creates(svc), 1, svc)

    def test_hostile_answers(self):
        for kind in ("oversize", "redirect", "garbage", "slow"):
            for svc in ("hc", "bugsink", "matomo"):
                self.fm.fault(svc, kind)
            with self.subTest(kind=kind):
                res = self.ensure()
                self.assertTrue(all(v != "ok" for v in res.values()), res)
                self.assertFalse(self.st.monitors("hello", "prod"))

    def test_bad_dsn_refused(self):
        self.platform.monitoring["bugsink"]["dsn_host"] = "other.example.org"
        res = self.ensure(kinds=("errors",))
        self.assertIn("DSN", res["errors"])
        self.assertFalse(self.dsn_file().exists())

    def test_adopts_registry_ids(self):
        pid = self.fm.add_project("legacy-name")
        self.t = dataclasses.replace(self.t, crashes=pid, matomo=42)
        self.assertEqual(self.ensure(kinds=("errors", "analytics")),
                         {"errors": "ok", "analytics": "ok"})
        rows = {r["kind"]: r["remote_id"] for r in self.st.monitors("hello", "prod")}
        self.assertEqual(rows, {"errors": str(pid), "analytics": "42"})
        self.assertEqual(self.fm.creates("bugsink") + self.fm.creates("matomo"), 0)

    def test_matomo_ambiguous(self):
        url = f"https://{self.t.hosts[0]}"
        self.fm.add_site(url)
        self.fm.add_site(url)
        res = self.ensure(kinds=("analytics",))
        self.assertIn("ambiguous", res["analytics"])

    def test_dsn_repaired_from_row(self):
        self.ensure(kinds=("errors",))
        dsn = self.dsn_file().read_text()
        self.dsn_file().unlink()
        self.assertEqual(self.ensure(kinds=("errors",)), {"errors": "ok"})
        self.assertEqual(self.dsn_file().read_text(), dsn)
        self.assertEqual(self.fm.creates("bugsink"), 1)

    def test_capabilities_redacted(self):
        from huta import cfg
        self.ensure(kinds=("uptime",))
        uuid = self.st.monitors("hello", "prod")[0]["remote_id"]
        url = self.M.ping_url(self.platform, uuid, "fail")
        # This process remembered it ...
        self.assertNotIn(uuid, cfg.redact(f"pinging {url} failed"))
        # ... and a fresh one finds it in the secrets dir, whole URL or bare id.
        with mock.patch.object(cfg, "_SECRETS", None):
            self.assertNotIn(uuid, cfg.redact(f"x {uuid} y", self.etc / "secrets"))

    def test_ping_url_built_not_taken(self):
        with self.assertRaises(self.M.MonitorError):
            self.M.ping_url(self.platform, "../../admin")
        self.assertEqual(self.M.ping_url(self.platform, "0" * 8 + "-0000-0000-0000-" + "0" * 12, "3"),
                         "https://hc.example.org/ping/00000000-0000-0000-0000-000000000000/3")

    def test_names(self):
        self.assertEqual(self.M.name(self.platform, self.t, "rotate"), "huta_hello_prod_rotate")
        long = self.M.name(self.platform, self.t, "j" * 120)
        self.assertEqual(len(long), 100)
        self.assertNotEqual(long, self.M.name(self.platform, self.t, "j" * 121))


class EnsureCrash(_EnsureBase):
    """monitor.ensure killed after each of its durable steps (a remote create, the capability,
    the row, the DSN secret); the next ensure must leave exactly one of each, complete."""

    def test_crash_after_every_step(self):
        from huta import monitor as M, runtime as R, store as S
        def run_child(crash_after):
            steps_file = self.dir / "steps.json"
            steps_file.write_text("[]")

            def op(name):
                steps = json.loads(steps_file.read_text()) + [name]
                steps_file.write_text(json.dumps(steps))
                if crash_after is not None and len(steps) == crash_after:
                    os._exit(77)

            real_handle = self.fm.handle

            def handle(method, url, headers, body, timeout=10):
                out = real_handle(method, url, headers, body, timeout)
                if out[0] == 201 or (method == "POST" and "addSite" in (body or b"").decode()):
                    op(f"remote create {url.split('/')[2]}")
                return out
            real_caps, real_row, real_secret = (M.remember_capability, S.Store.set_monitor,
                                                R.secret_set)

            def caps(*a, **k):
                real_caps(*a, **k)
                op("capability")

            def row(st, *a, **k):
                real_row(st, *a, **k)
                op(f"row {a[2]}")

            def secret(*a, **k):
                real_secret(*a, **k)
                op("dsn secret")
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    self.st = S.Store(self.platform.state_dir)
                    with mock.patch.object(M, "transport", handle), \
                            mock.patch.object(M, "remember_capability", caps), \
                            mock.patch.object(S.Store, "set_monitor", row), \
                            mock.patch.object(R, "secret_set", secret):
                        self.ensure()
                except BaseException:  # noqa: BLE001
                    code = 1
                os._exit(code)
            _, status = os.waitpid(pid, 0)
            return os.waitstatus_to_exitcode(status), json.loads(steps_file.read_text())

        code, steps = run_child(None)
        self.assertEqual(code, 0)
        self.assertEqual(len(steps), 8, steps)  # 3 creates, capability, 3 rows, dsn secret
        for k in range(1, len(steps) + 1):
            with self.subTest(after=steps[k - 1]):
                self.tearDown()
                self.setUp()
                code, _ = run_child(k)
                self.assertEqual(code, 77)
                self.assertEqual(set(self.ensure().values()), {"ok"})
                for svc in ("hc", "bugsink", "matomo"):
                    self.assertEqual(self.fm.creates(svc), 1, svc)
                rows = {r["kind"]: r for r in self.st.monitors("hello", "prod")}
                self.assertEqual(set(rows), {"errors", "analytics", "uptime"})
                proj = next(iter(self.fm.state()["bugsink"]["projects"].values()))
                self.assertTrue(self.dsn_file().read_text().endswith(f"/{proj['id']}"))
                caps = (self.etc / "secrets" / "_monitor" / "CAPABILITIES").read_text().split()
                self.assertEqual(caps, [rows["uptime"]["remote_id"]])


class StrictLookups(_EnsureBase):
    """An answer huta can't fully read never leads to a create (codex review of milestone 1)."""

    def answer(self, match: str, body):
        real = self.fm.handle

        def handle(method, url, headers, data, timeout=10):
            if match in url + (data or b"").decode() and method in ("GET", "POST") \
                    and "addSite" not in (data or b"").decode():
                return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()
            return real(method, url, headers, data, timeout)
        return mock.patch.object(self.M, "transport", handle)

    def test_matomo_unreadable_entry(self):
        with self.answer("getSitesIdFromSiteUrl", [{}]):
            res = self.ensure(kinds=("analytics",))
        self.assertIn("can't read", res["analytics"])
        self.assertEqual(self.fm.creates("matomo"), 0)

    def test_bugsink_unreadable_project(self):
        with self.answer("/projects/", {"next": None, "previous": None,
                                        "results": [{"id": "x", "name": "huta_hello_prod"}]}):
            res = self.ensure(kinds=("errors",))
        self.assertIn("can't read", res["errors"])
        self.assertEqual(self.fm.creates("bugsink"), 0)

    def test_bugsink_bad_next_link(self):
        with self.answer("/projects/", {"next": "https://elsewhere.example.org/", "previous": None,
                                        "results": []}):
            res = self.ensure(kinds=("errors",))
        self.assertIn("cursor", res["errors"])
        self.assertEqual(self.fm.creates("bugsink"), 0)

    def test_channels_kept_after_lost_create(self):
        self.fm.fault("hc", "lost-create")
        self.assertNotEqual(self.ensure(kinds=("uptime",))["uptime"], "ok")
        self.fm.fault("hc", None)
        s = self.fm.state()
        c = next(iter(s["hc"]["checks"].values()))
        self.assertEqual(c["channels"], "*")
        c["channels"] = "pager"                      # the operator changes them meanwhile
        self.fm._save(s)
        self.assertEqual(self.ensure(kinds=("uptime",)), {"uptime": "ok"})
        self.assertEqual(next(iter(self.fm.state()["hc"]["checks"].values()))["channels"], "pager")
        self.assertEqual(self.fm.creates("hc"), 1)

    def test_lock_and_store_failures_dont_raise(self):
        with mock.patch.object(self.M.os, "open", side_effect=PermissionError("no")):
            res = self.ensure()
        self.assertEqual(set(res), {"errors", "analytics", "uptime"})
        broken = mock.Mock()
        broken.event.side_effect = RuntimeError("database is locked")
        broken.monitor.side_effect = RuntimeError("database is locked")
        res = self.M.ensure(self.platform, broken, self.project, self.t,
                            log=mock.Mock(side_effect=RuntimeError("log")))
        self.assertEqual(set(res), {"errors", "analytics", "uptime"})


class Transport(unittest.TestCase):
    """The real HTTP transport against a local server that misbehaves."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                import time as T
                if self.path == "/drip-headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    for i in range(40):
                        self.wfile.write(f"X-{i}: y\r\n".encode())
                        self.wfile.flush()
                        T.sleep(0.1)
                    return
                if self.path == "/drip-body":
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    for _ in range(40):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        T.sleep(0.1)
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/big":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"x" * 5000)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path, timeout=1.5):
        from huta import monitor as M
        return M._transport("GET", self.base + path, {}, None, timeout)

    def test_ok(self):
        self.assertEqual(self.get("/")[0], 200)

    def test_dripping_is_cut_at_the_deadline(self):
        import time as T
        for path in ("/drip-headers", "/drip-body"):
            with self.subTest(path=path):
                t0 = T.monotonic()
                with self.assertRaises((TimeoutError, OSError)):
                    self.get(path)
                self.assertLess(T.monotonic() - t0, 2.5)

    def test_redirect_not_followed(self):
        self.assertEqual(self.get("/redirect")[0], 302)

    def test_size_cap(self):
        from huta import monitor as M
        with mock.patch.object(M, "LIMIT", 1000):
            status, _h, body = self.get("/big")
            self.assertLessEqual(len(body), 1000 + 65536)
            with mock.patch.object(M, "transport", lambda *a: (200, {}, body)):
                with self.assertRaisesRegex(M.MonitorError, "too large"):
                    M._call("GET", self.base + "/big", {}, None, M._deadline())


class PlanWithMonitoringDown(_EnsureBase):
    """A release is still planned when every monitoring service is down, or the ensure lock
    can't be taken: monitoring failures are events, never outcomes."""

    def plan(self):
        from tests._util import IMAGE, SHA, app_toml
        from huta import cli, recipe, runtime as R
        gh = mock.Mock()
        gh.read_file.return_value = (ROOT / "examples/hello/deploy/app.toml").read_text()
        with mock.patch.object(recipe, "check_image_map", lambda *a, **k: None), \
                mock.patch.object(R, "secret_value", lambda *a: "x" * 16), \
                mock.patch.object(R, "staged", lambda *a: {}), \
                mock.patch.object(R, "staged_list", lambda *a: []), \
                mock.patch.object(R, "live_versions", lambda *a: {}):
            rid = cli.plan_release(self.platform, self.project, self.st, self.t, SHA, IMAGE,
                                   "huta-build/hello:x", gh)
        return self.st.release(rid)

    def test_services_down(self):
        for svc in ("hc", "bugsink", "matomo"):
            self.fm.fault(svc, "down")
        r = self.plan()
        self.assertEqual(r["state"], "planned")
        fails = [e["msg"] for e in self.st.events(limit=20) if e["kind"] == "monitor-fail"]
        self.assertEqual(sorted(m.split(":")[0] for m in fails), ["analytics", "errors"])

    def test_lock_unavailable(self):
        real_open = os.open

        def no_lock(path, *a, **k):
            if str(path).endswith("monitor-ensure.lock"):
                raise PermissionError("no")
            return real_open(path, *a, **k)
        with mock.patch.object(os, "open", no_lock):
            self.assertEqual(self.plan()["state"], "planned")


class LookupEdges(StrictLookups):
    def test_bugsink_page_without_next(self):
        with self.answer("/projects/", {"previous": None, "results": []}):
            res = self.ensure(kinds=("errors",))
        self.assertNotEqual(res["errors"], "ok")
        self.assertEqual(self.fm.creates("bugsink"), 0)

    def test_found_by_name_whatever_its_tags(self):
        self.fm.fault("hc", "lost-create")
        self.ensure(kinds=("uptime",))
        self.fm.fault("hc", None)
        s = self.fm.state()
        c = next(iter(s["hc"]["checks"].values()))
        c["channels"], c["tags"] = "pager", "renamed"
        self.fm._save(s)
        self.assertEqual(self.ensure(kinds=("uptime",)), {"uptime": "ok"})
        self.assertEqual(next(iter(self.fm.state()["hc"]["checks"].values()))["channels"], "pager")


class ReleaseWithMonitoringDown(unittest.TestCase):
    """The milestone 1 gate in tests: a monitored target with every service down plans (above)
    and its release goes live, since nothing after planning calls a monitoring service."""

    def test_goes_live(self):
        from tests.fakehost import FakeHost
        from tests._util import app_toml
        from huta import monitor as M, store as S

        def down(*a, **k):
            raise FM.Down("connection refused")
        host = FakeHost()
        try:
            host.platform = dataclasses.replace(host.platform, monitoring={
                "prefix": "huta", "healthchecks": {"url": "http://127.0.0.1:1",
                                                   "ping": "https://hc.example.org/ping",
                                                   "host": "", "channels": "*", "self": False},
                "bugsink": None, "matomo": None})
            project = host.registry["hello"]
            t = dataclasses.replace(project.targets["prod"], monitor={"uptime": True})
            project.targets["prod"] = t
            with mock.patch.object(M, "transport", down):
                rid = host.plan(project, t, app_toml("hello"), "1" * 40)
                self.assertEqual(host.release(project, t, rid), S.LIVE)
        finally:
            host.close()
