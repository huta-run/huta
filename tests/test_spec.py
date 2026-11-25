"""The one release description (spec.py): which component is public, for any manifest."""

import unittest

from tests._util import app_toml

from huta import spec as SP


def app(order, comps):
    return {"order": order, "components": comps}


class Public(unittest.TestCase):
    def test_v1_manifests(self):
        for p in ("hello", "notes"):
            with self.subTest(project=p):
                a = app_toml(p)
                self.assertEqual(SP.public_name(a), "web")
                self.assertEqual(SP.health(a), "/healthz")
                self.assertEqual(SP.port(a), 8080)

    def test_marked_public_wins(self):
        a = app(["api", "web"], {"api": {"kind": "http"}, "web": {"kind": "http", "public": True}})
        self.assertEqual(SP.public_name(a), "web")

    def test_only_http_component(self):
        a = app(["worker", "web"], {"worker": {"kind": "worker"}, "web": {}})
        self.assertEqual(SP.public_name(a), "web")

    def test_defaults(self):
        a = app(["web"], {"web": {}})
        self.assertEqual((SP.health(a), SP.port(a)), ("/healthz", 8080))


if __name__ == "__main__":
    unittest.main()
