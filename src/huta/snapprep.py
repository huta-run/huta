"""Preparing a data snapshot (docs/PLAN-snapshot.md, steps 2-6; milestone S2: data volumes).

    pin the input  →  scratch filesystem  →  sanitize  →  check  →  containers gone
    →  validate (shape, logical size, needles)  →  pack  →  scratch and input gone  →  ready

Everything runs under the target's snapshot lock and one host-wide prepare lock; never the
target lock. Each step that leaves something on disk or in Docker is recorded as a step
(`_step`), so the crash tests can kill the process after it; `snapshot.sweep` removes whatever
a dead prepare left (fixed names derived from the id).

What the box will learn is a refusal code (`REFUSALS`), never the command's output or a file
name: those stay root-only, in the work directory's output.log, for `huta snapshot show`, and
go with the snapshot.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import threading
import time
from pathlib import Path

from . import logs as L
from . import runtime as R
from . import snapshot as SN
from . import store as S
from .cfg import MIN_NEEDLE, RETIRED, Platform, Project, Target, needles

OUT_MAX = 256 * 1024          # sanitize output kept for the operator
CHECK_TIMEOUT = 300
MAX_ENTRIES = 100_000
MAX_DEPTH = 16
MAX_PATH = 1024
NAME_OK = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,254}")
CHUNK = 1 << 20
TAR_MTIME = 0                 # every entry: fixed owner, mode and time (a deterministic tar)
REFUSALS = ("no-backup", "too-old", "no-room", "sanitize-failed", "timeout", "check-failed",
            "shape", "too-big", "secret-found", "secrets-unreadable", "error", "interrupted",
            "not-declared", "removed")
HOST_LOCK = ("_host", "_snapshot")      # one prepare on the host at a time
REDACTED = "‹redacted›".encode()

# A hook the crash tests set: called after every durable step with its name.
STEP_HOOK = None


class Refused(Exception):
    """A snapshot that can't be made. code goes to the row (and the box); detail stays on the
    host (events, output.log)."""

    def __init__(self, code: str, detail: str = ""):
        assert code in REFUSALS
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _step(name: str) -> None:
    if STEP_HOOK is not None:
        STEP_HOOK(name)


def _sh(cmd: list[str], timeout: float = 600, check: bool = True) -> subprocess.CompletedProcess:
    r = SN._sh(cmd, timeout=timeout)
    if check and r.returncode != 0:
        raise Refused("error", f"{' '.join(cmd[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


# ------------------------------------------------------------------ the needle set

def needle_set(platform: Platform, project: str, canaries: list[str]):
    """Every value to look for, fresh and for this project only (not the redaction cache):
    its live secret files, all its retired ones, the host's own tokens, and the canaries
    (which must exist and be at least MIN_NEEDLE long: a missing tripwire fails closed).
    Never staged, claimed or temporary files (a box chooses those). Returns (needles: name by
    bytes, canary values lowercased: name by bytes, a fingerprint of the sources, no values)."""
    sd = platform.secrets_dir
    files: list[tuple[str, Path]] = []
    for d, label in ((sd / project, ""), (sd / "_platform", "_platform/")):
        if d.is_dir() and not d.is_symlink():
            for f in sorted(d.iterdir()):
                if not f.name.startswith(".") and f.is_file() and not f.is_symlink():
                    files.append((label + f.name, f))
    r = sd / RETIRED
    if r.is_dir() and not r.is_symlink():
        for f in sorted(r.iterdir()):
            if f.name.startswith(project + ".") and f.is_file() and not f.is_symlink():
                files.append(("retired " + f.name.split(".")[1], f))
    out: dict[bytes, str] = {}
    fp = hashlib.sha256()
    for name, f in files:
        try:
            st_ = os.lstat(f)
            v = f.read_text().strip()
        except (OSError, UnicodeDecodeError) as e:
            raise Refused("secrets-unreadable", f"{name}: {type(e).__name__}") from None
        fp.update(f"{name}\0{st_.st_ino}\0{st_.st_size}\0{st_.st_mtime_ns}\n".encode())
        if len(v) >= MIN_NEEDLE:
            for n in needles(v):
                out.setdefault(n.encode(), name)
    canary: dict[bytes, str] = {}
    for c in canaries:
        f = sd / project / c
        try:
            if f.is_symlink() or not f.is_file():
                raise OSError("missing")
            v = f.read_text().strip()
        except (OSError, UnicodeDecodeError):
            raise Refused("secrets-unreadable", f"canary {c} is missing") from None
        if len(v) < MIN_NEEDLE:
            raise Refused("secrets-unreadable", f"canary {c} is shorter than {MIN_NEEDLE}")
        canary[v.lower().encode()] = f"canary {c}"
        for n in needles(v):
            out.setdefault(n.encode(), f"canary {c}")
    return out, canary, fp.hexdigest()


class Scanner:
    """One pass over a stream, in chunks with an overlap of the longest needle, so a value
    split across two reads is still found. Canaries are also matched case-insensitively."""

    def __init__(self, found: dict[bytes, str], canary: dict[bytes, str]):
        self.found, self.canary = found, canary
        alts = sorted(found, key=len, reverse=True)
        self.rx = re.compile(b"|".join(re.escape(x) for x in alts)) if alts else None
        calts = sorted(canary, key=len, reverse=True)
        self.crx = re.compile(b"|".join(re.escape(x) for x in calts)) if calts else None
        self.keep = max([len(x) for x in (*found, *canary)] or [1]) - 1

    def hit(self, data: bytes) -> str | None:
        if self.rx is not None:
            m = self.rx.search(data)
            if m:
                return self.found[m.group(0)]
        if self.crx is not None:
            m = self.crx.search(data.lower())
            if m:
                return self.canary[m.group(0)]
        return None

    def blank(self, data: bytes) -> bytes:
        """data with every needle (and canary, any case) replaced: for the operator's log."""
        if self.rx is not None:
            data = self.rx.sub(REDACTED, data)
        if self.crx is not None:
            low = data.lower()
            spans = [m.span() for m in self.crx.finditer(low)]
            for a, b in reversed(spans):
                data = data[:a] + REDACTED + data[b:]
        return data

    def scan_fd(self, fd: int) -> str | None:
        tail = b""
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                return None
            data = tail + chunk
            h = self.hit(data)
            if h:
                return h
            tail = data[-self.keep:] if self.keep else b""


# ------------------------------------------------------------------ the output tree

def walk(root: Path, scratch_bytes: int) -> list[tuple[str, int, int]]:
    """The tree sanitize wrote, walked with directory fds and no symlink ever followed: only
    regular files (one link each) and directories, on the scratch filesystem, with safe names,
    within the entry, depth, path and logical-size caps (a sparse file can't make root read
    terabytes of zeros). Returns [(relative path, kind "d"/"f", size)], sorted. Raises
    Refused("shape" / "too-big")."""
    out: list[tuple[str, int, int]] = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    top = os.open(root, flags)
    dev = os.fstat(top).st_dev
    total = 0

    def go(dfd: int, rel: str, depth: int) -> None:
        nonlocal total
        if depth > MAX_DEPTH:
            raise Refused("shape", f"{rel}: deeper than {MAX_DEPTH}")
        for name in sorted(os.listdir(dfd)):
            path = f"{rel}/{name}" if rel else name
            if not NAME_OK.fullmatch(name) or name in (".", "..") or len(path) > MAX_PATH:
                raise Refused("shape", f"a name huta refuses: {path!r}"[:300])
            st_ = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if st_.st_dev != dev:
                raise Refused("shape", f"{path}: on another filesystem")
            if len(out) >= MAX_ENTRIES:
                raise Refused("shape", f"more than {MAX_ENTRIES} entries")
            if stat.S_ISDIR(st_.st_mode):
                out.append((path, "d", 0))
                sub = os.open(name, flags, dir_fd=dfd)
                try:
                    go(sub, path, depth + 1)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(st_.st_mode):
                if st_.st_nlink != 1:
                    raise Refused("shape", f"{path}: a hard link")
                total += st_.st_size
                if total > scratch_bytes:
                    raise Refused("too-big", "more bytes than the scratch space holds")
                out.append((path, "f", st_.st_size))
            else:
                raise Refused("shape", f"{path}: not a regular file or directory")

    try:
        go(top, "", 1)
    finally:
        os.close(top)
    return out


def _open_rel(root: Path, rel: str, size: int) -> int:
    """Open a file of the walked tree again, component by component, never following a link,
    and check it is still the regular file of that size."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = rel.split("/")
    fd = os.open(root, flags)
    try:
        for p in parts[:-1]:
            nfd = os.open(p, flags, dir_fd=fd)
            os.close(fd)
            fd = nfd
        f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    st_ = os.fstat(f)
    if not stat.S_ISREG(st_.st_mode) or st_.st_size != size or st_.st_nlink != 1:
        os.close(f)
        raise Refused("shape", f"{rel} changed")
    return f


def scan(root: Path, tree, scanner: Scanner) -> str | None:
    """The first secret or canary found in the names or the bytes, or None."""
    names = "\n".join(p for p, _k, _s in tree).encode()
    h = scanner.hit(names)
    if h:
        return h
    for rel, kind, size in tree:
        if kind != "f":
            continue
        fd = _open_rel(root, rel, size)
        try:
            h = scanner.scan_fd(fd)
        finally:
            os.close(fd)
        if h:
            return h
    return None


class _Capped:
    """A file that refuses to grow past max bytes (the packed size cap) and hashes as it
    goes."""

    def __init__(self, f, cap: int):
        self.f, self.cap, self.n, self.h = f, cap, 0, hashlib.sha256()

    def write(self, b):
        self.n += len(b)
        if self.n > self.cap:
            raise Refused("too-big", "the packed snapshot is over the target's max")
        self.h.update(b)
        return self.f.write(b)

    def flush(self):
        self.f.flush()


def pack(root: Path, tree, dest: Path, cap: int) -> tuple[int, str]:
    """huta's own tar of the tree: sorted, owner 0:0, mode 0644/0755, mtime fixed, no extended
    metadata; gzip with mtime 0; written to dest (O_EXCL), fsynced. Returns (bytes, sha256)."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as raw:
        capped = _Capped(raw, cap)
        with gzip.GzipFile(filename="", mode="wb", fileobj=capped, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for rel, kind, size in tree:
                    ti = tarfile.TarInfo(rel)
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    ti.mtime = TAR_MTIME
                    if kind == "d":
                        ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                        tar.addfile(ti)
                        continue
                    ti.type, ti.mode, ti.size = tarfile.REGTYPE, 0o644, size
                    f = _open_rel(root, rel, size)
                    with os.fdopen(f, "rb") as src:
                        tar.addfile(ti, src)
        raw.flush()
        os.fsync(raw.fileno())
    return capped.n, capped.h.hexdigest()


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ room

def room_check(platform: Platform, t: Target) -> None:
    """Sparse images reserve nothing: refuse unless the disk can hold every huta volume and
    every other snapshot's scratch full, this scratch and its packed copy, and the reserve."""
    from .data import RESERVE
    outstanding = 0
    for img in [*(platform.state_dir / "data").glob("*/*.img"),
                *SN.work_root(platform).glob("*/*/*/scratch.img")]:
        if img.is_file() and not img.is_symlink():
            s = img.stat()
            outstanding += max(0, s.st_size - s.st_blocks * 512)
    root = SN.work_root(platform)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    v = os.statvfs(root)
    free = v.f_bavail * v.f_frsize
    need = t.snapshot["scratch"] + t.snapshot["max"] + outstanding + RESERVE
    if free < need:
        raise Refused("no-room", f"{free >> 30} GiB free, {need >> 30} GiB needed")


# ------------------------------------------------------------------ the containers

def _docker_run(args: list[str], env: dict, name: str, timeout: int) -> tuple[int, bytes]:
    """docker run attached; (exit code, the first OUT_MAX bytes of its output); killed at its
    timeout (exit 124). The crash tests replace this."""
    proc = subprocess.Popen(args, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", **env}, cwd="/",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    buf = bytearray()

    def _read():
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            if len(buf) < OUT_MAX:
                buf.extend(chunk[:OUT_MAX - len(buf)])

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        SN._sh(["docker", "kill", name], timeout=60)
        proc.wait(timeout=60)
        rc = 124
    reader.join(10)
    return rc, bytes(buf)


def _run_container(args: list[str], env: dict, name: str, timeout: int, log_path: Path,
                   label: str, log_drop=(), scanner: "Scanner | None" = None) -> int:
    """Run one container; its output, through the log gate and this snapshot's own needles
    (fresh, not only the redaction cache), is appended to the root-only log."""
    rc, buf = _docker_run(args, env, name, timeout)
    if scanner is not None:
        buf = scanner.blank(buf)
    lines = [g for g in (L.gate(x, log_drop) for x in buf.decode(errors="replace").splitlines())
             if g is not None]
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(f"--- {label}: exit {rc}\n" + "\n".join(lines) + "\n")
    return rc


def _docker_args(project: Project, t: Target, sid: str, name: str, comp: dict, image: str,
                 inp: Path, out: Path, out_ro: bool, env: dict, cmd: list[str],
                 comp_name: str) -> list[str]:
    lim = (t.components.get(comp_name) or {}).get("limits") or t.limits
    h = R.hardening(project, t, lim)
    args = ["docker", "run", "--name", name, "--network", "none",
            "--user", h["user"], "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--memory", h["mem_limit"],
            "--cpus", str(h["cpus"]), "--pids-limit", str(h["pids_limit"]),
            "--tmpfs", h["tmpfs"][0], "--pull", "never", "--log-driver", "none",
            "--label", f"run.huta.project={project.name}", "--label", f"run.huta.target={t.name}",
            "--label", f"run.huta.snapshot={sid}",
            "--mount", f"type=bind,source={inp},target={comp['data']},readonly",
            "--mount", f"type=bind,source={out},target=/snapshot"
                       + (",readonly" if out_ro else "")]
    for k in env:
        args += ["--env", k]
    return args + [image, *cmd]


# ------------------------------------------------------------------ prepare

def create(platform: Platform, st, project: Project, t: Target, name: str, box: str,
           transport: str, reason: str = "") -> str:
    """A `preparing` row for snapshot `name` of the live release (refusals as ConfigError-like
    RuntimeError before any row exists)."""
    if not t.snapshot:
        raise RuntimeError(f"{project.name}/{t.name} allows no snapshots (registry)")
    live = st.live(project.name, t.name)
    if not live:
        raise RuntimeError(f"{project.name}/{t.name}: nothing is live")
    rel = st.release(live["release_id"])
    app = json.loads(rel["app_toml"])
    d = next((x for x in app.get("snapshot", []) if x["name"] == name), None)
    if d is None:
        have = ", ".join(x["name"] for x in app.get("snapshot", [])) or "none"
        raise RuntimeError(f"the live release declares no snapshot {name!r} (it has: {have})")
    if (app.get("state") or {}).get("kind") != "data":
        raise RuntimeError("snapshots of a database are planned (milestone S5)")
    return st.new_snapshot(project=project.name, target=t.name, name=name, box=box,
                           transport=transport, release_id=rel["id"], definition=d,
                           reason=reason, per_day=t.snapshot["per_day"])


def prepare(platform: Platform, st, project: Project, t: Target, sid: str,
            log=lambda _m: None) -> str:
    """Steps 2-6 for one `preparing` row. Returns the final state (ready or failed). Never
    leaves a container, mount, loop device, scratch image or input links behind on purpose;
    a crash leaves them to the sweep."""
    fd = st.acquire(project.name, SN.lock_name(t.name), wait=60)
    try:
        row = st.snapshot(sid)
        if row is None or row["state"] != S.SNAP_PREPARING:
            return row["state"] if row else "missing"
        hfd = st.acquire(*HOST_LOCK, wait=3600)
        try:
            try:
                fields = _prepare(platform, st, project, t, row, log)
            except Refused as e:
                return _fail(platform, st, project, t, row, e.code, e.detail, log)
            except Exception as e:  # noqa: BLE001 — every failure ends in a recorded state
                return _fail(platform, st, project, t, row, "error",
                             f"{type(e).__name__}: {e}", log)
        finally:
            os.close(hfd)
        st.snapshot_transition(sid, S.SNAP_PREPARING, S.SNAP_READY,
                               f"ready: {fields['size']} bytes, {fields['files']} files, sha256 "
                               f"{fields['sha256'][:16]}", **fields)
        _step("ready")
        log(f"snapshot {sid} ready: {fields['size']} bytes, sha256 {fields['sha256']}")
        return S.SNAP_READY
    finally:
        os.close(fd)


def _fail(platform, st, project, t, row, code, detail, log) -> str:
    """Containers, scratch and input go now; the work directory (with output.log) stays for
    `huta snapshot show` until the sweep deletes the failed row."""
    try:
        _release(platform, project.name, t.name, row["id"])
    except Exception as e:  # noqa: BLE001 — the sweep retries
        detail += f"; cleanup: {e}"
    # The detail (file names, a secret's name) stays on the host: no release id on the event.
    st.event("snapshot-refused", f"{row['id']}: {code}: {detail}"[:1000], None, project.name,
             t.name)
    st.snapshot_transition(row["id"], S.SNAP_PREPARING, S.SNAP_FAILED, f"refused: {code}",
                           refusal=code)
    log(f"snapshot {row['id']} refused: {code}" + (f" ({detail})" if detail else ""))
    return S.SNAP_FAILED


def _release(platform: Platform, project: str, target: str, sid: str) -> None:
    """Everything but the work directory itself: containers (verified gone), the scratch
    mount and loop device, the scratch image, the input links."""
    SN._containers_gone(sid)
    w = SN.work_dir(platform, project, target, sid)
    SN._release_scratch(w, SN.work_root(platform))
    img = w / "scratch.img"
    if img.is_file() and not img.is_symlink():
        img.unlink()
    inp = SN.input_dir(platform, project, target, sid)
    if inp is not None:
        SN._rm_strict(inp, inp.parent, platform.backup["dir"])


def _prepare(platform: Platform, st, project: Project, t: Target, row, log) -> dict:
    from .data import backup_dir, copy_time, publish_lock, _recover
    from .recipe import check_image
    sid = row["id"]
    d = json.loads(row["definition"])
    rel = st.release(row["release_id"])
    app = json.loads(rel["app_toml"])
    comp = app["components"][d["component"]]
    image = st.images(rel).get(comp["image"], rel["image_id"])
    check_image(image, rel["sha"], project.name)
    needles_, canary, fingerprint = needle_set(platform, project.name, t.snapshot["canaries"])
    base = backup_dir(platform, t)
    # 2. Input: the current generation, pinned under the publish lock.
    inp = SN.input_dir(platform, project.name, t.name, sid)
    if not base.is_dir():
        raise Refused("no-backup", "no backup copy yet")
    with publish_lock(base):
        _recover(base)
        cur = base / "current"
        if not cur.is_dir() or cur.is_symlink():
            raise Refused("no-backup", "no backup copy yet")
        made = copy_time(base)
        if t.snapshot.get("max_age") and made is not None \
                and time.time() - made > t.snapshot["max_age"]:
            raise Refused("too-old", f"the backup copy is {(time.time() - made) / 86400:.1f} d old")
        inp.parent.mkdir(mode=0o700, exist_ok=True)
        SN._no_links(inp, platform.backup["dir"])
        tmp = inp.parent / f".{sid}.tmp"
        _sh(["cp", "-al", "--no-dereference", "--", str(cur), str(tmp)], timeout=3600)
        os.rename(tmp, inp)
    _step("input pinned")
    # 3. Scratch: a sparse ext4 image, loop-mounted nodev,nosuid,noexec; out/ is /snapshot.
    room_check(platform, t)
    w = SN.work_dir(platform, project.name, t.name, sid)
    for p in (SN.work_root(platform), w.parent.parent, w.parent):
        p.mkdir(mode=0o700, exist_ok=True)
    SN._no_links(w, SN.work_root(platform))
    w.mkdir(mode=0o700, exist_ok=True)
    img, mnt = w / "scratch.img", w / "mnt"
    if not img.exists():
        f = os.open(img, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.ftruncate(f, t.snapshot["scratch"])
        finally:
            os.close(f)
    _step("scratch image")
    _sh(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", f"huta-snap-{sid}"[:16], str(img)],
        timeout=600)
    mnt.mkdir(mode=0o700, exist_ok=True)
    _sh(["mount", "-o", "loop,nodev,nosuid,noexec,noatime", str(img), str(mnt)], timeout=120)
    _step("scratch mounted")
    out = mnt / "out"
    out.mkdir(mode=0o700, exist_ok=True)
    os.chown(out, project.uid, project.uid)
    os.chmod(out, 0o700)
    # 4. Sanitize, then the repo's check, each in its own container; then all of them gone.
    env = {**t.env, **((t.components.get(d["component"]) or {}).get("env") or {}),
           "RELEASE_SHA": rel["sha"], "HUTA_PROJECT": project.name, "HUTA_TARGET": t.name,
           "HUTA_SNAPSHOT": d["name"], "HUTA_SNAPSHOT_OUT": "/snapshot"}
    log_path = w / "output.log"
    names = SN.containers(sid)
    scanner = Scanner(needles_, canary)
    st.event("snapshot-sanitize", f"{d['name']}: {' '.join(d['cmd'])[:300]}", sid, project.name,
             t.name)
    rc = _run_container(_docker_args(project, t, sid, names[0], comp, image, inp, out, False,
                                     env, d["cmd"], d["component"]),
                        env, names[0], d["timeout"], log_path, "sanitize", project.log_drop,
                        scanner)
    _step("sanitized")
    SN._containers_gone(sid)
    if rc == 124:
        raise Refused("timeout", f"sanitize ran past its {d['timeout']} s")
    if rc != 0:
        raise Refused("sanitize-failed", f"sanitize exited {rc}")
    if d.get("check"):
        rc = _run_container(_docker_args(project, t, sid, names[1], comp, image, inp, out, True,
                                         env, d["check"], d["component"]),
                            env, names[1], CHECK_TIMEOUT, log_path, "check", project.log_drop,
                            scanner)
        _step("checked")
        SN._containers_gone(sid)
        if rc != 0:
            raise Refused("check-failed", f"check exited {rc}")
    # 5. Validate the frozen tree: shape and logical size, then every secret and canary.
    tree = walk(out, t.snapshot["scratch"])
    hit = scan(out, tree, scanner)
    if hit:
        raise Refused("secret-found", f"{hit} found in the output")
    # 6. Pack, fsync, rename; then scratch and input go.
    art, tmp = w / "artifact.tar.gz", w / ".artifact.tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    size, digest = pack(out, tree, tmp, t.snapshot["max"])
    os.rename(tmp, art)
    _fsync_dir(w)
    _step("packed")
    _release(platform, project.name, t.name, sid)
    _step("scratch removed")
    source = {"copy_time": made, "release": rel["id"], "sha": rel["sha"], "image": image,
              "component": d["component"]}
    return {"size": size, "sha256": digest, "files": sum(1 for _p, k, _s in tree if k == "f"),
            "needles": fingerprint, "source": json.dumps(source, sort_keys=True)}


def artifact(platform: Platform, row) -> Path:
    return SN.work_dir(platform, row["project"], row["target"], row["id"]) / "artifact.tar.gz"


def open_verified(platform: Platform, row) -> int:
    """An fd of the artifact, checked: a root-owned regular 0600 file, never through a link,
    whose size and sha256 are the row's. The caller reads from this fd (the bytes checked are
    the bytes sent)."""
    p = artifact(platform, row)
    SN._no_links(p, SN.work_root(platform))
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st_ = os.fstat(fd)
        if not stat.S_ISREG(st_.st_mode) or st_.st_uid != os.geteuid() \
                or st_.st_mode & 0o077 or st_.st_size != row["size"]:
            raise RuntimeError(f"snapshot {row['id']}: the artifact is not what was made")
        h = hashlib.sha256()
        for chunk in iter(lambda: os.read(fd, CHUNK), b""):
            h.update(chunk)
        if h.hexdigest() != row["sha256"]:
            raise RuntimeError(f"snapshot {row['id']}: the artifact's hash changed")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def export(platform: Platform, st, sid: str, out, who: str) -> int:
    """`huta snapshot export <id>`: the operator's own copy of a ready artifact (root on the
    host; a bypass of the owner's tap like `huta data export`, recorded). Returns bytes."""
    row = st.snapshot(sid)
    if row is None or row["state"] not in (S.SNAP_READY, S.SNAP_APPROVED):
        raise RuntimeError(f"snapshot {sid} is {row['state'] if row else 'unknown'}, not ready")
    fd = open_verified(platform, row)
    n = 0
    try:
        for chunk in iter(lambda: os.read(fd, CHUNK), b""):
            out.write(chunk)
            n += len(chunk)
    finally:
        os.close(fd)
    st.event("snapshot-export", f"exported on the host by {who} ({n} bytes)", sid,
             row["project"], row["target"])
    return n
