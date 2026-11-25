"""huta.db versioning (DESIGN-v2 §0) and the install guard's busy check."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests._util  # noqa: F401  (puts src/ on the path)

from huta import store
from huta import store as S


def open_store(d: Path) -> S.Store:
    with mock.patch.object(store, "assert_root_dir", lambda _p: None):
        return S.Store(d)


class Versioning(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="huta-store-"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_new_database_gets_the_current_version(self):
        st = open_store(self.d)
        self.assertEqual(st.version(), S.SCHEMA_VERSION)

    def test_database_from_before_versioning_is_kept(self):
        """A v1 huta.db (no user_version, today's tables, real rows) opens and keeps its rows."""
        con = sqlite3.connect(self.d / "huta.db")
        con.executescript(S.SCHEMA)
        con.execute("INSERT INTO releases VALUES ('0123abcd','p','prod','s','i','t','{}','{}',"
                    "'h','dev','live','a',1,1)")
        con.commit()
        con.close()
        st = open_store(self.d)
        self.assertEqual(st.version(), S.SCHEMA_VERSION)
        self.assertEqual(st.release("0123abcd")["state"], "live")

    def test_reopening_is_a_no_op(self):
        open_store(self.d).db.close()
        st = open_store(self.d)
        self.assertEqual(st.version(), S.SCHEMA_VERSION)

    def test_newer_database_is_refused(self):
        open_store(self.d).db.close()
        con = sqlite3.connect(self.d / "huta.db")
        con.execute(f"PRAGMA user_version = {S.SCHEMA_VERSION + 1}")
        con.close()
        with self.assertRaisesRegex(S.StoreError, "newer huta"):
            open_store(self.d)


class Busy(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="huta-busy-"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_idle(self):
        open_store(self.d).db.close()
        self.assertEqual(S.busy(self.d), [])

    def test_missing_state_dir_is_idle(self):
        self.assertEqual(S.busy(self.d / "nothing"), [])

    def test_phase_file(self):
        (self.d / "phases").mkdir()
        (self.d / "phases" / "p--prod.json").write_text("{}")
        (self.d / "phases" / "p--prod.stopped").write_text("x")   # not a release in progress
        self.assertEqual(len(S.busy(self.d)), 1)

    def test_running_release(self):
        st = open_store(self.d)
        rid = st.new_release(project="p", target="prod", sha="s" * 40, image_id="i",
                             image_tag="t", app_toml="{}", plan={}, plan_hash="h", built_by="dev")
        self.assertEqual(S.busy(self.d), [])
        st.transition(rid, S.PLANNED, S.APPROVED)
        self.assertIn("approved", S.busy(self.d)[0])
        st.db.close()

    def test_held_lock(self):
        st = open_store(self.d)
        with st.lock("p", "prod"):
            self.assertIn("p/prod is locked", S.busy(self.d)[0])
        self.assertEqual(S.busy(self.d), [])
        st.db.close()


if __name__ == "__main__":
    unittest.main()
