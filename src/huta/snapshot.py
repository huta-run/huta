"""Data snapshots for dev boxes (docs/PLAN-snapshot.md). Milestone S0: the records and their
recovery. Nothing creates a snapshot yet; `sweep` and the host commands exist so every later
step has its cleanup in place before it is built.

Where a snapshot's things live, all with fixed names derived from its id:

- work:   <state_dir>/snapshots/<project>/<target>/<id>/   (scratch image, its mount at mnt/,
          the artifact, the sanitize output kept for the operator)
- input:  <backup dir>/<project>/<target>/snaps/<id>        (hard links of the pinned
          generation; never pins/, which restore points prune by id)
- outbox: <pull outbox>/snapshots/<id>/                     (pull delivery)
- containers: huta-snap-<id>, huta-snap-<id>-check, huta-snap-<id>-pg
- unit:   huta-snapshot-<id>                                (the prepare)

Locks: `_snapshot-<target>` (in the store's locks/, so `install.sh` sees it) for every step and
decision on a target's snapshots; never the target lock, never reconcile.

`sweep` is the recovery: for each row whose lock is free it finishes what the state says (a dead
prepare fails, an expired plan expires, a terminal or overdue row loses its files and becomes
deleted), then removes leftovers no open row owns. Every removal is strict (fixed parent, a name
of the id's shape, never through a symlink, `--one-file-system`) and verified; a removal that
can't finish keeps the record (needs-human) and is retried by every sweep.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path

from . import store as S
from .cfg import Platform

ID_RE = re.compile(r"[0-9a-f]{8}")
NAME_RE = re.compile(r"[a-z][a-z0-9-]*")      # project and target names (cfg's rule)
START_BUDGET = 120        # seconds a new prepare's unit has to start and take its lock
PLAN_TTL = 30 * 60        # a ready snapshot's plan expires as release plans do
TERMINAL = (S.SNAP_FAILED, S.SNAP_REJECTED, S.SNAP_EXPIRED)


class SnapshotError(RuntimeError):
    pass


# ------------------------------------------------------------------ names and paths

def lock_name(target: str) -> str:
    return f"_snapshot-{target}"


def unit(sid: str) -> str:
    return f"huta-snapshot-{sid}"


def containers(sid: str) -> list[str]:
    return [f"huta-snap-{sid}", f"huta-snap-{sid}-check", f"huta-snap-{sid}-pg"]


def work_root(platform: Platform) -> Path:
    return platform.state_dir / "snapshots"


def work_dir(platform: Platform, project: str, target: str, sid: str) -> Path:
    return work_root(platform) / project / target / sid


def input_dir(platform: Platform, project: str, target: str, sid: str) -> Path | None:
    if not platform.backup:
        return None
    return platform.backup["dir"] / project / target / "snaps" / sid


def outbox_dir(platform: Platform, sid: str) -> Path | None:
    if not platform.pull:
        return None
    return Path(platform.pull["outbox"]) / "snapshots" / sid


# ------------------------------------------------------------------ host commands run here

def _sh(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd="/")


def unit_alive(sid: str) -> bool:
    r = _sh(["systemctl", "is-active", f"{unit(sid)}.service"], timeout=30)
    return r.stdout.strip() in ("active", "activating", "deactivating", "reloading")


def _containers_gone(sid: str) -> None:
    for name in containers(sid):
        _sh(["docker", "rm", "-f", name], timeout=120)
    for name in containers(sid):
        r = _sh(["docker", "ps", "-aq", "--filter", f"name=^{name}$"], timeout=60)
        if r.returncode != 0 or r.stdout.strip():
            raise SnapshotError(f"container {name} is still there")


def _unmount(mnt: Path) -> None:
    """Unmount mnt if it is a mount point: retried while busy, never lazily."""
    for attempt in range(5):
        if _sh(["mountpoint", "-q", str(mnt)], timeout=30).returncode != 0:
            return
        r = _sh(["umount", str(mnt)], timeout=120)
        if r.returncode == 0:
            return
        time.sleep(1 + attempt)
    raise SnapshotError(f"{mnt} is still mounted")


def _detach_loops(img: Path) -> None:
    r = _sh(["losetup", "-j", str(img)], timeout=30)
    for line in r.stdout.splitlines():
        dev = line.split(":", 1)[0].strip()
        if re.fullmatch(r"/dev/loop\d+", dev):
            d = _sh(["losetup", "-d", dev], timeout=60)
            if d.returncode != 0:
                raise SnapshotError(f"could not detach {dev}: {d.stderr.strip()[-200:]}")


def _no_links(p: Path, root: Path) -> None:
    """p must lie under root, and nothing from root down to p's parent may be a symlink (or
    anything but a directory): cleanup never acts through a redirected ancestor."""
    try:
        rel = p.relative_to(root)
    except ValueError:
        raise SnapshotError(f"refusing {p}: not under {root}") from None
    if ".." in rel.parts:
        raise SnapshotError(f"refusing {p}")
    cur = root
    for part in (None, *rel.parts[:-1]):
        cur = cur if part is None else cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return                       # nothing below a missing directory to act on
        if not stat.S_ISDIR(st.st_mode):
            raise SnapshotError(f"refusing {p}: {cur} is a symlink or not a directory")


def _rm_strict(p: Path, parent: Path, root: Path) -> None:
    """Remove p (a file or tree) only if it sits directly in `parent`, is named like a snapshot
    id, is not a symlink and has no symlinked ancestor below `root`; never crosses into another
    filesystem."""
    if p.parent != parent or not ID_RE.fullmatch(p.name.lstrip(".").removesuffix(".tmp")):
        raise SnapshotError(f"refusing to remove {p}")
    _no_links(p, root)
    if not os.path.lexists(p):
        return
    if p.is_symlink():
        raise SnapshotError(f"refusing to remove {p}: a symlink")
    r = _sh(["rm", "-rf", "--one-file-system", "--", str(p)], timeout=3600)
    if r.returncode != 0 or os.path.lexists(p):
        raise SnapshotError(f"could not remove {p}: {r.stderr.strip()[-200:]}")


def _release_scratch(w: Path, root: Path) -> None:
    """Unmount a work directory's scratch and detach its loop device, after checking that
    neither `mnt` nor `scratch.img` (nor anything above them) is a symlink: umount and losetup
    must only ever see this snapshot's own paths."""
    _no_links(w / "mnt" / "x", root)              # w and mnt: real directories (or absent)
    img = w / "scratch.img"
    _no_links(img, root)
    if os.path.islink(img):
        raise SnapshotError(f"refusing {img}: a symlink")
    if not os.path.isdir(w) or os.path.islink(w):
        return
    _unmount(w / "mnt")
    _detach_loops(img)


def remove_files(platform: Platform, project: str, target: str, sid: str) -> None:
    """Everything of one snapshot, in order: containers (verified gone), the scratch mount and
    its loop device, the work directory, the input links, the outbox copy. Raises
    SnapshotError when something can't be removed; what is left stays for the next sweep."""
    if not ID_RE.fullmatch(sid):
        raise SnapshotError(f"bad snapshot id {sid!r}")
    for name in (project, target):
        if not NAME_RE.fullmatch(name):
            raise SnapshotError(f"bad name {name!r}")
    _containers_gone(sid)
    w = work_dir(platform, project, target, sid)
    _release_scratch(w, work_root(platform))
    _rm_strict(w, w.parent, work_root(platform))
    inp = input_dir(platform, project, target, sid)
    if inp is not None:
        _rm_strict(inp, inp.parent, platform.backup["dir"])
    out = outbox_dir(platform, sid)
    if out is not None:
        _rm_strict(out, out.parent, Path(platform.pull["outbox"]))


# ------------------------------------------------------------------ sweep

def _try_lock(st, project: str, target: str) -> int | None:
    try:
        return st.acquire(project, lock_name(target), wait=0)
    except RuntimeError:
        return None


def _delete(platform: Platform, st, row, log) -> None:
    """A terminal or overdue row loses its files, then becomes deleted; a removal that fails
    leaves it needs-human (retried by every sweep)."""
    try:
        remove_files(platform, row["project"], row["target"], row["id"])
    except (SnapshotError, OSError, subprocess.TimeoutExpired) as e:
        st.event("snapshot-cleanup-failed", f"{type(e).__name__}: {e}"[:300], row["id"],
                 row["project"], row["target"])
        log(f"snapshot {row['id']}: cleanup failed ({e}); kept for the next sweep")
        if row["state"] != S.SNAP_NEEDS_HUMAN:
            st.snapshot_transition(row["id"], row["state"], S.SNAP_NEEDS_HUMAN,
                                   f"cleanup failed: {e}"[:300])
        return
    st.snapshot_transition(row["id"], row["state"], S.SNAP_DELETED, "files removed")
    log(f"snapshot {row['id']}: deleted")


def _plan_state(st, sid: str) -> str | None:
    p = st.plan_row(sid)
    return p["state"] if p else None


def sweep_row(platform: Platform, st, row, now: float, log) -> None:
    """Finish what one row's state says. The caller holds its target's snapshot lock."""
    sid, state = row["id"], row["state"]
    if state == S.SNAP_PREPARING:
        if now - row["created"] < START_BUDGET or unit_alive(sid):
            return                        # its unit is starting or running
        st.snapshot_transition(sid, state, S.SNAP_FAILED, "the prepare was interrupted",
                               refusal="interrupted")
    elif state == S.SNAP_READY:
        if now - row["updated"] < PLAN_TTL:
            return
        plan = (S.PLANNED, S.SUPERSEDED) if _plan_state(st, sid) == S.PLANNED else None
        st.snapshot_transition(sid, state, S.SNAP_EXPIRED, "approval expired (30 min)", plan=plan)
    elif state == S.SNAP_DELIVERED:
        if row["delete_after"] is None or now < row["delete_after"]:
            return
    elif state == S.SNAP_APPROVED:
        return                            # delivery (milestone S3) finishes or fails it
    elif state not in (*TERMINAL, S.SNAP_NEEDS_HUMAN):
        return                            # deleted
    _delete(platform, st, st.snapshot(sid), log)


def _orphans(platform: Platform, st, project: str, target: str, log) -> None:
    """Leftovers of this target that no open row owns: work directories, input links."""
    owned = {r["id"] for r in st.snapshots(project, target, states=S.SNAP_OPEN + (
        S.SNAP_DELIVERED, S.SNAP_NEEDS_HUMAN, *TERMINAL), limit=None)}
    dirs = [(work_root(platform) / project / target, work_root(platform))]
    if platform.backup:
        dirs.append((platform.backup["dir"] / project / target / "snaps", platform.backup["dir"]))
    for d, root in dirs:
        try:
            _no_links(d / "x", root)
        except SnapshotError:
            continue
        if not d.is_dir() or d.is_symlink():
            continue
        for p in sorted(d.iterdir()):
            sid = p.name.lstrip(".").removesuffix(".tmp")
            if not ID_RE.fullmatch(sid) or sid in owned:
                continue
            try:
                if d.name != "snaps":
                    _containers_gone(sid)
                    _release_scratch(p, root)
                _rm_strict(p, d, root)
                log(f"removed leftover {p}")
            except (SnapshotError, OSError, subprocess.TimeoutExpired) as e:
                st.event("snapshot-cleanup-failed", f"leftover {p.name}: {e}"[:300], None,
                         project, target)


def _targets(platform: Platform, st) -> set[tuple[str, str]]:
    """Every target with a snapshot row or a work directory, registry or not."""
    out = {(r["project"], r["target"]) for r in st.db.execute(
        "SELECT DISTINCT project, target FROM snapshots WHERE state != ?", (S.SNAP_DELETED,))}
    root = work_root(platform)
    if root.is_dir() and not root.is_symlink():
        for p in root.iterdir():
            if p.is_dir() and not p.is_symlink():
                out |= {(p.name, t.name) for t in p.iterdir() if t.is_dir() and not t.is_symlink()}
    # Input links left with no row and no work directory are production data: found too.
    broot = platform.backup["dir"] if platform.backup else None
    if broot is not None and broot.is_dir() and not broot.is_symlink():
        for p in broot.iterdir():
            if p.is_dir() and not p.is_symlink():
                for t in p.iterdir():
                    sn = t / "snaps"
                    if t.is_dir() and not t.is_symlink() and sn.is_dir() and not sn.is_symlink():
                        out.add((p.name, t.name))
    return out


def sweep(platform: Platform, st, project: str | None = None, target: str | None = None,
          log=lambda _m: None, now: float | None = None) -> None:
    """The recovery, for one target or all. Skips a target whose snapshot lock is held (its
    process is alive and will finish). Never raises for one snapshot: the next sweep retries."""
    now = time.time() if now is None else now
    for p, t in sorted(_targets(platform, st)):
        if (project and p != project) or (target and t != target):
            continue
        if not (NAME_RE.fullmatch(p) and NAME_RE.fullmatch(t)):
            continue                      # not a name huta made
        fd = _try_lock(st, p, t)
        if fd is None:
            continue
        try:
            for row in st.snapshots(p, t, states=S.SNAP_OPEN + (S.SNAP_DELIVERED,
                                                                 S.SNAP_NEEDS_HUMAN, *TERMINAL),
                                    limit=None):
                try:
                    sweep_row(platform, st, row, now, log)
                except Exception as e:  # noqa: BLE001 — one row never stops the rest
                    log(f"snapshot {row['id']}: {type(e).__name__}: {e}")
            _orphans(platform, st, p, t, log)
        finally:
            os.close(fd)


# ------------------------------------------------------------------ host commands

def remove(platform: Platform, st, sid: str, who: str, log=lambda _m: None) -> str:
    """`huta snapshot rm <id>` on the host: an open snapshot is rejected (its plan too), then
    its files go; a delivered one is deleted now. A prepare still running is stopped first."""
    row = st.snapshot(sid)
    if row is None:
        raise SnapshotError(f"no snapshot {sid}")
    if row["state"] == S.SNAP_PREPARING and unit_alive(sid):
        _sh(["systemctl", "stop", f"{unit(sid)}.service"], timeout=120)
    fd = st.acquire(row["project"], lock_name(row["target"]), wait=60)
    try:
        row = st.snapshot(sid)
        state = row["state"]
        if state == S.SNAP_DELETED:
            return "already deleted"
        why = f"removed on the host by {who}"
        if state == S.SNAP_PREPARING:
            st.snapshot_transition(sid, state, S.SNAP_FAILED, why, refusal="removed")
        elif state in (S.SNAP_READY, S.SNAP_APPROVED):
            ps = _plan_state(st, sid)
            plan = (ps, S.REJECTED) if ps in (S.PLANNED, S.APPROVED) else None
            st.snapshot_transition(sid, state, S.SNAP_REJECTED, why, plan=plan)
        _delete(platform, st, st.snapshot(sid), log)
        return st.snapshot(sid)["state"]
    finally:
        os.close(fd)


def _when(ts) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def list_text(st, project: str | None = None, target: str | None = None,
              all_: bool = False) -> str:
    states = None if all_ else S.SNAP_OPEN + (S.SNAP_DELIVERED, S.SNAP_NEEDS_HUMAN, *TERMINAL)
    rows = st.snapshots(project, target, states=states)
    if not rows:
        return "no snapshots"
    return "\n".join(f"{r['id']}  {_when(r['created'])}  {r['project']}/{r['target']}  "
                     f"{r['name']:<12} {r['box']:<12} {r['state']:<11}"
                     + (f" {r['size'] >> 10} KiB" if r["size"] else "") for r in rows)


def show_text(st, sid: str) -> str:
    r = st.snapshot(sid)
    if r is None:
        raise SnapshotError(f"no snapshot {sid}")
    out = [f"{r['id']} {r['project']}/{r['target']} snapshot {r['name']}: {r['state']}",
           f"  for box {r['box']} ({r['transport']}), release {r['release_id']}",
           f"  created {_when(r['created'])}, updated {_when(r['updated'])}"]
    if r["sha256"]:
        out.append(f"  artifact {r['size']} bytes, {r['files']} files, sha256 {r['sha256']}")
    if r["refusal"]:
        out.append(f"  refused: {r['refusal']}")
    if r["delete_after"]:
        out.append(f"  delivered {_when(r['delivered'])}, deleted after {_when(r['delete_after'])}")
    if r["reason"]:
        out.append(f"  box says: {r['reason']}")
    for e in reversed(st.events(sid, limit=30)):
        out.append(f"  {_when(e['ts'])}  {e['kind']:<24} {e['msg']}")
    return "\n".join(out)
