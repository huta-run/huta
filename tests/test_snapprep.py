"""Preparing a data snapshot (docs/PLAN-snapshot.md, milestone S2): input, scratch, sanitize,
check, validation, needle scan, pack; and crashing after every step, then sweeping.

The host is faked at the command level (tests/test_snapshot.Host): docker runs a Python
"sanitizer" that writes into the bind-mounted /snapshot, mount and mkfs only record, `cp -al`
and `rm` are real.
"""

import dataclasses
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
import unittest
from unittest import mock

from tests._util import app_toml, load
from tests.test_snapshot import Host

from huta import recipe, snapprep as SP, snapshot as SN, store as S
from huta.cfg import ConfigError, parse_app_toml  # noqa: F401

SECRET = "s3cr3t-value-123456"
CANARY = "canary.person@example.org"
DB = b"SQLite format 3\0" + b"rows " * 200


class Prep(Host):
    """Host plus a notes/prod target with a snapshot, a live release declaring one, a backup
    copy, a secret and a canary."""

    def __init__(self, sanitizer=None):
        super().__init__()
        _plat, reg = load(self.etc)
        self.project = reg["notes"]
        t = self.project.targets["prod"]
        self.t = dataclasses.replace(t, snapshot={"max": 1 << 20, "keep": 86400, "per_day": 50,
                                                  "scratch": 4 << 20, "canaries": ["CANARY"]})
        self.project.targets["prod"] = self.t
        self.platform = dataclasses.replace(self.platform, secrets_dir=self.etc / "secrets")
        sd = self.etc / "secrets" / "notes"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "NOTES_TOKEN").write_text(SECRET + "\n")
        (sd / "CANARY").write_text(CANARY + "\n")
        app = app_toml("notes")
        app["snapshot"] = [{"name": "db", "cmd": ["sanitize"], "timeout": 60, "component": "web",
                            "check": ["verify"]}]
        self.rid = self.st.new_release(project="notes", target="prod", sha="a" * 40,
                                       image_id="sha256:" + "1" * 64, image_tag="t",
                                       app_toml=json.dumps(app), plan={}, plan_hash="0",
                                       built_by="test")
        self.st.set_state(self.rid, "live")
        self.st.db.execute("INSERT INTO live (project, target, release_id, slot, since)"
                           " VALUES ('notes','prod',?,'a',0)", (self.rid,))
        cur = self.platform.backup["dir"] / "notes" / "prod" / "current"
        cur.mkdir(parents=True)
        (cur / "db.sqlite").write_bytes(DB)
        self.backup_digest = self.digest(cur)
        self.containers: set[str] = set()
        self.mounted: set[str] = set()
        self.sanitizer = sanitizer or self.clean_sanitizer
        self.check_rc = 0
        self.patches2 = [mock.patch.object(SP, "_docker_run", self.docker_run),
                         mock.patch.object(recipe, "check_image", lambda *a, **k: None),
                         mock.patch.object(os, "chown", lambda *a, **k: None),
                         mock.patch.object(SP, "room_check", lambda *a: None)]
        for p in self.patches2:
            p.start()

    def close(self):
        for p in self.patches2:
            p.stop()
        super().close()

    @staticmethod
    def digest(d):
        h = hashlib.sha256()
        for root, dirs, files in sorted(os.walk(d)):
            for f in sorted(files):
                h.update(f.encode() + open(os.path.join(root, f), "rb").read())
        return h.hexdigest()

    # the faked host -------------------------------------------------------

    def sh(self, cmd, timeout=600):
        self.calls.append(list(cmd))
        if cmd[:3] == ["docker", "rm", "-f"]:
            self.containers.discard(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["docker", "ps"]:
            name = cmd[-1].removeprefix("name=^").removesuffix("$")
            gone = name not in self.containers and name not in self.stuck
            return subprocess.CompletedProcess(cmd, 0, "" if gone else "abc\n", "")
        if cmd[0] == "mount":
            self.mounted.add(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "mountpoint":
            return subprocess.CompletedProcess(cmd, 0 if cmd[-1] in self.mounted else 1, "", "")
        if cmd[0] == "umount":
            self.mounted.discard(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] in ("mkfs.ext4", "losetup", "systemctl"):
            if cmd[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] in ("cp", "rm"):
            return subprocess.run(cmd, capture_output=True, text=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def docker_run(self, args, env, name, timeout):
        self.containers.add(name)
        src = {}
        for i, a in enumerate(args):
            if a == "--mount":
                kv = dict(x.split("=", 1) for x in args[i + 1].split(",") if "=" in x)
                src[kv["target"]] = kv["source"]
        out = src["/snapshot"]
        inp = src["/data"]
        if name.endswith("-check"):
            return self.check_rc, b"checked\n"
        return self.sanitizer(out, inp, env)

    def clean_sanitizer(self, out, inp, env):
        with open(os.path.join(out, "db.sqlite"), "wb") as f:
            f.write(open(os.path.join(inp, "db.sqlite"), "rb").read().replace(b"rows", b"anon"))
        os.mkdir(os.path.join(out, "extra"))
        with open(os.path.join(out, "extra", "readme.txt"), "w") as f:
            f.write("sanitized\n")
        return 0, f"wrote 2 files; secret {SECRET} printed\n".encode()

    def make(self):
        return SP.create(self.platform, self.st, self.project, self.t, "db", "dev", "pull")

    def prepare(self, sid=None):
        sid = sid or self.make()
        return sid, SP.prepare(self.platform, self.st, self.project, self.t, sid)

    def leftovers(self, sid):
        w = SN.work_dir(self.platform, "notes", "prod", sid)
        return {"containers": sorted(self.containers), "mounted": sorted(self.mounted),
                "input": SN.input_dir(self.platform, "notes", "prod", sid).exists(),
                "scratch": (w / "scratch.img").exists()}


class Prepare(unittest.TestCase):
    def setUp(self):
        self.h = Prep()

    def tearDown(self):
        self.h.close()

    def test_ready_with_a_deterministic_artifact(self):
        sid, state = self.h.prepare()
        self.assertEqual(state, S.SNAP_READY)
        row = self.h.st.snapshot(sid)
        art = SP.artifact(self.h.platform, row)
        data = art.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
        self.assertEqual((row["size"], row["files"]), (len(data), 2))
        self.assertEqual(self.h.leftovers(sid), {"containers": [], "mounted": [],
                                                  "input": False, "scratch": False})
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
            members = tar.getmembers()
            self.assertEqual([m.name for m in members], ["db.sqlite", "extra",
                                                         "extra/readme.txt"])
            for m in members:
                self.assertEqual((m.uid, m.gid, m.uname, m.mtime), (0, 0, "", 0))
            self.assertIn(b"anon", tar.extractfile("db.sqlite").read())
        self.assertEqual(data[4:8], b"\0\0\0\0", "gzip mtime 0")
        self.assertEqual(self.h.digest(self.h.platform.backup["dir"] / "notes" / "prod" /
                                       "current"), self.h.backup_digest, "backup untouched")
        source = json.loads(row["source"])
        self.assertEqual(source["release"], self.h.rid)
        # The same input packs to the same bytes.
        h2 = Prep()
        try:
            sid2, _ = h2.prepare()
            self.assertEqual(h2.st.snapshot(sid2)["sha256"], row["sha256"])
        finally:
            h2.close()

    def test_output_stays_on_the_host(self):
        sid, _ = self.h.prepare()
        log = (SN.work_dir(self.h.platform, "notes", "prod", sid) / "output.log").read_text()
        self.assertIn("wrote 2 files", log)
        self.assertNotIn(SECRET, log, "the log gate redacted it")
        row = self.h.st.snapshot(sid)
        self.assertNotIn("wrote 2 files", json.dumps([tuple(row)]))
        self.assertIn("output (root only", SN.show_text(self.h.st, sid, self.h.platform))

    def test_refusals(self):
        def writes(files):
            def s(out, inp, env):
                for rel, data in files.items():
                    p = os.path.join(out, rel)
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    if data == "symlink":
                        os.symlink("/etc/passwd", p)
                    elif data == "hardlink":
                        open(p + ".a", "w").write("x")
                        os.link(p + ".a", p)
                    elif data == "fifo":
                        os.mkfifo(p)
                    elif data == "sparse":
                        with open(p, "wb") as f:
                            f.truncate(64 << 20)
                    else:
                        open(p, "wb").write(data)
                return 0, b""
            return s

        cases = {
            "symlink": (writes({"x": "symlink"}), "shape"),
            "symlinked dir": (writes({"d": "symlink"}), "shape"),
            "hard link": (writes({"x": "hardlink"}), "shape"),
            "fifo": (writes({"x": "fifo"}), "shape"),
            "newline": (writes({"a\nb": b"x"}), "shape"),
            "leading dash": (writes({"-rf": b"x"}), "shape"),
            "depth": (writes({"/".join(["d"] * 20) + "/f": b"x"}), "shape"),
            "sparse": (writes({"big": "sparse"}), "too-big"),
            "too big packed": (writes({"r": os.urandom(2 << 20)}), "too-big"),
            "secret": (writes({"x": f"token={SECRET}".encode()}), "secret-found"),
            "secret base64": (writes({"x": __import__("base64").b64encode(SECRET.encode())}),
                              "secret-found"),
            "secret in a name": (writes({SECRET: b"x"}), "secret-found"),
            "canary upper": (writes({"x": CANARY.upper().encode()}), "secret-found"),
            "fails": (lambda out, inp, env: (3, b"boom"), "sanitize-failed"),
            "timeout": (lambda out, inp, env: (124, b""), "timeout"),
        }
        for why, (san, code) in cases.items():
            with self.subTest(why=why):
                h = Prep(san)
                try:
                    sid, state = h.prepare()
                    self.assertEqual(state, S.SNAP_FAILED)
                    self.assertEqual(h.st.snapshot(sid)["refusal"], code)
                    self.assertEqual(h.leftovers(sid), {"containers": [], "mounted": [],
                                                         "input": False, "scratch": False})
                finally:
                    h.close()

    def test_secret_across_a_chunk_boundary(self):
        def s(out, inp, env):
            pad = b"x" * (SP.CHUNK - len(SECRET) // 2)
            open(os.path.join(out, "x"), "wb").write(pad + SECRET.encode() + b"tail")
            return 0, b""
        h = Prep(s)
        try:
            with mock.patch.object(SP, "room_check", lambda *a: None):
                h.t = dataclasses.replace(h.t, snapshot={**h.t.snapshot, "scratch": 8 << 20,
                                                         "max": 8 << 20})
                sid, state = h.prepare()
            self.assertEqual(h.st.snapshot(sid)["refusal"], "secret-found")
        finally:
            h.close()

    def test_partial_pin_is_removed_at_once(self):
        real = self.h.sh

        def cp_fails(cmd, timeout=600):
            if cmd[0] == "cp":
                tmp = cmd[-1]
                os.makedirs(tmp)
                open(os.path.join(tmp, "db.sqlite"), "wb").write(DB)
                return subprocess.CompletedProcess(cmd, 1, "", "No space left")
            return real(cmd, timeout)
        with mock.patch.object(SN, "_sh", cp_fails):
            sid, state = self.h.prepare()
        self.assertEqual(state, S.SNAP_FAILED)
        snaps = SN.input_dir(self.h.platform, "notes", "prod", sid).parent
        self.assertEqual(sorted(os.listdir(snaps)), [], "no production data left behind")

    def test_non_ascii_canary_fails_closed(self):
        (self.h.etc / "secrets" / "notes" / "CANARY").write_text("cänary-123456\n")
        sid, _ = self.h.prepare()
        self.assertEqual(self.h.st.snapshot(sid)["refusal"], "secrets-unreadable")

    def test_export_never_through_a_symlinked_parent(self):
        sid, _ = self.h.prepare()
        real = self.h.root / "real"
        real.mkdir()
        (self.h.root / "link").symlink_to(real)
        with self.assertRaisesRegex(RuntimeError, "no symlink"):
            SP.create_file(str(self.h.root / "link" / "x.tar.gz"))
        self.assertEqual(list(real.iterdir()), [])
        fd = SP.create_file(str(real / "x.tar.gz"))
        os.close(fd)
        with self.assertRaisesRegex(RuntimeError, "exists"):
            SP.create_file(str(real / "x.tar.gz"))

    def test_check_failure(self):
        self.h.check_rc = 1
        sid, state = self.h.prepare()
        self.assertEqual(self.h.st.snapshot(sid)["refusal"], "check-failed")

    def test_the_secret_name_stays_on_the_host(self):
        def leaks(out, inp, env):
            with open(os.path.join(out, "x"), "w") as f:
                f.write(SECRET)
            return 0, b""
        h = Prep(leaks)
        try:
            sid, _ = h.prepare()
            row = h.st.snapshot(sid)
            self.assertEqual(row["refusal"], "secret-found")
            own = [e["msg"] for e in h.st.events(sid, limit=50)]
            self.assertFalse([m for m in own if "NOTES_TOKEN" in m], "not on the snapshot's events")
            self.assertIn("NOTES_TOKEN", SN.show_text(h.st, sid, h.platform))
        finally:
            h.close()

    def test_staged_values_are_never_needles(self):
        staged = self.h.etc / "secrets" / "notes" / ".staged"
        staged.mkdir()
        (staged / "NOTES_TOKEN").write_text("rows rows rows rows\n")   # what the copy holds
        sid, state = self.h.prepare()
        self.assertEqual(state, S.SNAP_READY)

    def test_missing_or_short_canary_fails_closed(self):
        for value in (None, "short"):
            with self.subTest(value=value):
                h = Prep()
                try:
                    f = h.etc / "secrets" / "notes" / "CANARY"
                    if value is None:
                        f.unlink()
                    else:
                        f.write_text(value)
                    sid, state = h.prepare()
                    self.assertEqual(h.st.snapshot(sid)["refusal"], "secrets-unreadable")
                finally:
                    h.close()

    def test_too_old_and_no_backup(self):
        self.h.t = dataclasses.replace(self.h.t, snapshot={**self.h.t.snapshot, "max_age": 60})
        cur = self.h.platform.backup["dir"] / "notes" / "prod" / "current"
        os.utime(cur, (time.time() - 3600, time.time() - 3600))
        sid, _ = self.h.prepare()
        self.assertEqual(self.h.st.snapshot(sid)["refusal"], "too-old")
        h = Prep()
        try:
            import shutil
            shutil.rmtree(h.platform.backup["dir"] / "notes" / "prod" / "current")
            sid, _ = h.prepare()
            self.assertEqual(h.st.snapshot(sid)["refusal"], "no-backup")
        finally:
            h.close()

    def test_create_refusals(self):
        with self.assertRaisesRegex(RuntimeError, "declares no snapshot 'other'"):
            SP.create(self.h.platform, self.h.st, self.h.project, self.h.t, "other", "dev", "pull")
        t = dataclasses.replace(self.h.t, snapshot={})
        with self.assertRaisesRegex(RuntimeError, "allows no snapshots"):
            SP.create(self.h.platform, self.h.st, self.h.project, t, "db", "dev", "pull")

    def test_export_checks_the_hash(self):
        sid, _ = self.h.prepare()
        buf = io.BytesIO()
        n = SP.export(self.h.platform, self.h.st, sid, buf, "owner")
        self.assertEqual(hashlib.sha256(buf.getvalue()).hexdigest(),
                         self.h.st.snapshot(sid)["sha256"])
        self.assertEqual(n, len(buf.getvalue()))
        art = SP.artifact(self.h.platform, self.h.st.snapshot(sid))
        with open(art, "r+b") as f:
            f.seek(10)
            f.write(b"X")
        with self.assertRaisesRegex(RuntimeError, "hash changed"):
            SP.export(self.h.platform, self.h.st, sid, io.BytesIO(), "owner")

    def test_a_second_prepare_of_a_row_does_nothing(self):
        sid, _ = self.h.prepare()
        self.assertEqual(SP.prepare(self.h.platform, self.h.st, self.h.project, self.h.t, sid),
                         S.SNAP_READY)


class PrepareCrash(unittest.TestCase):
    """Die after every step of a prepare; then sweep (twice, the second after a "reboot":
    nothing mounted), and the invariants hold."""

    def steps(self):
        h = Prep()
        try:
            seen = []
            with mock.patch.object(SP, "STEP_HOOK", seen.append):
                h.prepare()
            return seen
        finally:
            h.close()

    def test_crash_after_every_step(self):
        steps = self.steps()
        self.assertEqual(steps, ["input pinned", "scratch image", "scratch mounted", "sanitized",
                                 "checked", "packed", "scratch removed", "ready"])
        for k, step in enumerate(steps):
            with self.subTest(step=step):
                h = Prep()
                try:
                    sid = h.make()
                    pid = os.fork()
                    if pid == 0:
                        def hook(name):
                            if name == step:
                                os._exit(77)
                        try:
                            with mock.patch.object(SP, "STEP_HOOK", hook):
                                SP.prepare(h.platform, S.Store(h.platform.state_dir), h.project,
                                           h.t, sid)
                        finally:
                            os._exit(1)
                    _, status = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 77)
                    # The child's fake host state is lost with it: what it left on disk stays,
                    # and the containers and mounts are treated as still there.
                    names = SN.containers(sid)
                    if step == "sanitized":
                        h.containers.add(names[0])
                    if step == "checked":
                        h.containers.add(names[1])
                    if step in ("scratch mounted", "sanitized", "checked", "packed"):
                        h.mounted.add(str(SN.work_dir(h.platform, "notes", "prod", sid) / "mnt"))
                    h.st.db.execute("UPDATE snapshots SET created=created-1000 WHERE id=?", (sid,))
                    SN.sweep(h.platform, h.st)
                    SN.sweep(h.platform, h.st)
                    row = h.st.snapshot(sid)
                    if step == "ready":
                        self.assertEqual(row["state"], S.SNAP_READY)
                        art = SP.artifact(h.platform, row).read_bytes()
                        self.assertEqual(hashlib.sha256(art).hexdigest(), row["sha256"])
                    else:
                        self.assertEqual(row["state"], S.SNAP_DELETED)
                        self.assertFalse(SN.work_dir(h.platform, "notes", "prod", sid).exists())
                    self.assertEqual(h.leftovers(sid)["containers"], [])
                    self.assertEqual(h.leftovers(sid)["mounted"], [])
                    self.assertFalse(h.leftovers(sid)["input"])
                    self.assertFalse(h.leftovers(sid)["scratch"])
                    self.assertEqual(h.digest(h.platform.backup["dir"] / "notes" / "prod" /
                                              "current"), h.backup_digest)
                    self.assertEqual(h.st.release(h.rid)["state"], "live")
                finally:
                    h.close()


if __name__ == "__main__":
    unittest.main()
