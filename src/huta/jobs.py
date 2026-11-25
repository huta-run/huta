"""Scheduled work (DESIGN-v1 §4) and the per-target backup timer.

Units are generated in /etc/systemd/system with a marker line; huta only ever touches files
with that marker and its own name prefix:

  huta-job_<p>_<t>_<job>.timer / .service   → huta job run <p> <t> <job>
  huta-backup_<p>_<t>.timer / .service      → huta backup <p> <t>

Project, target and job names can't contain "_", so the names are unambiguous.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import runtime as R
from .cfg import ConfigError, Platform, Project, Target

UNIT_DIR = Path("/etc/systemd/system")
MARK = "# managed-by: huta"
HUTA = "/usr/local/sbin/huta"


def _run(cmd, check=True, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd="/")
    if check and r.returncode != 0:
        raise ConfigError(f"{' '.join(cmd[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def job_unit(t: Target, name: str) -> str:
    return f"huta-job_{t.project}_{t.name}_{name}"


def backup_unit(t: Target) -> str:
    return f"huta-backup_{t.project}_{t.name}"


def _service(desc: str, argv: list[str], timeout_s: int, container: str = "") -> str:
    lines = [f"{MARK}. Generated; do not edit.", "[Unit]", f"Description={desc}",
             "After=docker.service", "Requires=docker.service",
             "[Service]", "Type=oneshot", f"ExecStart={' '.join(argv)}",
             f"TimeoutStartSec={timeout_s + 900}"]
    if container:
        # If the unit is stopped or times out, its container goes too: killing the docker
        # client alone would leave the job running (design reviews: codex #13, grok #7).
        lines.append(f"ExecStopPost=-/usr/bin/docker rm -f {container}")
    return "\n".join(lines + [""])


def _timer(desc: str, calendar: str) -> str:
    return "\n".join([
        f"{MARK}. Generated; do not edit.", "[Unit]", f"Description={desc}",
        "[Timer]", f"OnCalendar={calendar}", "RandomizedDelaySec=60", "Persistent=false",
        "[Install]", "WantedBy=timers.target", ""])


def _write(path: Path, text: str) -> bool:
    if path.exists():
        if not path.read_text().startswith(MARK):
            raise ConfigError(f"{path} exists and is not managed by huta")
        if path.read_text() == text:
            return False
    tmp = path.with_name(f".{path.name}.huta-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return True


def wanted_units(t: Target, jobs: list[dict]) -> dict[str, str]:
    """unit file name → text, for this target's jobs and its backup timer."""
    out = {}
    for j in jobs:
        u = job_unit(t, j["name"])
        desc = f"huta job {t.project}/{t.name} {j['name']}"
        out[f"{u}.service"] = _service(desc, [HUTA, "job", "run", t.project, t.name, j["name"]],
                                       j["timeout"], container=u)
        out[f"{u}.timer"] = _timer(desc, j["schedule"])
    sched = next((x["backup"] for x in (t.data, t.postgres) if x and x["backup"] != "off"), None)
    if sched:
        cal = {"hourly": "hourly", "daily": "*-*-* 03:30:00"}.get(sched, sched)
        desc = f"huta backup {t.project}/{t.name}"
        # The copy may take 6 h and the ship 4 h: the unit must outlast both (grok #8).
        out[f"{backup_unit(t)}.service"] = _service(desc, [HUTA, "backup", t.project, t.name],
                                                    10 * 3600 + 600)
        out[f"{backup_unit(t)}.timer"] = _timer(desc, cal)
    return out


def sync_units(t: Target, jobs: list[dict]) -> list[str]:
    """Make the unit files match: write new/changed, remove stale (ours only)."""
    want = wanted_units(t, jobs)
    said, changed = [], False
    prefixes = (f"huta-job_{t.project}_{t.name}_", f"huta-backup_{t.project}_{t.name}.")
    stale = [p for p in UNIT_DIR.iterdir() if p.name.startswith(prefixes)
             and p.name not in want and p.is_file() and not p.is_symlink()
             and p.read_text().startswith(MARK)]
    for p in stale:
        if p.name.endswith(".timer"):
            _run(["systemctl", "disable", "--now", p.name], check=False)
            said.append(f"removed {p.stem}")
    for p in stale:
        p.unlink()
        changed = True
    for name, text in want.items():
        changed |= _write(UNIT_DIR / name, text)
    if changed:
        _run(["systemctl", "daemon-reload"])
    for name in want:
        if name.endswith(".timer"):
            r = _run(["systemctl", "is-enabled", name], check=False)
            if r.stdout.strip() != "enabled" or changed:
                _run(["systemctl", "enable", "--now", name])
                _run(["systemctl", "restart", name], check=False)
    if want and changed:
        said.append(f"timers: {', '.join(sorted(n[:-6] for n in want if n.endswith('.timer')))}")
    return said


def run_job(platform: Platform, project: Project, t: Target, name: str, st, log) -> int:
    """Run one job in the live release's image. On a target whose data allows one writer at a
    time, the job holds the target lock while it runs (a release waits up to 5 min for it, and
    a job waits up to 10 min for a release); otherwise only while it starts."""
    from .recipe import check_image, network_name, sh
    fd = st.acquire(project.name, t.name, wait=600)
    try:
        from .recipe import import_marker, reconcile
        if import_marker(platform, t).exists():
            raise ConfigError("a data import was interrupted: no job runs until it completes")
        # An interrupted release is finished first, so a job never runs next to its writer.
        reconcile(platform, st, project, t, log)
        live = st.live(project.name, t.name)
        if not live:
            raise ConfigError(f"{project.name}/{t.name}: nothing is live")
        rel = st.release(live["release_id"])
        app = json.loads(rel["app_toml"])
        job = next((j for j in app.get("jobs", []) if j["name"] == name), None)
        if job is None:
            raise ConfigError(f"the live release has no job {name!r}")
        from . import spec as SP
        from .recipe import slot_network_name
        comp_name = job.get("component") or SP.public_name(app)
        comp = app["components"][comp_name]
        image = st.images(rel).get(comp["image"], rel["image_id"])
        check_image(image, rel["sha"], project.name)
        # With several components a job runs on the live slot's network, so it reaches the
        # other components by name (DESIGN-v2 §3); one component: the target's network.
        network = slot_network_name(t, live["slot"]) if SP.several(app) else network_name(t)
        if comp.get("data"):
            from .data import mounted
            if not mounted(R.data_paths(platform, t)[1]):
                raise ConfigError("the data volume is not mounted")
        env = R.environment(platform, project, t, rel["sha"], "job", with_secrets=True, app=app)
        env["HUTA_JOB"] = name
        for k in t.job_secrets.get(name, []):      # this job's own secrets, never the slots'
            env[k] = R.secret_value(platform, project.name, k)
        cname = job_unit(t, name)
        labels = {"run.huta.project": project.name, "run.huta.target": t.name,
                  "run.huta.job": name, "run.huta.sha": rel["sha"]}
        # A container left by a crash: ours by name and labels, so it goes.
        for cid in sh(["docker", "ps", "-aq", "--filter", f"name=^{cname}$",
                       "--filter", f"label=run.huta.job={name}"], check=False).split():
            sh(["docker", "rm", "-f", cid], check=False)
        cmd = list(job["cmd"])
        entry = []
        if t.job_secrets.get(name):
            # A secret-bearing job runs exactly its app.toml argv: the image's ENTRYPOINT can't
            # wrap it unseen (job secrets review, grok #1).
            entry = ["--entrypoint", cmd[0]]
            cmd = cmd[1:]
        # With the target's Postgres a job also joins the live slot's database network (as the
        # slot does); docker run takes a second --network since Docker 25.
        from . import pg as PG
        pg_net = (["--network", PG.network(t, live["slot"])]
                  if t.postgres and PG.wants(app) else [])
        args = ["docker", "run", "--rm", *R.docker_run_args(platform, project, t, app,
                                                            network, env, cname, labels,
                                                            comp_name if SP.several(app) else None),
                *pg_net, *entry, image, *cmd]
        # Values reach docker through its environment (--env NAME), never the command line.
        proc = subprocess.Popen(args, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", **env}, cwd="/",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # The job's output reaches the journal only through the redactor (code review #21).
        from .cfg import redact
        from .logs import JobLog
        jlog = JobLog(platform, project, t, name, rel["id"], rel["sha"])

        def _relay(out=proc.stdout):
            for raw in iter(lambda: out.readline(65536), b""):
                text = raw.decode(errors="replace").rstrip("\n")
                print(redact(text), flush=True)
                jlog.line(text)          # its own gate: log_drop, redact, cut (huta logs --job)
            jlog.close()

        relay = threading.Thread(target=_relay, daemon=True)
        relay.start()
        mem = MemWatch(cname)
        mem.start()
        if R.concurrent_safe(app, t):
            # Releases may proceed while the job runs, but only once its container exists: a
            # one-writer release checks for running jobs and must see this one (grok #9).
            # No timeout that lets go anyway: the lock goes only once the container is seen
            # running or the client has exited (final reviews: codex #4, grok #4).
            deadline = time.monotonic() + 600
            while True:
                if sh(["docker", "ps", "-q", "--filter", f"name=^{cname}$"], check=False).strip() \
                        or proc.poll() is not None:
                    break
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait(timeout=60)
                    sh(["docker", "rm", "-f", cname], check=False)
                    raise ConfigError(f"job {name}: its container never started")
                time.sleep(0.5)
            os.close(fd)
            fd = -1
        t0 = time.monotonic()
        st.event("job-start", f"{name} ({rel['sha'][:12]})", rel["id"], project.name, t.name)
        try:
            rc = proc.wait(timeout=job["timeout"])
            why = f"exit {rc}"
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", cname], capture_output=True, timeout=60, cwd="/")
            proc.wait(timeout=120)
            rc, why = 124, f"killed after the {job['timeout']} s timeout"
        relay.join(10)
        mem.stop.set()
        mem.join(5)
        took = time.monotonic() - t0
        msg = f"{name}: {why} in {took:.0f} s{mem.summary()}"
        st.event("job-ok" if rc == 0 else "job-fail", msg, rel["id"], project.name, t.name)
        log(f"job {msg}")
        return 0 if rc == 0 else 1
    finally:
        if fd >= 0:
            os.close(fd)


CGROUP_ROOT = Path("/sys/fs/cgroup")


class MemWatch(threading.Thread):
    """Samples a job container's cgroup (v2) while it runs: the highest memory.peak seen, its
    memory.max and whether the kernel OOM-killed in it. memory.peak only grows, so the last
    sample is the peak up to that moment; a spike in the final second can be missed, and a
    job shorter than the first sample reports nothing. Best effort: never fails the job."""

    def __init__(self, cname: str):
        super().__init__(daemon=True)
        self.cname, self.stop = cname, threading.Event()
        self.peak = self.limit = 0
        self.oom = False
        self.dir: Path | None = None

    def _find(self) -> Path | None:
        r = subprocess.run(["docker", "inspect", "-f", "{{.Id}} {{.State.Pid}}", self.cname],
                           capture_output=True, text=True, timeout=10, cwd="/")
        cid, _, pid = r.stdout.strip().partition(" ")
        if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", cid) \
                or not re.fullmatch(r"[1-9][0-9]{0,9}", pid):
            return None
        with open(f"/proc/{pid}/cgroup") as f:
            line = next((x for x in f.read().splitlines() if x.startswith("0::")), "")
        rel = line[3:]
        if not re.fullmatch(r"(/[A-Za-z0-9_.@:-]+)+", rel) or "/.." in rel:
            return None
        # The pid may have exited and been reused by now: only a cgroup named after this
        # container counts (systemd driver: docker-<id>.scope; cgroupfs: .../<id>) (codex #4).
        if rel.rsplit("/", 1)[-1] not in (f"docker-{cid}.scope", cid):
            return None
        d = CGROUP_ROOT / rel.lstrip("/")
        return d if (d / "memory.peak").is_file() else None

    def _sample(self) -> bool:
        try:
            self.peak = max(self.peak, int((self.dir / "memory.peak").read_text()))
            lim = (self.dir / "memory.max").read_text().strip()
            self.limit = int(lim) if lim.isdigit() else 0
            ev = dict(x.split() for x in (self.dir / "memory.events").read_text().splitlines()
                      if len(x.split()) == 2)
            self.oom |= int(ev.get("oom_kill", "0")) > 0
            return True
        except (OSError, ValueError):
            return False                  # the container (and its cgroup) is gone

    def run(self):
        t0 = time.monotonic()
        try:
            while not self.stop.is_set():
                if self.dir is None:
                    self.dir = self._find()
                if self.dir is not None and not self._sample():
                    return
                self.stop.wait(0.5 if time.monotonic() - t0 < 10 else 2)
        except Exception:  # noqa: BLE001 — a measurement, never the job's outcome
            return

    def summary(self) -> str:
        if not self.peak:
            return ""
        s = f" · peak {self.peak / 2**20:.0f} MiB"
        if self.limit:
            s += f" of {self.limit / 2**20:.0f} MiB"
        return s + (" · OUT OF MEMORY (killed by the kernel)" if self.oom else "")


def start_after_release(t: Target, jobs: list[dict], st, rid: str, log) -> None:
    """Start the jobs marked after_release once a release is live (normal release or rollback).
    --no-block: the unit's `huta job run` waits for the target lock this release still holds,
    so the job starts when the release has finished, and its outcome is the job's own (a
    failure fails the unit like a timer run), never the release's. A crash between going live
    and this call skips the run; the timer still runs it at its next time."""
    for j in jobs:
        if not j.get("after_release"):
            continue
        u = f"{job_unit(t, j['name'])}.service"
        try:
            r = _run(["systemctl", "start", "--no-block", u], check=False, timeout=60)
        except Exception as e:  # noqa: BLE001 — the release is live; never its outcome (codex #1)
            r = subprocess.CompletedProcess([], 1, "", f"{type(e).__name__}: {e}")
        if r.returncode == 0:
            st.event("job-queued", f"{j['name']}: after the release", rid, t.project, t.name)
            log(f"job {j['name']}: started after the release ({u})")
        else:
            st.event("job-fail", f"{j['name']}: not started after the release: "
                     f"{(r.stderr or r.stdout).strip()[-200:]}", rid, t.project, t.name)
            log(f"WARNING job {j['name']} not started after the release; "
                f"huta job run {t.project} {t.name} {j['name']} runs it")


def jobs_text(st, t: Target, n: int = 20) -> str:
    """The live release's jobs (schedule, next run) and the last n run outcomes. No output."""
    live = st.live(t.project, t.name)
    app = json.loads(st.release(live["release_id"])["app_toml"]) if live else {}
    jobs = app.get("jobs", [])
    if not jobs:
        return f"{t.project}/{t.name}: the live release has no jobs"
    nxt = next_runs([f"{job_unit(t, j['name'])}.timer" for j in jobs])
    lines = [f"{j['name']:<24} {j['schedule']:<24} timeout {j['timeout']} s"
             + (" · after each release" if j.get("after_release") else "")
             + f" · next {nxt.get(job_unit(t, j['name']) + '.timer', '?')}" for j in jobs]
    rows = st.target_events(t.project, t.name, ("job-ok", "job-fail", "job-queued"), limit=n)
    if rows:
        lines.append("last runs:")
    for e in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(e["ts"]))
        lines.append(f"  {when}  {e['kind']:<10} {e['msg']}")
    return "\n".join(lines)


def next_runs(units: list[str]) -> dict[str, str]:
    out = {}
    if not units:
        return out
    r = _run(["systemctl", "list-timers", "--all", "--no-legend", "--plain", *units], check=False)
    for line in r.stdout.splitlines():
        parts = line.split()
        for u in units:
            if u in parts:
                out[u] = " ".join(parts[:4]) if parts[0] != "-" else "-"
    return out
