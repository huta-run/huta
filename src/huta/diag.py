"""huta diag <project> <target> <name>: a diagnostic the live release's app.toml declares
([[diag]] name, cmd, timeout), run on request from the target's dev boxes or the operator.

It runs in the live release's image with the slots' hardening (project uid, read-only root,
no capabilities, the target's limits), but:

- no network at all (`--network none`), so it can't reach the slots, the egress daemon or
  anything else;
- no secrets: only the registry's non-secret `env`, RELEASE_SHA, HUTA_PROJECT, HUTA_TARGET and
  HUTA_DIAG;
- with data, the **last backup copy** at the manifest's data path, read-only, never the live
  volume. The copy is a hard-linked snapshot of `<backup dir>/<p>/<t>/current` made for this run
  (`.diag-<random>` beside it, same filesystem, no data copied), so a backup rotating `current`
  meanwhile doesn't pull files from under the run. A WAL-mode SQLite database is copied with
  its -wal to /tmp by the diag and opened there (immutable=1 ignores the -wal; mode=ro can't
  make the -shm on a read-only mount);
- bounded: its `timeout` (at most 5 min), 256 KiB of output, one diag per target at a time.

Its output passes the log gate (log_drop, redaction, 8192 characters a line) before it is
answered; the run is an event (who, which diag, exit status, duration), never its output.

Crash safety: the container is named huta-diag_<p>_<t>; a run whose huta process died leaves it
and its snapshot, and the next diag of the target removes both before it starts.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time

from . import logs as L
from . import runtime as R
from .cfg import ConfigError, Platform, Project, Target

OUT_MAX = 256 * 1024


def _name(t: Target) -> str:
    return f"huta-diag_{t.project}_{t.name}"


def _cleanup(platform: Platform, t: Target) -> None:
    subprocess.run(["docker", "rm", "-f", _name(t)], capture_output=True, timeout=60, cwd="/")
    if platform.backup:
        from .data import backup_dir
        base = backup_dir(platform, t)
        if base.is_dir():
            for p in base.iterdir():
                if p.name.startswith(".diag-") and p.is_dir() and not p.is_symlink():
                    subprocess.run(["rm", "-rf", "--one-file-system", "--", str(p)],
                                   capture_output=True, timeout=600, cwd="/")


def run(platform: Platform, st, project: Project, t: Target, name: str, who: str) -> dict:
    from .recipe import check_image
    try:
        fd = st.acquire(project.name, "_diag-" + t.name, wait=0)
    except RuntimeError:
        raise ConfigError(f"a diag of {project.name}/{t.name} is running; try again when it ends") from None
    snap = None
    try:
        _cleanup(platform, t)
        live = st.live(project.name, t.name)
        if not live:
            raise ConfigError(f"{project.name}/{t.name}: nothing is live")
        rel = st.release(live["release_id"])
        app = json.loads(rel["app_toml"])
        d = next((x for x in app.get("diag", []) if x["name"] == name), None)
        if d is None:
            names = ", ".join(x["name"] for x in app.get("diag", [])) or "none"
            raise ConfigError(f"the live release has no diag {name!r} (it has: {names})")
        check_image(rel["image_id"], rel["sha"], project.name)
        h = R.hardening(project, t)
        env = {**t.env, "RELEASE_SHA": rel["sha"], "HUTA_PROJECT": project.name,
               "HUTA_TARGET": t.name, "HUTA_DIAG": name}
        args = ["docker", "run", "--rm", "--name", _name(t), "--network", "none",
                "--user", h["user"], "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--memory", h["mem_limit"],
                "--cpus", str(h["cpus"]), "--pids-limit", str(h["pids_limit"]),
                "--tmpfs", h["tmpfs"][0], "--pull", "never", "--log-driver", "none",
                "--label", f"run.huta.project={project.name}", "--label", f"run.huta.target={t.name}",
                "--label", f"run.huta.diag={name}"]
        mnt = R.data_mount(app)
        note = ""
        if mnt:
            from .data import backup_dir
            cur = backup_dir(platform, t) / "current" if platform.backup else None
            if cur is None or not cur.is_dir() or cur.is_symlink():
                note = "no backup copy yet: ran without data"
            else:
                snap = cur.parent / f".diag-{secrets.token_hex(6)}"
                r = subprocess.run(["cp", "-al", "--no-dereference", "--", str(cur), str(snap)],
                                   capture_output=True, text=True, timeout=600, cwd="/")
                if r.returncode != 0:
                    raise ConfigError(f"could not snapshot the backup copy: {r.stderr.strip()[-200:]}")
                from .data import last_backup
                b = last_backup(platform, t) or {}
                age = f"{(time.time() - b['ts']) / 3600:.1f} h old" if b.get("ts") else "age unknown"
                note = f"data: the last backup copy ({age}), read-only"
                args += ["--mount", f"type=bind,source={snap},target={mnt},readonly"]
        for k in env:
            args += ["--env", k]
        args += [rel["image_id"], *d["cmd"]]
        st.event("diag-start", f"{name} by {who}", rel["id"], project.name, t.name)
        t0 = time.monotonic()
        proc = subprocess.Popen(args, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", **env},
                                cwd="/", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        buf = bytearray()
        cut = [False]

        def _read():
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                if len(buf) < OUT_MAX:
                    buf.extend(chunk[:OUT_MAX - len(buf)])
                if len(chunk) and len(buf) >= OUT_MAX:
                    cut[0] = True        # keep draining so the process can't block on the pipe

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        try:
            rc = proc.wait(timeout=d["timeout"])
            why = f"exit {rc}"
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", _name(t)], capture_output=True, timeout=60, cwd="/")
            proc.wait(timeout=60)
            rc, why = 124, f"killed after its {d['timeout']} s timeout"
        reader.join(10)
        took = time.monotonic() - t0
        lines, dropped = [], 0
        for raw in buf.decode(errors="replace").splitlines():
            g = L.gate(raw, project.log_drop)        # the log gate; nothing is stored
            if g is None:
                dropped += 1
            else:
                lines.append(g)
        st.event("diag", f"{name} by {who}: {why} in {took:.0f} s", rel["id"], project.name, t.name)
        head = (f"{project.name}/{t.name} diag {name} (release {rel['id']}, sha {rel['sha'][:12]}): "
                f"{why} in {took:.1f} s" + (f" · {note}" if note else "")
                + (f" · {dropped} lines dropped (log_drop)" if dropped else ""))
        extra = [f"‹output cut at {OUT_MAX // 1024} KiB›"] if cut[0] else []
        res = L.answer(head, lines, len(lines), max(1, len(lines)), False, extra)
        res["exit"] = rc
        return res
    finally:
        if snap is not None:
            subprocess.run(["rm", "-rf", "--one-file-system", "--", str(snap)],
                           capture_output=True, timeout=600, cwd="/")
        os.close(fd)
