"""v1 snapshots: what huta generates today for v1 targets, byte for byte.

These are the compatibility contract for v2 (a target that uses no v2 feature runs exactly as
before). A failure here means v1 output changed. Fix the change, or, if the change is intended,
rewrite the snapshot with HUTA_UPDATE_GOLDEN=1 and say why in the commit.
"""

import shutil
import unittest
from pathlib import Path

from tests._util import CF_RANGES, IMAGE, SHA, app_toml, check_golden, etc_copy, load

from huta import edge, recipe

CASES = [("hello", "prod"), ("notes", "prod"), ("notes", "staging")]
CERT = (Path("/etc/huta/tls/example.pem"), Path("/etc/huta/tls/example.key"))


class V1Golden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.etc = etc_copy()
        cls.platform, cls.registry = load(cls.etc)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.etc)

    def each(self):
        for p, t in CASES:
            project = self.registry[p]
            with self.subTest(target=f"{p}/{t}"):
                yield project, project.targets[t], app_toml(p)

    def test_effective(self):
        """The dict a plan stores and approval recomputes (cli.plan_release, cmd_approve)."""
        for project, t, app in self.each():
            eff = recipe.effective(self.platform, project, t, app)
            check_golden(self, f"effective/{t.project}-{t.name}.json", eff)

    def test_compose(self):
        """The compose document a release writes (slot a old, slot b new)."""
        for project, t, app in self.each():
            doc = recipe.compose_doc(self.platform, project, t, app,
                                     {"a": (IMAGE, SHA), "b": (IMAGE, "f" * 40)},
                                     with_secrets=False)
            check_golden(self, f"compose/{t.project}-{t.name}.json", doc)

    def test_vhost(self):
        """The generated vhost: routed to slot a, and the 'not released yet' form."""
        for project, t, app in self.each():
            routed = edge.render_vhost(self.platform, t, CF_RANGES, CERT, t.slots[0])
            check_golden(self, f"vhost/{t.project}-{t.name}.caddy", routed)
            check_golden(self, f"vhost/{t.project}-{t.name}-unreleased.caddy",
                         edge.render_vhost(self.platform, t, CF_RANGES, CERT, None))
            # What a plan compares: the same with any upstream port.
            self.assertEqual(edge.vhost_shape(routed), edge.vhost_shape(
                edge.render_vhost(self.platform, t, CF_RANGES, CERT, t.slots[1])))

    def test_names(self):
        """Compose project, network and bridge names (the firewall matches dp-*; Linux allows
        15 characters for an interface name)."""
        for project, t, app in self.each():
            names = {"compose": recipe.compose_name(t), "network": recipe.network_name(t),
                     "bridge": recipe.bridge_name(t), "services": [recipe.service(s)
                                                                   for s in recipe.SLOTS]}
            self.assertLessEqual(len(names["bridge"]), 15)
            self.assertTrue(names["bridge"].startswith("dp-"))
            check_golden(self, f"names/{t.project}-{t.name}.json", names)


if __name__ == "__main__":
    unittest.main()
