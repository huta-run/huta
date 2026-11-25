"""huta on a dev box: build and ask. It never acts on the host; the host checks every claim.

  huta pack   [--app deploy/app.toml]          build the image(s) for HEAD of a clean checkout
  huta submit <target> [--app …] [--no-wait | --follow]   ask the host to plan this commit
                                               (--follow: report approval, deploy, outcome)
  huta status <project> <target>
  huta log    <release>
  huta jobs   <project> <target>
  huta logs   <project> <target> [--since 1h] [--until …] [--grep text] [--tail N]
              [--slot a|b|live|previous] [--release id] [--job name]   redacted by the host
  huta egress-log|traffic <project> <target> [--since 1h] [--until …]
  huta crashes <project> <target> [--since 1d]
  huta diag   <project> <target> <name>
  huta secret set <project> <NAME>              send a secret's value from stdin (or
  huta secret import <project> NAME… --from .env   from an env file); the host stages it and
                                               it goes live with the next approved release

  huta login                                    push only: docker login to the host's registry
  huta self-update                              replace this client with the host's version

The client keeps itself at the host's version: every answer names the host's revision, and when
it differs the client fetches the host's client and swaps it in after the command. Set
auto_update = false in client.toml to get only a notice.

Transport (~/.config/huta/client.toml), one of:
  transport = "pull"                     # the host fetches the image (same-host dev box)
  [pull]
  inbox = "/huta/inbox"
  outbox = "/huta/outbox"

  transport = "push"                     # this box sends the image and the request
  [push]
  registry = "host.tailnet.example:4882" # the host's registry (TLS); 4882 = HUTA on a keypad
  ssh = "huta@100.64.0.1"                # the host's forced-command user
  port = 22
  key = "~/.ssh/id_huta_push"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

CONFIG = Path.home() / ".config" / "huta" / "client.toml"
LIB = Path(__file__).resolve().parent.parent      # ~/.local/lib/huta-client when installed
SEEN = {"server": None}                           # the host's revision, from its last answer


class ClientError(Exception):
    pass


def _git(*args: str, cwd=None) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise ClientError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _repo():
    root = Path(_git("rev-parse", "--show-toplevel"))
    sha = _git("rev-parse", "HEAD", cwd=root)
    return root, sha


def _app(root: Path, rel: str) -> dict:
    p = root / rel
    if not p.is_file():
        raise ClientError(f"no {rel} in this checkout (use --app)")
    return tomllib.loads(p.read_text())


def _config() -> dict:
    if not CONFIG.exists():
        raise ClientError(f"no {CONFIG}; see `huta --help`")
    return tomllib.loads(CONFIG.read_text())


def build_ref(project: str, sha: str) -> str:
    return f"huta-build/{project}:{sha[:12]}"


def image_refs(project: str, sha: str, app: dict) -> dict[str, str]:
    """name -> local ref of every image the project builds. One image keeps the v1 name; with
    several, the public component's image keeps it and the others get huta-build/<p>/<name>."""
    imgs = app.get("images", {})
    if len(imgs) <= 1:
        return {n: build_ref(project, sha) for n in imgs}
    from .spec import public
    main = public(app)["image"]
    return {n: build_ref(project, sha) if n == main else f"huta-build/{project}/{n}:{sha[:12]}"
            for n in imgs}


def cmd_pack(a) -> int:
    root, sha = _repo()
    if _git("status", "--porcelain", "--untracked-files=no", cwd=root):
        raise ClientError("the checkout has uncommitted changes; commit first (releases are commits)")
    app = _app(root, a.app)
    project = app.get("project")
    imgs = app.get("images", {})
    if not imgs:
        raise ClientError("app.toml has no [images.<name>]")
    refs = image_refs(project, sha, app)
    # Build from `git archive` of the commit, never from the working tree: untracked or
    # git-ignored files (a .env, a key) can't end up in an image labelled as this commit
    # (transport review #13).
    with tempfile.TemporaryDirectory(prefix="huta-pack-") as tmp:
        arch = subprocess.run(["git", "archive", "--format=tar", sha], cwd=root,
                              capture_output=True)
        if arch.returncode != 0:
            raise ClientError(f"git archive failed: {arch.stderr.decode(errors='replace')[-500:]}")
        x = subprocess.run(["tar", "-x", "-C", tmp], input=arch.stdout, capture_output=True)
        if x.returncode != 0:
            raise ClientError("could not unpack the commit")
        export = Path(tmp)
        for name, img in imgs.items():
            ref = refs[name]
            print(f"building {ref} from {sha[:12]} (a clean export of the commit)", flush=True)
            dockerfile = (export / img["dockerfile"]).resolve()
            context = (export / img.get("context", ".")).resolve()
            for p in (dockerfile, context):
                if export.resolve() not in p.parents and p != export.resolve():
                    raise ClientError(f"{p} is outside the checkout")
            # Each image of a project with several says which it is, so the host can't be
            # handed one in place of another (DESIGN-v2 §3).
            named = ["--label", f"run.huta.image={name}"] if len(imgs) > 1 else []
            # Always linux/amd64 (the host's platform), also on arm64 Macs (emulated); no
            # provenance attestation, so the pushed digest is a plain image manifest.
            r = _run_with_heartbeat(["docker", "build", "-q", "-f", str(dockerfile),
                                "--platform", "linux/amd64", "--provenance=false",
                                "--build-arg", f"RELEASE_SHA={sha}",
                                "--label", f"org.opencontainers.image.revision={sha}",
                                "--label", f"run.huta.project={project}", *named,
                                "-t", ref, str(context)], None, 3600, "still building")
            if r.returncode != 0:
                raise ClientError(f"docker build failed:\n{r.stderr.strip()[-2000:]}")
            print(f"built {ref} ({r.stdout.strip()[:19]})")
    print(f"next: push {sha[:12]} to main if it isn't there, then `huta submit <target>`")
    return 0


def _progress(msg: str) -> None:
    print(f"… {msg}", flush=True)


def _ssh(cfg: dict, req: dict) -> dict:
    p = cfg["push"]
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-i", os.path.expanduser(p["key"]), "-p", str(p.get("port", 22))]
    if p.get("known_hosts"):
        # The host's key is pinned at install time: never trust-on-first-use.
        cmd += ["-o", f"UserKnownHostsFile={os.path.expanduser(p['known_hosts'])}",
                "-o", "StrictHostKeyChecking=yes"]
    cmd.append(p["ssh"])
    if req["op"] == "submit":
        _progress("the host is checking the commit and pulling the image (can take a minute)")
    try:
        r = _run_with_heartbeat(cmd, json.dumps(req), 1200, "still working")
    except subprocess.TimeoutExpired:
        raise ClientError("no answer over ssh within 20 minutes")
    out = r.stdout.strip()
    if not out:
        raise ClientError(f"no answer over ssh: {r.stderr.strip()[-500:]}")
    return _check(json.loads(out), req)


def _run_with_heartbeat(cmd, input_: str | None, timeout: float, what: str):
    """subprocess.run with a progress line every 30 s."""
    import threading
    done = threading.Event()
    t0 = time.monotonic()

    def beat():
        while not done.wait(30):
            _progress(f"{what} ({int(time.monotonic() - t0)} s)")

    threading.Thread(target=beat, daemon=True).start()
    try:
        return subprocess.run(cmd, input=input_, capture_output=True, text=True, timeout=timeout)
    finally:
        done.set()


def _check(ans, req: dict) -> dict:
    """An answer must be for this request (transport review #11)."""
    if not isinstance(ans, dict) or ans.get("v") != 1:
        raise ClientError("the host's answer is not a huta answer")
    if ans.get("id") not in (req["id"], None):
        raise ClientError(f"the host answered request {ans.get('id')}, not {req['id']}")
    if ans.get("id") is None and ans.get("ok"):
        raise ClientError("the host's answer has no request id")
    if isinstance(ans.get("huta"), str):
        SEEN["server"] = ans["huta"]
    return ans


def local_rev() -> str:
    try:
        return (LIB / "REVISION").read_text().strip()
    except OSError:
        return "unknown"


def _updatable() -> bool:
    """Only a per-user install updates itself (never the host's own copy in /opt/huta)."""
    return Path.home().resolve() in LIB.parents and (LIB / "huta" / "client.py").is_file()


BUNDLE_MEMBER = re.compile(r"huta-client/(src/huta/[a-z_]+\.py|install-client\.sh|REVISION)")


def self_update() -> str:
    """Fetch the client the host would install (op "client") and swap it in. Returns the new
    revision. The host is already trusted (pinned ssh key, or a root-owned outbox)."""
    import base64
    import hashlib
    import io
    import shutil
    import tarfile
    if not _updatable():
        raise ClientError(f"this client ({LIB}) isn't a per-user install; update it where it was installed")
    ans = _send(_req("client"), True)
    if not ans.get("ok"):
        raise ClientError(ans.get("error", "refused"))
    blob = base64.b64decode(ans["bundle"], validate=True)
    if hashlib.sha256(blob).hexdigest() != ans["sha256"]:
        raise ClientError("the client bundle is damaged (sha256 mismatch)")
    new = LIB.with_name(f".huta-client.new-{os.getpid()}")
    old = LIB.with_name(f".huta-client.old-{os.getpid()}")
    new.mkdir(mode=0o755)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for m in tar.getmembers():
                if not (m.isfile() and BUNDLE_MEMBER.fullmatch(m.name)):
                    raise ClientError(f"unexpected entry in the client bundle: {m.name[:80]!r}")
                rel = m.name.removeprefix("huta-client/").removeprefix("src/")
                dest = new / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(tar.extractfile(m).read())
                dest.chmod(0o755 if rel.endswith(".sh") else 0o644)
        if not (new / "huta" / "client.py").is_file():
            raise ClientError("the client bundle has no client")
        os.rename(LIB, old)
        os.rename(new, LIB)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
    return local_rev()


def _after_command() -> None:
    """Keep the client at the host's version: update by default, or only say so when the box
    set `auto_update = false`."""
    server = SEEN["server"]
    if not server or server == "unknown" or server == local_rev() or not _updatable():
        return
    try:
        auto = _config().get("auto_update", True) is not False
    except (ClientError, OSError, tomllib.TOMLDecodeError):
        auto = False
    if not auto:
        print(f"huta: the server runs {server[:12]}, this client {local_rev()[:12]}; "
              "update with `huta self-update`", file=sys.stderr)
        return
    try:
        print(f"huta: client updated to the server's version {self_update()[:12]}", file=sys.stderr)
    except (ClientError, OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        print(f"huta: couldn't update the client to the server's {server[:12]}: {e}; "
              "try `huta self-update`", file=sys.stderr)


def cmd_self_update(a) -> int:
    before = local_rev()
    after = self_update()
    print(f"client {before[:12]} → {after[:12]}" if before != after else f"client already at {after[:12]}")
    return 0


def _push_image(cfg: dict, ref: str) -> str:
    """Push the local build to the host's registry; return the manifest digest it reports."""
    remote = f"{cfg['push']['registry']}/{ref}"
    subprocess.run(["docker", "tag", ref, remote], check=True)
    r = subprocess.run(["docker", "push", remote], capture_output=True, text=True)
    if r.returncode != 0:
        raise ClientError(f"docker push failed (did you run `huta login`?):\n{r.stderr.strip()[-1000:]}")
    digests = re.findall(r"digest: (sha256:[0-9a-f]{64})", r.stdout)
    if not digests:
        raise ClientError("docker push did not report a digest")
    return digests[-1]


def _send(req: dict, wait: bool) -> dict | None:
    cfg = _config()
    mode = cfg.get("transport", "pull")
    if mode == "push":
        return _ssh(cfg, req)
    if mode != "pull":
        raise ClientError(f"unknown transport {mode!r}")
    inbox = Path(cfg["pull"]["inbox"])
    outbox = Path(cfg["pull"]["outbox"])
    tmp = inbox / f".{req['id']}.json.tmp"
    # 0600: a request can carry a secret's value (op secret), and only this user and the host
    # may read it (codex secrets #3).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(req))
    os.replace(tmp, inbox / f"{req['id']}.json")   # appears whole or not at all
    if not wait:
        print(f"sent request {req['id']}; the answer will be in {outbox}/{req['id']}.json")
        return None
    ans = outbox / f"{req['id']}.json"
    placed = inbox / f"{req['id']}.json"
    t0 = time.monotonic()
    deadline = t0 + 600
    nudge = t0 + 15
    said_claimed, beat = False, t0 + 30
    while time.monotonic() < deadline:
        if ans.exists():
            return _check(json.loads(ans.read_text()), req)
        if req["op"] == "submit":
            # Say where the request is, so a slow step never looks like a hang.
            if not said_claimed and not placed.exists():
                said_claimed = True
                _progress("the host picked up the request: checking the commit and importing "
                          "the image (can take a minute)")
            if time.monotonic() > beat:
                beat = time.monotonic() + 30
                _progress(f"still {'working' if said_claimed else 'waiting for the host to pick it up'}"
                          f" ({int(time.monotonic() - t0)} s)")
        if time.monotonic() > nudge and placed.exists():
            # Still unclaimed: it may have landed while the host was finishing a run and
            # missed the watch. Renaming it out and back in fires the watch again.
            nudge = time.monotonic() + 15
            try:
                os.replace(placed, tmp)
                os.replace(tmp, placed)
            except FileNotFoundError:
                pass                               # the host claimed it meanwhile
        time.sleep(1)
    if req["op"] == "secret":
        # Claimed but not answered yet: the host may still stage it, so wait for its answer.
        if not placed.exists():
            for _ in range(120):
                if ans.exists():
                    return _check(json.loads(ans.read_text()), req)
                time.sleep(1)
            raise ClientError(f"the host took the request but hasn't answered; check {ans} later "
                              "(the value may be staged)")
        # Don't leave a value lying in the inbox if the host never picked it up.
        for p in (placed, tmp):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        if not ans.exists():
            raise ClientError("the host didn't pick up the request in 10 minutes; it was removed, "
                              "send it again later")
        return _check(json.loads(ans.read_text()), req)
    raise ClientError(f"no answer after 10 minutes; check {ans} later")


def _show(ans: dict) -> int:
    if not ans.get("ok"):
        print(f"refused: {ans.get('error')}", file=sys.stderr)
        return 1
    if ans.get("text"):
        print(ans["text"])
    if isinstance(ans.get("warning"), str):
        print(f"\n{ans['warning'][:300]}\n  If that isn't what you meant, ask the owner to "
              "reject this plan.", file=sys.stderr)
    if ans.get("next"):
        print(f"\n{ans['next']}")
    return 0


def _req(op: str, **fields) -> dict:
    return {"v": 1, "id": secrets.token_hex(8), "op": op, **fields}


def cmd_submit(a) -> int:
    root, sha = _repo()
    app = _app(root, a.app)
    project = app.get("project")
    refs = image_refs(project, sha, app)
    for ref in refs.values():
        if subprocess.run(["docker", "image", "inspect", ref], capture_output=True).returncode != 0:
            raise ClientError(f"{ref} not built; run `huta pack` first")
    ref = build_ref(project, sha)
    fields = {"project": project, "target": a.target, "sha": sha, "image": ref}
    cfg = _config()
    push = cfg.get("transport") == "push"
    if len(refs) > 1:
        # Several images (DESIGN-v2 §3): all of them, each with its digest when pushed. A
        # one-image project sends exactly the v1 request, so older hosts keep working.
        fields["images"] = {}
        for name, r in refs.items():
            entry = {"image": r}
            if push:
                entry["digest"] = _push_image(cfg, r)
                print(f"pushed {r} ({entry['digest'][:19]})", flush=True)
            fields["images"][name] = entry
        if push:
            fields["digest"] = fields["images"][next(n for n, r in refs.items() if r == ref)]["digest"]
    elif push:
        fields["digest"] = _push_image(cfg, ref)
        print(f"pushed {ref} ({fields['digest'][:19]})", flush=True)
    ans = _send(_req("submit", **fields), not a.no_wait)
    if not ans:
        return 0
    rc = _show(ans)
    if rc or not getattr(a, "follow", False) or not ans.get("release"):
        return rc
    return _follow(ans["release"])


TERMINAL = {"live", "nothing-activated", "rolled-back", "needs-human", "rejected", "superseded"}


def _follow(rid: str) -> int:
    """--follow: report the release's events as they happen, until it ends (approval included:
    up to the 30-minute plan expiry, then the release itself)."""
    _progress(f"following {rid}: waiting for the owner's approval (Telegram button or "
              f"sudo huta approve {rid}); Ctrl-C stops following, not the release")
    seen = 0
    deadline = time.monotonic() + 50 * 60
    while time.monotonic() < deadline:
        ans = _send(_req("log", release=rid), True)
        if not ans.get("ok"):
            print(f"refused: {ans.get('error')}", file=sys.stderr)
            return 1
        lines = (ans.get("text") or "").splitlines()
        for line in lines[seen:]:
            print(f"  {line}", flush=True)
        seen = max(seen, len(lines))
        if ans.get("state") in TERMINAL:
            print(f"outcome: {ans['state']}")
            return 0 if ans["state"] == "live" else 1
        time.sleep(5)
    print(f"still not finished after 50 minutes; check with huta log {rid}", file=sys.stderr)
    return 1


def cmd_login(a) -> int:
    """Push transport: get this box's registry login from the host over ssh and hand it to
    `docker login` on stdin. The password is never printed or stored by huta."""
    cfg = _config()
    if cfg.get("transport") != "push":
        raise ClientError("`huta login` is only for the push transport")
    ans = _ssh(cfg, _req("credentials"))
    if not ans.get("ok"):
        raise ClientError(ans.get("error", "refused"))
    r = subprocess.run(["docker", "login", ans["registry"], "-u", ans["username"],
                        "--password-stdin"], input=ans["password"], capture_output=True, text=True)
    if r.returncode != 0:
        raise ClientError(f"docker login failed: {r.stderr.strip()[-300:]}")
    print(f"logged in to {ans['registry']} as {ans['username']}")
    return 0


def cmd_status(a) -> int:
    return _show(_send(_req("status", project=a.project, target=a.target), True))


def cmd_log(a) -> int:
    return _show(_send(_req("log", release=a.release), True))


def cmd_jobs(a) -> int:
    return _show(_send(_req("jobs", project=a.project, target=a.target), True))


LOG_OPTS = ("since", "until", "grep", "tail", "slot", "release", "job", "component")


def cmd_observe(a) -> int:
    fields = {k: getattr(a, k) for k in ("since", "until") if getattr(a, k, None) is not None}
    return _show(_send(_req(a.cmd, project=a.project, target=a.target, **fields), True))


def cmd_diag(a) -> int:
    ans = _send(_req("diag", project=a.project, target=a.target, diag=a.name), True)
    rc = _show(ans)
    return rc if rc or ans.get("exit") in (0, None) else 1


def cmd_logs(a) -> int:
    fields = {k: getattr(a, k) for k in LOG_OPTS if getattr(a, k) is not None}
    ans = _send(_req("logs", project=a.project, target=a.target, **fields), True)
    rc = _show(ans)
    if ans.get("ok") and ans.get("truncated"):
        print("(truncated: the host says how above)", file=sys.stderr)
    return rc


def _read_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines (comments, blank lines, `export `, simple quotes), as the host reads them."""
    from .runtime import parse_env_file
    return parse_env_file(Path(path).read_text())


def cmd_secret(a) -> int:
    """Send a value for a secret the host registry already names. The host stages it; it goes
    live only with a release the owner approves (the plan lists it). Values never go on the
    command line: they come from stdin (not echoed on a terminal) or from an env file."""
    if a.action == "set":
        if len(a.names) != 1 or a.file:
            raise ClientError("huta secret set <project> <NAME>; the value comes from stdin")
        if sys.stdin.isatty():
            import getpass
            value = getpass.getpass(f"{a.names[0]} (not echoed): ")
        else:
            value = sys.stdin.read()
        values = {a.names[0]: value.rstrip("\n")}
    else:
        if not a.file or not a.names:
            raise ClientError("huta secret import <project> NAME… --from <env file>")
        env = _read_env_file(a.file)
        missing = [n for n in a.names if n not in env]
        if missing:
            raise ClientError(f"not in {a.file}: {', '.join(missing)}")
        values = {n: env[n] for n in a.names}
    import re as _re
    from .proto import MAX_REQUEST, MAX_SECRET
    for name, value in values.items():
        if not value.strip():
            raise ClientError(f"{name}: empty value")
        if len(value) > MAX_SECRET or _re.search(r"[\x00-\x1f\x7f]", value):
            raise ClientError(f"{name}: a box can send one line of at most {MAX_SECRET} "
                              "characters; a multi-line secret is set by the operator on the host")
    rc = 0
    for name, value in values.items():
        req = _req("secret", project=a.project, name=name, value=value)
        if len(json.dumps(req).encode()) > MAX_REQUEST:
            raise ClientError(f"{name}: the value is too large to send")
        ans = _send(req, True)
        rc |= _show(ans)
    return rc


def _overview() -> str:
    """What bare `huta` prints: where this box ships to, and the next thing to type."""
    where = "not set up yet: see https://huta.run/install.md"
    try:
        c = tomllib.loads(CONFIG.read_text())
        t = c.get("transport")
        if t == "push":
            where = f"push to {c['push']['ssh'].split('@', 1)[-1]} (registry {c['push']['registry']})"
        elif t == "pull":
            where = f"pull (inbox {c['pull']['inbox']})"
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    return f"""huta: ship this project to your server; prod waits for the owner's approval.
this box: {where}

  huta pack                     build the image for HEAD (clean checkout, deploy/app.toml)
  huta submit <target>          ask the server to plan it, e.g. huta submit prod
  huta status <project> <target>  releases and what's live
  huta log <release>            one release's events
  huta jobs <project> <target>  the live release's jobs, next runs, last outcomes
  huta logs <project> <target>  container logs, redacted (--since 1h --grep … --slot live)
  huta egress-log|traffic|crashes <project> <target>   refusals, edge summary, crash reports
  huta diag <project> <target> <name>   a [[diag]] of the live release (no network, no secrets)
  huta secret set <project> <NAME>   send a secret's value (stdin); live after an approved release
  huta login                    docker login to the server's registry (push boxes)
  huta self-update              get the server's client now (it also updates itself on use)

Docs for agents and people: https://huta.run/llms.md   ·   huta <command> -h for options"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="huta", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")
    s = sub.add_parser("pack"); s.add_argument("--app", default="deploy/app.toml"); s.set_defaults(f=cmd_pack)
    s = sub.add_parser("submit"); s.add_argument("target"); s.add_argument("--app", default="deploy/app.toml")
    s.add_argument("--no-wait", action="store_true")
    s.add_argument("--follow", action="store_true",
                   help="after the plan, report approval, deploy steps and the outcome")
    s.set_defaults(f=cmd_submit)
    s = sub.add_parser("status"); s.add_argument("project"); s.add_argument("target"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("log"); s.add_argument("release"); s.set_defaults(f=cmd_log)
    s = sub.add_parser("jobs"); s.add_argument("project"); s.add_argument("target"); s.set_defaults(f=cmd_jobs)
    s = sub.add_parser("logs", help="the target's container logs, redacted by the host")
    s.add_argument("project"); s.add_argument("target")
    s.add_argument("--since", help="90s, 15m, 1h (default), 2d, or a UTC time 2026-11-25T10:00")
    s.add_argument("--until", help="same forms as --since")
    s.add_argument("--grep", help="plain substring, case-insensitive (no regex)")
    s.add_argument("--tail", type=int, help="the last N matching lines (default 500, max 5000)")
    s.add_argument("--slot", choices=["a", "b", "live", "previous"])
    s.add_argument("--release", help="a release id (8 hex)")
    s.add_argument("--job", help="a job's output instead of the slots' (if the host allows it)")
    s.add_argument("--component", help="one component's lines (targets with several)")
    s.set_defaults(f=cmd_logs)
    for name, what in (("egress-log", "what the egress proxy refused for the target, per minute"),
                       ("traffic", "edge summary: requests per minute by status class, and 5xx"),
                       ("crashes", "Bugsink issues of the target (or a legacy service)")):
        s = sub.add_parser(name, help=what); s.add_argument("project"); s.add_argument("target")
        s.add_argument("--since", help="90s, 15m, 1h, 2d, or a UTC time")
        if name != "crashes":
            s.add_argument("--until")
        s.set_defaults(f=cmd_observe)
    s = sub.add_parser("diag", help="run a diagnostic the live release's app.toml declares")
    s.add_argument("project"); s.add_argument("target"); s.add_argument("name")
    s.set_defaults(f=cmd_diag)
    s = sub.add_parser("secret", help="send a secret's value (stdin or --from); the owner approves it with a release")
    s.add_argument("action", choices=["set", "import"]); s.add_argument("project")
    s.add_argument("names", nargs="+"); s.add_argument("--from", dest="file")
    s.set_defaults(f=cmd_secret)
    s = sub.add_parser("login"); s.set_defaults(f=cmd_login)
    s = sub.add_parser("self-update"); s.set_defaults(f=cmd_self_update)
    a = ap.parse_args(argv)
    if not a.cmd:
        print(_overview())
        return 0
    try:
        rc = a.f(a)
        if a.cmd != "self-update":
            _after_command()
        return rc
    except (ClientError, OSError, KeyError, ValueError, subprocess.SubprocessError,
            tomllib.TOMLDecodeError) as e:
        print(f"huta: {e}", file=sys.stderr)
        return 1
