"""Crash tests: kill huta after every step of a release, recover, check what's left.

Each scenario sets a target up (usually one clean release r1, live), applies its faults, then
releases r2. A clean run of r2 gives the list of steps. For every step k, a fresh host runs r2
in a child that dies right after step k; then:

- test_crash_after_every_step: reconcile runs, the invariants must hold, a second reconcile
  changes nothing, and after a reboot plus reconcile they still hold;
- test_reconcile_crashes_too: reconcile itself dies after each of its own steps, and a final
  reconcile must still reach the invariants.

Invariants (check):
- no release is left running or approved; r2 ends in an outcome the scenario allows;
- no phase file and no pending edge journal is left;
- the vhost on disk and the config Caddy loaded agree;
- if something is live, the routed slot runs it, every other slot is stopped, and restart
  policies match (routed: unless-stopped, others: no); if nothing is live, nothing runs;
- one-writer data: no two writers ever ran at once; if r2 is live its writes are there; if
  not, its writes are gone and every write from before r2 is still there.
"""

import json
import unittest

from tests._util import app_toml
from tests.fakehost import CRASH_EXIT, FakeHost

from huta import edge, recipe
from huta import store as S

SHA1 = "1" * 40
SHA2 = "2" * 40
LIVE, NA, RB, NH = S.LIVE, S.NOTHING_ACTIVATED, S.ROLLED_BACK, S.NEEDS_HUMAN


class Scenario:
    project_name = target_name = ""
    first_release = False          # no r1: r2 is the target's first release
    adopt_file = ""                # a hand-written vhost r2 replaces (first release only)
    one_writer = False
    faults: dict = {}
    clean: str = LIVE              # r2's outcome without a crash
    allowed: set = {LIVE, NA}      # r2's outcomes after a crash and reconcile

    # -------------------------------------------------------- setup

    def setup_host(self):
        host = FakeHost()
        project = host.registry[self.project_name]
        t = project.targets[self.target_name]
        app = app_toml(self.project_name)
        r1 = None
        if self.adopt_file:
            (host.platform.caddy_sites / self.adopt_file).write_text(
                f"{t.hosts[0]} {{\n\treverse_proxy 127.0.0.1:9999\n}}\n")
            host._reload_caddy()
        if not self.first_release:
            r1 = host.plan(project, t, app, SHA1)
            self.assertEqual(host.release(project, t, r1), LIVE, "setup: r1 did not go live")
        if self.faults:
            host.fault(**self.faults)
        r2 = host.plan(project, t, app, SHA2)
        before = list(host.data_state(t)["writes"])
        return host, project, t, r1, r2, before

    def clean_steps(self):
        host, project, t, r1, r2, _ = self.setup_host()
        try:
            base = len(host.ops())
            return host.release(project, t, r2), host.ops()[base:]
        finally:
            host.close()

    # -------------------------------------------------------- invariants

    def check(self, host, project, t, r1, r2, before, where):
        st = host.store()
        try:
            rows = {r["id"]: r for r in st.db.execute("SELECT * FROM releases")}
            live = st.live(project.name, t.name)
        finally:
            st.db.close()
        stuck = [i for i, r in rows.items() if r["state"] in (S.RUNNING, S.APPROVED)]
        self.assertFalse(stuck, f"{where}: release(s) {stuck} left running/approved")
        self.assertIn(rows[r2]["state"], self.allowed, f"{where}: r2 ended {rows[r2]['state']}")
        self.assertIsNone(recipe._phase_get(host.platform, t), f"{where}: phase file left")
        pending = list((host.platform.state_dir / "edge").glob("*.pending"))
        self.assertFalse(pending, f"{where}: edge journal left pending: {pending}")
        self.assertEqual(host.disk_routes(), host.loaded_routes(),
                         f"{where}: vhost files on disk and the config Caddy loaded disagree")
        disk = edge.routed_port(host.platform, t)
        running = host.running(t)
        if live:
            routed = dict(zip(t.slots, recipe.SLOTS)).get(disk)
            self.assertEqual(routed, live["slot"], f"{where}: vhost and live pointer disagree")
            self.assertEqual(running, {routed: rows[live["release_id"]]["sha"]},
                             f"{where}: running {running}, routed {routed}")
            for c in host.containers(t):
                want = "unless-stopped" if c["labels"]["run.huta.slot"] == routed else "no"
                self.assertEqual(c["restart"], want,
                                 f"{where}: slot {c['labels']['run.huta.slot']} restart policy")
        else:
            self.assertEqual(running, {}, f"{where}: nothing is live but {running} runs")
            if self.adopt_file:
                self.assertTrue((host.platform.caddy_sites / self.adopt_file).exists(),
                                f"{where}: the adopted site's own file is gone")
        if self.one_writer:
            ds = host.data_state(t)
            self.assertEqual(ds["overlaps"], [], f"{where}: two writers ran at once")
            if live and live["release_id"] == r2:
                self.assertIn(SHA2, ds["writes"], f"{where}: r2 is live but its writes are gone")
            else:
                self.assertNotIn(SHA2, ds["writes"], f"{where}: r2's writes survived")
                self.assertEqual(ds["writes"][:len(before)], before,
                                 f"{where}: writes from before r2 were lost")
        return live

    def snapshot(self, host):
        st = host.store()
        try:
            db = ([tuple(r) for r in st.db.execute(
                "SELECT id, state, slot FROM releases ORDER BY id")],
                [tuple(r) for r in st.db.execute("SELECT * FROM live")])
        finally:
            st.db.close()
        files = {str(p.relative_to(host.root)): p.read_text()
                 for d in ("sites", "state/phases", "state/edge") if (host.root / d).exists()
                 for p in sorted((host.root / d).rglob("*")) if p.is_file()
                 and not p.name.endswith(".lock")}
        return (json.dumps(host._load("docker"), sort_keys=True),
                json.dumps(host._load("caddy"), sort_keys=True),
                json.dumps(host._load("data"), sort_keys=True), db, files)

    def recover_and_check(self, host, project, t, r1, r2, before, where):
        host.reconcile(project, t)
        self.check(host, project, t, r1, r2, before, where)
        snap = self.snapshot(host)
        host.reconcile(project, t)
        self.assertEqual(snap, self.snapshot(host), f"{where}: a second reconcile changed state")
        host.reboot()
        host.reconcile(project, t)
        self.check(host, project, t, r1, r2, before, f"{where}, after a reboot")

    # -------------------------------------------------------- tests

    def test_clean_release(self):
        out, steps = self.clean_steps()
        self.assertTrue(steps, "the release took no steps")
        self.assertEqual(out, self.clean, steps)

    def test_crash_after_every_step(self):
        _out, steps = self.clean_steps()
        for k, step in enumerate(steps, 1):
            where = f"crash after step {k} ({step})"
            with self.subTest(step=where):
                host, project, t, r1, r2, before = self.setup_host()
                try:
                    base = len(host.ops())
                    code, err = host.in_child(lambda: host.release(project, t, r2), base + k)
                    self.assertEqual(code, CRASH_EXIT, f"{where}: child ended early\n{err}")
                    self.recover_and_check(host, project, t, r1, r2, before, where)
                finally:
                    host.close()

    def test_reconcile_crashes_too(self):
        _out, steps = self.clean_steps()
        for k, step in enumerate(steps, 1):
            j = 1
            while True:
                where = f"crash after step {k} ({step}), then reconcile dies after its step {j}"
                host, project, t, r1, r2, before = self.setup_host()
                try:
                    base = len(host.ops())
                    code, err = host.in_child(lambda: host.release(project, t, r2), base + k)
                    self.assertEqual(code, CRASH_EXIT, f"{where}: child ended early\n{err}")
                    base = len(host.ops())
                    code, err = host.in_child(lambda: host.reconcile(project, t), base + j)
                    if code != CRASH_EXIT:
                        # Reconcile ended before step j, so all of its steps are covered. If it
                        # failed instead, test_crash_after_every_step reports why.
                        break
                    with self.subTest(step=where):
                        self.recover_and_check(host, project, t, r1, r2, before, where)
                finally:
                    host.close()
                j += 1


# ------------------------------------------------------------ scenarios


class StatelessRelease(Scenario, unittest.TestCase):
    """Both slots run during the swap; the vhost switch is the only cut-over."""
    project_name, target_name = "hello", "prod"


class StatelessFirstRelease(Scenario, unittest.TestCase):
    project_name, target_name = "hello", "prod"
    first_release = True


class StatelessPublicFail(Scenario, unittest.TestCase):
    """Public checks fail after the switch: the vhost goes back to the old slot."""
    project_name, target_name = "hello", "prod"
    faults = {"public_fail": True}
    clean, allowed = RB, {NA, RB}


class PublicFailNoCert(Scenario, unittest.TestCase):
    """As StatelessPublicFail, but when reconcile finishes the checks the newest certificate
    pair is unusable (the vhost still names a working one). The revert must not depend on it."""
    project_name, target_name = "hello", "prod"
    faults = {"public_fail": True, "no_cert": True}
    clean, allowed = RB, {NA, RB, NH}


class PublicFailNoCloudflare(Scenario, unittest.TestCase):
    """As StatelessPublicFail, but Cloudflare can't be reached while reconcile recovers."""
    project_name, target_name = "hello", "prod"
    faults = {"public_fail": True, "no_cf": True}
    clean, allowed = RB, {NA, RB, NH}


class PublicFailOldPhase(Scenario, unittest.TestCase):
    """A phase file from an older huta (no saved vhost) when reconcile must revert: it points
    the installed vhost back at the old slot, still without a certificate check or Cloudflare."""
    project_name, target_name = "hello", "prod"
    faults = {"public_fail": True, "old_phase": True, "no_cert": True, "no_cf": True}
    clean, allowed = RB, {NA, RB, NH}


class StatelessSwitchRefused(Scenario, unittest.TestCase):
    """Caddy refuses the new config: nothing changes."""
    project_name, target_name = "hello", "prod"
    faults = {"caddy_invalid": 19101}      # slot b, where r2 starts
    clean, allowed = NA, {NA}


class FirstReleasePublicFail(Scenario, unittest.TestCase):
    """A first release whose public checks fail: nothing to go back to, a human decides."""
    project_name, target_name = "hello", "prod"
    first_release = True
    faults = {"public_fail": True}
    clean, allowed = NH, {NA, NH}


class AdoptionRelease(Scenario, unittest.TestCase):
    """The first release replaces a hand-written vhost serving the same host."""
    project_name, target_name = "hello", "legacy"
    first_release, adopt_file = True, "legacy-site.caddy"


class AdoptionPublicFail(Scenario, unittest.TestCase):
    """The adopted site's own file comes back when the first release fails its checks."""
    project_name, target_name = "hello", "legacy"
    first_release, adopt_file = True, "legacy-site.caddy"
    faults = {"public_fail": True}
    clean, allowed = RB, {NA, RB}


class OneWriterRelease(Scenario, unittest.TestCase):
    """Data without concurrent: stop old → copy → start new → checks → switch."""
    project_name, target_name = "notes", "staging"
    one_writer = True


class OneWriterUnhealthy(Scenario, unittest.TestCase):
    """The new slot never gets healthy: the copy is restored and the old slot comes back."""
    project_name, target_name = "notes", "staging"
    one_writer = True
    faults = {"unhealthy": SHA2}
    clean, allowed = NA, {NA}


class OneWriterPublicFail(Scenario, unittest.TestCase):
    """Public checks fail after a one-writer switch: no automatic way back (the new release
    has written the data); a human decides."""
    project_name, target_name = "notes", "staging"
    one_writer = True
    faults = {"public_fail": True}
    clean, allowed = NH, {NA, NH}


class ConcurrentData(Scenario, unittest.TestCase):
    """Data that both the repo and the host allow two releases on (overlapping slots)."""
    project_name, target_name = "notes", "prod"


if __name__ == "__main__":
    unittest.main()


class PhaseRecords(unittest.TestCase):
    """A phase record reconcile can't resolve is kept and reported, never cleared."""

    def run_with_phase(self, text):
        host = FakeHost()
        try:
            project = host.registry["hello"]
            t = project.targets["prod"]
            r1 = host.plan(project, t, app_toml("hello"), SHA1)
            self.assertEqual(host.release(project, t, r1), LIVE)
            f = recipe._phase_file(host.platform, t)
            f.write_text(text)
            with self.assertRaisesRegex(recipe.RecipeError, "needs a human"):
                host.reconcile(project, t)
            self.assertEqual(f.read_text(), text, "the phase file was changed")
            st = host.store()
            try:
                ev = st.db.execute("SELECT msg FROM events WHERE kind='needs-human'").fetchall()
            finally:
                st.db.close()
            self.assertTrue(ev, "no needs-human event recorded")
        finally:
            host.close()

    def test_unreadable(self):
        self.run_with_phase("{not json")

    def test_unknown_stage(self):
        self.run_with_phase(json.dumps({"v": 2, "rid": "x", "stage": "teleporting", "idle": "b"}))

    def test_newer_version(self):
        self.run_with_phase(json.dumps({"v": 99, "rid": "x", "stage": "checks", "idle": "b"}))
