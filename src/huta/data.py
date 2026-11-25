"""Persistent data for a target (DESIGN-v1 §3): its own ext4 filesystem in a sparse image file,
loop-mounted by a generated systemd mount unit; consistent backups by freezing that filesystem.

Paths (root-only, under the state dir):
  <state>/data/<p>/<t>.img   the filesystem image
  <state>/data/<p>/<t>/      its mount point; the directory underneath is root 0000, so an
                             unmounted volume is unusable instead of silently on the root disk
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .cfg import ConfigError, Platform, Project, Target, assert_root_dir
from .runtime import data_paths

UNIT_DIR = Path("/etc/systemd/system")
MARK = "# managed-by: huta"
FREEZE_LIMIT = 60          # seconds the filesystem may stay frozen
RESERVE = 20 << 30         # free space the host disk must keep beyond every volume's full size
THAW_GUARD = 90            # a transient timer thaws it even if huta dies while it is frozen


class DataError(Exception):
    pass


def _run(cmd: list[str], timeout: float = 300, check: bool = True, env=None,
         input_=None) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd="/",
                           env=env, input=input_)
    except subprocess.TimeoutExpired:
        raise DataError(f"{cmd[0]}: no result after {int(timeout)} s")
    if check and r.returncode != 0:
        raise DataError(f"{' '.join(cmd[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def unit_name(where: Path) -> str:
    return _run(["systemd-escape", "--path", "--suffix=mount", str(where)]).stdout.strip()


def mounted(where: Path) -> bool:
    return os.path.ismount(where)


def loop_device(where: Path) -> str:
    dev = _run(["findmnt", "-n", "-o", "SOURCE", "--mountpoint", str(where)]).stdout.strip()
    if not re.fullmatch(r"/dev/loop\d+", dev):
        raise DataError(f"{where} is not a loop mount ({dev or 'nothing'})")
    return dev


def ensure(platform: Platform, project: Project, t: Target) -> str:
    """Create, mount and grow the volume as the host registry says. Idempotent."""
    if not t.data:
        raise DataError(f"{project.name}/{t.name} has no data in the host registry")
    img, where = data_paths(platform, t)
    base = img.parent
    for d in (platform.state_dir / "data", base):
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    assert_root_dir(base)
    said = []
    if not mounted(where):
        where.mkdir(mode=0o000, exist_ok=True)
        os.chown(where, 0, 0)
        os.chmod(where, 0o000)
    _check_room(platform, t, img)
    if not img.exists():
        # Made under a temporary name and renamed when formatted: a crash can't leave an
        # unformatted image under the real name (codex code review #14).
        tmp = img.with_name(f".{img.name}.new")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.ftruncate(fd, t.data["bytes"])
        finally:
            os.close(fd)
        _run(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", f"huta-{t.project}"[:16],
              "-E", f"root_owner={project.uid}:{project.uid}", str(tmp)])
        os.rename(tmp, img)
        said.append(f"created a {t.data['size']} volume")
    unit = unit_name(where)
    text = "\n".join([
        f"{MARK} — data of {t.project}/{t.name}. Do not edit; see /etc/huta/projects.d.",
        "[Unit]", f"Description=huta data {t.project}/{t.name}",
        "Before=docker.service",
        "[Mount]", f"What={img}", f"Where={where}", "Type=ext4",
        "Options=loop,nodev,nosuid,noexec,noatime",
        "[Install]", "WantedBy=multi-user.target", ""])
    path = UNIT_DIR / unit
    if not path.exists() or path.read_text() != text:
        if path.exists() and not path.read_text().startswith(MARK):
            raise DataError(f"{path} exists and is not managed by huta")
        _write(path, text)
        _run(["systemctl", "daemon-reload"])
    if not mounted(where):
        _run(["systemctl", "enable", "--now", unit])
        said.append("mounted")
    if not mounted(where):
        raise DataError(f"{where}: not mounted after starting {unit}")
    st = os.stat(where)
    if st.st_uid != project.uid or st.st_mode & 0o777 != 0o700:
        os.chown(where, project.uid, project.uid)
        os.chmod(where, 0o700)
    # Grow (never shrink) to the registry size. Each step is checked against what is really
    # there, so a growth interrupted half-way finishes on the next call.
    have = img.stat().st_size
    if t.data["bytes"] > have:
        os.truncate(img, t.data["bytes"])
        said.append(f"grown to {t.data['size']}")
    elif t.data["bytes"] < have:
        said.append(f"registry says {t.data['size']}, volume is larger: shrinking is not done")
    dev = loop_device(where)
    if int(_run(["blockdev", "--getsize64", dev]).stdout.strip()) < img.stat().st_size:
        _run(["losetup", "-c", dev])
    if _fs_bytes(dev) + (8 << 20) < int(_run(["blockdev", "--getsize64", dev]).stdout.strip()):
        _run(["resize2fs", dev], timeout=1800)
    return ", ".join(said) or "volume ready"


def _fs_bytes(dev: str) -> int:
    out = _run(["dumpe2fs", "-h", dev]).stdout
    blocks = int(re.search(r"^Block count:\s+(\d+)", out, re.M).group(1))
    size = int(re.search(r"^Block size:\s+(\d+)", out, re.M).group(1))
    return blocks * size


def _check_room(platform: Platform, t: Target, img: Path) -> None:
    """Sparse images don't reserve space: refuse to create or grow one unless the disk could
    hold every huta volume full, plus a reserve (design #17, code review #7)."""
    have = img.stat().st_size if img.exists() else 0
    if t.data["bytes"] <= have:
        return
    outstanding = 0          # what all other volumes may still grow into
    for other in (platform.state_dir / "data").glob("*/*.img"):
        if other != img and other.is_file() and not other.is_symlink():
            st = other.stat()
            outstanding += max(0, st.st_size - st.st_blocks * 512)
    used_now = (img.stat().st_blocks * 512) if img.exists() else 0
    s = os.statvfs(img.parent)
    free = s.f_bavail * s.f_frsize
    need = t.data["bytes"] - used_now + outstanding + RESERVE
    if free < need:
        raise DataError(f"not enough disk for a {t.data['size']} volume: {free >> 30} GiB free, "
                        f"{need >> 30} GiB needed (every volume full, plus a "
                        f"{RESERVE >> 30} GiB reserve)")


def usage(platform: Platform, t: Target) -> tuple[int, int] | None:
    _img, where = data_paths(platform, t)
    if not mounted(where):
        return None
    s = os.statvfs(where)
    return (s.f_blocks - s.f_bfree) * s.f_frsize, s.f_blocks * s.f_frsize


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.huta-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ------------------------------------------------------------------ backup

def backup_dir(platform: Platform, t: Target) -> Path:
    return platform.backup["dir"] / t.project / t.name


RSYNC = ["rsync", "-a", "--delete", "--numeric-ids", "--one-file-system", "--no-specials",
         "--no-devices", "--modify-window=-1"]   # -1: compare mtimes to the nanosecond


def thaw(where: Path) -> None:
    subprocess.run(["fsfreeze", "-u", str(where)], capture_output=True, timeout=30, cwd="/")


def copy_consistent(platform: Platform, t: Target, log, quiet: bool = False) -> Path:
    """A new backup generation of the volume, published only when complete:

      next/      built here: hard links to current/ for unchanged files (--link-dest), one rsync
                 pass while the app runs, then the filesystem is frozen and a second pass with
                 --checksum copies everything that differs (content, not just size and time)
      current/   the last complete generation (what ship and export read)
      previous/  the one before

    A transient systemd timer thaws the volume after 90 s even if huta dies while it is frozen.
    quiet=True: nothing writes (the release stopped the old slot), so one pass, no freeze.
    Symlinks are copied as links, devices and specials are skipped, the copy never leaves the
    volume (--one-file-system). Returns current/."""
    if not platform.backup:
        raise DataError("no [backup] in huta.toml")
    _img, where = data_paths(platform, t)
    if not mounted(where):
        raise DataError(f"{where} is not mounted")
    try:
        ph = json.loads((platform.state_dir / "phases" / f"{t.project}--{t.name}.json").read_text())
    except (OSError, ValueError):
        ph = {}
    if ph.get("copied"):
        # An unfinished one-writer release owns current/ (its restore point): never rotate it
        # away (final reviews: codex #2, grok #1).
        raise DataError("a release was interrupted before its switch: run huta reconcile first")
    if (platform.state_dir / "phases" / f"{t.project}--{t.name}.import").exists():
        # Never publish a half-imported tree over the last good copy (grok #3).
        raise DataError("a data import was interrupted: no backup until it completes")
    root = platform.backup["dir"]
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    assert_root_dir(root)
    base = backup_dir(platform, t)
    for d in (base.parent, base):
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    recover_publish(base)
    cur, nxt, prev = base / "current", base / "next", base / "previous"
    if nxt.exists():
        _rmtree(nxt)
    nxt.mkdir(mode=0o700)
    link = [f"--link-dest={cur}"] if cur.is_dir() else []
    t0 = time.monotonic()
    _run([*RSYNC, *link, f"{where}/", f"{nxt}/"], timeout=6 * 3600)
    if quiet:
        return _publish(base, nxt, t, t0, 0.0, log)
    loop_device(where)          # freeze only what is really this target's loop mount
    guard = f"huta-thaw-{t.project}-{t.name}-{os.getpid()}"
    _run(["systemd-run", "--quiet", f"--unit={guard}", f"--on-active={THAW_GUARD}",
          "/usr/sbin/fsfreeze", "-u", str(where)])
    frozen = 0.0
    import signal
    # A stop (SIGTERM) while frozen must still thaw: turn it into an exception so the finally
    # runs (grok #8). SIGKILL is what the transient thaw timer is for.
    old_term = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
    try:
        f0 = time.monotonic()
        try:
            _run(["fsfreeze", "-f", str(where)], timeout=30)
            _run([*RSYNC, "--checksum", f"{where}/", f"{nxt}/"], timeout=FREEZE_LIMIT)
        finally:
            thaw(where)
            frozen = time.monotonic() - f0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        subprocess.run(["systemctl", "stop", f"{guard}.timer"], capture_output=True, timeout=30,
                       cwd="/")
    return _publish(base, nxt, t, t0, frozen, log)


def recover_publish(base: Path) -> None:
    """A crash between the two renames of _publish leaves no current/: the previous complete
    generation becomes current again (codex code review #13)."""
    cur, prev = base / "current", base / "previous"
    if not cur.exists() and prev.is_dir() and not prev.is_symlink():
        os.rename(prev, cur)


def _publish(base: Path, nxt: Path, t: Target, t0: float, frozen: float, log) -> Path:
    cur, prev = base / "current", base / "previous"
    os.chmod(nxt, 0o700)
    _run(["sync", "-f", str(nxt)], timeout=600)
    if prev.exists():
        _rmtree(prev)
    if cur.exists():
        os.rename(cur, prev)
    os.rename(nxt, cur)
    log(f"backup copy of {t.project}/{t.name}: {time.monotonic() - t0:.1f} s"
        + (f", frozen {frozen:.2f} s" if frozen else ", quiet (nothing running)"))
    return cur


def restore_copy(platform: Platform, project: Project, t: Target, log) -> None:
    """Put the last copy back onto the volume (nothing may run). Used when a non-concurrent
    release fails before its switch: the new slot may have written, the copy was taken after
    the old slot stopped, so nothing is lost."""
    recover_publish(backup_dir(platform, t))
    cur = backup_dir(platform, t) / "current"
    _img, where = data_paths(platform, t)
    if not cur.is_dir() or not mounted(where):
        raise DataError("no copy to restore, or the volume is not mounted")
    _run([*RSYNC, f"{cur}/", f"{where}/"], timeout=6 * 3600)
    os.chown(where, project.uid, project.uid)
    os.chmod(where, 0o700)
    log(f"data restored from the copy taken before the release")


def _rmtree(p: Path) -> None:
    """Remove one of huta's own backup generations (never follows links)."""
    if p.is_symlink() or p.name not in ("next", "previous"):
        raise DataError(f"refusing to remove {p}")
    _run(["rm", "-rf", "--one-file-system", "--", str(p)], timeout=3600)


def ship(platform: Platform, t: Target, log) -> str:
    """Run the platform's ship command. 'ok', 'skipped' (exit 75) or raises."""
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "HUTA_BACKUP_DIR": str(backup_dir(platform, t) / "current"),
           "HUTA_PROJECT": t.project, "HUTA_TARGET": t.name}
    r = _run(platform.backup["ship"], timeout=4 * 3600, check=False, env=env)
    if r.returncode == 75:
        log(f"ship skipped (target unavailable): {(r.stdout + r.stderr).strip()[-200:]}")
        return "skipped"
    if r.returncode != 0:
        raise DataError(f"ship failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    return "ok"


def backup(platform: Platform, t: Target, st, log, do_ship: bool = True,
           quiet: bool = False) -> str:
    """Copy, then ship; record both separately. Caller holds the target lock. A failed copy
    raises (current/ stays the last good one); a failed ship is recorded and raised."""
    if not t.data or t.data["backup"] == "off":
        return "backups are off for this target"
    copy_consistent(platform, t, log, quiet=quiet)
    stamp = backup_dir(platform, t) / "last.json"
    rec = {"ts": time.time(), "shipped": "not shipped"}
    try:
        if do_ship:
            rec["shipped"] = ship(platform, t, log)
            rec["shipped_ts"] = time.time() if rec["shipped"] == "ok" else None
    except DataError as e:
        rec["shipped"] = "failed"
        raise DataError(f"copied locally, but {e}")
    finally:
        old = last_backup(platform, t) or {}
        if rec.get("shipped_ts") is None and old.get("shipped_ts"):
            rec["shipped_ts"] = old["shipped_ts"]      # keep when it last reached off-site
        _write(stamp, json.dumps(rec) + "\n")
        os.chmod(stamp, 0o600)
        st.event("backup", f"local copy ok, ship {rec['shipped']}", None, t.project, t.name)
    return f"local copy ok, ship {rec['shipped']}"


def last_backup(platform: Platform, t: Target) -> dict | None:
    if not platform.backup:
        return None
    try:
        return json.loads((backup_dir(platform, t) / "last.json").read_text())
    except (OSError, ValueError):
        return None


def last_backup_text(platform: Platform, t: Target) -> str:
    if not t.data:
        return "no data"
    if t.data["backup"] == "off":
        return "backups OFF (operator's choice)"
    b = last_backup(platform, t)
    if not b:
        return "no backup yet"
    age = (time.time() - b["ts"]) / 3600
    off = (f"off-site {(time.time() - b['shipped_ts']) / 3600:.1f} h ago"
           if b.get("shipped_ts") else "never off-site")
    return f"local copy {age:.1f} h ago, last ship {b['shipped']}, {off}"


# ------------------------------------------------------------------ import / export

def import_dir(platform: Platform, project: Project, t: Target, src: Path, marker: Path,
               log) -> None:
    """Copy an existing directory into the volume (caller checked nothing runs). A marker file
    blocks every start and release of the target until the import has completed, so a crash
    mid-import can't run code on half the data (codex code review #15)."""
    src = Path(src)
    if not src.is_dir() or src.is_symlink():
        raise DataError(f"{src} is not a directory")
    _img, where = data_paths(platform, t)
    if not mounted(where):
        raise DataError(f"{where} is not mounted")
    _write(marker, f"{src}\n")
    # Symlinks are copied as links (never followed), devices and specials skipped; ownership
    # is set by rsync itself (--chown), so no root walk over the tree afterwards.
    _run(["rsync", "-a", "--numeric-ids", "--no-specials", "--no-devices", "--one-file-system",
          f"--chown={project.uid}:{project.uid}", f"{src}/", f"{where}/"], timeout=6 * 3600)
    os.chown(where, project.uid, project.uid)
    os.chmod(where, 0o700)
    marker.unlink()
    log(f"imported {src} into {project.name}/{t.name}")


def export_tar(platform: Platform, t: Target, paths: list[str], out) -> None:
    """tar of the last backup copy (or parts of it) to a binary stream."""
    recover_publish(backup_dir(platform, t))
    cur = backup_dir(platform, t) / "current"
    if not cur.is_dir():
        raise DataError("no backup copy yet: run huta backup first")
    for p in paths:
        if p.startswith("/") or ".." in Path(p).parts or not re.fullmatch(r"[A-Za-z0-9._/-]+", p):
            raise ConfigError(f"bad path {p!r}")
        # The app made this tree: no part of the path may be a symlink, or tar would follow it
        # out of the copy (`logs -> /etc`, then `logs/shadow`; codex code review #6).
        walk = cur
        for part in Path(p).parts:
            walk = walk / part
            if walk.is_symlink():
                raise ConfigError(f"{p}: {walk.relative_to(cur)} is a symlink; refusing")
            if not walk.exists():
                raise ConfigError(f"{p}: not in the copy")
    proc = subprocess.Popen(["tar", "-C", str(cur), "--numeric-owner", "-cf", "-", "--",
                             *(paths or ["."])], stdout=subprocess.PIPE, cwd="/")
    assert proc.stdout
    while chunk := proc.stdout.read(1 << 20):
        out.write(chunk)
    if proc.wait() != 0:
        raise DataError("tar failed")
