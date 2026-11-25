"""Restore plans (DESIGN-v2 §2): back to a pinned copy for code that can't run on the data."""

import argparse
import unittest
from unittest import mock

from tests._util import app_toml
from tests.fakehost import CRASH_EXIT, FakeHost
from tests.test_crash import SHA1, SHA2, versioned

from huta import cli, recipe
from huta import store as S


class Restore(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.project = self.host.registry["notes"]
        self.t = self.project.targets["staging"]
        app = app_toml("notes")
        self.r1 = self.host.plan(self.project, self.t, versioned(app, 1), SHA1)
        self.assertEqual(self.host.release(self.project, self.t, self.r1), S.LIVE)
        self.r2 = self.host.plan(self.project, self.t, versioned(app, 2, migrate=True), SHA2)
        self.assertEqual(self.host.release(self.project, self.t, self.r2), S.LIVE)
        self.assertEqual(self.host.schema(self.t), 2)
        self.writes_before_r2 = list(self.host.data_state(self.t)["pins"][self.r2])
        self.planned = []
        self.stack = [mock.patch.object(cli, "ctx", self.ctx),
                      mock.patch.object(cli, "say", lambda _m: None),
                      mock.patch.object(cli, "plan_release", self.fake_plan_release),
                      mock.patch.object(cli, "GitHub", lambda platform: None),
                      mock.patch.object(cli, "print_plan", lambda st, rid: None)]
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
        return (self.host.platform, self.host.registry[name] if name else self.host.registry,
                self.host.store())

    def fake_plan_release(self, platform, project, st, t, sha, image_id, tag, gh, note="", images=None):
        rel = st.release(self.r1)
        recipe.migration(st, t, __import__("json").loads(rel["app_toml"]))  # must not refuse
        rid = st.new_release(project=project.name, target=t.name, sha=sha, image_id=image_id,
                             image_tag=tag, app_toml=rel["app_toml"], plan={}, plan_hash="0" * 64,
                             built_by="test")
        self.planned.append(rid)
        return rid

    def plan(self):
        cli.cmd_restore(argparse.Namespace(project="notes", target="staging", to=self.r1))
        st = self.host.store()
        try:
            return st.db.execute("SELECT id FROM plans WHERE kind='restore'"
                                 " ORDER BY created DESC").fetchone()[0]
        finally:
            st.db.close()

    def approve(self, pid, confirm=True, via="cli"):
        return cli.cmd_approve(argparse.Namespace(release=pid, via=via, confirm_data_loss=confirm))

    def plan_state(self, pid):
        st = self.host.store()
        try:
            return st.plan_row(pid)["state"]
        finally:
            st.db.close()

    def check_restored(self):
        self.assertEqual(self.host.data_state(self.t)["writes"], self.writes_before_r2)
        self.assertEqual(self.host.schema(self.t), 1)
        self.assertEqual(self.host.running(self.t), {}, "the target must stay stopped")
        self.assertTrue(recipe.stopped_marker(self.host.platform, self.t).exists())

    def test_old_code_is_refused_on_new_data(self):
        st = self.host.store()
        try:
            app = __import__("json").loads(st.release(self.r1)["app_toml"])
            with self.assertRaisesRegex(recipe.RecipeError, "needs a restore"):
                recipe.migration(st, self.t, app)
        finally:
            st.db.close()

    def test_restore_then_release(self):
        pid = self.plan()
        with self.assertRaisesRegex(cli.ConfigError, "confirm-data-loss"):
            self.approve(pid, confirm=False)
        self.assertEqual(self.plan_state(pid), S.PLANNED)
        self.assertEqual(self.approve(pid), 0)
        self.assertEqual(self.plan_state(pid), "applied")
        self.check_restored()
        self.assertEqual(len(self.planned), 1, "a release of the old code is planned")
        rid = self.planned[0]
        st = self.host.store()
        st.transition(rid, S.PLANNED, S.APPROVED)
        st.db.close()
        self.assertEqual(self.host.release(self.project, self.t, rid), S.LIVE)
        self.assertEqual(self.host.running(self.t), {"a": SHA1})
        self.assertFalse(recipe.stopped_marker(self.host.platform, self.t).exists())
        self.assertEqual(self.host.schema(self.t), 1)

    def test_telegram_button_counts_as_confirmation(self):
        pid = self.plan()
        self.assertEqual(self.approve(pid, confirm=False, via="telegram"), 0)
        self.check_restored()

    def test_changed_point_is_refused(self):
        pid = self.plan()
        self.host.fault(pin_changed=True)
        with self.assertRaisesRegex(cli.ConfigError, "changed since planning"):
            self.approve(pid)
        self.assertEqual(self.plan_state(pid), S.SUPERSEDED)
        self.assertEqual(self.host.schema(self.t), 2, "nothing was restored")

    def test_crash_during_restore(self):
        pid = self.plan()
        base = len(self.host.ops())
        code, err = self.host.in_child(lambda: self.approve(pid), None)
        steps = self.host.ops()[base:]
        self.assertEqual(code, 0, err)
        self.assertTrue(steps)
        for k, step in enumerate(steps, 1):
            with self.subTest(step=f"crash after restore step {k} ({step})"):
                self.tearDown()
                self.setUp()
                pid = self.plan()
                base = len(self.host.ops())
                code, err = self.host.in_child(lambda: self.approve(pid), base + k)
                if code != CRASH_EXIT:
                    continue      # the step came after the restore (planning the release)
                self.host.reconcile(self.project, self.t)
                self.check_restored()
                self.assertEqual(self.plan_state(pid), "applied")


if __name__ == "__main__":
    unittest.main()
