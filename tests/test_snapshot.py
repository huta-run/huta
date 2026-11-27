"""Data snapshots (docs/PLAN-snapshot.md). Milestone S0: the records, one id with plans, the
sweep that recovers them, the host's rm, and the backup publish lock the input will be pinned
under."""

import dataclasses
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._util import etc_copy, load

from huta import data as D
from huta import snapshot as SN
from huta import store as S


SNAP_TOML = '[[snapshot]]\nname = "db"\ncmd = ["python", "/app/app.py", "sanitize"]\n'


def json_plan(st, rid):
    import json
    return json.loads(st.release(rid)["plan"])


class Host:
    """A platform in a temporary directory, with the host commands sweep runs faked: units
    and containers by flags, mounts never, `rm` for real."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="huta-snap-"))
        self.etc = etc_copy()
        plat, _reg = load(self.etc)
        self.platform = dataclasses.replace(
            plat, state_dir=self.root / "state",
            backup={**(plat.backup or {}), "dir": self.root / "backup"},
            pull={**(plat.pull or {}), "outbox": str(self.root / "outbox")})
        self.alive: set[str] = set()
        self.stuck: set[str] = set()       # containers that docker rm can't remove
        self.calls: list[list[str]] = []
        self.patches = [mock.patch.object(S, "assert_root_dir", lambda _p: None),
                        mock.patch.object(SN, "_sh", self.sh)]
        for p in self.patches:
            p.start()
        self.st = S.Store(self.platform.state_dir)

    def close(self):
        self.st.db.close()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.etc, ignore_errors=True)

    def sh(self, cmd, timeout=600):
        self.calls.append(list(cmd))
        ok = subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["systemctl", "is-active"]:
            sid = cmd[2].removeprefix("huta-snapshot-").removesuffix(".service")
            return subprocess.CompletedProcess(cmd, 0 if sid in self.alive else 3,
                                               "active\n" if sid in self.alive else "inactive\n", "")
        if cmd[:2] == ["docker", "ps"]:
            name = cmd[-1].removeprefix("name=^").removesuffix("$")
            return subprocess.CompletedProcess(cmd, 0, "abc\n" if name in self.stuck else "", "")
        if cmd[0] == "mountpoint":
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[0] == "rm":
            return subprocess.run(cmd, capture_output=True, text=True)
        return ok

    def row(self, state=S.SNAP_PREPARING, age=0.0, **fields):
        sid = self.st.new_snapshot(project="hello", target="prod", name="db", box="dev",
                                   transport="pull", release_id="r" * 8,
                                   definition={"name": "db", "cmd": ["x"]}, reason="",
                                   per_day=100, now=time.time() - age)
        if state != S.SNAP_PREPARING or fields:
            self.st.db.execute(f"UPDATE snapshots SET state=?, updated=?"
                               f"{''.join(f', {k}=?' for k in fields)} WHERE id=?",
                               (state, time.time() - age, *fields.values(), sid))
        return sid

    def files(self, sid):
        """Lay out everything a snapshot can leave: work dir, input links, outbox copy."""
        w = SN.work_dir(self.platform, "hello", "prod", sid)
        (w / "mnt").mkdir(parents=True)
        (w / "artifact.tar.gz").write_text("x")
        i = SN.input_dir(self.platform, "hello", "prod", sid)
        i.mkdir(parents=True)
        (i / "db").write_text("data")
        o = SN.outbox_dir(self.platform, sid)
        o.mkdir(parents=True)
        (o / "hello-prod-db.tar.gz").write_text("x")
        return [w, i, o]

    def state(self, sid):
        return self.st.snapshot(sid)["state"]


class Records(unittest.TestCase):
    def setUp(self):
        self.h = Host()
        self.st = self.h.st

    def tearDown(self):
        self.h.close()

    def test_one_open_per_target(self):
        self.h.row()
        with self.assertRaisesRegex(RuntimeError, "already has an open snapshot"):
            self.h.row()
        with self.assertRaises(Exception):         # the index holds even without the check
            self.st.db.execute("INSERT INTO snapshots (id, project, target, name, box, transport,"
                               " release_id, definition, def_hash, state, created, updated)"
                               " VALUES ('0000abcd','hello','prod','db','b','pull','r','{}','h',"
                               "'ready',0,0)")

    def test_per_day_counts_failures(self):
        sid = self.st.new_snapshot(project="hello", target="prod", name="db", box="dev",
                                   transport="pull", release_id="r", definition={}, reason="",
                                   per_day=1)
        self.st.snapshot_transition(sid, S.SNAP_PREPARING, S.SNAP_FAILED, "x")
        with self.assertRaisesRegex(RuntimeError, "at most 1"):
            self.st.new_snapshot(project="hello", target="prod", name="db", box="dev",
                                 transport="pull", release_id="r", definition={}, reason="",
                                 per_day=1)
        self.st.new_snapshot(project="hello", target="prod", name="db", box="dev",
                             transport="pull", release_id="r", definition={}, reason="",
                             per_day=1, now=time.time() + 86400 + 1)

    def test_transitions_only_along_the_steps(self):
        sid = self.h.row()
        with self.assertRaisesRegex(RuntimeError, "not a step"):
            self.st.snapshot_transition(sid, S.SNAP_PREPARING, S.SNAP_DELIVERED)
        with self.assertRaisesRegex(RuntimeError, "is preparing, not ready"):
            self.st.snapshot_transition(sid, S.SNAP_READY, S.SNAP_APPROVED)
        with self.assertRaisesRegex(RuntimeError, "unknown fields"):
            self.st.snapshot_transition(sid, S.SNAP_PREPARING, S.SNAP_READY, state="x")
        self.st.snapshot_transition(sid, S.SNAP_PREPARING, S.SNAP_READY, "ready", size=10,
                                    sha256="ab" * 32, files=1)
        r = self.st.snapshot(sid)
        self.assertEqual((r["state"], r["size"], r["files"]), (S.SNAP_READY, 10, 1))
        self.assertEqual(self.st.events(sid, limit=1)[0]["kind"], "snapshot-ready")

    def test_plan_moves_with_it_or_not_at_all(self):
        sid = self.h.row()
        self.st.snapshot_transition(sid, S.SNAP_PREPARING, S.SNAP_READY)
        self.assertEqual(self.st.new_plan("hello", "prod", "snapshot", {"x": 1}, pid=sid), sid)
        with self.assertRaisesRegex(RuntimeError, "plan"):
            self.st.snapshot_transition(sid, S.SNAP_READY, S.SNAP_APPROVED,
                                        plan=(S.APPROVED, "applied"))
        self.assertEqual(self.h.state(sid), S.SNAP_READY, "rolled back with the plan")
        self.st.snapshot_transition(sid, S.SNAP_READY, S.SNAP_APPROVED,
                                    plan=(S.PLANNED, S.APPROVED))
        self.assertEqual(self.st.plan_row(sid)["state"], S.APPROVED)

    def test_ids_are_unique_across_releases_plans_and_snapshots(self):
        sid = self.h.row()
        seq = iter([sid, "00c0ffee"])
        with mock.patch.object(S.secrets, "token_hex", lambda _n: next(seq)):
            self.assertEqual(self.st.new_id(), "00c0ffee")

    def test_busy_sees_the_snapshot_lock(self):
        fd = self.st.acquire("hello", SN.lock_name("prod"))
        try:
            self.assertTrue(any("_snapshot-prod" in x for x in S.busy(self.h.platform.state_dir)))
        finally:
            os.close(fd)


class Sweep(unittest.TestCase):
    def setUp(self):
        self.h = Host()
        self.st = self.h.st

    def tearDown(self):
        self.h.close()

    def sweep(self, **k):
        SN.sweep(self.h.platform, self.st, **k)

    def test_empty(self):
        self.sweep()
        self.assertEqual(self.st.snapshots(), [])

    def test_young_or_running_prepare_is_left(self):
        young = self.h.row()
        self.sweep()
        self.assertEqual(self.h.state(young), S.SNAP_PREPARING)
        self.st.db.execute("UPDATE snapshots SET created=created-1000 WHERE id=?", (young,))
        self.h.alive.add(young)
        self.sweep()
        self.assertEqual(self.h.state(young), S.SNAP_PREPARING)

    def test_dead_prepare_fails_and_its_files_go(self):
        sid = self.h.row(age=SN.START_BUDGET + 1)
        paths = self.h.files(sid)
        self.sweep()
        self.assertEqual(self.h.state(sid), S.SNAP_DELETED)
        self.assertEqual(self.st.snapshot(sid)["refusal"], "interrupted")
        for p in paths:
            self.assertFalse(p.exists(), p)
        kinds = [e["kind"] for e in self.st.events(sid, limit=10)]
        self.assertIn("snapshot-failed", kinds)

    def test_a_held_lock_skips_the_target(self):
        sid = self.h.row(age=SN.START_BUDGET + 1)
        fd = self.st.acquire("hello", SN.lock_name("prod"))
        try:
            self.sweep()
        finally:
            os.close(fd)
        self.assertEqual(self.h.state(sid), S.SNAP_PREPARING)

    def test_ready_expires_with_its_plan(self):
        sid = self.h.row(S.SNAP_READY, age=SN.PLAN_TTL + 1)
        self.st.new_plan("hello", "prod", "snapshot", {}, pid=sid)
        self.sweep()
        self.assertEqual(self.h.state(sid), S.SNAP_DELETED)
        self.assertEqual(self.st.plan_row(sid)["state"], S.SUPERSEDED)
        fresh = self.h.row(S.SNAP_READY)
        self.sweep()
        self.assertEqual(self.h.state(fresh), S.SNAP_READY)

    def test_delivered_goes_at_its_deadline(self):
        due = self.h.row(S.SNAP_DELIVERED, delete_after=time.time() - 1)
        self.h.files(due)
        self.sweep()
        self.assertEqual(self.h.state(due), S.SNAP_DELETED)
        kept = self.h.row(S.SNAP_DELIVERED, delete_after=time.time() + 3600)
        self.sweep()
        self.assertEqual(self.h.state(kept), S.SNAP_DELIVERED)

    def test_approved_is_left_for_delivery(self):
        sid = self.h.row(S.SNAP_APPROVED, age=10 * 3600)
        self.sweep()
        self.assertEqual(self.h.state(sid), S.SNAP_APPROVED)

    def test_cleanup_that_cant_finish_needs_a_human_and_is_retried(self):
        sid = self.h.row(S.SNAP_FAILED)
        self.h.files(sid)
        self.h.stuck.add(f"huta-snap-{sid}")
        self.sweep()
        self.assertEqual(self.h.state(sid), S.SNAP_NEEDS_HUMAN)
        self.assertTrue(SN.work_dir(self.h.platform, "hello", "prod", sid).exists(),
                        "nothing removed while a container may still use it")
        self.assertIn("snapshot-cleanup-failed", [e["kind"] for e in self.st.events(sid)])
        self.h.stuck.clear()
        self.sweep()
        self.assertEqual(self.h.state(sid), S.SNAP_DELETED)

    def test_leftovers_without_an_open_row_go_others_stay(self):
        owned = self.h.row(S.SNAP_READY)
        keep = self.h.files(owned)
        stray = self.h.files("deadbeef")
        (SN.work_root(self.h.platform) / "hello" / "prod" / "not-an-id").mkdir()
        self.sweep()
        for p in stray:
            if p.parent.name != "snapshots":      # the outbox is the delivery's (S3) business
                self.assertFalse(p.exists(), p)
        for p in keep:
            self.assertTrue(p.exists(), p)
        self.assertTrue((SN.work_root(self.h.platform) / "hello" / "prod" / "not-an-id").exists())

    def test_symlinks_are_never_followed(self):
        sid = self.h.row(S.SNAP_FAILED)
        outside = self.h.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x")
        w = SN.work_dir(self.h.platform, "hello", "prod", sid)
        w.parent.mkdir(parents=True)
        w.symlink_to(outside)
        self.sweep()
        self.assertTrue((outside / "keep").exists())
        self.assertEqual(self.h.state(sid), S.SNAP_NEEDS_HUMAN)

    def test_a_symlinked_ancestor_is_never_followed(self):
        sid = self.h.row(S.SNAP_FAILED)
        outside = self.h.root / "outside"
        (outside / sid).mkdir(parents=True)
        (outside / sid / "keep").write_text("x")
        snaps = SN.input_dir(self.h.platform, "hello", "prod", sid).parent
        snaps.parent.mkdir(parents=True)
        snaps.symlink_to(outside)
        self.sweep()
        self.assertTrue((outside / sid / "keep").exists())
        self.assertEqual(self.h.state(sid), S.SNAP_NEEDS_HUMAN)

    def test_symlinked_mount_or_image_is_never_touched(self):
        for leaf in ("mnt", "scratch.img"):
            with self.subTest(leaf=leaf):
                sid = self.h.row(S.SNAP_FAILED)
                w = SN.work_dir(self.h.platform, "hello", "prod", sid)
                w.mkdir(parents=True)
                (w / leaf).symlink_to(self.h.root)
                self.h.calls.clear()
                self.sweep()
                self.assertEqual(self.h.state(sid), S.SNAP_NEEDS_HUMAN)
                self.assertFalse([c for c in self.h.calls if c[0] in ("umount", "losetup")])
                (w / leaf).unlink()
                self.sweep()
                self.assertEqual(self.h.state(sid), S.SNAP_DELETED)

    def test_input_only_leftover_is_found(self):
        i = SN.input_dir(self.h.platform, "hello", "prod", "deadbeef")
        i.mkdir(parents=True)
        (i / "db").write_text("production")
        self.sweep()
        self.assertFalse(i.exists())

    def test_many_rows_are_all_owned(self):
        ids = []
        for i in range(205):
            sid = self.st.new_snapshot(project="hello", target="prod", name="db", box="dev",
                                       transport="pull", release_id="r", definition={},
                                       reason="", per_day=1000)
            self.st.snapshot_transition(sid, S.SNAP_PREPARING, S.SNAP_READY)
            self.st.db.execute("UPDATE snapshots SET state=?, delete_after=?, created=? WHERE"
                               " id=?", (S.SNAP_DELIVERED, time.time() + 3600, i, sid))
            ids.append(sid)
        oldest = ids[0]
        w = SN.work_dir(self.h.platform, "hello", "prod", oldest)
        w.mkdir(parents=True)
        self.sweep()
        self.assertTrue(w.exists(), "the 201st row still owns its files")

    def test_works_for_a_target_no_longer_in_the_registry(self):
        sid = self.st.new_snapshot(project="gone", target="old", name="db", box="dev",
                                   transport="push", release_id="r", definition={}, reason="",
                                   per_day=1)
        self.st.snapshot_transition(sid, S.SNAP_PREPARING, S.SNAP_FAILED)
        self.sweep()
        self.assertEqual(self.h.state(sid), S.SNAP_DELETED)

    def test_crash_during_cleanup_then_sweep_again(self):
        sid = self.h.row(S.SNAP_REJECTED)
        paths = self.h.files(sid)
        real = SN._rm_strict
        n = {"k": 0}

        def die_after_first(p, parent, root):
            real(p, parent, root)
            n["k"] += 1
            os._exit(77)
        pid = os.fork()
        if pid == 0:
            try:
                with mock.patch.object(SN, "_rm_strict", die_after_first):
                    SN.sweep(self.h.platform, S.Store(self.h.platform.state_dir))
            finally:
                os._exit(77)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 77)
        self.assertEqual(self.h.state(sid), S.SNAP_REJECTED)
        self.sweep()
        self.sweep()
        self.assertEqual(self.h.state(sid), S.SNAP_DELETED)
        for p in paths:
            self.assertFalse(p.exists(), p)


class HostRemove(unittest.TestCase):
    def setUp(self):
        self.h = Host()
        self.st = self.h.st

    def tearDown(self):
        self.h.close()

    def test_ready_is_rejected_with_its_plan_then_deleted(self):
        sid = self.h.row(S.SNAP_READY)
        self.st.new_plan("hello", "prod", "snapshot", {}, pid=sid)
        self.h.files(sid)
        self.assertEqual(SN.remove(self.h.platform, self.st, sid, "owner"), S.SNAP_DELETED)
        self.assertEqual(self.st.plan_row(sid)["state"], S.REJECTED)
        self.assertIn("removed on the host by owner",
                      [e["msg"] for e in self.st.events(sid)])

    def test_running_prepare_is_stopped_first(self):
        sid = self.h.row()
        self.h.alive.add(sid)
        self.assertEqual(SN.remove(self.h.platform, self.st, sid, "owner"), S.SNAP_DELETED)
        self.assertIn(["systemctl", "stop", f"huta-snapshot-{sid}.service"], self.h.calls)

    def test_texts(self):
        sid = self.h.row(S.SNAP_READY, size=4096, sha256="ab" * 32, files=2)
        self.assertIn(sid, SN.list_text(self.st))
        self.assertIn("4 KiB", SN.list_text(self.st))
        self.assertIn("sha256 " + "ab" * 32, SN.show_text(self.st, sid))
        self.assertEqual(SN.list_text(self.st, "other"), "no snapshots")


class Contract(unittest.TestCase):
    """S1: the registry's snapshot key, the pull group, effective and the 📦 plan line."""

    def setUp(self):
        self.etc = etc_copy()

    def tearDown(self):
        shutil.rmtree(self.etc, ignore_errors=True)

    def target(self, line, project="notes", target="prod"):
        from huta import cfg
        f = self.etc / "projects.d" / f"{project}.toml"
        head, sep, tail = f.read_text().partition(f"[targets.{target}]\n")
        f.write_text(head + sep + line + "\n" + tail)
        with mock.patch.object(cfg, "assert_root_dir", lambda _p: None):
            return cfg.load_project(project, self.etc).targets[target]

    def test_defaults(self):
        t = self.target("snapshot = {}")
        self.assertEqual(t.snapshot, {"max": 200 << 20, "keep": 86400, "per_day": 4,
                                      "scratch": 2 << 30, "canaries": []})
        self.assertEqual(self.target("", "notes", "staging").snapshot, {})

    def test_values(self):
        t = self.target('snapshot = { max = "1g", keep = "7d", per_day = 2, scratch = "8g", '
                        'max_age = "3d", canaries = ["CANARY_EMAIL"] }')
        self.assertEqual((t.snapshot["max"], t.snapshot["keep"], t.snapshot["max_age"]),
                         (1 << 30, 7 * 86400, 3 * 86400))
        self.assertEqual(t.snapshot["canaries"], ["CANARY_EMAIL"])

    def test_refused(self):
        from huta.cfg import ConfigError
        for line, pattern in (
                ('snapshot = { max = "3g" }', "out of range"),
                ('snapshot = { keep = "8d" }', "out of range"),
                ('snapshot = { scratch = "100m", max = "200m" }', "at least snapshot.max"),
                ('snapshot = { scratch = "17g" }', "out of range"),
                ("snapshot = { per_day = 0 }", "1..24"),
                ('snapshot = { per_day = true }', "1..24"),
                ('snapshot = { shell = true }', "snapshot = "),
                ('snapshot = { canaries = ["lower"] }', "canaries"),
                ('snapshot = { canaries = [{}] }', "canaries"),
                ('snapshot = { canaries = ["NOTES_TOKEN"] }', "also used by a target"),
                ('snapshot = { canaries = ["ROTATE_KEY"] }', "also used by a target"),
                ('snapshot = { canaries = ["PRICE"] }', "also used by a target")):
            with self.subTest(line=line):
                etc = self.etc
                self.etc = etc_copy()
                try:
                    with self.assertRaisesRegex(ConfigError, pattern):
                        self.target(line)
                finally:
                    shutil.rmtree(self.etc, ignore_errors=True)
                    self.etc = etc

    def test_needs_data_or_postgres(self):
        from huta.cfg import ConfigError
        with self.assertRaisesRegex(ConfigError, "needs data or postgres"):
            self.target("snapshot = {}", "hello", "prod")

    def test_pull_owner_gid(self):
        from huta import cfg
        p = {"inbox": "/srv/i", "outbox": "/srv/o", "container": "dev", "owner_uid": 1001000}
        self.assertNotIn("owner_gid", cfg._pull_cfg(p))
        self.assertEqual(cfg._pull_cfg({**p, "owner_gid": 1001000})["owner_gid"], 1001000)
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(cfg.ConfigError):
                cfg._pull_cfg({**p, "owner_gid": bad})

    def test_effective_only_when_declared(self):
        from tests._util import app_toml
        from huta import recipe
        plat, reg = load(self.etc)
        project = reg["notes"]
        t = project.targets["prod"]
        app = {**app_toml("notes")}
        app.pop("snapshot", None)
        self.assertNotIn("snapshot", recipe.effective(plat, project, t, app))
        app["snapshot"] = [{"name": "db", "cmd": ["x"], "timeout": 600, "component": "web"}]
        self.assertNotIn("snapshot", recipe.effective(plat, project, t, app),
                         "a target that allows none: nothing to run, nothing to show")
        t = dataclasses.replace(t, snapshot={"max": 1, "keep": 1, "per_day": 1, "scratch": 1,
                                             "canaries": []})
        self.assertEqual(recipe.effective(plat, project, t, app)["snapshot"], app["snapshot"])

    def test_plan_lines(self):
        from huta import cli
        a = {"name": "db", "cmd": ["node", "s.mjs"], "timeout": 600, "component": "web"}
        b = {**a, "cmd": ["node", "s2.mjs"], "check": ["node", "v.mjs"], "timeout": 90}
        self.assertEqual(cli.snapshot_lines([a], [a]), [])
        new = cli.snapshot_lines([], [a])
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0].startswith("📦 snapshot db (new, "))
        self.assertTrue(new[0].endswith("runs node s.mjs"))
        self.assertIn("10 min", new[0])
        changed = cli.snapshot_lines([a], [b])[0]
        self.assertIn("(changed, ", changed)
        self.assertTrue(changed.endswith("runs node s2.mjs; check node v.mjs"))
        self.assertIn("90 s", changed)
        # Argument boundaries show, and long commands that differ late differ up front.
        q = cli.snapshot_lines([], [{**a, "cmd": ["sh", "-c", "a b"]}])[0]
        self.assertTrue(q.endswith("runs sh -c 'a b'"))
        x = {**a, "cmd": ["x" * 400, "safe"]}
        y = {**a, "cmd": ["x" * 400, "unsafe"]}
        lx, ly = cli.snapshot_lines([], [x])[0], cli.snapshot_lines([], [y])[0]
        self.assertNotEqual(lx[:40], ly[:40])
        self.assertEqual(cli.snapshot_lines([a], []), ["📦 snapshot db removed"])

    def test_a_release_adding_a_snapshot_is_infra(self):
        """The unattended plan approves only code-only releases: a [[snapshot]] change never
        is one (docs/PLAN-snapshot.md)."""
        from tests._util import IMAGE, ROOT, SHA
        from huta import cli, recipe, runtime as R
        h = Host()
        try:
            _plat, reg = load(self.etc)
            project, t = reg["notes"], reg["notes"].targets["prod"]
            t = dataclasses.replace(t, snapshot={"max": 1 << 20, "keep": 3600, "per_day": 1,
                                                 "scratch": 1 << 21, "canaries": []})
            plat = h.platform
            base = (ROOT / "examples/notes/deploy/app.toml").read_text().split(
                "\n# A data snapshot")[0] + "\n"
            gh = mock.Mock()
            gh.compare.return_value = {"status": "ahead", "ahead": 1, "behind": 0}
            ctx = [mock.patch.object(recipe, "check_image_map", lambda *a, **k: None),
                   mock.patch.object(R, "secret_value", lambda *a: "x" * 16),
                   mock.patch.object(R, "staged", lambda *a: {}),
                   mock.patch.object(R, "staged_list", lambda *a: []),
                   mock.patch.object(R, "live_versions", lambda *a: {}),
                   mock.patch.object(recipe, "migration", lambda *a: None)]
            for c in ctx:
                c.start()
            try:
                gh.read_file.return_value = base
                r1 = cli.plan_release(plat, project, h.st, t, SHA, IMAGE, "huta-build/notes:x", gh)
                h.st.db.execute("UPDATE releases SET state='live' WHERE id=?", (r1,))
                h.st.db.execute("INSERT INTO live (project, target, release_id, slot, since)"
                                " VALUES ('notes','prod',?,'a',0)", (r1,))
                self.assertEqual(json_plan(h.st, r1)["class"], "infra")   # first release
                gh.read_file.return_value = base + "\n" + SNAP_TOML
                r2 = cli.plan_release(plat, project, h.st, t, "b" * 40, IMAGE,
                                      "huta-build/notes:y", gh)
                p2 = json_plan(h.st, r2)
                self.assertEqual(p2["class"], "infra")
                self.assertEqual([c for c in p2["changes"] if c.startswith("📦")],
                                 cli.snapshot_lines([], p2["effective"]["snapshot"]))
                self.assertTrue(any(c.endswith("runs python /app/app.py sanitize")
                                    for c in p2["changes"]))
                self.assertFalse([c for c in p2["changes"] if "changed:" in c and "snapshot" in c])
            finally:
                for c in ctx:
                    c.stop()
        finally:
            h.close()

    def test_plan_line_is_shown_in_telegram(self):
        from huta import notify as N
        with mock.patch.object(S, "assert_root_dir", lambda _p: None):
            st = S.Store(Path(tempfile.mkdtemp(prefix="huta-snap-st-")))
        try:
            plan = {"project": "notes", "target": "prod", "sha": "a" * 40, "class": "infra",
                    "changes": ["changed: env", "📦 snapshot db (new, x): runs node s.mjs",
                                "📦 snapshot big (new, y): runs " + "z" * 2000],
                    "checks": {}, "effective": {}, "from": None}
            rid = st.new_release(project="notes", target="prod", sha="a" * 40, image_id="i",
                                 image_tag="t", app_toml="{}", plan=plan, plan_hash="0",
                                 built_by="dev")
            text = N.render(st, rid)
            self.assertIn("📦 snapshot db (new, x): runs node s.mjs", text)
            self.assertIn(f"(whole: <code>sudo huta show {rid}</code>)", text)
            self.assertNotIn("changed: env", text)
            five = [f"📦 snapshot {'s' * 31}{i} (changed, {'d' * 11}{i}): runs python "
                    + "q" * 950 for i in range(5)]
            st.db.execute("UPDATE releases SET plan=? WHERE id=?",
                          (__import__("json").dumps({**plan, "changes": five}), rid))
            text = N.render(st, rid)
            self.assertLess(len(text), 4096)
            for i in range(5):
                self.assertIn(f"📦 snapshot {'s' * 31}{i} (changed, {'d' * 11}{i}): ", text)
        finally:
            shutil.rmtree(st.dir, ignore_errors=True)


class PublishLock(unittest.TestCase):
    """A pin must never see a generation half-rotated: rotation and pins share a short lock."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="huta-publish-"))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_publish_waits_for_the_lock(self):
        (self.base / "current").mkdir()
        nxt = self.base / "next"
        nxt.mkdir()
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            with D.publish_lock(self.base):
                os.write(w, b"x")
                time.sleep(3)
            os._exit(0)
        os.read(r, 1)
        t = mock.Mock(project="p", name="t")
        with mock.patch.object(D, "PUBLISH_WAIT", 0.5), \
                mock.patch.object(D, "_run", lambda *a, **k: None), \
                self.assertRaisesRegex(D.DataError, "rotated or pinned"):
            D._publish(self.base, nxt, t, time.monotonic(), 0.0, lambda _m: None)
        os.waitpid(pid, 0)
        self.assertTrue(nxt.exists(), "nothing rotated while the lock was held")

    def test_copy_time_is_the_publish_time(self):
        nxt = self.base / "next"
        nxt.mkdir()
        os.utime(nxt, (1, 1))                   # rsync keeps the source's old times
        t = mock.Mock(project="p", name="t")
        with mock.patch.object(D, "_run", lambda *a, **k: None):
            D._publish(self.base, nxt, t, time.monotonic(), 0.0, lambda _m: None)
        self.assertAlmostEqual(D.copy_time(self.base), time.time(), delta=5)
        self.assertIsNone(D.copy_time(self.base / "missing"))


if __name__ == "__main__":
    unittest.main()
