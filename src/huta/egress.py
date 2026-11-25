"""Egress for huta containers (DESIGN-v1 §2).

The containers' networks have no route out (the host firewall forwards nothing from `dp-*`
bridges). The only way out is this daemon on the host:

- port 4880: an HTTP CONNECT proxy. A tunnel opens only if the target's allowlist matches the
  requested host:port; names are resolved here and must resolve to global addresses only (a
  literal IP entry is the one way to reach a non-global address); the daemon connects to the
  address it checked.
- ports 4881..4899: plain TCP forwards to the platform's named upstreams, for targets that list
  them.

The source address says which target is asking: every target has its own Docker network and
subnet. huta (root) writes the policy; this daemon runs unprivileged and only reads it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

POLICY_DIR = Path("/run/huta-egress")
POLICY = POLICY_DIR / "policy.json"
MAX_HEAD = 8192
PER_TARGET = 256
CONNECT_TIMEOUT = 30
IDLE_TIMEOUT = 600

log = logging.getLogger("huta-egress")


# ------------------------------------------------------------------ policy (written by root)

def build_policy(platform, registry, network_name) -> dict:
    """subnet → target, allowlist and upstreams, for every target that has a network."""
    targets = []
    for project in registry.values():
        for t in project.targets.values():
            if not (t.egress_allow or t.upstreams):
                continue
            subnets = []
            names = network_name(t)
            for net in (names if isinstance(names, list) else [names]):
                r = subprocess.run(["docker", "network", "inspect", net, "--format",
                                    "{{json .IPAM.Config}}"], capture_output=True, text=True,
                                   timeout=30, cwd="/")
                if r.returncode == 0:
                    subnets += [c["Subnet"] for c in json.loads(r.stdout or "[]") or []
                                if c.get("Subnet")]
            if not subnets:
                continue   # no network yet: nothing of this target runs
            targets.append({"target": f"{project.name}/{t.name}", "subnets": subnets,
                            "allow": t.egress_allow,
                            "upstreams": {u: platform.upstreams[u]["port"] for u in t.upstreams}})
    # The publisher's own network (DESIGN-v2 §5): only Cloudflare's API.
    from .external import EGRESS, NETWORK
    r = subprocess.run(["docker", "network", "inspect", NETWORK, "--format",
                        "{{json .IPAM.Config}}"], capture_output=True, text=True, timeout=30, cwd="/")
    if r.returncode == 0:
        subnets = [c["Subnet"] for c in json.loads(r.stdout or "[]") or [] if c.get("Subnet")]
        if subnets:
            targets.append({"target": "huta/external", "subnets": subnets, "allow": list(EGRESS),
                            "upstreams": {}})
    return {"v": 1, "ts": int(time.time()), "targets": targets,
            "upstreams": {n: u for n, u in platform.upstreams.items()},
            "deny": sorted(set(_host_networks()) | ({platform.public_ipv4} if platform.public_ipv4 else set()))}


def _host_networks() -> list[str]:
    """Addresses a name must never resolve to even when 'global': this host's own addresses and
    every Docker subnet (codex design review #3)."""
    out = set()
    r = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=10, cwd="/")
    for link in json.loads(r.stdout or "[]"):
        for a in link.get("addr_info", []):
            if a.get("local"):
                out.add(str(ipaddress.ip_address(a["local"])))
    ids = subprocess.run(["docker", "network", "ls", "-q"], capture_output=True, text=True,
                         timeout=30, cwd="/").stdout.split()
    if ids:
        r = subprocess.run(["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}",
                            *ids], capture_output=True, text=True, timeout=30, cwd="/")
        for line in r.stdout.splitlines():
            for c in json.loads(line or "null") or []:
                if c.get("Subnet"):
                    out.add(c["Subnet"])
    return sorted(out)


def write_policy(policy: dict) -> bool:
    """Write the policy if it changed. Root-owned, world-readable (no secrets in it)."""
    POLICY_DIR.mkdir(mode=0o755, exist_ok=True)
    os.chmod(POLICY_DIR, 0o755)
    body = dict(policy)
    body.pop("ts", None)
    try:
        old = json.loads(POLICY.read_text())
        old.pop("ts", None)
        if old == body:
            return False
    except (OSError, ValueError):
        pass
    tmp = POLICY_DIR / f".policy.{os.getpid()}"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, json.dumps(policy, indent=1).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, POLICY)
    return True


def sync(platform, registry) -> bool:
    """Build and publish under one host-wide lock: two releases syncing at once can't publish
    an older view over a newer one (codex code review #20)."""
    import fcntl
    from .recipe import network_names
    POLICY_DIR.mkdir(mode=0o755, exist_ok=True)
    fd = os.open(platform.state_dir / "egress.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        sync_intra(registry)
        return write_policy(build_policy(platform, registry, network_names))
    finally:
        os.close(fd)


# ------------------------------------------------------------------ bridges whose containers talk
# A slot with several components is one bridge on which those containers talk to each other
# (ICC on). The host's guard drops everything forwarded from dp-* bridges, which, with Docker's
# bridge netfilter, includes traffic between two containers on the same bridge. So the guard
# accepts exactly "this bridge to itself" for the bridges listed in a set huta keeps
# (systemd/nftables-huta-egress.nft.example; DESIGN-v2 §3). Never a bridge to another one.

INTRA_SET = ("inet", "huta_guard", "huta_intra")


def intra_ready() -> bool:
    """Whether the host's guard has the set (the operator loads the fragment once)."""
    r = subprocess.run(["nft", "list", "set", *INTRA_SET], capture_output=True, text=True,
                       timeout=30, cwd="/")
    return r.returncode == 0


def intra_bridges(registry) -> list[str]:
    """Bridges whose containers talk to each other and that exist now: slot networks of targets
    with several components, and each target's database networks (DESIGN-v2 §3, §4)."""
    from . import pg as PG
    from .recipe import slot_bridge_name, slot_network_name
    out = []
    for project in registry.values():
        for t in project.targets.values():
            if t.kind == "gateway":
                continue
            nets = [(slot_network_name(t, s), slot_bridge_name(t, s)) for s in ("a", "b")]
            if t.postgres:
                nets += [(PG.network(t, w), PG.bridge(t, w)) for w in ("a", "b", "m")]
            for net, br in nets:
                r = subprocess.run(["docker", "network", "inspect", net],
                                   capture_output=True, timeout=30, cwd="/")
                if r.returncode == 0:
                    out.append(br)
    return out


def sync_intra(registry) -> None:
    """Add every existing slot bridge to the set (elements are only added: a removed network's
    name matches nothing). No set: nothing to do; a release with several components refuses to
    plan until the operator adds it."""
    if not intra_ready():
        return
    for br in intra_bridges(registry):
        subprocess.run(["nft", "add", "element", *INTRA_SET, "{", f'"{br}" . "{br}"', "}"],
                       capture_output=True, timeout=30, cwd="/")


# ------------------------------------------------------------------ matching

def allowed(allow: list[str], host: str, port: int) -> str | None:
    """The entry that allows host:port, or None. host is lowercase without a trailing dot."""
    lit = _literal(host)
    for entry in allow:
        ehost, _, eport = entry.rpartition(":")
        if int(eport) != port:
            continue
        ehost = ehost.strip("[]")
        if lit is not None:
            if _literal(ehost) == lit:
                return entry
            continue
        if ehost == host or ehost == "*" or (ehost.startswith("*.") and host.endswith(ehost[1:])):
            return entry
    return None


def _literal(host: str):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


_V6_NEVER = [ipaddress.ip_network(n) for n in (
    "64:ff9b::/96", "64:ff9b:1::/48",   # NAT64: an IPv4 address in disguise
    "fec0::/10",                        # site-local (deprecated, still routable on some hosts)
    "::/96",                            # IPv4-compatible
    "2001::/32",                        # Teredo
)]


def is_public(ip) -> bool:
    """Global, and not an IPv6 form that carries or reaches a non-public IPv4 (grok #5)."""
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.version == 6:
        if any(ip in n for n in _V6_NEVER):
            return False
        if ip.sixtofour is not None and not is_public(ip.sixtofour):
            return False
    return ip.is_global and not ip.is_multicast


# ------------------------------------------------------------------ daemon

class Policy:
    def __init__(self, path: Path = POLICY):
        self.path = path
        self.mtime = None
        self.targets: list[tuple[list, dict]] = []
        self.upstreams: dict = {}
        self.deny: list = []

    def get(self):
        try:
            m = self.path.stat().st_mtime_ns
        except OSError:
            self.targets, self.upstreams = [], {}
            return self
        if m != self.mtime:
            try:
                d = json.loads(self.path.read_text())
                self.targets = [([ipaddress.ip_network(s) for s in t["subnets"]], t)
                                for t in d.get("targets", [])]
                self.upstreams = d.get("upstreams", {})
                self.deny = [ipaddress.ip_network(n, strict=False) for n in d.get("deny", [])]
                self.mtime = m
                log.info("policy loaded: %d targets", len(self.targets))
            except (OSError, ValueError, KeyError) as e:
                log.error("policy unreadable, refusing everything: %s", e)
                self.targets, self.upstreams = [], {}
        return self

    def who(self, addr: str) -> dict | None:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return None
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        for nets, t in self.targets:
            if any(ip in n for n in nets):
                return t
        return None


OPEN: dict[str, int] = {}
TOTAL = 4096                 # all connections the daemon holds at once, every target together
_all = [0]


class _Slot:
    """One counted connection: taken before the first await, released in finally (codex code
    review #8). Sources that aren't a target count against the total only."""

    def __init__(self, name: str | None):
        self.name, self.ok = name, False

    def __enter__(self):
        if _all[0] >= TOTAL or (self.name and OPEN.get(self.name, 0) >= PER_TARGET):
            return self
        _all[0] += 1
        if self.name:
            OPEN[self.name] = OPEN.get(self.name, 0) + 1
        self.ok = True
        return self

    def __exit__(self, *exc):
        if self.ok:
            _all[0] -= 1
            if self.name:
                OPEN[self.name] -= 1


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter, counter: list,
                peer_w: asyncio.StreamWriter) -> None:
    """Copy one direction. A normal EOF is passed on as a half-close (the other direction keeps
    going); an error or an idle/backpressure timeout closes both sides (code review #9, #10)."""
    try:
        while True:
            chunk = await asyncio.wait_for(r.read(65536), IDLE_TIMEOUT)
            if not chunk:
                if w.can_write_eof():
                    w.write_eof()
                else:
                    w.close()
                return
            counter[0] += len(chunk)
            w.write(chunk)
            await asyncio.wait_for(w.drain(), IDLE_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError, OSError, RuntimeError):
        for x in (w, peer_w):
            try:
                x.close()
            except Exception:  # noqa: BLE001
                pass


async def _splice(cr, cw, ur, uw) -> tuple[int, int]:
    up, down = [0], [0]
    try:
        await asyncio.gather(_pipe(cr, uw, up, cw), _pipe(ur, cw, down, uw))
    finally:
        for x in (cw, uw):
            try:
                x.close()
            except Exception:  # noqa: BLE001
                pass
    return up[0], down[0]


def _parse_connect(head: bytes) -> tuple[str, int] | None:
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    hp = parts[1]
    if hp.startswith("["):
        host, _, rest = hp[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = hp.rpartition(":")
    host = host.lower().rstrip(".")
    if not port.isdigit() or not 1 <= int(port) <= 65535 or not host or len(host) > 253:
        return None
    if not all(c.isalnum() or c in ".-:" for c in host):
        return None
    # Numbers that only look like a name ("2130706433", "0177.0.0.1", "127.1", "0x7f.1") are
    # read by the resolver as addresses: refuse any all-numeric form that isn't a plain IP.
    if _literal(host) is None and re.fullmatch(r"[0-9.]+|(0x[0-9a-f]+\.?)+[0-9a-fx.]*", host):
        return None
    return host, int(port)


async def _resolve(host: str, port: int, entry: str, deny: list) -> str:
    lit = _literal(host)
    if lit is not None:
        return str(lit)           # only reachable through a literal-IP entry (allowed() checked)
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), 10)
    addrs = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not addrs:
        raise OSError("no address")
    norm = [a.ipv4_mapped if getattr(a, "ipv4_mapped", None) else a for a in addrs]
    bad = [a for a in norm if not is_public(a) or any(a in n for n in deny)]
    if bad:
        # One non-public answer is enough to refuse: the name is pointed somewhere private.
        raise PermissionError(f"resolves to a non-public address ({bad[0]})")
    v4 = [a for a in addrs if a.version == 4]
    return str((v4 or addrs)[0])


async def handle_connect(policy: Policy, cr: asyncio.StreamReader, cw: asyncio.StreamWriter):
    peer = (cw.get_extra_info("peername") or ("?",))[0]
    t = policy.get().who(peer)
    name = t["target"] if t else None
    with _Slot(name) as slot:
        if not slot.ok:
            log.warning("refused %s (%s): too many open connections", name or peer, peer)
            cw.write(b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\n\r\n")
            return await _close(cw)
        await _connect(policy, t, name, peer, cr, cw)


async def _connect(policy, t, name, peer, cr, cw):
    def refuse(code: str, why: str):
        log.warning("refused %s (%s): %s", name or peer, peer, why)
        cw.write(f"HTTP/1.1 {code}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())

    def masked(host: str) -> str:
        """A refused name is the app's choice and could carry a secret (<secret>.evil.example):
        log its last two labels and a short hash, never the whole name (grok #7)."""
        import hashlib
        parts = host.split(".")
        tail = ".".join(parts[-2:]) if len(parts) > 2 else host
        if len(tail) > 40 or (_literal(host) is None and len(parts) > 2):
            return f"…{tail[-40:]} #{hashlib.sha256(host.encode()).hexdigest()[:8]}"
        return host

    try:
        head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 10)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            ConnectionError):
        cw.close()
        return
    if t is None:
        refuse("403 Forbidden", "source is not a huta target")
        return await _close(cw)
    hp = _parse_connect(head)
    if hp is None:
        refuse("405 Method Not Allowed", "only CONNECT host:port is supported")
        return await _close(cw)
    host, port = hp
    entry = allowed(t["allow"], host, port)
    if entry is None:
        refuse("403 Forbidden", f"{masked(host)}:{port} is not on the allowlist")
        return await _close(cw)
    # A wildcard entry lets the app pick any name, so that name is masked in logs too.
    shown = masked(host) if entry.rpartition(":")[0].strip("[]").startswith("*") else host
    try:
        addr = await _resolve(host, port, entry, policy.deny)
        ur, uw = await asyncio.wait_for(asyncio.open_connection(addr, port), CONNECT_TIMEOUT)
    except PermissionError as e:
        refuse("403 Forbidden", f"{shown}:{port} {e}")
        return await _close(cw)
    except (OSError, asyncio.TimeoutError) as e:
        refuse("502 Bad Gateway", f"{shown}:{port} unreachable: {type(e).__name__}")
        return await _close(cw)
    cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    t0 = time.monotonic()
    up, down = await _splice(cr, cw, ur, uw)
    log.info("tunnel %s → %s:%d (%s) up %d down %d bytes %.1f s", name, shown, port, addr, up,
             down, time.monotonic() - t0)


async def handle_upstream(policy: Policy, port: int, cr, cw):
    peer = (cw.get_extra_info("peername") or ("?",))[0]
    t = policy.get().who(peer)
    name = t["target"] if t else None
    uname = next((n for n, u in policy.upstreams.items() if u["port"] == port), None)
    to = policy.upstreams[uname]["to"] if uname else ""
    with _Slot(name) as slot:
        if t is None or uname is None or t.get("upstreams", {}).get(uname) != port:
            log.warning("refused %s (%s): upstream on port %d not allowed", name or peer, peer, port)
            return await _close(cw)
        if not slot.ok:
            log.warning("refused %s: too many open connections", name)
            return await _close(cw)
        host, _, uport = to.rpartition(":")
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(uport)),
                                            CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("%s → upstream %s unreachable: %s", name, uname, type(e).__name__)
            return await _close(cw)
        t0 = time.monotonic()
        up, down = await _splice(cr, cw, ur, uw)
        log.info("upstream %s → %s up %d down %d bytes %.1f s", name, uname, up, down,
                 time.monotonic() - t0)


async def _close(w):
    try:
        await asyncio.wait_for(w.drain(), 5)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        pass
    w.close()


async def _main(port: int) -> None:
    policy = Policy()
    servers = [await asyncio.start_server(lambda r, w: handle_connect(policy, r, w),
                                          "0.0.0.0", port, limit=MAX_HEAD)]
    started: dict[int, asyncio.base_events.Server] = {}
    while True:
        want = {int(u["port"]) for u in policy.get().upstreams.values()}
        for gone in [p for p in started if p not in want]:
            started.pop(gone).close()
            log.info("upstream port %d closed (no longer in the policy)", gone)
        # A listener per upstream port the policy names; where it forwards is read from the
        # policy on every connection, so a changed `to` applies without a restart.
        for uname, u in policy.get().upstreams.items():
            uport = int(u["port"])
            if uport in started:
                continue
            try:
                started[uport] = await asyncio.start_server(
                    lambda r, w, p=uport: handle_upstream(policy, p, r, w), "0.0.0.0", uport)
                log.info("upstream %s listening on %d", uname, uport)
            except OSError as e:
                log.error("upstream %s: cannot listen on %d: %s", uname, uport, e)
        await asyncio.sleep(10)


def serve(argv=None) -> int:
    """Entry point of huta-egress.service (unprivileged)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if os.geteuid() == 0:
        print("huta-egress must not run as root (the unit uses DynamicUser)", file=sys.stderr)
        return 2
    asyncio.run(_main(4880))
    return 0
