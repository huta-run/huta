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
    schema_r1 = schema_r2 = None   # migration scenarios: the schema each release declares
    migrate_r2 = False
    online = False                 # r2 migrates online (r1 runs on both schemas; never restores)
    check_r2 = True                # with online: the repo's [migrate] check exists
    monitored = False              # the target reports errors, the repo asks for SENTRY_DSN

    def app(self):
        """The manifest both releases use (scenarios with several components shape it)."""
        app = app_toml(self.project_name)
        if self.monitored:
            app["monitor"] = {"env": ["sentry"]}
        return app
    clean: str = LIVE              # r2's outcome without a crash
    allowed: set = {LIVE, NA}      # r2's outcomes after a crash and reconcile

    # -------------------------------------------------------- setup

    def setup_host(self):
        host = FakeHost()
        project = host.registry[self.project_name]
        t = project.targets[self.target_name]
        if self.monitored:
            import dataclasses
            from huta import runtime
            host.platform = dataclasses.replace(host.platform, monitoring={
                "prefix": "huta", "healthchecks": None, "matomo": None,
                "bugsink": {"dsn_host": "bugs.example.org", "team": "0" * 8 + "-0000-0000-0000-" + "0" * 12}})
            t = dataclasses.replace(t, monitor={"errors": True},
                                    monitor_egress=["bugs.example.org:443"])
            project.targets[t.name] = t
            d = runtime.secret_dir(host.platform, project.name)
            d.mkdir(parents=True, exist_ok=True)
            (d / "HUTA_SENTRY_DSN_PROD").write_text("https://k@bugs.example.org/1\n")
        app = self.app()
        r1 = None
        if self.adopt_file:
            (host.platform.caddy_sites / self.adopt_file).write_text(
                f"{t.hosts[0]} {{\n\treverse_proxy 127.0.0.1:9999\n}}\n")
            host._reload_caddy()
        if not self.first_release:
            a1 = versioned(app, self.schema_r1)
            if self.online:
                a1["state"]["runs_on"] = [self.schema_r1, self.schema_r2]
            r1 = host.plan(project, t, a1, SHA1)
            self.assertEqual(host.release(project, t, r1), LIVE, "setup: r1 did not go live")
        if self.faults:
            host.fault(**self.faults)
        a2 = versioned(app, self.schema_r2, self.migrate_r2)
        if self.online:
            a2["migrate"]["online"] = True
            if self.check_r2:
                a2["migrate"]["check"] = ["python", "/app/app.py", "schema"]
        r2 = host.plan(project, t, a2, SHA2)
        self.ops_before_r2 = len(host.ops())
        self.schema_before = host.schema(t)
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
            live_sha = rows[live["release_id"]]["sha"]
            for c in host.containers(t):
                slot, comp = c["labels"]["run.huta.slot"], c["labels"].get("run.huta.component", "")
                want = "unless-stopped" if slot == routed else "no"
                self.assertEqual(c["restart"], want, f"{where}: slot {slot} {comp} restart policy")
                if slot == routed:
                    self.assertTrue(c["running"], f"{where}: routed {comp} not running")
                    self.assertEqual(c["labels"]["run.huta.sha"], live_sha, f"{where}: {comp} sha")
                else:
                    self.assertFalse(c["running"], f"{where}: unrouted slot {slot} {comp} runs")
            if json.loads(rows[live["release_id"]]["app_toml"])["components"].keys() - {""}:
                app_live = json.loads(rows[live["release_id"]]["app_toml"])
                have = {c["labels"].get("run.huta.component") for c in host.containers(t)
                        if c["labels"]["run.huta.slot"] == routed}
                if len(app_live["components"]) > 1:
                    self.assertEqual(have, set(app_live["components"]),
                                     f"{where}: routed slot lacks components")
        else:
            self.assertEqual(running, {}, f"{where}: nothing is live but {running} runs")
            if self.adopt_file:
                self.assertTrue((host.platform.caddy_sites / self.adopt_file).exists(),
                                f"{where}: the adopted site's own file is gone")
        self.assertEqual(host._load("docker").get("worker_overlaps", []), [],
                         f"{where}: two copies of a worker ran at once")
        deploys = host._load("cf")["deploys"]
        keys = [(x["name"], x["sha"], x["content"]) for x in deploys]
        self.assertEqual(len(keys), len(set(keys)), f"{where}: the same files published twice")
        if live and live["release_id"] == r2 and rows[r2]["state"] == LIVE:
            app2 = json.loads(rows[r2]["app_toml"])
            for name in app2.get("external") or {}:
                st = host.store()
                try:
                    rec = st.db.execute("SELECT 1 FROM external_deploys WHERE release_id=? AND"
                                        " name=?", (r2, name)).fetchone()
                finally:
                    st.db.close()
                self.assertTrue(rec, f"{where}: r2 is live but {name} was never published")
        if self.one_writer:
            ds = host.data_state(t)
            self.assertEqual(ds["overlaps"], [], f"{where}: two writers ran at once")
            if live and live["release_id"] == r2:
                self.assertIn(SHA2, ds["writes"], f"{where}: r2 is live but its writes are gone")
            else:
                self.assertFalse([w for w in ds["writes"] if SHA2 in w],
                                 f"{where}: r2's writes (or its migration's) survived")
                self.assertEqual(ds["writes"][:len(before)], before,
                                 f"{where}: writes from before r2 were lost")
        if self.schema_r2 is not None:
            if live and live["release_id"] == r2:
                self.assertEqual(host.schema(t), self.schema_r2, f"{where}: recorded schema")
            elif self.online:
                # Online never restores: the schema is wherever the migration left it, and the
                # record agrees with what the data reports (unless a human must look).
                if rows[r2]["state"] != NH:
                    self.assertEqual(host.schema(t), host.data_state(t).get("schema", self.schema_before),
                                     f"{where}: recorded schema vs the data")
            else:
                self.assertEqual(host.schema(t), self.schema_before, f"{where}: recorded schema")
        if self.online:
            self.assertNotIn("data restore", host.ops()[self.ops_before_r2:],
                             f"{where}: an online migration was restored")
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


def versioned(app, schema, migrate=False):
    """The manifest with [state] schema (and a [migrate]) as a release declares them."""
    if schema is None:
        return app
    app = json.loads(json.dumps(app))
    app["state"] = {**app["state"], "schema": schema, "runs_on": [schema]}
    app.pop("migrate", None)
    if migrate:
        app["migrate"] = {"image": "web", "cmd": ["python", "/app/app.py", "migrate"],
                          "timeout": "5m", "timeout_s": 300}
    return app


# ------------------------------------------------------------ scenarios


class StatelessRelease(Scenario, unittest.TestCase):
    """Both slots run during the swap; the vhost switch is the only cut-over."""
    project_name, target_name = "hello", "prod"


class MonitoredRelease(Scenario, unittest.TestCase):
    """A release that gets SENTRY_DSN and the Bugsink host in its egress
    (docs/PLAN-monitoring.md milestone 2): recovery is as for any release."""
    project_name, target_name = "notes", "prod"
    monitored = True


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


class MigrateRelease(Scenario, unittest.TestCase):
    """Exclusive migration on a one-writer target: stop → copy → pin → migrate → start → switch."""
    project_name, target_name = "notes", "staging"
    one_writer = True
    schema_r1, schema_r2, migrate_r2 = 1, 2, True


class MigrateOnConcurrentData(Scenario, unittest.TestCase):
    """A target whose releases may overlap still migrates exclusive: the old slot stops first."""
    project_name, target_name = "notes", "prod"
    one_writer = True
    schema_r1, schema_r2, migrate_r2 = 1, 2, True


class MigrateFails(Scenario, unittest.TestCase):
    """The migration exits non-zero: the copy goes back, the schema stays, the old slot runs."""
    project_name, target_name = "notes", "staging"
    one_writer = True
    schema_r1, schema_r2, migrate_r2 = 1, 2, True
    faults = {"migrate_fail": True}
    clean, allowed = NA, {NA}


class MigrateUnhealthy(Scenario, unittest.TestCase):
    """The migration works, the new code never gets healthy: undone, nothing lost."""
    project_name, target_name = "notes", "staging"
    one_writer = True
    schema_r1, schema_r2, migrate_r2 = 1, 2, True
    faults = {"unhealthy": SHA2}
    clean, allowed = NA, {NA}


class MigratePublicFail(Scenario, unittest.TestCase):
    """Migrated and switched, public checks fail: the new code keeps serving; a human decides."""
    project_name, target_name = "notes", "staging"
    one_writer = True
    schema_r1, schema_r2, migrate_r2 = 1, 2, True
    faults = {"public_fail": True}
    clean, allowed = NH, {NA, NH}


class MigrateFirstRelease(Scenario, unittest.TestCase):
    """A first release that migrates fresh data: copied first, undone the same way."""
    project_name, target_name = "notes", "staging"
    first_release = True
    one_writer = True
    schema_r2, migrate_r2 = 1, True


def stateless(app):
    """The shop manifest without data: its slots may overlap (workers still hand over)."""
    app = json.loads(json.dumps(app))
    app["state"] = {"kind": "none"}
    for c in app["components"].values():
        c.pop("data", None)
    return app


class SeveralOneWriter(Scenario, unittest.TestCase):
    """web + api + worker on data: the whole old slot stops, the new one starts (workers after
    the switch)."""
    project_name, target_name = "shop", "prod"
    one_writer = True


class SeveralConcurrent(Scenario, unittest.TestCase):
    """web + api + worker without data: slots overlap; the old worker stops before the new one
    starts, after the switch."""
    project_name, target_name = "shop", "live"

    def app(self):
        return stateless(app_toml("shop"))


class SeveralWorkerDies(Scenario, unittest.TestCase):
    """The new worker exits in its liveness window: back to the old slot and its worker."""
    project_name, target_name = "shop", "live"
    faults = {"worker_dies": True}
    # A dying worker must never leave the release recorded live (codex, v2 code review).
    clean, allowed = RB, {NA, RB, NH}

    def app(self):
        return stateless(app_toml("shop"))


class SeveralFromOne(Scenario, unittest.TestCase):
    """A target moving from one component to several: r1 is web only, r2 adds an api and a
    worker."""
    project_name, target_name = "shop", "live"

    def setup_host(self):
        self._r = 0
        return super().setup_host()

    def app(self):
        self._r = getattr(self, "_r", 0) + 1
        full = stateless(app_toml("shop"))
        if self._r == 1:
            web = dict(full["components"]["web"])
            web.pop("public", None)
            return {**full, "order": ["web"], "components": {"web": web},
                    "images": {"web": full["images"]["web"]}}
        return full


class PgMigrate(Scenario, unittest.TestCase):
    """A target on its own Postgres: the migration dumps and pins, runs on the database's own
    network, and any failure restores the dump."""
    project_name, target_name = "ledger", "prod"
    one_writer = True
    schema_r1, schema_r2, migrate_r2 = 1, 2, True


class PgMigrateFails(Scenario, unittest.TestCase):
    project_name, target_name = "ledger", "prod"
    one_writer = True
    schema_r1, schema_r2, migrate_r2 = 1, 2, True
    faults = {"migrate_fail": True}
    clean, allowed = NA, {NA}


class PgRelease(Scenario, unittest.TestCase):
    """No migration: both slots use the database at once (Postgres handles concurrent clients)."""
    project_name, target_name = "ledger", "prod"
    schema_r1, schema_r2 = 1, 1


class Publish(Scenario, unittest.TestCase):
    """A release that publishes a Pages site and a Worker after its switch."""
    project_name, target_name = "site", "prod"


class PublishFails(Scenario, unittest.TestCase):
    """Publishing fails: the host target stays live, the release is needs-human."""
    project_name, target_name = "site", "prod"
    faults = {"publish_fail": True}

    def setup_host(self):
        # r1 publishes fine; the fault applies to r2 only (the base class sets faults after r1).
        return super().setup_host()
    clean, allowed = NH, {NA, NH}


class OnlineMigrate(Scenario, unittest.TestCase):
    """Online: the old release keeps serving while the migration runs; nothing is restored."""
    project_name, target_name = "notes", "prod"
    schema_r1, schema_r2, migrate_r2, online = 1, 2, True, True
    allowed = {LIVE, NA, NH}


class OnlineMigrateFails(Scenario, unittest.TestCase):
    """The online migration fails: needs-human, the old release still serves, no restore."""
    project_name, target_name = "notes", "prod"
    schema_r1, schema_r2, migrate_r2, online = 1, 2, True, True
    faults = {"migrate_fail": True}
    clean, allowed = NH, {NA, NH}


class OnlineNoCheck(Scenario, unittest.TestCase):
    """Online without a check: a crash mid-migration can't be resolved, so a human decides."""
    project_name, target_name = "notes", "prod"
    schema_r1, schema_r2, migrate_r2, online, check_r2 = 1, 2, True, True, False
    allowed = {LIVE, NA, NH}


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
