"""Plans that are not releases (DESIGN-v2 §0): edge changes planned, approved, applied."""

import argparse
import unittest
from unittest import mock

from tests.fakehost import FakeHost

from huta import cli, edge
from huta import store as S


class FakeCloudflare:
    created: list = []

    def __init__(self, platform):
        pass

    def dns_covers(self, host):
        return "A 192.0.2.1 (proxied)"

    def zone_ssl(self, host):
        return "strict"

    def create_proxied_a(self, host, ip):
        return "created"

    def ensure_host_strict(self, host):
        return "strict"

    @staticmethod
    def ip_ranges():
        return ["173.245.48.0/20"]


class EdgePlans(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.project = self.host.registry["hello"]
        self.t = self.project.targets["prod"]
        self.stack = [
            mock.patch.object(cli, "Cloudflare", FakeCloudflare),
            mock.patch.object(cli, "zone_for", lambda h, p: "example.org"),
            mock.patch.object(edge, "ensure_cert", lambda pl, cf, t: ("certificate ok", self.host.cert)),
            mock.patch.object(edge, "prune_certs", lambda *a: None),
            mock.patch.object(cli, "ctx", self.ctx),
            mock.patch.object(cli, "say", lambda _m: None),
        ]
        for m in self.stack:
            m.start()
        self.patched = self.host.patched()
        self.patched.__enter__()

    def tearDown(self):
        self.patched.__exit__(None, None, None)
        for m in self.stack:
            m.stop()
        self.host.close()

    def ctx(self, name=None):
        st = self.host.store()
        return (self.host.platform, self.host.registry[name] if name else self.host.registry, st)

    def edge(self, **kw):
        return cli.cmd_edge(argparse.Namespace(project="hello", target="prod",
                                               apply=kw.get("apply", False), now=kw.get("now", False)))

    def plan_id(self):
        st = self.host.store()
        try:
            return st.db.execute("SELECT id FROM plans ORDER BY created DESC").fetchone()[0]
        finally:
            st.db.close()

    def state(self, pid):
        st = self.host.store()
        try:
            return st.plan_row(pid)["state"]
        finally:
            st.db.close()

    def test_dry_run_changes_nothing(self):
        self.edge()
        self.assertFalse(edge.vhost_path(self.host.platform, self.t).exists())

    def test_apply_plans_and_approve_applies(self):
        self.edge(apply=True)
        self.assertFalse(edge.vhost_path(self.host.platform, self.t).exists(), "applied unapproved")
        pid = self.plan_id()
        self.assertEqual(self.state(pid), S.PLANNED)
        cli.cmd_approve(argparse.Namespace(release=pid, via="cli"))
        self.assertEqual(self.state(pid), "applied")
        self.assertIn("not released yet", edge.vhost_path(self.host.platform, self.t).read_text())

    def test_stale_plan_is_superseded(self):
        self.edge(apply=True)
        pid = self.plan_id()
        self.edge(now=True)            # the edge changes under the plan
        with self.assertRaisesRegex(cli.ConfigError, "changed since"):
            cli.cmd_approve(argparse.Namespace(release=pid, via="cli"))
        self.assertEqual(self.state(pid), S.SUPERSEDED)

    def test_reject(self):
        self.edge(apply=True)
        pid = self.plan_id()
        cli.cmd_reject(argparse.Namespace(release=pid))
        self.assertEqual(self.state(pid), S.REJECTED)
        with self.assertRaisesRegex(cli.ConfigError, "not planned"):
            cli.cmd_approve(argparse.Namespace(release=pid, via="cli"))

    def test_now_is_recorded(self):
        self.edge(now=True)
        st = self.host.store()
        try:
            ev = [r["msg"] for r in st.db.execute("SELECT msg FROM events WHERE kind='edge'")]
        finally:
            st.db.close()
        self.assertTrue(any("--now" in m for m in ev), ev)


if __name__ == "__main__":
    unittest.main()
