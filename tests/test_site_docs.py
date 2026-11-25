"""The agent-facing site stays one text: llms-full holds every page, the .txt and .md twins match."""

import unittest
from pathlib import Path

from tests._util import ROOT

PUB = ROOT / "site" / "public"


class SiteDocs(unittest.TestCase):
    def test_twins(self):
        for a, b in (("llms.txt", "llms.md"), ("llms-full.txt", "llms-full.md")):
            with self.subTest(file=a):
                self.assertEqual((PUB / a).read_text(), (PUB / b).read_text(), f"{a} != {b}")

    def test_full_holds_every_page(self):
        full = (PUB / "llms-full.txt").read_text()
        for f in [*sorted((PUB / "docs").glob("*.md")), PUB / "host.md", PUB / "install.md"]:
            with self.subTest(page=f.name):
                self.assertIn(f.read_text().strip(), full, f"{f.name} differs from its copy in llms-full")

    def test_no_stale_v1_claims(self):
        text = "\n".join(p.read_text() for p in PUB.rglob("*.md"))
        for stale in ("exactly one HTTP container", "Status: v1", "huta supports exactly one component"):
            with self.subTest(phrase=stale):
                self.assertNotIn(stale, text)


if __name__ == "__main__":
    unittest.main()
