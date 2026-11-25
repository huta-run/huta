"""Restore points on disk (data.py): hard-linked pins, fingerprints, removal guards."""

import dataclasses
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests._util import etc_copy, load

from huta import data as D


class Pins(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="huta-pins-"))
        etc = etc_copy()
        try:
            platform, reg = load(etc)
        finally:
            shutil.rmtree(etc)
        self.platform = dataclasses.replace(platform, backup={"dir": self.tmp, "ship": ["/bin/true"]})
        self.t = reg["notes"].targets["staging"]
        cur = D.backup_dir(self.platform, self.t) / "current"
        (cur / "sub").mkdir(parents=True)
        (cur / "notes.db").write_text("v1 data")
        (cur / "sub" / "a.txt").write_text("a")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_pin_is_hard_links_with_a_stable_fingerprint(self):
        path, fp = D.pin_copy(self.platform, self.t, "0123abcd")
        cur = D.backup_dir(self.platform, self.t) / "current"
        self.assertEqual(path, D.pins_dir(self.platform, self.t) / "0123abcd")
        self.assertEqual(os.stat(path / "notes.db").st_ino, os.stat(cur / "notes.db").st_ino)
        self.assertEqual(D.fingerprint(path), fp)
        # The next backup generation replaces files (rsync never edits in place): simulate it.
        os.unlink(cur / "notes.db")
        (cur / "notes.db").write_text("v2 data, different")
        self.assertEqual((path / "notes.db").read_text(), "v1 data")
        self.assertEqual(D.fingerprint(path), fp)

    def test_fingerprint_sees_changes(self):
        path, fp = D.pin_copy(self.platform, self.t, "0123abcd")
        os.unlink(path / "sub" / "a.txt")
        self.assertNotEqual(D.fingerprint(path), fp)

    def test_drop_and_guards(self):
        path, _ = D.pin_copy(self.platform, self.t, "0123abcd")
        D.drop_pin(self.platform, self.t, "0123abcd")
        self.assertFalse(path.exists())
        with self.assertRaises(D.DataError):
            D._rm_pin(D.backup_dir(self.platform, self.t) / "current")
        with self.assertRaises(D.DataError):
            D.pin_copy(self.platform, self.t, "../etc")

    def test_restore_refuses_foreign_paths(self):
        with self.assertRaisesRegex(D.DataError, "not one of this target's"):
            D.restore_pin(self.platform, None, self.t, self.tmp, "x", print)


if __name__ == "__main__":
    unittest.main()
