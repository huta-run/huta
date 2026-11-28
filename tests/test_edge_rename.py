"""A live target whose first host changes (the registry dropped or reordered `hosts`): its vhost
file is still found, by its marker line, and the next vhost change writes the new name and
removes the old file in the same transaction."""

import dataclasses
import unittest

from tests._util import app_toml
from tests.fakehost import FakeHost

from huta import edge
from huta import store as S


class FirstHostChanges(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.project = self.host.registry["hello"]
        t = dataclasses.replace(self.project.targets["prod"],
                                hosts=["hello.example.org", "www.hello.example.org"])
        self.project.targets["prod"] = t
        self.r1 = self.host.plan(self.project, t, app_toml("hello"), "1" * 40)
        self.assertEqual(self.host.release(self.project, t, self.r1), S.LIVE)
        self.old = self.host.platform.caddy_sites / "hello.example.org.caddy"
        self.assertTrue(self.old.exists())
        # The apex goes (it now redirects at the CDN): www alone is served.
        self.t = dataclasses.replace(t, hosts=["www.hello.example.org"])
        self.project.targets["prod"] = self.t
        self.new = self.host.platform.caddy_sites / "www.hello.example.org.caddy"

    def tearDown(self):
        self.host.close()

    def test_still_routed_under_the_old_name(self):
        p = self.host.platform
        self.assertFalse(self.new.exists())
        self.assertEqual(edge.installed_path(p, self.t), self.old)
        self.assertIsNotNone(edge.routed_port(p, self.t), "reconcile must see the route")
        self.host.reconcile(self.project, self.t)
        st = self.host.store()
        try:
            self.assertEqual(st.release(self.r1)["state"], S.LIVE)
        finally:
            st.db.close()

    def test_next_release_renames_it(self):
        r2 = self.host.plan(self.project, self.t, app_toml("hello"), "2" * 40)
        self.assertEqual(self.host.release(self.project, self.t, r2), S.LIVE)
        self.assertTrue(self.new.exists())
        self.assertFalse(self.old.exists(), "the old name goes in the same transaction")
        self.assertEqual(set(self.host.loaded_routes()), {"www.hello.example.org"})
        self.assertEqual(self.host.disk_routes(), self.host.loaded_routes())

    def test_edge_change_renames_it(self):
        p = self.host.platform
        port = edge.routed_port(p, self.t)
        with self.host.patched(), edge.edge_lock(p):
            msg, undo = edge.change_vhost(p, self.t, edge.render_vhost(
                p, self.t, [], self.host.cert, port))
        self.assertEqual(msg, "vhost switched, caddy reloaded")
        self.assertTrue(self.new.exists())
        self.assertFalse(self.old.exists())
        self.assertEqual({str(x[0]) for x in undo}, {str(self.new), str(self.old)})

    def test_two_old_files_are_refused(self):
        other = self.host.platform.caddy_sites / "other.example.org.caddy"
        other.write_text(self.old.read_text())
        with self.assertRaisesRegex(edge.EdgeError, "several vhost files"):
            edge.installed_path(self.host.platform, self.t)

    def test_another_targets_file_is_left_alone(self):
        text = self.old.read_text().replace("target prod.", "target staging.")
        stray = self.host.platform.caddy_sites / "x.example.org.caddy"
        stray.write_text(text)
        self.assertEqual(edge.stale_vhosts(self.host.platform, self.t), [self.old])
        r2 = self.host.plan(self.project, self.t, app_toml("hello"), "2" * 40)
        self.host.release(self.project, self.t, r2)
        self.assertTrue(stray.exists())

    def test_undo_brings_the_old_name_back(self):
        state = edge.undo_state(self.host.platform, self.t)
        self.assertEqual(state[0], [str(self.new), None])
        self.assertEqual(state[1][0], str(self.old))


if __name__ == "__main__":
    unittest.main()
