"""Bringing an image built on dev into the host's Docker without trusting the archive.

A `docker save` archive carries its own names (manifest.json RepoTags, index.json annotations,
the legacy `repositories` file). Loading it as-is can overwrite any tag on the host, such as a
production service's image (codex review #1). So the archive is checked, stripped of every name,
loaded as a bare image id, and only then named inside huta's namespace.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import select
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from .recipe import RecipeError, image_facts, sh

BUILD_REF_RE = r"^huta-build/{project}:[0-9a-f]{{12}}$"
MAX_ARCHIVE = 2 * 1024 ** 3
MAX_MEMBERS = 20000
MAX_META = 4 * 1024 ** 2
NAME_KEYS = ("io.containerd.image.name", "org.opencontainers.image.ref.name")


def _incoming(state_dir: Path) -> Path:
    d = state_dir / "incoming"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _fetch_from_dev(container: str, ref: str, dest: Path, timeout: float = 900) -> None:
    """The dev box controls what `docker save` emits, so: stderr goes to a capped temp file
    (no pipe deadlock), stdout is read with a deadline and a size cap (transport review #5)."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    errf = tempfile.TemporaryFile()
    deadline = time.monotonic() + timeout
    with os.fdopen(fd, "wb") as out:
        p = subprocess.Popen(["incus", "exec", container, "--", "docker", "save", ref],
                             stdout=subprocess.PIPE, stderr=errf, cwd="/")
        try:
            total = 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RecipeError(f"docker save on {container} took longer than {int(timeout)} s")
                ready, _, _ = select.select([p.stdout], [], [], min(left, 30))
                if not ready:
                    continue
                chunk = os.read(p.stdout.fileno(), 1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE:
                    raise RecipeError("image archive larger than 2 GiB; refused")
                out.write(chunk)
            if p.wait(timeout=max(1, deadline - time.monotonic())) != 0:
                errf.seek(0)
                err = errf.read(4096).decode(errors="replace")
                raise RecipeError(f"docker save on {container} failed: {err.strip()[-300:]}")
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            errf.close()


def _strip_names(src: Path, dst: Path) -> set[str]:
    """Validate the archive and write a copy without any image names. Returns the ids the
    loaded image may have: with Docker's containerd store (Docker 29+ default on new installs) the image id is the
    digest of the manifest in index.json; with the classic store it is the config digest."""
    with tarfile.open(src, "r:") as tin:
        members = []
        logical = 0
        for m in tin:
            members.append(m)
            if len(members) > MAX_MEMBERS:
                raise RecipeError(f"archive has more than {MAX_MEMBERS} entries; refused")
            n = m.name
            if n.startswith("/") or ".." in Path(n).parts or not (m.isfile() or m.isdir()):
                raise RecipeError(f"archive entry {n[:80]!r} is not a plain relative file; refused")
            if m.issparse():
                raise RecipeError(f"archive entry {n[:80]!r} is sparse; refused")
            logical += m.size
            if logical > MAX_ARCHIVE:
                raise RecipeError("archive expands past 2 GiB; refused")
        names = {}
        for m in members:
            key = m.name.removeprefix("./").rstrip("/")
            if key in names:
                raise RecipeError(f"archive has {key[:80]!r} twice; refused")
            names[key] = m
        for meta in ("manifest.json", "index.json", "oci-layout"):
            if meta in names and names[meta].size > MAX_META:
                raise RecipeError(f"{meta} is larger than {MAX_META} bytes; refused")
        if "manifest.json" not in names:
            raise RecipeError("archive has no manifest.json")
        manifest = json.load(tin.extractfile(names["manifest.json"]))
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise RecipeError("archive must hold exactly one image")
        cfg = manifest[0].get("Config", "")
        m = re.fullmatch(r"(?:blobs/sha256/)?([0-9a-f]{64})(?:\.json)?", cfg)
        if not m:
            raise RecipeError(f"unexpected config path {cfg!r}")
        ids = {"sha256:" + m.group(1)}
        manifest[0]["RepoTags"] = None
        index = None
        if "index.json" in names:
            index = json.load(tin.extractfile(names["index.json"]))
            if len(index.get("manifests", [])) != 1:
                raise RecipeError("archive index must hold exactly one manifest")
            dg = index["manifests"][0].get("digest", "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", dg):
                raise RecipeError("archive index has no valid manifest digest")
            ids.add(dg)
            for d in index.get("manifests", []):
                ann = d.get("annotations") or {}
                for k in NAME_KEYS:
                    ann.pop(k, None)
                if ann:
                    d["annotations"] = ann
                else:
                    d.pop("annotations", None)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fout, tarfile.open(fileobj=fout, mode="w:") as tout:
            for mem in members:
                key = mem.name.removeprefix("./")
                if key == "repositories":
                    continue
                if key in ("manifest.json", "index.json"):
                    data = json.dumps(manifest if key == "manifest.json" else index).encode()
                    info = tarfile.TarInfo(mem.name)
                    info.size, info.mode, info.mtime = len(data), 0o644, mem.mtime
                    tout.addfile(info, io.BytesIO(data))
                elif mem.isfile():
                    tout.addfile(mem, tin.extractfile(mem))
                else:
                    tout.addfile(mem)
    return ids


def import_from_dev(project: str, ref: str, state_dir: Path, container: str = "dev") -> tuple[str, str, str]:
    """Returns (image id, huta tag, claimed sha)."""
    if not re.match(BUILD_REF_RE.format(project=re.escape(project)), ref):
        raise RecipeError(f"dev image must be named huta-build/{project}:<12 hex>, got {ref!r}")
    inc = _incoming(state_dir)
    token = secrets.token_hex(6)
    raw, clean = inc / f"{token}.tar", inc / f"{token}.clean.tar"
    before = _tag_map()          # every existing name → id, so a moved tag can be put back
    image_id = None
    try:
        _fetch_from_dev(container, ref, raw)
        expected = _strip_names(raw, clean)
        out = sh(["docker", "load", "-q", "-i", str(clean)])
        loaded = re.findall(r"^Loaded image(?: ID)?: (sha256:[0-9a-f]{64})$", out, re.M)
        image_id = loaded[0] if len(loaded) == 1 else None
        moved = _restore_tags(before)
        tagged = re.findall(r"^Loaded image: (?!sha256:)(\S+)$", out, re.M)
        if tagged or moved:
            raise RecipeError(f"docker load created or moved names {sorted(set(tagged) | set(moved))}; "
                              "they were put back and the import refused")
        if len(loaded) != 1 or loaded[0] not in expected:
            raise RecipeError(f"docker load reported {loaded}, expected one of {sorted(expected)}")
        return _name_it(project, image_id)
    except Exception:
        # Anything refused after the load must not leave the image behind (grok #3).
        if image_id and image_id not in before.values():
            subprocess.run(["docker", "image", "rm", "-f", image_id], capture_output=True, timeout=120)
        raise
    finally:
        raw.unlink(missing_ok=True)
        clean.unlink(missing_ok=True)


def _tag_map() -> dict[str, str]:
    out = sh(["docker", "images", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"])
    m = {}
    for line in out.splitlines():
        name, _, iid = line.partition(" ")
        if not name.endswith(":<none>") and not name.startswith("<none>"):
            m[name] = iid
    return m


def _restore_tags(before: dict[str, str]) -> list[str]:
    """Put back any existing name that now points at another image. Returns the names moved."""
    after = _tag_map()
    moved = []
    for name, old in before.items():
        if after.get(name) != old:
            moved.append(name)
            subprocess.run(["docker", "tag", old, name], capture_output=True, timeout=60)
    return moved


def _name_it(project: str, image_id: str) -> tuple[str, str, str]:
    f = image_facts(image_id)
    sha = f["labels"].get("org.opencontainers.image.revision", "")
    if f["labels"].get("run.huta.project") != project or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RecipeError("image labels don't name this project and a full sha; refused")
    tag = f"huta/{project}:{sha[:12]}-{image_id.split(':')[1][:12]}"
    existing = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                              capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() != image_id:
        raise RecipeError(f"{tag} already names another image; refused")
    sh(["docker", "tag", image_id, tag])
    return image_id, tag, sha


def import_from_registry(project: str, ref: str, digest: str, registry: str) -> tuple[str, str, str]:
    """Push transport: pull exactly the manifest the box pushed, by digest (a tag in the
    registry is mutable; the digest is not), then name it in huta's namespace and drop the
    registry name. Returns (image id, huta tag, claimed sha)."""
    if not re.match(BUILD_REF_RE.format(project=re.escape(project)), ref):
        raise RecipeError(f"image must be named huta-build/{project}:<12 hex>, got {ref!r}")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RecipeError("bad digest")
    repo = f"{registry}/{ref.split(':')[0]}"
    pinned = f"{repo}@{digest}"
    sh(["docker", "pull", "-q", pinned])
    image_id = image_facts(pinned)["id"]
    try:
        return _name_it(project, image_id)
    finally:
        subprocess.run(["docker", "image", "rm", pinned], capture_output=True)
