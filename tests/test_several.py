"""Several containers per target (DESIGN-v2 §3): the generated compose and the client's names."""

import shutil
import unittest

from tests._util import IMAGE, SHA, app_toml, check_golden, etc_copy, load

from huta import client, recipe
from huta import spec as SP


class Compose(unittest.TestCase):
    def setUp(self):
        self.etc = etc_copy()
        self.platform, self.reg = load(self.etc)

    def tearDown(self):
        shutil.rmtree(self.etc)

    def test_compose_snapshot(self):
        project = self.reg["shop"]
        t = project.targets["prod"]
        app = app_toml("shop")
        imgs = {"web": IMAGE, "api": "sha256:" + "cd" * 32}
        doc = recipe.compose_doc(self.platform, project, t, app,
                                 {"a": (imgs, SHA), "b": (imgs, "f" * 40)}, with_secrets=False)
        check_golden(self, "compose/shop-prod.json", doc)
        a = doc["services"]
        self.assertEqual(sorted(a), ["slot-a-api", "slot-a-web", "slot-a-worker",
                                     "slot-b-api", "slot-b-web", "slot-b-worker"])
        self.assertIn("ports", a["slot-a-web"])
        self.assertNotIn("ports", a["slot-a-api"])
        self.assertEqual(a["slot-a-worker"]["restart"], "no")
        self.assertEqual(a["slot-a-worker"]["command"], ["python", "-m", "worker"])
        # Target secrets reach the public component only; per-component ones their own.
        self.assertIn("SHOP_SESSION_KEY", a["slot-a-web"]["environment"])
        self.assertNotIn("SHOP_SESSION_KEY", a["slot-a-api"]["environment"])
        self.assertIn("API_DB_KEY", a["slot-a-api"]["environment"])
        self.assertNotIn("API_DB_KEY", a["slot-a-web"]["environment"])
        self.assertEqual(a["slot-a-api"]["mem_limit"], "512m")
        self.assertNotIn("volumes", a["slot-a-web"])
        self.assertIn("volumes", a["slot-a-worker"])
        nets = doc["networks"]
        self.assertEqual(sorted(nets), ["a2", "b2"])
        self.assertNotEqual(nets["a2"]["driver_opts"]["com.docker.network.bridge.name"],
                            nets["b2"]["driver_opts"]["com.docker.network.bridge.name"])
        for n in nets.values():
            self.assertLessEqual(len(n["driver_opts"]["com.docker.network.bridge.name"]), 15)
            self.assertEqual(n["driver_opts"]["com.docker.network.bridge.enable_icc"], "true")
        self.assertEqual(recipe.services(app, "b", "serving"), ["slot-b-web", "slot-b-api"])
        self.assertEqual(recipe.services(app, "b", "workers"), ["slot-b-worker"])

    def test_one_component_services_unchanged(self):
        app = app_toml("hello")
        self.assertFalse(SP.several(app))
        self.assertEqual(recipe.services(app, "a"), ["slot-a"])
        self.assertEqual(recipe.services(app, "a", "workers"), [])


class ClientNames(unittest.TestCase):
    def test_refs(self):
        self.assertEqual(client.image_refs("hello", SHA, app_toml("hello")),
                         {"web": f"huta-build/hello:{SHA[:12]}"})
        self.assertEqual(client.image_refs("shop", SHA, app_toml("shop")),
                         {"web": f"huta-build/shop:{SHA[:12]}",
                          "api": f"huta-build/shop/api:{SHA[:12]}"})


if __name__ == "__main__":
    unittest.main()
