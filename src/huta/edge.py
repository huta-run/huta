"""The edge: Origin CA certificates and generated Caddy vhosts (DESIGN.md §2, D-007).

Rules kept here:
- a vhost file without our marker is never touched;
- every vhost change is a transaction under one global edge lock: back up durably, write
  atomically, validate as the caddy user, reload, and restore on any failure or crash
  (recover() replays an interrupted transaction);
- the vhost routes to exactly one slot, the live one. A new release only gets traffic when the
  vhost is switched to it, after huta has verified it (review 2026-11-09, codex #2);
- zone-wide Cloudflare settings are never changed (per-host rules only).
"""

from __future__ import annotations

import fcntl
import grp
import json
import os
import pwd
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .cfg import Platform, Target, assert_root_dir

# The markers and the lock file below (<state_dir>/edge/edge.lock) are also an interface: other
# tools that edit Caddy on the same host take the lock and leave marked files alone. Change
# either and they silently lose that protection.
MARKER = "# managed-by: huta"
LEGACY_MARKERS = ("# managed-by: slipway",)   # files written before the rename to huta
UPSTREAM_RE = re.compile(r"reverse_proxy 127\.0\.0\.1:(\d+)")


class EdgeError(Exception):
    pass


def _run(cmd: list[str], env=None, check=True, input_=None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd="/", input=input_)
    if check and r.returncode != 0:
        raise EdgeError(f"{cmd[0]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def _atomic_write(path: Path, text: str, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.huta-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


@contextmanager
def edge_lock(platform: Platform):
    """One edge change at a time across all projects (Caddy config is global)."""
    d = platform.state_dir / "edge"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(d / "edge.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # wait: edge changes are short
        yield
    finally:
        os.close(fd)


# ------------------------------------------------------------------ TLS (Origin CA)

def _cert_files(platform: Platform, target: Target) -> list[tuple[int, Path, Path]]:
    """All generations of this target's certificate, newest first."""
    base = target.hosts[0]
    out = []
    if platform.tls_dir.exists():
        for p in platform.tls_dir.glob(f"{base}.*.pem"):
            m = re.fullmatch(re.escape(base) + r"\.(\d+)\.pem", p.name)
            if m:
                out.append((int(m.group(1)), p, p.with_suffix(".key")))
    return sorted(out, reverse=True)


def _pubkey(cmd: list[str]) -> str:
    return _run(cmd).stdout


def cert_ok(platform: Platform, target: Target, days: int = 30) -> tuple[Path, Path] | None:
    """The newest certificate pair if it is complete, matching, covers every host and is not
    about to expire; otherwise None."""
    for _gen, pem, key in _cert_files(platform, target)[:1]:
        if not (pem.is_file() and key.is_file()) or pem.is_symlink() or key.is_symlink():
            return None
        if _run(["openssl", "x509", "-in", str(pem), "-noout", "-checkend", str(days * 86400)],
                check=False).returncode != 0:
            return None
        if _pubkey(["openssl", "x509", "-in", str(pem), "-noout", "-pubkey"]) != \
                _pubkey(["openssl", "pkey", "-in", str(key), "-pubout"]):
            return None
        san = _run(["openssl", "x509", "-in", str(pem), "-noout", "-ext", "subjectAltName"]).stdout
        names = set(re.findall(r"DNS:([^,\s]+)", san))
        if not set(target.hosts) <= names:
            return None
        return pem, key
    return None


def ensure_cert(platform: Platform, cf, target: Target) -> tuple[str, tuple[Path, Path]]:
    """One Origin CA certificate per target, covering all its hosts. A new generation is written
    side by side; the vhost switches to it in an edge transaction, so a crash never pairs a new
    key with an old certificate (codex #11)."""
    have = cert_ok(platform, target)
    if have:
        return "certificate present", have
    caddy_gid = grp.getgrnam("caddy").gr_gid
    platform.tls_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(platform.tls_dir, 0, caddy_gid)
    os.chmod(platform.tls_dir, 0o750)
    assert_root_dir(platform.tls_dir)
    gen = int(time.time())
    base = target.hosts[0]
    key = platform.tls_dir / f"{base}.{gen}.key"
    pem = platform.tls_dir / f"{base}.{gen}.pem"
    san = ",".join(f"DNS:{h}" for h in target.hosts)
    key_pem = _run(["openssl", "genpkey", "-algorithm", "EC",
                    "-pkeyopt", "ec_paramgen_curve:P-256"]).stdout
    csr = _run(["openssl", "req", "-new", "-key", "/dev/stdin", "-subj", f"/CN={base}",
                "-addext", f"subjectAltName={san}"], input_=key_pem).stdout
    if "BEGIN CERTIFICATE REQUEST" not in csr or "PRIVATE KEY" not in key_pem:
        raise EdgeError("could not generate key and CSR")
    cert = cf.origin_cert(target.hosts, csr)
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.write(fd, key_pem.encode())
        os.fchown(fd, 0, caddy_gid)
        os.fsync(fd)
    finally:
        os.close(fd)
    _atomic_write(pem, cert if cert.endswith("\n") else cert + "\n", 0o644)
    have = cert_ok(platform, target)
    if not have:
        raise EdgeError("new certificate pair does not verify (key/cert/SAN mismatch)")
    return f"origin CA certificate issued for {', '.join(target.hosts)} (15 years)", have


def prune_certs(platform: Platform, target: Target, in_use: tuple[Path, Path]) -> None:
    """Keep the generation in use and the one before it."""
    gens = _cert_files(platform, target)
    keep = {in_use[0]}
    older = [g for g in gens if g[1] != in_use[0]]
    if older:
        keep.add(older[0][1])
    for _g, pem, key in gens:
        if pem not in keep:
            pem.unlink(missing_ok=True)
            key.unlink(missing_ok=True)


# ------------------------------------------------------------------ vhost

def vhost_path(platform: Platform, target: Target) -> Path:
    return platform.caddy_sites / f"{target.hosts[0]}.caddy"


def render_vhost(platform: Platform, target: Target, cf_ranges: list[str],
                 cert: tuple[Path, Path], port: int | None) -> str:
    """port None renders the 'not yet released' form (503), so the edge can exist first."""
    names = ", ".join(target.hosts)
    pem, key = cert
    lines = [
        marker_line(target),
        f"# Generated from /etc/huta/projects.d/{target.project}.toml. Do not edit by hand.",
        f"{names} {{",
        f"\ttls {pem} {key}",
    ]
    if target.cloudflare == "orange":
        lines += [f"\t@notcf not remote_ip {' '.join(cf_ranges)}", "\tabort @notcf"]
    lines += ["\tencode zstd gzip"]
    knobs = target.edge or {}
    if "body" in knobs:
        lines += ["\trequest_body {", f"\t\tmax_size {int(knobs['body'])}", "\t}"]
    gateway = target.gateway if target.kind == "gateway" else None
    if port is None and not gateway:
        lines += ['\trespond "not released yet" 503']
    else:
        # No trusted_proxies: remote_ip stays the TCP peer, so the Cloudflare-only filter keeps
        # working (grok design review #8). The visitor's address is CF-Connecting-IP.
        upstream = gateway["to"] if gateway else f"127.0.0.1:{port}"
        inner = []
        if gateway and gateway.get("secret"):
            inner += [f"\t\timport {gateway_secret_path(platform, target)}"]
        if "timeout" in knobs:
            inner += ["\t\ttransport http {", f"\t\t\tread_timeout {knobs['timeout']}",
                      f"\t\t\twrite_timeout {knobs['timeout']}", "\t\t}"]
        if inner:
            lines += [f"\treverse_proxy {upstream} {{", *inner, "\t}"]
        else:
            lines += [f"\treverse_proxy {upstream}"]
    if target.access_log:
        lines += ["\tlog {", f"\t\toutput file {platform.caddy_log_dir}/{target.hosts[0]}.log",
                  "\t}"]
    else:
        lines += ["\t# access_log = false: no access log for this site"]
    lines += ["}", ""]
    return "\n".join(lines)


def gateway_secret_path(platform: Platform, target: Target) -> Path:
    return platform.caddy_secrets / f"{target.project}--{target.name}.caddy"


SECRET_VALUE_RE = re.compile(r"[A-Za-z0-9._~+/=:@%^*!,;-]{1,4096}")


def write_gateway_secret(platform: Platform, target: Target, value: str) -> bool:
    """The header line a gateway's vhost imports, root:<caddy group> 0640 in a directory only
    root and that group can enter. The value goes in quotes, so it may not contain anything
    Caddy would read as syntax (DESIGN-v2 §1). Returns whether the file changed."""
    if not SECRET_VALUE_RE.fullmatch(value):
        raise EdgeError(f"gateway secret for {target.project}/{target.name}: the value has "
                        "characters a Caddy header can't carry (quotes, braces, $, spaces)")
    gid = grp.getgrnam(platform.caddy_group).gr_gid
    d = platform.caddy_secrets
    d.mkdir(mode=0o750, parents=True, exist_ok=True)
    st = os.lstat(d)
    if not os.path.isdir(d) or os.path.islink(d) or st.st_uid != os.getuid():
        raise EdgeError(f"{d} must be a directory owned by root")
    os.chown(d, os.getuid(), gid)
    os.chmod(d, 0o750)
    path = gateway_secret_path(platform, target)
    text = f'header_up {target.gateway["header"]} "{value}"\n'
    if path.exists() and not path.is_symlink() and path.read_text() == text:
        return False
    tmp = path.with_name(f".{path.name}.huta-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, text.encode())
        os.fchown(fd, os.getuid(), gid)
        os.fchmod(fd, 0o640)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return True


def vhost_shape(text: str) -> str:
    """The vhost without its upstream port: what a plan compares (ports move on every swap)."""
    return UPSTREAM_RE.sub("reverse_proxy 127.0.0.1:<slot>", text)


def _managed(text: str) -> bool:
    return text.startswith((MARKER, *LEGACY_MARKERS))


def marker_line(target: Target) -> str:
    return f"{MARKER} — project {target.project}, target {target.name}."


def stale_vhosts(platform: Platform, target: Target) -> list[Path]:
    """This target's managed vhost files under another name than its first host's: left when
    the registry changed `hosts[0]`. Found by their marker line, never by name."""
    want = vhost_path(platform, target)
    line = marker_line(target)
    legacy = tuple(f"{m} — project {target.project}, target {target.name}."
                   for m in LEGACY_MARKERS)
    out = []
    d = platform.caddy_sites
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.caddy")):
        if p == want or p.is_symlink() or not p.is_file():
            continue
        try:
            with open(p, errors="replace") as f:
                first = f.readline().rstrip("\n")
        except OSError:
            continue
        if first == line or first in legacy:
            out.append(p)
    return out


def installed_path(platform: Platform, target: Target) -> Path | None:
    """Where this target's managed vhost is now: its first host's file, else the one file of
    this target under an older name (hosts changed). Two old ones: refused, never guessed."""
    p = vhost_path(platform, target)
    if p.exists():
        return p
    stale = stale_vhosts(platform, target)
    if len(stale) > 1:
        raise EdgeError(f"{target.project}/{target.name} has several vhost files "
                        f"({', '.join(x.name for x in stale)}): keep one, then huta edge --apply")
    return stale[0] if stale else None


def undo_state(platform: Platform, target: Target) -> list:
    """[path, text or None] for every file a vhost change of this target touches: what puts
    the served config back."""
    p = vhost_path(platform, target)
    out = [[str(p), p.read_text() if p.exists() else None]]
    out += [[str(x), x.read_text()] for x in stale_vhosts(platform, target)]
    return out


def installed(platform: Platform, target: Target) -> str | None:
    """huta's vhost text, or None. A foreign file at that path is refused, unless it is the
    file this target adopts (it will be replaced by the first release). After a change of
    `hosts[0]` the target's file under its old name is still its vhost (found by its marker
    line), until the next vhost change renames it."""
    p = vhost_path(platform, target)
    if not p.exists():
        old = installed_path(platform, target)
        return old.read_text() if old is not None else None
    text = p.read_text()
    if not _managed(text):
        if target.adopt == p.name:
            return None
        raise EdgeError(f"{p} exists and is not managed by huta; refusing to touch it")
    return text


def adapt_hosts(path: Path) -> set[str]:
    """The hosts a vhost file serves, as Caddy itself reads it (`caddy adapt`). A file Caddy
    can't read alone, or with a site that matches no host, is refused: guessing could delete a
    site that file still serves (final review, grok #3)."""
    import json as _json
    r = subprocess.run(["caddy", "adapt", "--config", str(path), "--adapter", "caddyfile"],
                       capture_output=True, text=True, cwd="/", timeout=30,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent"})
    if r.returncode != 0:
        raise EdgeError(f"{path.name}: caddy can't read it on its own, so huta can't tell what it "
                        f"serves: {(r.stderr or r.stdout).strip()[-200:]}")
    hosts = set()
    for srv in (_json.loads(r.stdout).get("apps", {}).get("http", {}).get("servers", {}) or {}).values():
        for route in srv.get("routes", []) or []:
            matches = route.get("match") or []
            if not matches or any(not m.get("host") for m in matches):
                raise EdgeError(f"{path.name}: a site without a host name; refusing to adopt it")
            for m in matches:
                hosts.update(h.lower() for h in m["host"])
    return hosts


def adopt_pending(platform: Platform, target: Target) -> Path | None:
    """The foreign vhost file still serving this target's hosts, if any (DESIGN-v1 §6). It must
    serve only the target's hosts: anything else in it would silently disappear."""
    if not target.adopt:
        return None
    p = platform.caddy_sites / target.adopt
    if p.is_symlink() or not p.is_file():
        return None
    text = p.read_text()
    if _managed(text):
        return None
    extra = adapt_hosts(p) - set(target.hosts)
    if extra:
        raise EdgeError(f"{p} also serves {', '.join(sorted(extra))}: move those to their own "
                        "file before adopting it")
    return p


def routed_port(platform: Platform, target: Target) -> int | None:
    text = installed(platform, target)
    if not text:
        return None
    m = UPSTREAM_RE.search(text)
    return int(m.group(1)) if m else None


def _caddy_env() -> dict:
    """The caddy unit's own environment (Environment= and EnvironmentFile=), so validation sees
    the same values the running server does. Values never leave this process."""
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
    show = _run(["systemctl", "show", "caddy", "-p", "Environment", "-p", "EnvironmentFiles"]).stdout
    for line in show.splitlines():
        k, _, v = line.partition("=")
        if k == "Environment" and v:
            for item in shlex.split(v):
                ek, _, ev = item.partition("=")
                env[ek] = ev
        elif k == "EnvironmentFiles" and v:
            for chunk in v.split(" ("):
                path = chunk.split(")")[-1].strip()
                if path.startswith("/") and os.path.exists(path):
                    for fl in Path(path).read_text().splitlines():
                        fl = fl.strip()
                        if fl and not fl.startswith("#") and "=" in fl:
                            ek, _, ev = fl.partition("=")
                            env[ek.strip()] = ev.strip().strip('"').strip("'")
    return env


def validate_caddy() -> None:
    """Validate as the caddy user, with the unit's environment and caddy's own HOME, from /.
    As root it passes where the server fails and creates root-owned log files; without HOME it
    writes storage into ./caddy (NOTES, 2026-11-09)."""
    env = _caddy_env()
    env["HOME"] = pwd.getpwnam("caddy").pw_dir
    keep = ",".join(sorted(k for k in env if k != "PATH"))
    r = subprocess.run(["runuser", "-u", "caddy", f"--whitelist-environment={keep}", "--",
                        "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter",
                        "caddyfile"], capture_output=True, text=True, env=env, cwd="/")
    if r.returncode != 0:
        raise EdgeError(f"caddy validate failed: {(r.stderr or r.stdout).strip()[-300:]}")


def ensure_log_file(platform: Platform, host: str) -> None:
    """Create the log file owned by caddy without following links: the directory belongs to
    caddy, so a planted symlink must not make root create or chown another file."""
    d = platform.caddy_log_dir
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(f"{host}.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=dfd)
        except FileExistsError:
            return
        try:
            st = os.fstat(dfd)
            os.fchown(fd, st.st_uid, st.st_gid)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def _journal(platform: Platform) -> Path:
    return platform.state_dir / "edge"


def change_vhost(platform: Platform, target: Target, text: str, adopt: bool = False):
    """The only way a vhost changes. Must be called with edge_lock held. With adopt, the
    target's foreign vhost file (if still there) is removed in the same transaction, after a
    copy is kept in the journal. Returns (message, undo): undo is the list of (path, old text or
    None) that puts everything back, for an automatic revert."""
    path = vhost_path(platform, target)
    assert_root_dir(platform.caddy_sites)
    old = installed(platform, target)
    changes: list[tuple[Path, str | None]] = []
    foreign = adopt_pending(platform, target) if adopt else None
    if foreign is not None and foreign != path:
        changes.append((foreign, None))
    # hosts[0] changed: the file under the old name goes in the same transaction.
    stale = stale_vhosts(platform, target)
    changes += [(x, None) for x in stale]
    if old != text or foreign is not None or stale:
        changes.insert(0, (path, text))
    if not changes:
        return "vhost unchanged", []
    for h in target.hosts:
        ensure_log_file(platform, h)
    if foreign is not None:
        keep = _journal(platform) / "adopted"
        keep.mkdir(mode=0o700, exist_ok=True)
        _atomic_write(keep / f"{foreign.name}.{int(time.time())}", foreign.read_text(), 0o600)
    undo = transact(platform, changes)
    if foreign is not None:
        msg = f"vhost installed, {foreign.name} adopted (copy in {_journal(platform) / 'adopted'})"
    else:
        msg = "vhost installed, caddy reloaded" if old is None else "vhost switched, caddy reloaded"
    return msg, undo


def transact(platform: Platform, changes: list[tuple[Path, str | None]]) -> list:
    """Write or remove several vhost files as one change: journal, apply, validate as caddy,
    reload; on any failure put every file back. Returns the undo list. edge_lock held."""
    j = _journal(platform)
    j.mkdir(mode=0o700, exist_ok=True)
    undo = []
    for path, _new in changes:
        if path.parent != platform.caddy_sites or not path.name.endswith(".caddy"):
            raise EdgeError(f"refusing to change {path}")
        old = path.read_text() if path.exists() else None
        prev = j / f"{path.name}.prev"
        if old is None:
            prev.unlink(missing_ok=True)
        else:
            _atomic_write(prev, old, 0o600)
        undo.append((path, old))
    pending = j / f"{changes[0][0].name}.pending"
    _atomic_write(pending, json.dumps({"paths": [str(p) for p, _ in changes]}) + "\n", 0o600)
    try:
        for path, new in changes:
            if new is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, new, 0o644)
        validate_caddy()
        _run(["systemctl", "reload", "caddy"])
    except BaseException as e:
        for path, _new in changes:
            _restore(path, j / f"{path.name}.prev")
        pending.unlink(missing_ok=True)
        if isinstance(e, EdgeError):
            raise EdgeError(f"vhost change refused and reverted, live config unchanged: {e}")
        raise
    pending.unlink(missing_ok=True)
    return undo


def _restore(path: Path, prev: Path) -> None:
    if prev.exists():
        _atomic_write(path, prev.read_text(), 0o644)
    else:
        path.unlink(missing_ok=True)


def recover(platform: Platform) -> list[str]:
    """Undo vhost transactions a crash left half-done. Call with edge_lock held."""
    done = []
    j = _journal(platform)
    if not j.exists():
        return done
    for pending in j.glob("*.pending"):
        raw = pending.read_text().strip()
        try:
            paths = [Path(p) for p in json.loads(raw)["paths"]]
        except (ValueError, KeyError, TypeError):
            paths = [Path(raw)]                 # one path per file (before multi-file changes)
        for path in paths:
            if path.parent != platform.caddy_sites or not path.name.endswith(".caddy"):
                raise EdgeError(f"refusing odd journal entry {pending}")
        for path in paths:
            _restore(path, j / (path.name + ".prev"))
        validate_caddy()
        _run(["systemctl", "reload", "caddy"])
        pending.unlink()
        done.append(f"restored {', '.join(p.name for p in paths)} after an interrupted change")
    return done
