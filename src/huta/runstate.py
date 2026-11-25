"""What a target's containers are doing now, for `huta status <project> <target>` (host and box).

Read-only and bounded: `docker inspect`, the containers' cgroup v2 files, one health request to
the live slot on loopback (the image is untrusted: recipe._get_json, no proxies, no redirects,
capped body, 3 s), statvfs of the data mount, the last backup record, and the open TCP
connections from the target's bridge to the egress daemon's ports (`ss`). Nothing in the answer
comes from the app except its health answer's ok and sha.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .cfg import Platform, Project, Target
from . import spec as SP

CGROUP_ROOT = Path("/sys/fs/cgroup")


def _mib(n: int | None) -> str:
    return "?" if n is None else f"{n / 2**20:.0f} MiB"


def _read(d: Path, name: str) -> str | None:
    try:
        return (d / name).read_text().strip()
    except OSError:
        return None


def _int(v: str | None) -> int | None:
    return int(v) if v and v.isdigit() else None


def _kv(v: str | None) -> dict:
    out = {}
    for line in (v or "").splitlines():
        p = line.split()
        if len(p) == 2 and p[1].isdigit():
            out[p[0]] = int(p[1])
    return out


def cgroup_dir(cid: str, pid) -> Path | None:
    """The container's own cgroup, checked by name so a reused pid can't point elsewhere."""
    if not re.fullmatch(r"[0-9a-f]{64}", cid or "") or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            line = next((x for x in f.read().splitlines() if x.startswith("0::")), "")
    except OSError:
        return None
    rel = line[3:]
    if not re.fullmatch(r"(/[A-Za-z0-9_.@:-]+)+", rel) or "/.." in rel:
        return None
    if rel.rsplit("/", 1)[-1] not in (f"docker-{cid}.scope", cid):
        return None
    d = CGROUP_ROOT / rel.lstrip("/")
    return d if d.is_dir() else None


def usage(d: Path, sample: float = 1.0) -> list[str]:
    """Memory, pids and cpu of one cgroup against its limits, and the kernel's event counters."""
    mem, peak = _int(_read(d, "memory.current")), _int(_read(d, "memory.peak"))
    mmax = _read(d, "memory.max")
    mev = _kv(_read(d, "memory.events"))
    pids, ppeak = _int(_read(d, "pids.current")), _int(_read(d, "pids.peak"))
    pmax = _read(d, "pids.max")
    pev = _kv(_read(d, "pids.events"))
    u0 = _kv(_read(d, "cpu.stat")).get("usage_usec")
    t0 = time.monotonic()
    time.sleep(sample)
    u1 = _kv(_read(d, "cpu.stat")).get("usage_usec")
    cpu = ((u1 - u0) / 1e6 / (time.monotonic() - t0)) if u0 is not None and u1 is not None else None
    cmax = (_read(d, "cpu.max") or "max").split()
    climit = (f"{int(cmax[0]) / int(cmax[1]):g}" if len(cmax) == 2 and cmax[0].isdigit()
              and cmax[1].isdigit() else "no limit")
    lim = (f"{int(mmax) / 2**20:.0f} MiB" if mmax and mmax.isdigit() else "no limit")
    return [
        f"memory {_mib(mem)} now, peak {_mib(peak) if peak is not None else 'n/a'}, limit {lim}"
        f" · memory.events oom {mev.get('oom', 0)} oom_kill {mev.get('oom_kill', 0)}"
        f" max {mev.get('max', 0)}",
        f"pids {pids if pids is not None else '?'} now, peak "
        f"{ppeak if ppeak is not None else 'n/a (kernel has no pids.peak)'}, limit {pmax or '?'}"
        f" · pids.events max {pev.get('max', 0)} (forks refused at the limit)",
        f"cpu {cpu:.2f} cores (1 s sample) of {climit}" if cpu is not None else "cpu ?",
    ]


def _since(iso: str) -> str:
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", iso or "")
    if not m or iso.startswith("0001"):
        return "-"
    ts = datetime.fromisoformat(m.group(1) + "+00:00").timestamp()
    return f"{m.group(1)}Z ({_ago(time.time() - ts)} ago)"


def _ago(s: float) -> str:
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min" if s < 7200 else \
        f"{s / 3600:.1f} h" if s < 172800 else f"{s / 86400:.1f} d"


def egress_open(t: Target) -> int | None:
    """TCP connections from the target's bridge to the egress daemon's ports (4880..4899)."""
    from .recipe import network_name
    r = subprocess.run(["docker", "network", "inspect", network_name(t), "--format",
                        "{{json .IPAM.Config}}"], capture_output=True, text=True, timeout=30,
                       cwd="/")
    if r.returncode != 0:
        return None
    try:
        nets = [ipaddress.ip_network(c["Subnet"]) for c in json.loads(r.stdout or "[]") or []
                if c.get("Subnet")]
    except (ValueError, KeyError, TypeError):
        return None
    r = subprocess.run(["ss", "-Htn", "state", "established"], capture_output=True, text=True,
                       timeout=30, cwd="/")
    n = 0
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        local, peer = p[-2], p[-1]
        try:
            lport = int(local.rsplit(":", 1)[1])
            pip = ipaddress.ip_address(peer.rsplit(":", 1)[0].strip("[]"))
        except (ValueError, IndexError):
            continue
        if 4880 <= lport <= 4899 and any(pip in net for net in nets):
            n += 1
    return n


def runtime_text(platform: Platform, st, project: Project, t: Target) -> str:
    from . import data as D
    from . import egress as E
    from . import recipe
    from . import runtime as R
    from .logs import _docker_inspect, _release_of
    live = st.live(project.name, t.name)
    lines = ["now:"]
    for c in sorted(_docker_inspect(t), key=lambda c: (c.get("Config") or {}).get("Labels", {})
                    .get("run.huta.slot", "")):
        labels = (c.get("Config") or {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != recipe.compose_name(t):
            continue
        slot = labels.get("run.huta.slot")
        if slot not in recipe.SLOTS:
            continue
        s = c.get("State") or {}
        sha = labels.get("run.huta.sha", "")
        rid = _release_of(st, t, sha, c.get("Created", "")) or "-"
        role = "live" if live and live["slot"] == slot else "idle"
        lines.append(f"slot {slot} ({role}): release {rid} sha {sha[:12]} · {s.get('Status', '?')}"
                     f" · started {_since(s.get('StartedAt', ''))}"
                     f" · restarts {c.get('RestartCount', 0)}"
                     + (f" · last exit {s.get('ExitCode')} at {_since(s.get('FinishedAt', ''))}"
                        if s.get("FinishedAt", "").startswith(("1", "2")) else "")
                     + (" · OOM-KILLED" if s.get("OOMKilled") else ""))
        if s.get("Running"):
            d = cgroup_dir(c.get("Id", ""), s.get("Pid"))
            lines += [f"  {x}" for x in (usage(d) if d else ["cgroup not found"])]
        if role == "live" and s.get("Running"):
            rel = st.release(live["release_id"])
            app = json.loads(rel["app_toml"])
            health = SP.health(app)
            port = t.slots[recipe.SLOTS.index(slot)]
            t0 = time.monotonic()
            try:
                body = recipe._get_json(f"http://127.0.0.1:{port}{health}", 3)
                ok = body.get("ok") is True and body.get("sha") == rel["sha"]
                res = "ok" if ok else (f"not ok (ok={body.get('ok')!r}, "
                                       f"sha {str(body.get('sha'))[:12]})")
            except Exception as e:  # noqa: BLE001 — the answer is the report
                res = f"failed: {type(e).__name__}"
            lines.append(f"  health {health} now: {res} in {(time.monotonic() - t0) * 1000:.0f} ms")
    if len(lines) == 1:
        lines.append("no slot containers")
    if t.data:
        where = R.data_paths(platform, t)[1]
        if D.mounted(where):
            v = os.statvfs(where)
            size, free = v.f_blocks * v.f_frsize, v.f_bavail * v.f_frsize
            lines.append(f"data: {_mib(size - free)} used of {_mib(size)}"
                         f" ({(size - free) * 100 / size:.0f} %)" if size else "data: empty volume")
        else:
            lines.append("data: not mounted")
        lines.append(f"backup: {D.last_backup_text(platform, t)}")
    if t.egress_allow or t.upstreams:
        n = egress_open(t)
        lines.append(f"egress: {'?' if n is None else n} open connections (cap {E.PER_TARGET})")
    return "\n".join(lines)
