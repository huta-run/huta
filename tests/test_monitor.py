"""Monitoring hooks (docs/PLAN-monitoring.md).

Milestone 0: the planned keys are refused by name, and the fake services in tests/fakemon.py
answer with the fields the real ones have (tests/fixtures/monitor/shapes.json), so the clients
built later are tested against the right shapes.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests._util import FIXTURES, etc_copy, load
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

    def test_huta_toml(self):
        self.refused(self.etc / "huta.toml", '\n[monitoring]\nprefix = "huta"\n',
                     r"\[monitoring\] is planned")

    def test_target_keys(self):
        for key, value in (("monitor", "{ uptime = true }"), ("watch", '{ window = "15m" }'),
                           ("matomo", "7")):
            with self.subTest(key=key):
                f = self.etc / "projects.d" / "hello.toml"
                text = f.read_text()
                head, sep, tail = text.partition("[targets.prod]\n")
                f.write_text(head + sep + f"{key} = {value}\n" + tail)
                with self.assertRaisesRegex(ConfigError, f"{key} is planned"):
                    load(self.etc)
                f.write_text(text)

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
