"""Pull transport, host side: claim requests a dev box dropped into a shared inbox, act on them
one at a time, answer in a read-only outbox.

The inbox is owned by the dev box's (mapped) user, so nothing in it is trusted:
- only names `<16 hex>.json` are requests; the dev box writes `.<id>.json.tmp` and renames;
- a request is first **claimed**: renamed atomically into a root-only work directory on the same
  filesystem. Only then is it opened (O_NOFOLLOW), checked (regular file, dev uid, size cap),
  read and deleted, so it can't be swapped underneath us (transport review #4);
- anything else (directories, odd names, stale temp files) is removed without following links;
- at most MAX_PER_RUN requests per run, one at a time (a generator), and the scan itself is
  capped, so a flood can't exhaust memory or starve later requests forever (#3, #7).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from . import proto
from .cfg import assert_root_dir

REQ_NAME = re.compile(r"([0-9a-f]{16})\.json")
MAX_SCAN = 2000
MAX_PER_RUN = 50
STALE_TMP = 600


def _remove(dfd: int, name: str) -> None:
    """Remove an entry of the untrusted directory without following links."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return
    try:
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(name, dir_fd=dfd)       # fd-based, symlink-safe
        else:
            os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


def claim_requests(inbox: Path, work: Path, owner_uid: int):
    """Yield (request id or None, raw bytes or None, problem) one at a time."""
    # The work dir and its parents must be root's alone, or a planted symlink could redirect
    # what root chmods, creates or deletes (grok #5). Mode is set through the opened fd.
    assert_root_dir(work.parent)
    work.mkdir(mode=0o700, exist_ok=True)
    wfd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    os.fchmod(wfd, 0o700)
    assert_root_dir(work)
    ifd = os.open(inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        names, odd = [], []
        with os.scandir(ifd) as it:
            for n, entry in enumerate(it):
                if n >= MAX_SCAN:
                    break
                if REQ_NAME.fullmatch(entry.name):
                    names.append(entry.name)
                elif entry.name.startswith(".") and entry.name.endswith(".json.tmp"):
                    try:
                        if time.time() - entry.stat(follow_symlinks=False).st_mtime > STALE_TMP:
                            odd.append(entry.name)
                    except OSError:
                        pass
                else:
                    odd.append(entry.name)
        for name in odd:
            _remove(ifd, name)
            yield None, None, f"removed unexpected inbox entry {name[:60]!r}"
        for name in sorted(names)[:MAX_PER_RUN]:
            rid = REQ_NAME.fullmatch(name).group(1)
            claimed = f"{rid}.{os.getpid()}"
            try:
                os.rename(name, claimed, src_dir_fd=ifd, dst_dir_fd=wfd)
            except FileNotFoundError:
                continue      # the client is re-placing it (its nudge); the next scan gets it
            except OSError as e:
                yield rid, None, f"cannot claim: {e.strerror}"
                continue
            raw, problem = None, ""
            try:
                st = os.stat(claimed, dir_fd=wfd, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    problem = "not a regular file"
                elif st.st_uid != owner_uid:
                    problem = "not owned by the dev user"
                elif st.st_size > proto.MAX_REQUEST:
                    problem = "too large"
                else:
                    fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=wfd)
                    try:
                        raw = os.read(fd, proto.MAX_REQUEST + 1)
                    finally:
                        os.close(fd)
            except OSError as e:
                problem = f"cannot read: {e.strerror}"
            finally:
                _remove(wfd, claimed)
            yield rid, raw, problem
    finally:
        os.close(ifd)
        os.close(wfd)


def answer(outbox: Path, rid: str, body: dict) -> None:
    """Write the answer inside a root-only outbox, relative to its fd (grok #5)."""
    assert_root_dir(outbox)
    body = {"v": 1, "id": rid, "ts": int(time.time()), **body}
    ofd = os.open(outbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        tmp = f".{rid}.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644, dir_fd=ofd)
        try:
            os.write(fd, (json.dumps(body, indent=2) + "\n").encode())
        finally:
            os.close(fd)
        os.replace(tmp, f"{rid}.json", src_dir_fd=ofd, dst_dir_fd=ofd)
    finally:
        os.close(ofd)


def prune_outbox(outbox: Path, keep_seconds: int = 7 * 86400, keep_max: int = 500) -> None:
    assert_root_dir(outbox)
    files = sorted(outbox.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = time.time() - keep_seconds
    for i, p in enumerate(files):
        if i >= keep_max or p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
