"""External publishing (DESIGN-v2 §5): what comes out of an image, and the two contracts."""

import io
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

import tests._util  # noqa: F401

from huta import external as E


def tar_of(entries) -> io.BytesIO:
    """entries: (name, kind, data) with kind "f", "d", "l" (symlink), "c" (char device)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, kind, data in entries:
            ti = tarfile.TarInfo(name)
            if kind == "d":
                ti.type = tarfile.DIRTYPE
                tar.addfile(ti)
            elif kind == "l":
                ti.type, ti.linkname = tarfile.SYMTYPE, data
                tar.addfile(ti)
            elif kind == "c":
                ti.type = tarfile.CHRTYPE
                tar.addfile(ti)
            else:
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    return buf


class Files(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="huta-ext-"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_plain_files(self):
        h, n, size = E._validate_tar(tar_of([("out", "d", None), ("out/index.html", "f", b"<h1>"),
                                             ("out/a", "d", None), ("out/a/b.css", "f", b"x{}")]),
                                     self.d, "out")
        self.assertEqual((n, size), (2, 7))
        self.assertEqual((self.d / "a" / "b.css").read_bytes(), b"x{}")
        self.assertEqual(E.content_hash(self.d), h)

    def test_refused(self):
        for entries in ([("out", "d", None), ("out/x", "l", "/etc/passwd")],
                        [("out", "d", None), ("out/../x", "f", b"")],
                        [("other/x", "f", b"")],
                        [("out", "d", None), ("out/dev", "c", None)],
                        [("out", "d", None), ("out/a\nb", "f", b"")]):
            with self.subTest(entries=entries):
                d = Path(tempfile.mkdtemp(dir=self.d))
                with self.assertRaises(E.PublishError):
                    E._validate_tar(tar_of(entries), d, "out")

    def test_size_cap(self):
        big = b"x" * (E.MAX_FILE + 1)
        with self.assertRaisesRegex(E.PublishError, "too much"):
            E._validate_tar(tar_of([("out", "d", None), ("out/big", "f", big)]), self.d, "out")

    def test_contracts(self):
        (self.d / "index.html").write_text("ok")
        E.check_contract("cloudflare-pages", self.d, None)
        (self.d / "_worker.js").write_text("code")
        with self.assertRaisesRegex(E.PublishError, "may not include code"):
            E.check_contract("cloudflare-pages", self.d, None)
        with self.assertRaisesRegex(E.PublishError, "not in the files"):
            E.check_contract("cloudflare-worker", self.d, "index.js")
        E.check_contract("cloudflare-worker", self.d, "_worker.js")

    def test_worker_config(self):
        reg = {"name": "edge", "account": "0" * 32, "compatibility_date": "2026-11-01",
               "route_zones": [("example.org/api/*", "example.org")]}
        text = E._worker_config(reg, {"main": "index.js"}, "s" * 40, "c" * 64)
        self.assertIn('name = "edge"', text)
        self.assertIn('main = "/src/index.js"', text)
        self.assertIn("no_bundle = true", text)
        self.assertIn('pattern = "example.org/api/*", zone_name = "example.org"', text)


if __name__ == "__main__":
    unittest.main()
