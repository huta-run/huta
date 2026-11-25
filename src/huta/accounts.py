"""Push boxes: one system user `huta-<box>` (group huta-push), one ssh key pinned to the box's
address, one registry login. `huta enroll` creates them and prints the client's settings;
`huta revoke` removes them. sshd needs no per-box change: a static
`AllowUsers huta-*@<tailnet range>` plus `Match User huta-*` → ForceCommand covers every box,
and each key line carries `from="<box ip>"`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import grp
import ipaddress
import os
import pwd
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from .cfg import BOX_RE, ConfigError, read_secret

GROUP = "huta-push"
HOME_ROOT = Path("/var/lib/huta-ssh")
KEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,2}( [A-Za-z0-9@._-]{1,64})?$")
# huta-<box> must fit the 32-character user name limit.


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd="/")
    if r.returncode != 0:
        raise ConfigError(f"{cmd[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def _bcrypt(pw: str) -> str:
    lib = ctypes.CDLL(ctypes.util.find_library("crypt"))
    lib.crypt_gensalt.restype = ctypes.c_char_p
    lib.crypt_gensalt.argtypes = [ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_int]
    lib.crypt.restype = ctypes.c_char_p
    lib.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    h = lib.crypt(pw.encode(), lib.crypt_gensalt(b"$2b$", 12, None, 0)).decode()
    if not h.startswith("$2b$12$") or lib.crypt(pw.encode(), h.encode()).decode() != h:
        raise ConfigError("bcrypt via libcrypt failed")
    return h


def box_ok(box: str) -> bool:
    """A box may push if its user exists and is in the push group (the socket's group)."""
    try:
        return f"huta-{box}" in grp.getgrnam(GROUP).gr_mem
    except KeyError:
        return False


def write_htpasswd(secrets_dir: Path, htpasswd: Path) -> list[str]:
    """Rebuild the registry's htpasswd from every login file in secrets/registry/."""
    d = secrets_dir / "registry"
    users = sorted(p.name for p in d.iterdir() if p.is_file() and BOX_RE.match(p.name)) if d.exists() else []
    lines = [f"{u}:{_bcrypt(read_secret(d / u))}" for u in users]
    htpasswd.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = htpasswd.with_name(".htpasswd.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, htpasswd)
    return users


def restart_registry(project: str, service: str = "registry") -> None:
    """The registry reads htpasswd at start: restart its one container (a few seconds). Anything
    other than exactly one match is an error, never a guess (codex #6, grok #3)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ConfigError("bad registry_project")
    ids = _run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
                "--filter", f"label=com.docker.compose.service={service}"]).split()
    if len(ids) != 1:
        raise ConfigError(f"expected one {service} container in compose project {project!r}, found {len(ids)}")
    _run(["docker", "restart", ids[0]])


def enroll(box: str, pubkey: str, from_ip: str, secrets_dir: Path) -> None:
    if not BOX_RE.match(box):
        raise ConfigError("box name: lowercase letters, digits and dashes")
    if not KEY_RE.match(pubkey.strip()):
        raise ConfigError("public key must be one 'ssh-ed25519 AAAA… [comment]' line")
    # Plain addresses only: an IPv6 scope id ("fe80::1%…") may carry quotes or newlines into the
    # authorized_keys line (codex review of 674b48c, #2).
    if not re.fullmatch(r"[0-9A-Fa-f.:]{2,45}", from_ip or ""):
        raise ConfigError("--from must be a plain IP address (the box's tailnet address)")
    try:
        ip = ipaddress.ip_address(from_ip)
    except ValueError:
        raise ConfigError("--from must be the box's IP address (its tailnet address)")
    _check_key(pubkey.strip())
    user = f"huta-{box}"
    try:
        grp.getgrnam(GROUP)
    except KeyError:
        _run(["groupadd", "--system", GROUP])
    home = HOME_ROOT / box
    try:
        pwd.getpwnam(user)
        # Re-enroll: make sure sshd reads the key we write (grok #3).
        _run(["usermod", "--home", str(home), "--shell", "/bin/sh", "-aG", GROUP, user])
    except KeyError:
        _run(["useradd", "--system", "--home-dir", str(home), "--no-create-home",
              "--shell", "/bin/sh", "-G", GROUP, user])
    for d in (HOME_ROOT, home, home / ".ssh"):
        d.mkdir(mode=0o755, exist_ok=True)
        os.chown(d, 0, 0)
        os.chmod(d, 0o755)
    ak = home / ".ssh" / "authorized_keys"
    try:
        toks = ak.read_text().split()
        i = next(i for i, t in enumerate(toks) if t in ("ssh-ed25519", "ecdsa-sha2-nistp256"))
        replaced = toks[i:i + 2] != pubkey.strip().split()[:2]
    except (OSError, StopIteration):
        replaced = False
    tmp = ak.with_name(".authorized_keys.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        # command= too, so even without the sshd Match block the key can't get a shell (grok #2).
        f.write(f'restrict,command="/usr/local/sbin/huta-ssh",from="{ip}" {pubkey.strip()}\n')
    os.replace(tmp, ak)
    reg = secrets_dir / "registry"
    reg.mkdir(mode=0o700, exist_ok=True)
    pw = reg / box
    if replaced and pw.exists():
        pw.unlink()            # a new key means a new registry login too (codex #8, grok #3)
    if not pw.exists():
        fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))


def _check_key(line: str) -> None:
    """Parse the key for real (ssh-keygen), not just its look, before anything changes (#9)."""
    r = subprocess.run(["ssh-keygen", "-l", "-f", "-"], input=line + "\n", capture_output=True,
                       text=True, timeout=10)
    if r.returncode != 0:
        raise ConfigError("that public key does not parse (ssh-keygen -l refused it)")


def revoke(box: str, secrets_dir: Path) -> list[str]:
    """Cut access first (key, push group, registry login), then end the box's sessions, then
    remove the user. A box that is mid-request can't block this (codex review of 674b48c, #4)."""
    if not BOX_RE.match(box):
        raise ConfigError("bad box name")
    done = []
    user = f"huta-{box}"
    home = HOME_ROOT / box
    ak = home / ".ssh" / "authorized_keys"
    if ak.is_file() and not ak.is_symlink():
        ak.unlink()
        done.append("ssh key removed")
    pw = secrets_dir / "registry" / box
    if pw.exists():
        pw.unlink()
        done.append("registry login removed")
    try:
        pwd.getpwnam(user)
    except KeyError:
        user = None
    if user:
        if box_ok(box):
            _run(["gpasswd", "-d", user, GROUP])
            done.append(f"{user} can no longer push")
        subprocess.run(["pkill", "-KILL", "-u", user], capture_output=True, timeout=10)
        for _ in range(20):
            if subprocess.run(["pgrep", "-u", user], capture_output=True).returncode != 0:
                break
            time.sleep(0.25)
        _run(["userdel", user])
        done.append(f"user {user} removed")
    if home.is_dir() and not home.is_symlink() and home.parent == HOME_ROOT:
        shutil.rmtree(home)
        done.append(f"{home} removed")
    return done


def host_key() -> str:
    t = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
    return f"{t[0]} {t[1]}"
