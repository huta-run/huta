"""Gateways (DESIGN-v2 §1): a hostname for a service huta doesn't run, and the edge knobs."""

import argparse
import dataclasses
import grp
import os
import shutil
import stat
import unittest
from pathlib import Path
from unittest import mock

from tests._util import CF_RANGES, check_golden, etc_copy, load
from tests.fakehost import FakeHost
from tests.test_plans import FakeCloudflare

from huta import cfg, cli, edge, recipe, runtime

CERT = (Path("/etc/huta/tls/example.pem"), Path("/etc/huta/tls/example.key"))


def registry_with(text: str, name: str = "x"):
    etc = etc_copy()
    try:
        (etc / "projects.d" / f"{name}.toml").write_text(text)
        return load(etc)[1][name]
    finally:
        shutil.rmtree(etc)


GW = '''schema = 1
project = "x"
[targets.g]
kind = "gateway"
hosts = ["g.example.org"]
'''


class Registry(unittest.TestCase):
    def test_fixture(self):
        etc = etc_copy()
        try:
            _platform, reg = load(etc)
        finally:
            shutil.rmtree(etc)
        g = reg["gw"].targets["media"]
        self.assertEqual((g.kind, g.slots, reg["gw"].uid), ("gateway", (), 0))
        self.assertEqual(g.gateway, {"to": "100.64.0.5:8080", "secret": "GATEWAY_KEY",
                                     "header": "X-Gateway-Key"})
        self.assertEqual(g.edge, {"body": 1 << 30, "timeout": "10m"})

    def test_upstream_must_be_private(self):
        for to in ("8.8.8.8:80", "1.1.1.1:443", "example.org:80", "100.128.0.1:80", "10.0.0.1"):
            with self.subTest(to=to), self.assertRaises(cfg.ConfigError):
                registry_with(GW + f'gateway = {{ to = "{to}" }}\n')
        for to in ("10.0.0.5:8080", "192.168.1.2:80", "172.16.0.9:3000", "100.64.0.5:8080",
                   "127.0.0.1:9000", "[fd00::5]:8080"):
            with self.subTest(to=to):
                registry_with(GW + f'gateway = {{ to = "{to}" }}\n')

    def test_secret_needs_header(self):
        with self.assertRaisesRegex(cfg.ConfigError, "go together"):
            registry_with(GW + 'gateway = { to = "10.0.0.5:80", secret = "K" }\n')
        with self.assertRaisesRegex(cfg.ConfigError, "plain header"):
            registry_with(GW + 'gateway = { to = "10.0.0.5:80", secret = "K", header = "Host" }\n')

    def test_no_slots_or_app_keys(self):
        with self.assertRaisesRegex(cfg.ConfigError, "unknown"):
            registry_with(GW + 'gateway = { to = "10.0.0.5:80" }\nslots = [19200, 19201]\n')

    def test_app_project_still_needs_uid(self):
        with self.assertRaisesRegex(cfg.ConfigError, "uid"):
            registry_with('schema = 1\nproject = "x"\nrepo = "e/x"\napp_toml = "a.toml"\n'
                          '[targets.p]\nhosts = ["p.example.org"]\nslots = [19200, 19201]\n')

    def test_edge_knobs_validated(self):
        with self.assertRaises(cfg.ConfigError):
            registry_with(GW + 'gateway = { to = "10.0.0.5:80" }\nedge = { timeout = "forever" }\n')
        with self.assertRaises(cfg.ConfigError):
            registry_with(GW + 'gateway = { to = "10.0.0.5:80" }\nedge = { colour = "red" }\n')


class Vhosts(unittest.TestCase):
    def setUp(self):
        self.etc = etc_copy()
        self.platform, self.reg = load(self.etc)

    def tearDown(self):
        shutil.rmtree(self.etc)

    def test_gateway_vhosts(self):
        for name in ("media", "local"):
            with self.subTest(target=name):
                t = self.reg["gw"].targets[name]
                check_golden(self, f"vhost/gw-{name}.caddy",
                             edge.render_vhost(self.platform, t, CF_RANGES, CERT, None))

    def test_app_with_knobs(self):
        t = dataclasses.replace(self.reg["hello"].targets["prod"],
                                edge={"body": 1 << 20, "timeout": "90s"})
        text = edge.render_vhost(self.platform, t, CF_RANGES, CERT, 19100)
        self.assertIn("max_size 1048576", text)
        self.assertIn("read_timeout 90s", text)
        self.assertEqual(edge.UPSTREAM_RE.search(text).group(1), "19100")
        plain = edge.render_vhost(self.platform, self.reg["hello"].targets["prod"], CF_RANGES,
                                  CERT, 19100)
        self.assertNotIn("max_size", plain)
        self.assertIn("\treverse_proxy 127.0.0.1:19100\n", plain)


class GatewayEdge(unittest.TestCase):
    """huta edge on a gateway: plan, approve, secret header file, nothing else runs."""

    def setUp(self):
        self.host = FakeHost()
        group = grp.getgrgid(os.getgid()).gr_name
        self.host.platform = dataclasses.replace(
            self.host.platform, caddy_secrets=self.host.root / "caddy-secrets", caddy_group=group)
        self.stack = [
            mock.patch.object(cli, "Cloudflare", FakeCloudflare),
            mock.patch.object(cli, "zone_for", lambda h, p: "example.org"),
            mock.patch.object(edge, "ensure_cert", lambda pl, cf, t: ("certificate ok", self.host.cert)),
            mock.patch.object(edge, "prune_certs", lambda *a: None),
            mock.patch.object(cli, "ctx", self.ctx),
            mock.patch.object(cli, "say", lambda _m: None),
            mock.patch.object(runtime, "secret_value", lambda pl, p, n: "s3cret-value"),
        ]
        for m in self.stack:
            m.start()
        self.patched = self.host.patched()
        self.patched.__enter__()
        mock.patch.object(runtime, "secret_value", lambda pl, p, n: "s3cret-value").start()

    def tearDown(self):
        mock.patch.stopall()
        self.patched.__exit__(None, None, None)
        for m in self.stack:
            m.stop()
        self.host.close()

    def ctx(self, name=None):
        return (self.host.platform, self.host.registry[name] if name else self.host.registry,
                self.host.store())

    def test_plan_and_apply(self):
        cli.cmd_edge(argparse.Namespace(project="gw", target="media", apply=True, now=False))
        st = self.host.store()
        pid = st.db.execute("SELECT id FROM plans").fetchone()[0]
        st.db.close()
        cli.cmd_approve(argparse.Namespace(release=pid, via="cli"))
        t = self.host.registry["gw"].targets["media"]
        vhost = edge.vhost_path(self.host.platform, t).read_text()
        self.assertIn("reverse_proxy 100.64.0.5:8080 {", vhost)
        self.assertNotIn("s3cret", vhost, "the secret must not be in the world-readable vhost")
        secret = edge.gateway_secret_path(self.host.platform, t)
        self.assertEqual(secret.read_text(), 'header_up X-Gateway-Key "s3cret-value"\n')
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(secret.parent.stat().st_mode), 0o750)

    def test_secret_value_with_syntax_refused(self):
        t = self.host.registry["gw"].targets["media"]
        for bad in ('a"b', "a b", "a}b", "a$b", "a\nb"):
            with self.subTest(value=bad), self.assertRaises(edge.EdgeError):
                edge.write_gateway_secret(self.host.platform, t, bad)

    def test_release_commands_refused(self):
        project = self.host.registry["gw"]
        with self.assertRaisesRegex(cfg.ConfigError, "is a gateway"):
            cli.target_of(project, "media")
        recipe.reconcile(self.host.platform, None, project, project.targets["media"], None)


if __name__ == "__main__":
    unittest.main()
