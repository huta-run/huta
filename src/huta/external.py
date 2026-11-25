"""Publishing to Cloudflare Pages and Workers as a step of a release (DESIGN-v2 §5).

- The files come out of the release's image without running it: `docker create` + `docker cp`
  streams the directory as tar, read through a validator (regular files and directories only,
  no links or devices, relative paths, count and size caps). Their content hash goes in the
  plan and is checked again before publishing.
- Two contracts. Static (Pages): files only; `_worker.js`, `functions/`, `wrangler.*` and
  `package.json` are code and refused. Worker: one prebuilt bundle (`main`), uploaded without
  bundling.
- wrangler runs in a container pinned by digest in huta.toml ([external] wrangler), with only
  the registry's token, the files read-only, a config huta writes (the repo's wrangler.toml is
  never used), and egress through the egress daemon to api.cloudflare.com only (its own network,
  "huta/external" in the egress policy).
- Intent before every call: the phase records name, hash and the deployment before. After a
  crash, Cloudflare is asked whether a deployment of this commit exists before anything is sent
  again. Rollback is its own approved plan.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from .cfg import Platform, Project, Target
from .ext import ExtError, _req

API = "https://api.cloudflare.com/client/v4"
MAX_FILES = 20000
MAX_FILE = 25 << 20          # Cloudflare Pages' own limit per file
MAX_TOTAL = 512 << 20
CODE_NAMES = {"_worker.js", "functions", "wrangler.toml", "wrangler.json", "wrangler.jsonc",
              "package.json"}
NETWORK = "huta_external"
BRIDGE = "dp-" + hashlib.sha1(b"huta/external").hexdigest()[:12]
EGRESS = ["api.cloudflare.com:443"]


class PublishError(Exception):
    pass


# ------------------------------------------------------------------ files out of the image

def staging(platform: Platform, t: Target, rid: str, name: str) -> Path:
    return platform.state_dir / "external" / t.project / t.name / rid / name


def _validate_tar(stream, dest: Path, top: str) -> tuple[str, int, int]:
    """Unpack `docker cp`'s tar of <dir> into dest, refusing anything but plain files and
    directories. Returns (content hash, files, bytes)."""
    files, total, entries, dirs = 0, 0, [], 0
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for m in tar:
            name = m.name
            parts = name.split("/")
            if parts[0] != top or ".." in parts or name.startswith("/"):
                raise PublishError(f"unexpected path in the image's files: {name!r}")
            rel = "/".join(parts[1:])
            if not rel:
                if not m.isdir():
                    raise PublishError(f"{top} in the image is not a directory (a link?)")
                continue
            if m.issparse() if hasattr(m, "issparse") else m.type == tarfile.GNUTYPE_SPARSE:
                raise PublishError(f"{rel}: sparse files are not published")
            if not re.fullmatch(r"[A-Za-z0-9._@+~=,%-][A-Za-z0-9._@+~=,%/ -]{0,400}", rel) \
                    or "//" in rel:
                raise PublishError(f"a file name huta won't publish: {rel!r}")
            if m.isdir():
                dirs += 1
                if dirs + files > MAX_FILES:
                    raise PublishError(f"too much to publish: at most {MAX_FILES} entries")
                (dest / rel).mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            if not m.isreg():
                raise PublishError(f"{rel}: only regular files and directories are published "
                                   "(no links or devices)")
            files += 1
            total += m.size
            if files + dirs > MAX_FILES or m.size > MAX_FILE or total > MAX_TOTAL:
                raise PublishError(f"too much to publish: at most {MAX_FILES} files, "
                                   f"{MAX_FILE >> 20} MiB each, {MAX_TOTAL >> 20} MiB in all")
            out = dest / rel
            out.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            data = tar.extractfile(m).read()
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            entries.append(f"{rel}\0{hashlib.sha256(data).hexdigest()}")
    h = hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest()
    return h, files, total


def content_hash(d: Path) -> str:
    entries = []
    for root, dirs, names in os.walk(d):
        dirs.sort()
        for n in sorted(names):
            p = Path(root) / n
            entries.append(f"{p.relative_to(d)}\0{hashlib.sha256(p.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest()


def check_contract(kind: str, d: Path, main: str | None) -> None:
    top = {p.name for p in d.iterdir()}
    if kind == "cloudflare-pages":
        code = sorted(top & CODE_NAMES)
        if code:
            raise PublishError(f"static Pages files may not include code: {', '.join(code)} "
                               "(publish code as a cloudflare-worker)")
    else:
        if not main or not (d / main).is_file():
            raise PublishError(f"the worker bundle {main!r} is not in the files")


def extract(platform: Platform, t: Target, rid: str, name: str, image: str, cfg: dict) -> dict:
    """Copy one [external.<name>] directory out of the image into staging (rid: a key of this
    plan's own). Returns {"hash", "files", "bytes"}."""
    from .recipe import sh
    dest = staging(platform, t, rid, name)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(mode=0o755, parents=True)
    cid = sh(["docker", "create", "--network", "none", image, "/nonexistent"]).strip()
    try:
        src = cfg["dir"].rstrip("/") or "/"
        p = subprocess.Popen(["docker", "cp", f"{cid}:{src}", "-"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd="/")
        try:
            h, files, total = _validate_tar(p.stdout, dest, Path(src).name)
        finally:
            p.stdout.close()
            p.wait(timeout=600)
        if p.returncode != 0:
            raise PublishError(f"docker cp {src}: {p.stderr.read().decode(errors='replace')[-200:]}")
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=60, cwd="/")
    check_contract(cfg["kind"], dest, cfg.get("main"))
    return {"hash": h, "files": files, "bytes": total}


# ------------------------------------------------------------------ Cloudflare

def _api(platform: Platform, reg: dict, method: str, path: str, body=None):
    token = platform.secret(reg["token"])
    r = _req(method, f"{API}/accounts/{reg['account']}{path}",
             {"Authorization": f"Bearer {token}"}, body)
    if not r.get("success"):
        errs = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in r.get("errors", []))
        raise ExtError(f"cloudflare {method} {path.split('?')[0]}: {errs or 'failed'}")
    return r["result"]


def current(platform: Platform, kind: str, reg: dict) -> str | None:
    """The deployment (Pages) or version (Workers) serving now, if any."""
    try:
        if kind == "cloudflare-pages":
            p = _api(platform, reg, "GET", f"/pages/projects/{reg['name']}")
            return ((p or {}).get("canonical_deployment") or {}).get("id")
        d = _api(platform, reg, "GET", f"/workers/scripts/{reg['name']}/deployments")
        deps = (d or {}).get("deployments") or []
        return (deps[0]["versions"][0]["version_id"]) if deps else None
    except ExtError:
        return None


def find(platform: Platform, kind: str, reg: dict, sha: str, content: str) -> str | None:
    """A deployment of this commit's files that already exists (after a crash, before sending
    anything again)."""
    if kind == "cloudflare-pages":
        for d in _api(platform, reg, "GET", f"/pages/projects/{reg['name']}/deployments?per_page=20") or []:
            meta = ((d.get("deployment_trigger") or {}).get("metadata") or {})
            if meta.get("commit_hash") == sha and d.get("environment") == "production":
                return d["id"]
        return None
    for v in (_api(platform, reg, "GET", f"/workers/scripts/{reg['name']}/versions") or {}).get("items", []):
        if (v.get("metadata") or {}).get("message") == f"huta {sha[:12]} {content[:12]}":
            return v["id"]
    return None


def ensure_network() -> None:
    from .recipe import sh
    if subprocess.run(["docker", "network", "inspect", NETWORK], capture_output=True,
                      timeout=30, cwd="/").returncode != 0:
        sh(["docker", "network", "create", "--driver", "bridge",
            "-o", f"com.docker.network.bridge.name={BRIDGE}",
            "-o", "com.docker.network.bridge.enable_icc=false", NETWORK])


def _worker_config(reg: dict, cfg: dict, sha: str, content: str) -> str:
    routes = ", ".join(f'{{ pattern = {json.dumps(r)}, zone_name = {json.dumps(z)} }}'
                       for r, z in reg.get("route_zones", []))
    return "\n".join([
        f"name = {json.dumps(reg['name'])}",
        f"main = {json.dumps('/src/' + cfg['main'])}",
        f"account_id = {json.dumps(reg['account'])}",
        f"compatibility_date = {json.dumps(reg['compatibility_date'])}",
        "workers_dev = false",
        "no_bundle = true",
        f"routes = [{routes}]",
        ""])


def publish(platform: Platform, project: Project, t: Target, rid: str, sha: str, name: str,
            cfg: dict, reg: dict, content: str, log, key: str | None = None) -> str:
    """Run wrangler for one external. Returns the new deployment or version id. key: the
    plan's staging key (its files, extracted at planning)."""
    from .recipe import sh
    from . import runtime as R
    d = staging(platform, t, key or sha[:12], name)
    if content_hash(d) != content:
        raise PublishError(f"{name}: the files changed since planning")
    ensure_network()
    from . import egress
    from .cfg import load_registry
    egress.sync(platform, load_registry(platform=platform))
    proxy = f"http://{R.EGRESS_HOST}:{platform.egress_port}"
    env = {"CLOUDFLARE_API_TOKEN": platform.secret(reg["token"]),
           "CLOUDFLARE_ACCOUNT_ID": reg["account"], "HTTPS_PROXY": proxy,
           "https_proxy": proxy, "HTTP_PROXY": proxy, "http_proxy": proxy, "NO_PROXY": "",
           "no_proxy": "", "NODE_USE_ENV_PROXY": "1", "WRANGLER_SEND_METRICS": "false",
           "CI": "1", "HOME": "/tmp"}
    image = platform.wrangler_image
    args = ["docker", "run", "--rm", "--name", f"huta-publish-{t.project}-{t.name}",
            "--network", NETWORK, "--add-host", f"{R.EGRESS_HOST}:host-gateway",
            "--user", f"{project.uid}:{project.uid}", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--memory", "1g", "--pids-limit", "256",
            "--tmpfs", "/tmp:size=256m,mode=1777", "--dns", R.NO_DNS, "--log-driver", "none",
            "--mount", f"type=bind,source={d},target=/src,readonly"]
    for k in env:
        args += ["--env", k]
    tag = f"huta {sha[:12]} {content[:12]}"
    if cfg["kind"] == "cloudflare-pages":
        cmd = ["wrangler", "pages", "deploy", "/src", "--project-name", reg["name"],
               "--branch", "main", "--commit-hash", sha, "--commit-message", tag]
    else:
        from .cfg import zone_for
        reg = {**reg, "route_zones": [(r, zone_for(r.split("/", 1)[0].removeprefix("*."), platform))
                                      for r in reg["routes"]]}
        conf = d.parent / f".{name}.wrangler.toml"
        from .recipe import edge
        edge._atomic_write(conf, _worker_config(reg, cfg, sha, content), 0o644)
        args += ["--mount", f"type=bind,source={conf},target=/cfg/wrangler.toml,readonly"]
        cmd = ["wrangler", "deploy", "--config", "/cfg/wrangler.toml", "--message", tag]
    sh(["docker", "rm", "-f", f"huta-publish-{t.project}-{t.name}"], check=False)
    r = subprocess.run([*args, image, *cmd], env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", **env},
                       capture_output=True, text=True, timeout=1800, cwd="/")
    from .cfg import redact
    out = redact(r.stdout + r.stderr)
    if r.returncode != 0:
        raise PublishError(f"wrangler failed (exit {r.returncode}): {out[-400:]}")
    dep = find(platform, cfg["kind"], reg, sha, content)
    if not dep:
        raise PublishError("wrangler reported success but Cloudflare shows no such deployment")
    log(f"{name}: published to {cfg['kind'].split('-', 1)[1]} {reg['name']} ({dep})")
    return dep


def rollback(platform: Platform, kind: str, reg: dict, to: str) -> None:
    if kind == "cloudflare-pages":
        _api(platform, reg, "POST", f"/pages/projects/{reg['name']}/deployments/{to}/rollback")
    else:
        _api(platform, reg, "POST", f"/workers/scripts/{reg['name']}/deployments",
             {"strategy": "percentage", "versions": [{"version_id": to, "percentage": 100}]})
