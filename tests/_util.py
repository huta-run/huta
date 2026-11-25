"""Shared helpers for huta's tests. Standard library only (huta itself has no dependencies).

Run everything from the repo root:  python3 -m unittest discover -s tests -t .
Rewrite the v1 snapshots (only when a change to v1 output is intended and explained in the
commit):  HUTA_UPDATE_GOLDEN=1 python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"

# Made-up values for the parts of a release that come from outside the config.
SHA = "0123456789abcdef0123456789abcdef01234567"
IMAGE = "sha256:" + "ab" * 32
CF_RANGES = ["173.245.48.0/20", "2400:cb00::/32"]


def etc_copy() -> Path:
    """A private copy of the fixture /etc/huta (with the empty secrets/ git can't hold)."""
    d = Path(tempfile.mkdtemp(prefix="huta-test-etc-"))
    shutil.copytree(FIXTURES / "etc", d, dirs_exist_ok=True)
    (d / "secrets").mkdir(exist_ok=True)
    return d


def load(etc: Path):
    """(platform, registry) from a fixture dir. The root-ownership check is skipped: tests run
    as an ordinary user on files they own."""
    from huta import cfg
    with mock.patch.object(cfg, "assert_root_dir", lambda _p: None):
        platform = cfg.load_platform(etc)
        registry = {p.stem: cfg.load_project(p.stem, etc)
                    for p in sorted((etc / "projects.d").glob("*.toml"))}
    return platform, registry


def app_toml(project: str) -> dict:
    from huta.cfg import parse_app_toml
    return parse_app_toml((ROOT / "examples" / project / "deploy" / "app.toml").read_text(),
                          project)


def check_golden(case, name: str, value) -> None:
    """Compare value with tests/golden/<name>; write it instead when HUTA_UPDATE_GOLDEN=1."""
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True,
                                                           ensure_ascii=False, default=str) + "\n"
    path = GOLDEN / name
    if os.environ.get("HUTA_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    if not path.exists():
        case.fail(f"no snapshot {path.relative_to(ROOT)}: run with HUTA_UPDATE_GOLDEN=1 once")
    case.assertEqual(path.read_text(), text,
                     f"v1 output changed: {path.relative_to(ROOT)} (a compatibility break unless "
                     "intended and explained)")
