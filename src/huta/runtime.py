"""What a container of a target gets: environment, secrets, egress wiring, the data mount and the
hardening. One place, so slots and jobs can't drift apart (DESIGN-v1 §1–4).
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

from .cfg import ENV_NAME_RE, ConfigError, Platform, Project, Target, read_secret, size_bytes
from . import spec as SP

EGRESS_HOST = "huta-egress"     # /etc/hosts name of the egress daemon inside containers
# Docker's embedded resolver forwards external names from the host's namespace, a side channel
# around "no egress" (a name like <secret>.attacker.example). A loopback resolver nobody listens
# on makes every external lookup fail; the egress daemon resolves names itself.
NO_DNS = "127.0.0.254"
SECRET_PLACEHOLDER = "‹secret›"


# ------------------------------------------------------------------ secrets

def secret_dir(platform: Platform, project: str) -> Path:
    return platform.secrets_dir / project


def secret_value(platform: Platform, project: str, name: str) -> str:
    p = secret_dir(platform, project) / name
    if not p.exists():
        raise ConfigError(f"secret {name} is not set: sudo huta secret set {project} {name}")
    return read_secret(p)


def _root_dir(d: Path) -> None:
    d.mkdir(mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or st.st_uid != 0:
        raise ConfigError(f"{d} must be a root-owned directory")
    os.chmod(d, 0o700)


def secret_set(platform: Platform, project: str, name: str, value: str) -> None:
    """Set a secret's live value (the operator, on the host). A value a box staged for the
    same name is dropped: the operator's is newer."""
    d = secret_dir(platform, project)
    _write_secret(d, name, value)
    staged_drop(platform, project, name)


def _write_secret(d: Path, name: str, value: str) -> None:
    if not ENV_NAME_RE.match(name):
        raise ConfigError("secret names are UPPER_CASE")
    if not value or "\0" in value or len(value) > 60000:
        raise ConfigError("empty or invalid value")
    if d.name == STAGED:
        _root_dir(d.parent)
    _root_dir(d)
    if d.name != STAGED:
        retire(d / name, d, name)
    # A unique temp name: two writers (two boxes, or a box and the operator) never share one.
    tmp = d / f".{name}.{os.getpid()}.{secrets.token_hex(6)}.new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, value.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, d / name)
    _fsync_dir(d)


def retire(p: Path, live: Path, name: str) -> None:
    """Keep a value that was live and is about to be replaced or removed in secrets/.retired/
    (a hard link: no copy, no read), so logs kept for days are still redacted against it
    (cfg.redact, RETIRED_DAYS). p holds the value (the live file or its journal copy), live is
    secrets/<project>; nothing happens if p doesn't exist."""
    import time
    from .cfg import RETIRED, RETIRED_DAYS
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        return
    r = live.parent / RETIRED
    _root_dir(r)
    os.link(p, r / f"{live.name}.{name}.{int(time.time())}.{secrets.token_hex(4)}")
    for f in r.iterdir():
        try:
            if time.time() - int(f.name.split(".")[2]) > RETIRED_DAYS * 86400:
                f.unlink()
        except (IndexError, ValueError):
            pass
    _fsync_dir(r)


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# A dev box may send a new value for a secret the registry already names. It waits here, staged,
# and goes live only with a release the owner approves (the plan lists it): a box must not be
# able to swap a key under a running app (point mail at its own account, say) unseen.
STAGED = ".staged"


def staged_dir(platform: Platform, project: str) -> Path:
    return secret_dir(platform, project) / STAGED


def secret_stage(platform: Platform, project: str, name: str, value: str) -> None:
    _write_secret(staged_dir(platform, project), name, value)


def _fingerprint(p: Path) -> str | None:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
        return None
    return f"{st.st_ino}:{st.st_mtime_ns}:{st.st_size}"


def staged(platform: Platform, project: str, names) -> dict[str, str]:
    """{name: fingerprint} of the staged values among names. The fingerprint (inode, mtime,
    size) lets approval prove it applies exactly the value the plan listed."""
    d = staged_dir(platform, project)
    if not d.is_dir():
        return {}
    out = {}
    for n in names:
        if ENV_NAME_RE.match(n) and (f := _fingerprint(d / n)):
            out[n] = f
    return out


def staged_list(platform: Platform, project: str) -> list[tuple[str, float]]:
    d = staged_dir(platform, project)
    if not d.is_dir():
        return []
    return sorted((p.name, p.stat().st_mtime) for p in d.iterdir()
                  if p.is_file() and not p.is_symlink() and ENV_NAME_RE.match(p.name))


def live_versions(platform: Platform, project: str, names) -> dict[str, str]:
    """{name: fingerprint or ""} of the live values: a plan records them, and approval refuses
    when one changed since (another target's release applied a staged value, or the operator
    set one), so no plan goes live with a value it didn't list."""
    d = secret_dir(platform, project)
    return {n: (_fingerprint(d / n) or "") for n in names if ENV_NAME_RE.match(n)}


# Applying staged values is journaled by release id in the staged directory, so a crash at any
# point can be settled once that release's outcome is known (settle_secrets, from reconcile):
#   .claim-<rid>-<NAME>   the planned file, taken out of reach of a box that sends again
#   .prev-<rid>-<NAME>    the live value it replaced (a hard link)
#   .none-<rid>-<NAME>    there was no live value
# Values go live with the release or not at all: on nothing-activated or rolled-back the old
# values come back and the new ones are staged again. Every step holds the project's secrets
# lock (secrets_lock).
CLAIM, PREV, NONE = ".claim-", ".prev-", ".none-"
SECRETS_LOCK = "_secrets"         # Store.lock(project, SECRETS_LOCK): stage, set, apply, settle


def _journal(d: Path, kind: str, rid: str, name: str) -> Path:
    return d / f"{kind}{rid}-{name}"


def _parse_journal(fname: str):
    for kind in (CLAIM, PREV, NONE):
        if fname.startswith(kind):
            rid, sep, name = fname[len(kind):].partition("-")
            if sep and re.fullmatch(r"[0-9a-f]{8}", rid) and ENV_NAME_RE.match(name):
                return kind, rid, name
    return None


def claim_staged(platform: Platform, project: str, rid: str, planned: dict[str, str]) -> None:
    """Take every planned staged file (rename to .claim-<rid>-NAME) and check its fingerprint
    on the claimed file: a value a box sends meanwhile lands beside it and stays staged, so it
    can't slip in between check and rename. On any mismatch every claim goes back and
    ConfigError is raised; nothing changed."""
    d = staged_dir(platform, project)
    done: list[str] = []
    try:
        for n, fp in sorted(planned.items()):
            if not ENV_NAME_RE.match(n):
                raise ConfigError(f"bad secret name {n!r} in the plan")
            c = _journal(d, CLAIM, rid, n)
            try:
                os.rename(d / n, c)
            except FileNotFoundError:
                raise ConfigError(f"the staged value of {n} is gone since planning") from None
            done.append(n)
            if _fingerprint(c) != fp:
                raise ConfigError(f"the staged value of {n} changed since planning")
            read_secret(c, remember=False)   # same checks as a live secret; not a needle yet
    except BaseException:
        for n in done:
            _restage(d, n, _journal(d, CLAIM, rid, n))
        raise
    _fsync_dir(d)


def promote_claimed(platform: Platform, project: str, rid: str, applied: list[str]) -> None:
    """Make the claimed values live, keeping each old value aside first. `applied` grows as each
    one goes live, so a caller sees what changed even if this raises."""
    d, live = staged_dir(platform, project), secret_dir(platform, project)
    for c in sorted(d.iterdir()):
        j = _parse_journal(c.name)
        if not j or j[0] != CLAIM or j[1] != rid:
            continue
        n = j[2]
        if (live / n).exists():
            os.link(live / n, _journal(d, PREV, rid, n))
        else:
            os.close(os.open(_journal(d, NONE, rid, n), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600))
        _fsync_dir(d)
        os.replace(c, live / n)
        _fsync_dir(live)
        applied.append(n)


def settle_secrets(platform: Platform, project: str, rid: str, keep: bool) -> list[str]:
    """After the release's outcome: keep the new values (drop the old copies) or put the old
    values back and stage the new ones again (unless a newer value was sent meanwhile).
    Returns the names it reverted."""
    d, live = staged_dir(platform, project), secret_dir(platform, project)
    if not d.is_dir():
        return []
    reverted = []
    for p in sorted(d.iterdir()):
        j = _parse_journal(p.name)
        if not j or j[1] != rid:
            continue
        kind, _, n = j
        if kind == CLAIM:                 # claimed, never promoted
            _restage(d, n, p)
            continue
        promoted = (live / n).exists() and not (
            kind == PREV and os.lstat(live / n).st_ino == os.lstat(p).st_ino)
        if not keep and promoted:
            try:
                os.link(live / n, d / n)  # the new value is staged again…
            except FileExistsError:
                pass                      # …unless a box sent a newer one meanwhile
            retire(live / n, live, n)     # containers had it while it was live
            if kind == PREV:
                os.replace(p, live / n)   # the old value is live again
            else:
                (live / n).unlink()
            reverted.append(n)
        elif keep and kind == PREV and promoted:
            retire(p, live, n)            # the old value, replaced for good
        p.unlink(missing_ok=True)
    _fsync_dir(live)
    _fsync_dir(d)
    return reverted


def pending_journal(platform: Platform, project: str) -> set[str]:
    """Release ids with an apply that hasn't been settled."""
    d = staged_dir(platform, project)
    if not d.is_dir():
        return set()
    return {j[1] for p in d.iterdir() if (j := _parse_journal(p.name))}


def _restage(d: Path, name: str, claim: Path) -> None:
    """Put a claimed value back as staged, unless a newer one was sent meanwhile."""
    try:
        os.link(claim, d / name)
    except FileExistsError:
        pass
    claim.unlink(missing_ok=True)


def staged_drop(platform: Platform, project: str, name: str) -> bool:
    p = staged_dir(platform, project) / name
    if ENV_NAME_RE.match(name) and p.is_file() and not p.is_symlink():
        p.unlink()
        return True
    return False


def secret_list(platform: Platform, project: str) -> list[tuple[str, float]]:
    d = secret_dir(platform, project)
    if not d.is_dir():
        return []
    return sorted((p.name, p.stat().st_mtime) for p in d.iterdir()
                  if p.is_file() and not p.is_symlink() and ENV_NAME_RE.match(p.name))


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=VALUE lines of a .env file (comments, blank lines, `export `, simple quotes)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, sep, v = line.partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


# ------------------------------------------------------------------ environment

def upstream_var(name: str) -> str:
    return "HUTA_UPSTREAM_" + name.upper().replace("-", "_")


def environment(platform: Platform, project: Project, t: Target, sha: str, slot: str,
                with_secrets: bool) -> dict[str, str]:
    """The container's environment. with_secrets=False puts a placeholder where each secret
    goes (for plans: names only, never values)."""
    env = dict(t.env)
    for name in t.secrets:
        env[name] = secret_value(platform, project.name, name) if with_secrets else SECRET_PLACEHOLDER
    if t.egress_allow:
        proxy = f"http://{EGRESS_HOST}:{platform.egress_port}"
        # Both cases, and ALL_PROXY cleared, so a variable baked into the image can't pick
        # another route. Plain-HTTP requests reach the daemon too, which refuses them clearly.
        # The egress host itself is dialed directly: its upstream ports (HUTA_UPSTREAM_*) are
        # plain TCP forwards, and the proxy refuses the host's own addresses anyway.
        direct = f"localhost,127.0.0.1,{EGRESS_HOST}"
        env.update({"HTTPS_PROXY": proxy, "https_proxy": proxy, "HTTP_PROXY": proxy,
                    "http_proxy": proxy, "ALL_PROXY": "", "all_proxy": "",
                    "NO_PROXY": direct, "no_proxy": direct,
                    "NODE_USE_ENV_PROXY": "1"})
    for u in t.upstreams:
        env[upstream_var(u)] = f"{EGRESS_HOST}:{platform.upstreams[u]['port']}"
    env.update({"RELEASE_SHA": sha, "HUTA_PROJECT": project.name, "HUTA_TARGET": t.name,
                "HUTA_SLOT": slot})
    return env


def compose_escape(env: dict[str, str]) -> dict[str, str]:
    """Compose interpolates $ in its file; a value is data, so every $ is doubled."""
    return {k: v.replace("$", "$$") for k, v in env.items()}


# ------------------------------------------------------------------ data

def data_paths(platform: Platform, t: Target) -> tuple[Path, Path]:
    """(image file, mount point)."""
    base = platform.state_dir / "data" / t.project
    return base / f"{t.name}.img", base / t.name


def data_mount(app: dict) -> str | None:
    """Where the public component mounts the data volume (v1: the only component)."""
    return SP.public(app).get("data")


def concurrent_safe(app: dict, t: Target | None = None) -> bool:
    """Two releases may run at once: no data, or both the repo (concurrent_safe) and the host
    (data.concurrent) say so."""
    writers = [c for c in app["components"].values() if c.get("data")]
    if not writers:
        return True
    return all(bool(c.get("concurrent_safe", False)) for c in writers) \
        and bool(t and t.data and t.data.get("concurrent"))


# ------------------------------------------------------------------ hardening (shared)

def hardening(project: Project, t: Target) -> dict:
    lim = t.limits
    tmp = size_bytes(lim["tmp"], "limits.tmp", 1 << 20, 4 << 30) if "tmp" in lim else 16 << 20
    return {
        "user": f"{project.uid}:{project.uid}",
        "read_only": True,
        "tmpfs": [f"/tmp:size={tmp},mode=1777"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": str(lim.get("memory", "256m")),
        "cpus": float(lim.get("cpus", 1)),
        "pids_limit": int(lim.get("pids", 128)),
        "dns": [NO_DNS],
    }


def needs_gateway(t: Target) -> bool:
    return bool(t.egress_allow or t.upstreams)


def docker_run_args(platform: Platform, project: Project, t: Target, app: dict, network: str,
                    env: dict[str, str], name: str, labels: dict[str, str]) -> list[str]:
    """`docker run` flags matching the compose slots, for jobs."""
    h = hardening(project, t)
    args = ["--name", name, "--network", network, "--user", h["user"], "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", h["mem_limit"], "--cpus", str(h["cpus"]),
            "--pids-limit", str(h["pids_limit"]), "--tmpfs", h["tmpfs"][0], "--pull", "never",
            "--dns", NO_DNS,
            # Output reaches the journal only through huta's redactor, not Docker's log store
            # (job secrets review, codex #2).
            "--log-driver", "none"]
    for k, v in labels.items():
        args += ["--label", f"{k}={v}"]
    if needs_gateway(t):
        args += ["--add-host", f"{EGRESS_HOST}:host-gateway"]
    mnt = data_mount(app)
    if mnt:
        _img, where = data_paths(platform, t)
        args += ["--mount", f"type=bind,source={where},target={mnt}"]
    # --env NAME only: the values come from huta's own environment for this call, so they
    # never appear on a command line (ps).
    for k in env:
        args += ["--env", k]
    return args

