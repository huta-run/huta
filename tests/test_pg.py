"""Postgres per target (DESIGN-v2 §4): generated documents, the silent-upgrade guard, revert."""

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._util import IMAGE, SHA, app_toml, check_golden, etc_copy, load

from huta import pg as PG
from huta import recipe, runtime


class Docs(unittest.TestCase):
    def setUp(self):
        self.etc = etc_copy()
        self.platform, self.reg = load(self.etc)
        self.tmp = Path(tempfile.mkdtemp(prefix="huta-pg-"))
        self.platform = dataclasses.replace(self.platform, state_dir=self.tmp / "state",
                                            backup={"dir": self.tmp / "b", "ship": ["/bin/true"]})
        self.project = self.reg["ledger"]
        self.t = self.project.targets["prod"]
        self.secret = mock.patch.object(runtime, "secret_value", lambda pl, p, n: f"pw-{n}")
        self.secret.start()

    def tearDown(self):
        self.secret.stop()
        shutil.rmtree(self.etc)
        shutil.rmtree(self.tmp)

    def test_database_compose(self):
        fixed = dataclasses.replace(self.platform, state_dir=Path("/var/lib/huta"))
        doc = PG.compose_doc(fixed, self.project, self.t, with_secrets=False)
        check_golden(self, "compose/ledger-prod-pg.json", doc)
        svc = doc["services"]["postgres"]
        self.assertTrue(svc["read_only"])
        self.assertEqual(svc["user"], "29004:29004")
        self.assertNotIn("ports", svc)
        self.assertEqual(svc["environment"]["PGDATA"], "/var/lib/postgresql/data/pgdata-17")
        for n in doc["networks"].values():
            self.assertTrue(n["internal"], "database networks have no route out")
            self.assertTrue(n["driver_opts"]["com.docker.network.bridge.name"].startswith("dp-"))

    def test_app_compose_joins_its_slot_network_only(self):
        app = app_toml("ledger")
        fixed = dataclasses.replace(self.platform, state_dir=Path("/var/lib/huta"))
        doc = recipe.compose_doc(fixed, self.project, self.t, app,
                                 {"a": (IMAGE, SHA), "b": (IMAGE, "f" * 40)}, with_secrets=False)
        check_golden(self, "compose/ledger-prod.json", doc)
        self.assertEqual(doc["services"]["slot-a"]["networks"], ["n2", "pga"])
        self.assertEqual(doc["services"]["slot-b"]["networks"], ["n2", "pgb"])
        self.assertTrue(doc["networks"]["pga"]["external"])
        env = doc["services"]["slot-a"]["environment"]
        self.assertEqual(env["DATABASE_URL"], "postgresql://app:‹secret›@postgres:5432/app")

    def test_url_roles(self):
        self.assertEqual(PG.database_url(self.platform, self.project, self.t, "migrate"),
                         f"postgresql://migrate:pw-{PG.secret_name(self.t, 'migrate')}@postgres:5432/app")
        self.assertEqual(PG.secret_name(dataclasses.replace(self.t, name="eu-prod"), "app"),
                         "HUTA_PG_EU_PROD_APP")

    def write_running(self, t):
        f = PG._compose_file(self.platform, t)
        f.write_text(json.dumps(PG.compose_doc(self.platform, self.project, t), indent=2) + "\n")

    def test_pending_and_guard(self):
        self.write_running(self.t)
        self.assertIsNone(PG.pending(self.platform, self.project, self.t))
        t17b = dataclasses.replace(self.t, postgres={**self.t.postgres, "version": "18"})
        plat = dataclasses.replace(self.platform, postgres_images={
            **self.platform.postgres_images, "18": "postgres@sha256:" + "2" * 64})
        change = PG.pending(plat, self.project, t17b)
        self.assertTrue(change["new_cluster"])
        with mock.patch("huta.data.ensure", lambda *a: "ok"), \
                mock.patch.object(PG, "password", lambda *a, **k: "pw"), \
                mock.patch.object(PG.subprocess, "run",
                                  lambda *a, **k: type("R", (), {"returncode": 0})()):
            with self.assertRaisesRegex(PG.PgError, "plan it with huta postgres"):
                PG.ensure(plat, None, self.project, t17b, print)

    def test_failed_upgrade_puts_the_old_server_back(self):
        self.write_running(self.t)
        old = PG._compose_file(self.platform, self.t).read_text()
        t18 = dataclasses.replace(self.t, postgres={**self.t.postgres, "version": "18"})
        plat = dataclasses.replace(self.platform, postgres_images={
            **self.platform.postgres_images, "18": "postgres@sha256:" + "2" * 64})
        calls = []

        def sh(cmd, **k):
            calls.append(cmd)
            if "--force-recreate" in cmd and PG._compose_file(plat, t18).read_text() != old:
                raise PG.PgError("new server would not start")
            return ""

        with mock.patch.object(PG, "_sh", sh), \
                mock.patch.object(PG, "copy", lambda *a, **k: self.tmp / "b"), \
                mock.patch("huta.data.pin_copy", lambda *a: (self.tmp / "pin", "fp")), \
                mock.patch.object(PG, "_wait_ready", lambda *a: None), \
                mock.patch.object(PG, "_roles", lambda *a: None), \
                mock.patch.object(PG.subprocess, "run",
                                  lambda *a, **k: type("R", (), {"returncode": 0})()), \
                mock.patch.object(recipe, "_stop_members", lambda *a, **k: None), \
                mock.patch.object(recipe, "jobs_running", lambda t: []), \
                mock.patch.object(recipe, "slot_members", lambda t: {}), \
                mock.patch.object(recipe, "schema_now", lambda *a: 1):
            st = mock.MagicMock()
            with self.assertRaisesRegex(PG.PgError, "undone"):
                PG.upgrade(plat, st, self.project, t18, "0badc0de", print)
        self.assertEqual(PG._compose_file(plat, t18).read_text(), old, "old definition back")
        self.assertIsNone(recipe._phase_get(plat, t18), "no phase left")
        self.assertEqual(sum("--force-recreate" in c for c in calls), 2)


if __name__ == "__main__":
    unittest.main()
