"""Host configuration: /etc/huta/huta.toml and /etc/huta/projects.d/<p>.toml.

The host decides users, ports, hosts, secrets, limits and policy. Nothing in a repo can widen
what is written here (DESIGN.md §1, P-002).
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ETC = Path("/etc/huta")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
BOX_RE = re.compile(r"^[a-z][a-z0-9-]{0,26}$")   # push boxes: huta-<box> fits a 32-char user name
# What a project's `boxes` may name: a push box, or a pull container (up to 63; codex boxes #2).
GRANT_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class ConfigError(Exception):
    pass


def assert_root_dir(path: Path) -> None:
    """Root acts on files under these paths, so the path and every ancestor must be owned by
    root and not writable by group or others (codex review: symlink/substitution risk)."""
    p = Path(path)
    for d in [p, *p.parents]:
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise ConfigError(f"{d} is a symlink; refusing")
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise ConfigError(f"{d} must be root-owned and not group/world-writable")


def read_secret(p: Path, remember: bool = True) -> str:
    """A secret must be a root-owned regular file nobody else can read, in root-only
    directories (grok #11)."""
    for d in (p.parent, p.parent.parent):
        st = os.lstat(d)
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
            raise ConfigError(f"{d} must be a root-owned directory with mode 0700")
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
            raise ConfigError(f"{p} must be a root-owned file with mode 0600")
        value = os.read(fd, 65536).decode().strip()
    finally:
        os.close(fd)
    if remember:
        remember_secret(value)
    return value


_SECRETS: list[str] | None = None
REDACTED = "‹redacted›"
MIN_NEEDLE = 8          # shorter strings are too common to blank out
RETIRED = ".retired"    # secrets/.retired/: values replaced or removed, still redacted a while
RETIRED_DAYS = 30


USERINFO_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://([^/?#@\s,;&|\"']+)@")
MIN_CONTEXT = 3         # ":pw@" and "user:pw@" carry their own context, so they may be short
MIN_PAIR = 6            # a bare "user:pw" (and its base64, as in Basic auth)


def needles(value: str) -> set[str]:
    """Every form of a secret value that gives it away in text: the whole value, its parts
    (list entries, `k=v` values such as cookie pairs, every URL userinfo `user:pass@` in it,
    wherever it sits, and its password) and each of those URL-encoded, JSON-escaped and
    base64-encoded (at all three alignments, so the value is found inside a longer encoded
    blob, like `Basic <user:pass>`). Parts shorter than MIN_NEEDLE are not redacted on their
    own; `:pass@`, `user:pass@` and base64 of `user:pass` are, from MIN_CONTEXT characters, and a
    bare `user:pass` from MIN_PAIR."""
    import base64
    import json
    import urllib.parse
    parts, short = {value}, set()
    for tok in re.split(r"[\s,;&|]+", value):
        tok = tok.strip("\"'")
        parts.add(tok)
        if "=" in tok:
            parts.add(tok.split("=", 1)[1])
    # Userinfo anywhere, not only at the start of an entry: "ro=socks5://u:p@host,ua=…".
    for m in USERINFO_RE.finditer(value):
        for ui in {m.group(1), urllib.parse.unquote(m.group(1))}:
            parts.add(ui)
            short |= {ui + "@", urllib.parse.quote(ui, safe=":") + "@"}
            if ":" in ui:
                pw = ui.split(":", 1)[1]
                parts.add(pw)
                short |= {":" + pw + "@", ":" + urllib.parse.quote(pw, safe="") + "@"}
                if len(ui) >= MIN_PAIR:
                    short.add(ui)
                # Basic auth: the encoded pair is distinctive even when the pair is short.
                b64 = base64.b64encode(ui.encode()).decode()
                short |= {b64, b64.rstrip("=")}
    out = {f for f in short if len(f) >= MIN_CONTEXT}
    for p in parts:
        if len(p) < MIN_NEEDLE:
            continue
        b = p.encode()
        forms = {p, urllib.parse.quote(p, safe=""), urllib.parse.quote_plus(p, safe=""),
                 json.dumps(p)[1:-1]}
        for k in range(3):
            for enc in (base64.b64encode, base64.urlsafe_b64encode):
                s = enc(b"\0" * k + b).decode()
                # Only the characters made entirely of the value's own bits.
                forms.add(s[-(-k * 8 // 6):(k + len(b)) * 8 // 6])
        out |= {f for f in forms if len(f) >= MIN_NEEDLE}
    return out


def _add_needles(value: str) -> None:
    new = needles(value) - set(_SECRETS)
    if new:
        _SECRETS.extend(new)
        _SECRETS.sort(key=len, reverse=True)


def remember_secret(value: str) -> None:
    """Redact a value this process read, even if it was written after the redaction cache was
    loaded (a value another process applied meanwhile)."""
    if _SECRETS is not None and len(value) >= MIN_NEEDLE:
        _add_needles(value)


def _retired_fresh(f: Path) -> bool:
    """Retired files are named <project>.<NAME>.<unix time>.<random>."""
    import time
    try:
        return time.time() - int(f.name.split(".")[2]) < RETIRED_DAYS * 86400
    except (IndexError, ValueError):
        return False


def redact_obj(obj, keep: frozenset = frozenset()):
    """Redact every string inside a JSON-able value before it is serialized (transport review
    #10: redacting serialized JSON can miss escaped values or break the JSON)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: (v if k in keep else redact_obj(v, keep)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, keep) for v in obj]
    return obj


def redact(text: str, secrets_dir: Path = ETC / "secrets") -> str:
    """Replace every known secret value, and the forms of it `needles` derives, in text. Used
    on everything stored or printed."""
    global _SECRETS
    if _SECRETS is None:
        _SECRETS = []
        if secrets_dir.exists():
            for f in secrets_dir.rglob("*"):
                rel = f.relative_to(secrets_dir).parts
                # Not staged values, journals or temp files (all under dot names): a box chooses
                # those, and as needles they could blank out text the owner must read (grok
                # secrets #3). A value counts once it is live, or read for use (remember_secret).
                # Retired values were live: logs kept for days can still hold them.
                if rel[0] == RETIRED:
                    if len(rel) != 2 or not _retired_fresh(f):
                        continue
                elif any(part.startswith(".") for part in rel):
                    continue
                if f.is_file() and not f.is_symlink():
                    v = f.read_text().strip()
                    if len(v) >= MIN_NEEDLE:
                        _add_needles(v)
    for v in _SECRETS:
        if v in text:
            text = text.replace(v, REDACTED)
    return text


@dataclass
class Platform:
    host: str
    state_dir: Path
    caddy_sites: Path
    tls_dir: Path
    caddy_log_dir: Path
    cf_zones: list[str]
    secrets_dir: Path
    telegram: dict = field(default_factory=dict)
    pull: dict = field(default_factory=dict)      # [transport.pull]: inbox, outbox, container, owner_uid
    push: dict = field(default_factory=dict)      # [transport.push]: registry_local, registry_public, boxes
    public_ipv4: str = ""                         # where `huta edge` points new DNS records
    notify: list = field(default_factory=list)    # argv of a command that takes a message on stdin
    egress_port: int = 4880                       # the egress daemon's CONNECT port
    upstreams: dict = field(default_factory=dict) # name -> {"to": "host:port", "port": int}
    backup: dict = field(default_factory=dict)    # {"dir": Path, "ship": argv}
    logs_max_total: int = 2 << 30                 # [logs] max_total: all targets' logs together
    logs_syslog: bool = False                     # [logs] syslog: also send stored lines to syslog
    crashes: dict = field(default_factory=dict)   # [crashes] url, host (Bugsink; token is a secret)
    # Gateway secret headers: one file per gateway, root:<caddy_group> 0640, that its vhost
    # imports (the vhost itself stays world-readable; DESIGN-v2 §1).
    caddy_secrets: Path = Path("/etc/caddy/huta-secrets")
    caddy_group: str = "caddy"
    postgres_images: dict = field(default_factory=dict)   # [postgres.images] version -> ref@digest

    def secret(self, name: str) -> str:
        return read_secret(self.secrets_dir / "_platform" / name)


@dataclass
class Target:
    project: str
    name: str
    hosts: list[str]
    cloudflare: str
    slots: tuple[int, int]
    egress: str
    limits: dict
    approve: str
    require_ci: bool
    keep_releases: int
    drain: int = 10      # seconds the old slot keeps running after the switch
    env: dict = field(default_factory=dict)          # non-secret environment
    secrets: list = field(default_factory=list)      # secret names (/etc/huta/secrets/<p>/<NAME>)
    upstreams: list = field(default_factory=list)    # platform upstream names
    data: dict | None = None                         # {"size": "2g", "bytes": int, "backup": str}
    adopt: str = ""                                  # a foreign vhost file the first release replaces
    access_log: bool = True
    job_secrets: dict = field(default_factory=dict)  # job name -> secret names only that job gets
    # Container logs huta keeps (huta logs): days, max bytes, whether the project's boxes may
    # read the slots' logs and the jobs' output.
    logs: dict = field(default_factory=lambda: dict(LOGS_DEFAULT))
    crashes: int | None = None                       # its Bugsink project id (huta crashes)
    kind: str = "app"                                # "app" or "gateway" (DESIGN-v2 §1)
    gateway: dict | None = None                      # {"to": "ip:port", "secret": NAME, "header": H}
    edge: dict = field(default_factory=dict)         # {"body": bytes, "timeout": "10m"}
    health_timeout: int = 30                         # seconds a new slot has to report healthy
    # Several containers (DESIGN-v2 §3): per component limits, env and secrets; how long a new
    # worker must stay up after the switch.
    components: dict = field(default_factory=dict)
    worker_liveness: int = 10
    # Postgres of the target (DESIGN-v2 §4): {"version", "size", "bytes", "backup", "limits"}.
    postgres: dict | None = None
    migrate_secrets: list = field(default_factory=list)   # the only secrets [migrate] gets

    @property
    def egress_allow(self) -> list[str]:
        return [] if self.egress == "none" else list(self.egress)


@dataclass
class Project:
    name: str
    repo: str
    app_toml: str
    uid: int
    targets: dict[str, Target]
    boxes: tuple = ()         # dev boxes that may submit and read it; none: the host CLI only
    log_drop: tuple = ()      # lowercase substrings: log lines holding one are never stored
    # Services of the project that run outside huta, readable like a target (huta logs/crashes):
    # name -> {"unit": "x.service", "env_file": Path, "crashes": int | None}
    legacy: dict = field(default_factory=dict)


LOGS_DEFAULT = {"days": 7, "max": 200 << 20, "boxes": True, "jobs": False}


def _logs_cfg(v, where: str) -> dict:
    if not isinstance(v, dict) or set(v) - {"days", "max", "boxes", "jobs"}:
        raise ConfigError(f"{where}: logs = {{ days = 7, max = \"200m\", boxes = true, "
                          "jobs = false }")
    out = dict(LOGS_DEFAULT)
    if "days" in v:
        if not isinstance(v["days"], int) or isinstance(v["days"], bool) or not 1 <= v["days"] <= 90:
            raise ConfigError(f"{where}: logs.days must be 1..90")
        out["days"] = v["days"]
    if "max" in v:
        out["max"] = size_bytes(v["max"], f"{where} logs.max", 16 << 20, 20 << 30)
    for k in ("boxes", "jobs"):
        if k in v:
            if not isinstance(v[k], bool):
                raise ConfigError(f"{where}: logs.{k} must be true or false")
            out[k] = v[k]
    return out


def _crash_id(v, where: str) -> int | None:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v < 1 << 31:
        raise ConfigError(f"{where}: crashes must be a Bugsink project id (a positive integer)")
    return v


UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,120}\.service$")


def _legacy(v, targets: set, where: str) -> dict:
    """[legacy.<name>]: a systemd unit of this project that huta doesn't run. Its journal is
    readable through huta logs (redacted with its own env file's values too), never changed."""
    if not isinstance(v, dict) or len(v) > 8:
        raise ConfigError(f"{where}: [legacy.<name>] tables, at most 8")
    out = {}
    for name, c in v.items():
        w = f"{where} [legacy.{name}]"
        if not NAME_RE.match(name) or name in targets:
            raise ConfigError(f"{w}: a lowercase name that is not also a target")
        if not isinstance(c, dict) or set(c) - {"unit", "env_file", "crashes"}:
            raise ConfigError(f"{w}: keys are unit, env_file, crashes")
        unit, env = c.get("unit"), c.get("env_file")
        if not isinstance(unit, str) or not UNIT_RE.match(unit):
            raise ConfigError(f"{w}: unit must be a systemd service name like app.service")
        if not isinstance(env, str) or not env.startswith("/") or "/../" in env:
            raise ConfigError(f"{w}: env_file must be an absolute path (its values are redacted; "
                              "without it nothing is shown)")
        out[name] = {"unit": unit, "env_file": Path(env), "crashes": _crash_id(c.get("crashes"), w)}
    return out


def _log_drop(v, where: str) -> tuple:
    if not isinstance(v, list) or len(v) > 32 or not all(
            isinstance(s, str) and 3 <= len(s) <= 200 and not re.search(r"[\x00-\x1f\x7f]", s)
            for s in v):
        raise ConfigError(f"{where}: log_drop must be a list of up to 32 strings of 3..200 "
                          "characters")
    return tuple(sorted({s.lower() for s in v}))


def _need(d: dict, key: str, typ, where: str):
    if key not in d:
        raise ConfigError(f"{where}: missing '{key}'")
    v = d[key]
    if not isinstance(v, typ):
        raise ConfigError(f"{where}: '{key}' must be {typ.__name__}")
    return v


def load_platform(etc: Path = ETC) -> Platform:
    for d in (etc, etc / "projects.d", etc / "secrets"):
        assert_root_dir(d)
    d = tomllib.loads((etc / "huta.toml").read_text())
    if d.get("schema") != 1:
        raise ConfigError("huta.toml: schema must be 1")
    p = _need(d, "platform", dict, "huta.toml")
    cf = d.get("cloudflare", {})
    zones = [z.lower() for z in cf.get("zones", [])]
    for z in zones:
        if not HOST_RE.match(z):
            raise ConfigError(f"huta.toml: bad zone {z!r}")
    return Platform(
        host=_need(p, "host", str, "platform"),
        state_dir=Path(p.get("state_dir", "/var/lib/huta")),
        caddy_sites=Path(p.get("caddy_sites", "/etc/caddy/Caddyfile.d")),
        tls_dir=Path(p.get("tls_dir", str(etc / "tls"))),
        caddy_log_dir=Path(p.get("caddy_log_dir", "/var/log/caddy")),
        cf_zones=zones,
        secrets_dir=etc / "secrets",
        telegram=d.get("telegram", {}),
        pull=_pull_cfg(d.get("transport", {}).get("pull", {})),
        push=_push_cfg(d.get("transport", {}).get("push", {})),
        notify=_notify_cfg(p.get("notify", [])),
        public_ipv4=_ipv4(p.get("public_ipv4", "")),
        egress_port=_egress_port(d.get("egress", {})),
        upstreams=_upstreams_cfg(d.get("upstreams", {})),
        backup=_backup_cfg(d.get("backup", {})),
        logs_max_total=_logs_host(d.get("logs", {}))[0],
        logs_syslog=_logs_host(d.get("logs", {}))[1],
        crashes=_crashes_host(d.get("crashes", {})),
        caddy_secrets=Path(p.get("caddy_secrets", "/etc/caddy/huta-secrets")),
        caddy_group=_caddy_group(p.get("caddy_group", "caddy")),
        postgres_images=_postgres_images(d.get("postgres", {})),
    )


def _postgres_images(v) -> dict:
    """[postgres.images]: version name -> image pinned by digest. Only these run."""
    if not isinstance(v, dict) or set(v) - {"images"}:
        raise ConfigError("[postgres] accepts only images")
    imgs = v.get("images", {})
    if not isinstance(imgs, dict):
        raise ConfigError("[postgres.images] maps a version name to an image@sha256:<digest>")
    for name, ref in imgs.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,30}", name) or not isinstance(ref, str) or \
                not re.fullmatch(r"[a-z0-9][a-z0-9./_-]{0,200}@sha256:[0-9a-f]{64}", ref):
            raise ConfigError(f"[postgres.images] {name!r}: an image pinned by digest, like "
                              "postgres@sha256:<64 hex>")
    return dict(imgs)


def _postgres_cfg(v, where: str) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) - {"version", "size", "backup", "limits"} \
            or "version" not in v or "size" not in v:
        raise ConfigError(f"{where}: postgres = {{ version = \"17\", size = \"10g\", "
                          "backup = \"hourly\" }")
    nbytes = size_bytes(v["size"], f"{where} postgres.size", 256 << 20, 2 << 40)
    backup = v.get("backup", "daily")
    if backup not in ("hourly", "daily", "off") and not calendar_ok(backup):
        raise ConfigError(f"{where}: postgres.backup must be hourly, daily, off or an OnCalendar")
    limits = v.get("limits", {})
    if not isinstance(limits, dict) or set(limits) - {"memory", "cpus", "pids", "tmp"}:
        raise ConfigError(f"{where}: postgres.limits accepts memory, cpus, pids, tmp")
    return {"version": str(v["version"]), "size": v["size"].lower(), "bytes": nbytes,
            "backup": backup, "concurrent": False, "limits": dict(limits)}


def _components_cfg(v, target_env: dict, where: str) -> dict:
    """[targets.<t>.components.<c>]: limits, env and secrets for one component. The target's
    own secrets reach only its public component; others get only what is named here."""
    if not isinstance(v, dict) or len(v) > 8:
        raise ConfigError(f"{where}: components = {{ <name> = {{ limits, env, secrets }} }}")
    out = {}
    for name, c in v.items():
        w = f"{where} components.{name}"
        if not NAME_RE.match(name) or not isinstance(c, dict) or set(c) - {"limits", "env", "secrets"}:
            raise ConfigError(f"{w}: accepts limits, env and secrets")
        limits = c.get("limits", {})
        if not isinstance(limits, dict) or set(limits) - {"memory", "cpus", "pids", "tmp"}:
            raise ConfigError(f"{w}: limits accepts memory, cpus, pids, tmp")
        if "tmp" in limits:
            size_bytes(limits["tmp"], f"{w} limits.tmp", 1 << 20, 4 << 30)
        env = c.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(x, str) and len(x) <= 4096
                                                and "\n" not in x and "\0" not in x
                                                for x in env.values()):
            raise ConfigError(f"{w}: env values must be one-line strings")
        for k in env:
            _env_name(k, f"{w} env")
        secrets = c.get("secrets", [])
        if not isinstance(secrets, list) or len(set(secrets)) != len(secrets):
            raise ConfigError(f"{w}: secrets must be a list of names")
        for k in secrets:
            _env_name(k, f"{w} secrets")
            if k in env or k in target_env:
                raise ConfigError(f"{w}: {k} is both in env and secrets")
        out[name] = {"limits": dict(limits), "env": dict(env), "secrets": list(secrets)}
    return out


def _worker_liveness(v, where: str) -> int:
    if type(v) is not int or not 1 <= v <= 300:
        raise ConfigError(f"{where}: worker_liveness must be 1..300 seconds")
    return v


def _health_timeout(v, where: str) -> int:
    if type(v) is not int or not 5 <= v <= 900:
        raise ConfigError(f"{where}: health_timeout must be 5..900 seconds")
    return v


def _migrate_secrets(v, secrets: list, where: str) -> list:
    """Secrets the migration step gets. It gets no others: a migration is the repo's code
    running with the data and nothing else (DESIGN-v2 §2)."""
    if not isinstance(v, list) or len(set(v)) != len(v) or len(v) > 20:
        raise ConfigError(f"{where}: migrate_secrets must be a list of secret names")
    for n in v:
        _env_name(n, f"{where} migrate_secrets")
    return list(v)


def _caddy_group(v) -> str:
    if not isinstance(v, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", v):
        raise ConfigError("platform.caddy_group must be a group name")
    return v


GATEWAY_NETS = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                "::1/128", "fc00::/7")


def _gateway_cfg(v, where: str) -> dict:
    """gateway = { to = "ip:port", secret = "NAME", header = "X-Name" }: the upstream is an IP
    literal in loopback, private, CGNAT or ULA space, so the host never proxies to the public
    internet (DESIGN-v2 §1)."""
    import ipaddress
    if not isinstance(v, dict) or set(v) - {"to", "secret", "header"} or "to" not in v:
        raise ConfigError(f"{where}: gateway = {{ to = \"10.0.0.5:8080\", secret = \"NAME\", "
                          "header = \"X-Gateway-Key\" }")
    host, port = _hostport(v["to"], f"{where} gateway.to")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(f"{where}: gateway.to must be an IP address, not a name") from None
    if not any(ip in ipaddress.ip_network(n) for n in GATEWAY_NETS):
        raise ConfigError(f"{where}: gateway.to must be loopback, private, CGNAT (100.64/10) or "
                          "ULA: huta does not proxy to public addresses")
    out = {"to": f"[{host}]:{port}" if ip.version == 6 else f"{host}:{port}"}
    if "secret" in v or "header" in v:
        if not ("secret" in v and "header" in v):
            raise ConfigError(f"{where}: gateway.secret and gateway.header go together")
        out["secret"] = _env_name(v["secret"], f"{where} gateway.secret")
        h = v["header"]
        if not isinstance(h, str) or not HEADER_RE.match(h) or h.lower() in FORBIDDEN_REQ_HEADERS:
            raise ConfigError(f"{where}: gateway.header must be a plain header name")
        out["header"] = h
    return out


def _edge_cfg(v, where: str) -> dict:
    """edge = { body = "1g", timeout = "10m" }: request body limit and upstream timeouts. Unset
    keys render nothing, so a vhost without them is unchanged."""
    if not isinstance(v, dict) or set(v) - {"body", "timeout"}:
        raise ConfigError(f"{where}: edge = {{ body = \"1g\", timeout = \"10m\" }}")
    out = {}
    if "body" in v:
        out["body"] = size_bytes(v["body"], f"{where} edge.body", 1 << 10, 64 << 30)
    if "timeout" in v:
        to = v["timeout"]
        if not isinstance(to, str) or not re.fullmatch(r"[1-9]\d{0,3}[smh]", to) \
                or duration_s(to, f"{where} edge.timeout", 24 * 3600) > 24 * 3600:
            raise ConfigError(f"{where}: edge.timeout must be like \"90s\", \"10m\" or \"2h\"")
        out["timeout"] = to
    return out


def _crashes_host(v: dict) -> dict:
    """[crashes]: the Bugsink huta reads for huta crashes, on this host (loopback), with the
    platform secret BUGSINK_TOKEN. host: the Host header its ALLOWED_HOSTS accepts."""
    if not v:
        return {}
    if not isinstance(v, dict) or set(v) - {"url", "host"}:
        raise ConfigError("[crashes] accepts url and host")
    url = v.get("url", "")
    if not isinstance(url, str) or not re.fullmatch(r"http://(127\.0\.0\.1|\[::1\]|localhost):\d{1,5}", url):
        raise ConfigError("[crashes] url must be http://127.0.0.1:<port> (Bugsink on this host)")
    host = v.get("host", "")
    if host and (not isinstance(host, str) or not HOST_RE.match(host)):
        raise ConfigError("[crashes] host must be a hostname")
    return {"url": url, "host": host}


def _logs_host(v: dict) -> tuple[int, bool]:
    if not isinstance(v, dict) or set(v) - {"max_total", "syslog"}:
        raise ConfigError("[logs] accepts max_total and syslog")
    if not isinstance(v.get("syslog", False), bool):
        raise ConfigError("[logs] syslog must be true or false")
    return (size_bytes(v.get("max_total", "2g"), "[logs] max_total", 64 << 20, 500 << 30),
            bool(v.get("syslog", False)))


UPSTREAM_PORTS = range(4881, 4900)


def _egress_port(e: dict) -> int:
    if set(e) - {"port"}:
        raise ConfigError(f"[egress] accepts only 'port', got {sorted(e)}")
    port = e.get("port", 4880)
    if port != 4880:
        raise ConfigError("[egress] port is 4880 (the firewall rule allows 4880-4899)")
    return port


def _hostport(v: str, where: str) -> tuple[str, int]:
    import ipaddress
    m = re.fullmatch(r"\[([0-9a-fA-F:.]+)\]:(\d{1,5})|([^:\[\]]+):(\d{1,5})", v or "")
    if not m:
        raise ConfigError(f"{where}: {v!r} must be host:port (IPv6 as [addr]:port)")
    host = (m.group(1) or m.group(3)).lower()
    port = int(m.group(2) or m.group(4))
    if not 1 <= port <= 65535:
        raise ConfigError(f"{where}: {v!r}: bad port")
    if m.group(1):
        ipaddress.IPv6Address(host)
    return host, port


def _upstreams_cfg(u: dict) -> dict:
    out, ports = {}, set()
    for name, c in u.items():
        w = f"[upstreams.{name}]"
        if not NAME_RE.match(name) or not isinstance(c, dict) or set(c) - {"to", "port"}:
            raise ConfigError(f"{w}: needs exactly 'to' and 'port'")
        _hostport(c.get("to", ""), w)
        port = c.get("port")
        if not isinstance(port, int) or port not in UPSTREAM_PORTS or port in ports:
            raise ConfigError(f"{w}: port must be unique in 4881..4899")
        ports.add(port)
        out[name] = {"to": c["to"], "port": port}
    return out


def _backup_cfg(b: dict) -> dict:
    if not b:
        return {}
    if set(b) - {"dir", "ship"}:
        raise ConfigError(f"[backup] accepts dir and ship, got {sorted(b)}")
    d = b.get("dir", "")
    if not isinstance(d, str) or not d.startswith("/"):
        raise ConfigError("[backup] dir must be an absolute path")
    ship = b.get("ship", [])
    if not isinstance(ship, list) or not ship or not all(isinstance(a, str) for a in ship) \
            or not ship[0].startswith("/"):
        raise ConfigError("[backup] ship must be an argv whose first item is an absolute path")
    return {"dir": Path(d), "ship": ship}


ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV = {"PATH", "HOME", "USER", "HOSTNAME", "RELEASE_SHA", "NODE_USE_ENV_PROXY",
                "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "LD_PRELOAD",
                "https_proxy", "http_proxy", "all_proxy", "no_proxy",
                "LD_LIBRARY_PATH", "NODE_OPTIONS", "PYTHONPATH"}


def _env_name(n, where: str) -> str:
    if not isinstance(n, str) or not ENV_NAME_RE.match(n) or n in RESERVED_ENV \
            or n.startswith("HUTA_"):
        raise ConfigError(f"{where}: {n!r} is not an allowed variable name")
    return n


EGRESS_HOST_RE = re.compile(r"^(\*|(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})$")


def egress_entry(v, where: str) -> str:
    import ipaddress
    if not isinstance(v, str):
        raise ConfigError(f"{where}: egress entries are strings")
    host, port = _hostport(v, where)
    try:
        ipaddress.ip_address(host)
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    except ValueError:
        pass
    if not EGRESS_HOST_RE.match(host):
        raise ConfigError(f"{where}: {v!r}: host must be a name, *.suffix, * or an IP")
    return f"{host}:{port}"


SIZE_RE = re.compile(r"^(\d{1,5})([mg])$")


def size_bytes(v, where: str, lo: int, hi: int) -> int:
    m = SIZE_RE.match(str(v).lower()) if isinstance(v, str) else None
    if not m:
        raise ConfigError(f"{where}: size like '512m' or '2g'")
    n = int(m.group(1)) * (1 << 20 if m.group(2) == "m" else 1 << 30)
    if not lo <= n <= hi:
        raise ConfigError(f"{where}: {v} is out of range")
    return n


def calendar_gap(spec: str) -> float:
    """Shortest gap in seconds between the next few times the schedule fires."""
    import subprocess
    from datetime import datetime
    r = subprocess.run(["systemd-analyze", "calendar", "--iterations=6", spec],
                       capture_output=True, text=True, timeout=10, cwd="/")
    times = []
    for line in r.stdout.splitlines():
        # The "(in UTC)" lines, so a DST change can't shorten or stretch a gap.
        m = re.search(r"\(in UTC\): \w+ (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
        if m:
            times.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
    gaps = [b - a for a, b in zip(times, times[1:])]
    return min(gaps) if gaps else float("inf")


def calendar_ok(spec: str) -> bool:
    """A systemd OnCalendar expression, as systemd itself reads it."""
    import subprocess
    if not isinstance(spec, str) or not re.fullmatch(r"[A-Za-z0-9 *:/,.~-]{1,64}", spec):
        return False
    r = subprocess.run(["systemd-analyze", "calendar", spec], capture_output=True, text=True,
                       timeout=10, cwd="/")
    return r.returncode == 0


def _ipv4(v) -> str:
    import ipaddress
    if v == "":
        return ""
    try:
        ip = ipaddress.IPv4Address(v)
    except (ValueError, TypeError):
        raise ConfigError("platform.public_ipv4 must be an IPv4 address")
    if ip.is_private or ip.is_loopback:
        raise ConfigError("platform.public_ipv4 must be a public address")
    return str(ip)


def _pull_cfg(p: dict) -> dict:
    if not p:
        return {}
    for k in ("inbox", "outbox", "container"):
        if not isinstance(p.get(k), str) or not p[k]:
            raise ConfigError(f"[transport.pull] needs {k}")
    if not GRANT_RE.match(p["container"]):
        raise ConfigError("[transport.pull] container: bad name")
    if not isinstance(p.get("owner_uid"), int) or p["owner_uid"] <= 0:
        raise ConfigError("[transport.pull] owner_uid must be the dev user's uid as seen on the host")
    inbox = Path(p["inbox"])
    work = Path(p.get("work", str(inbox.parent / "work")))   # root-only, same filesystem as inbox
    return {"inbox": inbox, "outbox": Path(p["outbox"]), "work": work,
            "container": p["container"], "owner_uid": p["owner_uid"]}


def _push_cfg(p: dict) -> dict:
    if not p:
        return {}
    for k in ("registry_local", "registry_public"):
        if not isinstance(p.get(k), str) or not re.fullmatch(r"[a-z0-9.-]+(:\d+)?", p[k]):
            raise ConfigError(f"[transport.push] {k} must be host[:port]")
    boxes = p.get("boxes", [])
    if not isinstance(boxes, list) or not all(isinstance(b, str) and NAME_RE.match(b) for b in boxes):
        raise ConfigError("[transport.push] boxes must be a list of names")
    ssh_host = p.get("ssh_host", "")
    if ssh_host and not re.fullmatch(r"[A-Za-z0-9.:-]+", ssh_host):
        raise ConfigError("[transport.push] ssh_host must be a host name or address")
    ssh_port = p.get("ssh_port", 22)
    if not isinstance(ssh_port, int) or not 1 <= ssh_port <= 65535:
        raise ConfigError("[transport.push] ssh_port must be a port")
    # A box may push when `huta enroll` made its user (member of huta-push); `boxes` is optional
    # and only kept for older configs.
    return {"registry_local": p["registry_local"], "registry_public": p["registry_public"],
            "boxes": boxes, "ssh_host": ssh_host, "ssh_port": ssh_port,
            "registry_project": p.get("registry_project", "huta-registry")}


def _notify_cfg(argv) -> list:
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ConfigError("platform.notify must be a list of strings (argv)")
    if argv and not argv[0].startswith("/"):
        raise ConfigError("platform.notify[0] must be an absolute path")
    return argv


def load_project(name: str, etc: Path = ETC) -> Project:
    if not NAME_RE.match(name):
        raise ConfigError(f"bad project name {name!r}")
    path = etc / "projects.d" / f"{name}.toml"
    d = tomllib.loads(path.read_text())
    where = str(path)
    if d.get("schema") != 1:
        raise ConfigError(f"{where}: schema must be 1")
    if d.get("project") != name:
        raise ConfigError(f"{where}: project must be {name!r}")
    raw_targets = _need(d, "targets", dict, where)
    # A project of gateways only runs nothing: no repo, manifest or uid (DESIGN-v2 §1).
    gateways_only = bool(raw_targets) and all(
        isinstance(x, dict) and x.get("kind") == "gateway" for x in raw_targets.values())
    if gateways_only and "uid" not in d:
        uid = 0
    else:
        uid = _need(d, "uid", int, where)
        if not 20000 <= uid < 30000:
            raise ConfigError(f"{where}: uid must be in 20000..29999")
    targets = {}
    for tname, t in raw_targets.items():
        tw = f"{where} [targets.{tname}]"
        if not NAME_RE.match(tname):
            raise ConfigError(f"{tw}: bad target name")
        if not isinstance(t, dict):
            raise ConfigError(f"{tw}: must be a table")
        hosts = [h.lower() for h in _need(t, "hosts", list, tw)]
        for h in hosts:
            if not HOST_RE.match(h):
                raise ConfigError(f"{tw}: bad host {h!r}")
        kind = t.get("kind", "app")
        if kind not in ("app", "gateway"):
            raise ConfigError(f"{tw}: kind is \"app\" (default) or \"gateway\"")
        edge_cfg = _edge_cfg(t.get("edge", {}), tw)
        if kind == "gateway":
            unknown = set(t) - {"kind", "hosts", "gateway", "edge", "cloudflare", "access_log",
                                "approve"}
            if unknown:
                raise ConfigError(f"{tw}: a gateway accepts hosts, gateway, edge, cloudflare, "
                                  f"access_log; unknown {sorted(unknown)}")
            if t.get("cloudflare", "orange") != "orange" or t.get("approve", "always") != "always":
                raise ConfigError(f"{tw}: cloudflare = \"orange\" and approve = \"always\" only")
            access_log = t.get("access_log", True)
            if not isinstance(access_log, bool):
                raise ConfigError(f"{tw}: access_log must be true or false")
            targets[tname] = Target(
                project=name, name=tname, hosts=hosts, cloudflare="orange", slots=(),
                egress="none", limits={}, approve="always", require_ci=False, keep_releases=0,
                access_log=access_log, kind="gateway", edge=edge_cfg,
                gateway=_gateway_cfg(t.get("gateway"), tw))
            continue
        slots = _need(t, "slots", list, tw)
        if len(slots) != 2 or not all(isinstance(s, int) and 1024 < s < 65536 for s in slots):
            raise ConfigError(f"{tw}: slots must be two ports")
        # dp-* bridges get no forward accept in nftables: "none" is enforced by the host
        # firewall. A list goes through the egress daemon, the only way out (DESIGN-v1 §2).
        egress = t.get("egress", "none")
        if egress != "none":
            if not isinstance(egress, list) or not egress or len(egress) > 64:
                raise ConfigError(f"{tw}: egress is \"none\" or a list of host:port entries")
            egress = sorted({egress_entry(e, f"{tw} egress") for e in egress})
        approve = t.get("approve", "always")
        if approve != "always":
            # Unattended prod needs CI-attested images (planned); until then every release asks.
            raise ConfigError(f"{tw}: approve = \"always\" only (unattended releases are planned)")
        cloudflare = t.get("cloudflare", "orange")
        if cloudflare != "orange":
            # The edge uses Cloudflare Origin CA certificates, which browsers trust only behind
            # the Cloudflare proxy. A plain (grey) mode needs ACME certificates: planned.
            raise ConfigError(f"{tw}: cloudflare = \"orange\" only")
        drain = t.get("drain", 10)
        if not isinstance(drain, int) or not 0 <= drain <= 300:
            raise ConfigError(f"{tw}: drain must be 0..300 seconds")
        unknown = set(t) - {"hosts", "slots", "egress", "approve", "cloudflare", "limits",
                            "require_ci", "keep_releases", "drain", "env", "secrets",
                            "upstreams", "data", "adopt", "access_log", "job_secrets", "logs",
                            "crashes", "kind", "edge", "health_timeout", "migrate_secrets",
                            "components", "worker_liveness", "postgres"}
        if unknown:
            raise ConfigError(f"{tw}: unknown keys {sorted(unknown)}")
        env = t.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(v, str) and len(v) <= 4096
                                                and "\n" not in v and "\0" not in v
                                                for v in env.values()):
            raise ConfigError(f"{tw}: env values must be one-line strings")
        for k in env:
            _env_name(k, f"{tw} env")
        secrets = t.get("secrets", [])
        if not isinstance(secrets, list) or len(set(secrets)) != len(secrets):
            raise ConfigError(f"{tw}: secrets must be a list of names")
        for k in secrets:
            _env_name(k, f"{tw} secrets")
            if k in env:
                raise ConfigError(f"{tw}: {k} is both in env and secrets")
        # Secrets one job needs and the web container must not have (least privilege): a
        # watcher's chat token, say. They reach only `huta job run` of that job.
        job_secrets = t.get("job_secrets", {})
        if not isinstance(job_secrets, dict) or len(job_secrets) > 20:
            raise ConfigError(f"{tw}: job_secrets = {{ <job> = [\"NAME\", …] }}")
        for jname, names in job_secrets.items():
            if not NAME_RE.match(jname) or not isinstance(names, list) \
                    or not all(isinstance(n, str) for n in names) or len(set(names)) != len(names):
                raise ConfigError(f"{tw}: job_secrets.{jname} must be a list of names")
            for k in names:
                _env_name(k, f"{tw} job_secrets.{jname}")
                if k in env or k in secrets:
                    raise ConfigError(f"{tw}: {k} is in job_secrets and also in env or secrets")
        upstreams = t.get("upstreams", [])
        if not isinstance(upstreams, list) or not all(isinstance(u, str) and NAME_RE.match(u)
                                                      for u in upstreams):
            raise ConfigError(f"{tw}: upstreams must be a list of names")
        data = t.get("data")
        if data is not None:
            if not isinstance(data, dict) or set(data) - {"size", "backup", "concurrent"}:
                raise ConfigError(f"{tw}: data = {{ size = \"2g\", backup = \"daily\", "
                                  "concurrent = false }")
            if not isinstance(data.get("concurrent", False), bool):
                raise ConfigError(f"{tw}: data.concurrent must be true or false")
            nbytes = size_bytes(data.get("size", ""), f"{tw} data.size", 64 << 20, 512 << 30)
            backup = data.get("backup", "daily")
            if backup not in ("hourly", "daily", "off") and not calendar_ok(backup):
                raise ConfigError(f"{tw}: data.backup must be hourly, daily, off or an OnCalendar")
            # concurrent: the operator allows two releases on this data at once (both the host
            # and the repo's concurrent_safe must say so; design reviews: codex #5, grok #5).
            data = {"size": data["size"].lower(), "bytes": nbytes, "backup": backup,
                    "concurrent": bool(data.get("concurrent", False))}
        adopt = t.get("adopt", "")
        if adopt and (not isinstance(adopt, str)
                      or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.caddy", adopt)):
            raise ConfigError(f"{tw}: adopt must be a file name like site.caddy")
        access_log = t.get("access_log", True)
        if not isinstance(access_log, bool):
            raise ConfigError(f"{tw}: access_log must be true or false")
        limits = t.get("limits", {})
        if not isinstance(limits, dict) or set(limits) - {"memory", "cpus", "pids", "tmp"}:
            raise ConfigError(f"{tw}: limits accepts memory, cpus, pids, tmp")
        if "tmp" in limits:
            size_bytes(limits["tmp"], f"{tw} limits.tmp", 1 << 20, 4 << 30)
        targets[tname] = Target(
            project=name, name=tname, hosts=hosts, cloudflare=cloudflare,
            slots=(slots[0], slots[1]), egress=egress,
            limits=limits, approve=approve,
            require_ci=bool(t.get("require_ci", True)),
            keep_releases=int(t.get("keep_releases", 5)), drain=drain,
            env=dict(env), secrets=list(secrets), upstreams=sorted(upstreams), data=data,
            adopt=adopt, access_log=access_log,
            job_secrets={k: list(v) for k, v in sorted(job_secrets.items())},
            logs=_logs_cfg(t.get("logs", {}), tw),
            crashes=_crash_id(t.get("crashes"), tw), edge=edge_cfg,
            health_timeout=_health_timeout(t.get("health_timeout", 30), tw),
            migrate_secrets=_migrate_secrets(t.get("migrate_secrets", []), secrets, tw),
            components=_components_cfg(t.get("components", {}), env, tw),
            worker_liveness=_worker_liveness(t.get("worker_liveness", 10), tw),
            postgres=_postgres_cfg(t.get("postgres"), tw),
        )
    # Which dev boxes may submit this project and read its status, log and jobs (pull: the
    # dev container's name; push: the enrolled box). Absent or empty: no box, the host CLI only.
    boxes = d.get("boxes", [])
    if not isinstance(boxes, list) or len(boxes) > 64 or not all(
            isinstance(b, str) and GRANT_RE.match(b) for b in boxes) or len(set(boxes)) != len(boxes):
        raise ConfigError(f"{where}: boxes must be a list of distinct box names")
    if gateways_only:
        repo, app_toml = d.get("repo", ""), d.get("app_toml", "")
        if not isinstance(repo, str) or not isinstance(app_toml, str):
            raise ConfigError(f"{where}: repo and app_toml must be strings")
    else:
        repo, app_toml = _need(d, "repo", str, where), _need(d, "app_toml", str, where)
    return Project(name=name, repo=repo, app_toml=app_toml, uid=uid, targets=targets,
                   boxes=tuple(boxes), log_drop=_log_drop(d.get("log_drop", []), where),
                   legacy=_legacy(d.get("legacy", {}), set(targets), where))


def load_registry(etc: Path = ETC, platform: "Platform | None" = None) -> dict[str, Project]:
    """Every project, validated together: no two projects may share a uid, and no two targets a
    port or a host. With the platform: upstream names exist, and state targets have a backup."""
    projects = {}
    for f in sorted((etc / "projects.d").glob("*.toml")):
        projects[f.stem] = load_project(f.stem, etc)
    if platform is not None:
        for p in projects.values():
            for t in p.targets.values():
                for u in t.upstreams:
                    if u not in platform.upstreams:
                        raise ConfigError(f"{p.name}/{t.name}: no [upstreams.{u}] in huta.toml")
                if t.postgres:
                    if t.postgres["version"] not in platform.postgres_images:
                        raise ConfigError(f"{p.name}/{t.name}: postgres version "
                                          f"{t.postgres['version']!r} is not in huta.toml "
                                          "[postgres.images]")
                    if not platform.backup:
                        raise ConfigError(f"{p.name}/{t.name}: postgres needs [backup] in "
                                          "huta.toml (its dumps live there)")
                if t.data and not platform.backup:
                    # Even with backup = "off" (no schedule, no shipping): a one-writer release
                    # copies the data before the new code runs, to restore it if that fails
                    # (codex code review #3).
                    raise ConfigError(f"{p.name}/{t.name}: data needs [backup] in huta.toml "
                                      "(its dir holds the copies; backup = \"off\" only turns off "
                                      "the schedule and shipping)")
    for p in projects.values():
        for t in p.targets.values():
            if t.postgres and f"{t.name}-pg" in p.targets:
                raise ConfigError(f"{p.name}: target {t.name}-pg would share its database's "
                                  "volume name; rename it")
    ports, hosts, uids = {}, {}, {}
    for p in projects.values():
        if p.uid and p.uid in uids:
            raise ConfigError(f"uid {p.uid} used by {uids[p.uid]} and {p.name}")
        if p.uid:
            uids[p.uid] = p.name
        for t in p.targets.values():
            for port in t.slots:
                if port in ports:
                    raise ConfigError(f"port {port} used by {ports[port]} and {p.name}/{t.name}")
                ports[port] = f"{p.name}/{t.name}"
            for h in t.hosts:
                if h in hosts:
                    raise ConfigError(f"host {h} used by {hosts[h]} and {p.name}/{t.name}")
                hosts[h] = f"{p.name}/{t.name}"
    return projects


def zone_for(host: str, platform: Platform) -> str:
    """The allowlisted zone a host belongs to, or ConfigError (D-016 amendment)."""
    best = ""
    for z in platform.cf_zones:
        if (host == z or host.endswith("." + z)) and len(z) > len(best):
            best = z
    if not best:
        raise ConfigError(f"{host}: not in an allowlisted Cloudflare zone")
    return best


APP_KEYS = {"schema", "project", "order", "build", "images", "components", "state",
            "preflight", "smoke", "jobs", "external", "diag", "migrate"}
COMPONENT_KEYS = {"image", "kind", "port", "health", "data", "concurrent_safe", "upstream_api",
                  "public", "cmd"}
MAX_COMPONENTS = 8
RESERVED_COMPONENTS = {"postgres", "huta-egress"}   # names other containers answer to


def parse_app_toml(text: str, project: str) -> dict:
    """The repo side of the contract, read from git at the release sha (P-003). Strict: an
    unknown key is an error, so a misplaced line (TOML puts keys after [table] into that table)
    is caught instead of silently ignored."""
    d = tomllib.loads(text)
    if d.get("schema") != 1:
        raise ConfigError("app.toml: schema must be 1")
    if d.get("project") != project:
        raise ConfigError(f"app.toml: project must be {project!r}")
    unknown = set(d) - APP_KEYS
    if unknown:
        raise ConfigError(f"app.toml: unknown top-level keys {sorted(unknown)}")
    comps = d.get("components", {})
    if not comps:
        raise ConfigError("app.toml: no components")
    for cname, c in comps.items():
        bad = set(c) - COMPONENT_KEYS
        if bad:
            raise ConfigError(f"app.toml: components.{cname} has unknown keys {sorted(bad)}"
                              " (top-level keys must come before the first [table])")
    order = d.get("order")
    if not isinstance(order, list) or sorted(order) != sorted(comps) or len(set(order)) != len(order):
        raise ConfigError("app.toml: top-level 'order' must list every component exactly once")
    # Implemented exactly this much; anything more is refused, not ignored (codex #13).
    if len(comps) > MAX_COMPONENTS:
        raise ConfigError(f"app.toml: at most {MAX_COMPONENTS} components")
    for key in ("preflight", "external"):
        if d.get(key):
            raise ConfigError(f"app.toml: '{key}' is not supported by huta yet")
    st_kind = (d.get("state") or {}).get("kind", "none") if isinstance(d.get("state"), dict) else None
    if st_kind not in ("none", "data", "postgres"):
        raise ConfigError("app.toml: state.kind must be \"none\", \"data\" or \"postgres\"")
    for cname, c in comps.items():
        kind = c.get("kind", "http")
        if kind not in ("http", "worker"):
            raise ConfigError(f"app.toml: components.{cname}: kind is \"http\" or \"worker\"")
        if cname in RESERVED_COMPONENTS:
            raise ConfigError(f"app.toml: components.{cname}: that name is reserved")
        if not isinstance(c.get("public", False), bool):
            raise ConfigError(f"app.toml: components.{cname}.public must be true or false")
        if kind == "worker" and ({"port", "health", "public"} & set(c)):
            raise ConfigError(f"app.toml: components.{cname} is a worker: no port, health or "
                              "public (it serves nothing)")
        if "cmd" in c:
            cmd = c["cmd"]
            if not isinstance(cmd, list) or not 1 <= len(cmd) <= 64 or not all(
                    isinstance(a, str) and 0 < len(a) <= 1000 and "\0" not in a for a in cmd):
                raise ConfigError(f"app.toml: components.{cname}.cmd must be an argv list "
                                  "(no shell)")
        if c.get("upstream_api"):
            raise ConfigError(f"app.toml: components.{cname}.upstream_api is not supported yet")
        data = c.get("data")
        if data is not None:
            if st_kind != "data":
                raise ConfigError(f"app.toml: components.{cname}.data needs [state] kind = \"data\""
                                  + (" (a target's database and a data volume together are "
                                     "not supported yet)" if st_kind == "postgres" else ""))
            if not isinstance(data, str) or not re.fullmatch(r"/[a-z0-9][a-z0-9/_.-]{0,100}", data) \
                    or ".." in data or data.rstrip("/") in RESERVED_MOUNTS:
                raise ConfigError(f"app.toml: components.{cname}.data must be an absolute path "
                                  "like /data (not a system directory)")
        if not isinstance(c.get("concurrent_safe", False), bool):
            raise ConfigError(f"app.toml: components.{cname}.concurrent_safe must be true or false")
    if st_kind == "data" and not any(c.get("data") for c in comps.values()):
        name = order[0] if len(order) == 1 else "<name>"
        raise ConfigError(f"app.toml: [state] kind = \"data\" needs components.{name}.data")
    http = [n for n in order if comps[n].get("kind", "http") == "http"]
    marked = [n for n in order if comps[n].get("public") is True]
    if len(marked) > 1 or not http or (not marked and len(http) != 1):
        raise ConfigError("app.toml: exactly one HTTP component is public: mark it public = true "
                          "(not needed when there is only one)")
    d["smoke"] = _smoke(d.get("smoke", []))
    d["jobs"] = _jobs(d.get("jobs", []))
    for j in d["jobs"]:
        if "component" in j and j["component"] not in comps:
            raise ConfigError(f"app.toml: job {j['name']}: no component {j['component']!r}")
    d["diag"] = _diag(d.get("diag", []))
    # Nested tables are strict too (site review: [images.*], [state], [build] were unchecked).
    imgs = d.get("images", {})
    if not isinstance(imgs, dict) or not 1 <= len(imgs) <= MAX_COMPONENTS:
        raise ConfigError(f"app.toml: huta needs 1..{MAX_COMPONENTS} [images.<name>] tables")
    for iname, img in imgs.items():
        if not NAME_RE.match(iname) or not isinstance(img, dict):
            raise ConfigError(f"app.toml: bad image table {iname!r}")
        bad = set(img) - {"dockerfile", "context"}
        if bad:
            raise ConfigError(f"app.toml: images.{iname} has unknown keys {sorted(bad)}")
        if not isinstance(img.get("dockerfile"), str) or not img["dockerfile"]:
            raise ConfigError(f"app.toml: images.{iname}.dockerfile is required")
    for cname, c in comps.items():
        if c.get("image") not in imgs:
            raise ConfigError(f"app.toml: components.{cname}.image must name one of [images]: "
                              f"{sorted(imgs)}")
    st = d.get("state", {})
    if not isinstance(st, dict) or set(st) - {"kind", "schema", "runs_on"}:
        raise ConfigError(f"app.toml: [state] accepts kind, schema and runs_on, got {sorted(st)}")
    _schema_cfg(st)
    if "migrate" in d:
        d["migrate"] = _migrate_cfg(d["migrate"], imgs, st)
    b = d.get("build", {})
    if not isinstance(b, dict) or set(b) - {"test"}:
        raise ConfigError(f"app.toml: [build] accepts only 'test', got {sorted(b)}")
    for cname, c in comps.items():
        if not NAME_RE.match(cname):
            raise ConfigError(f"app.toml: bad component name {cname!r}")
        port = c.get("port", 8080)
        if not isinstance(port, int) or not 1 <= port < 65536:
            raise ConfigError(f"app.toml: {cname}.port invalid")
        health = c.get("health", "/healthz")
        if not isinstance(health, str) or not re.match(r"^/[A-Za-z0-9/_.-]*$", health):
            raise ConfigError(f"app.toml: {cname}.health invalid")
    return d


def _schema_cfg(st: dict) -> None:
    """[state] schema = M, runs_on = [...] (DESIGN-v2 §2): the data schema this release leaves,
    and the schemas its code runs on."""
    if "schema" not in st:
        if "runs_on" in st:
            raise ConfigError("app.toml: [state] runs_on needs schema")
        return
    if st.get("kind", "none") not in ("data", "postgres"):
        raise ConfigError("app.toml: [state] schema needs kind = \"data\" or \"postgres\" (what "
                          "it describes)")
    m = st["schema"]
    if type(m) is not int or not 1 <= m <= 1_000_000:
        raise ConfigError("app.toml: [state] schema must be an integer 1..1000000")
    runs_on = st.setdefault("runs_on", [m])
    if not isinstance(runs_on, list) or not 1 <= len(runs_on) <= 32 or \
            not all(type(x) is int and 1 <= x <= 1_000_000 for x in runs_on) or m not in runs_on:
        raise ConfigError("app.toml: [state] runs_on must be a list of schema numbers that "
                          "includes schema")
    st["runs_on"] = sorted(set(runs_on))


def _migrate_cfg(m, imgs: dict, st: dict) -> dict:
    """[migrate]: one run of the release's own image before the new code starts (DESIGN-v2 §2).
    Argv only (no shell); online mode and a result check are not built yet."""
    if not isinstance(m, dict):
        raise ConfigError("app.toml: [migrate] must be a table")
    for key in ("online", "check"):
        if key in m:
            raise ConfigError(f"app.toml: [migrate] {key} is not supported by huta yet "
                              "(online migrations are planned)")
    bad = set(m) - {"image", "cmd", "timeout"}
    if bad:
        raise ConfigError(f"app.toml: [migrate] has unknown keys {sorted(bad)}")
    if "schema" not in st:
        raise ConfigError("app.toml: [migrate] needs [state] schema (what it migrates to)")
    if m.get("image") not in imgs:
        raise ConfigError(f"app.toml: [migrate] image must name one of [images]: {sorted(imgs)}")
    cmd = m.get("cmd")
    if not isinstance(cmd, list) or not 1 <= len(cmd) <= 64 or not all(
            isinstance(a, str) and 0 < len(a) <= 1000 and "\0" not in a for a in cmd):
        raise ConfigError("app.toml: [migrate] cmd must be an argv list of strings (no shell)")
    timeout = m.get("timeout", "10m")
    secs = duration_s(timeout, "app.toml: [migrate] timeout", 6 * 3600)
    return {"image": m["image"], "cmd": list(cmd), "timeout": timeout, "timeout_s": secs}


RESERVED_MOUNTS = {"", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys",
                   "/run", "/tmp", "/var/run", "/boot", "/root"}
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
FORBIDDEN_REQ_HEADERS = {"host", "content-length", "transfer-encoding", "connection",
                         "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"}


def _smoke(items) -> list[dict]:
    """[[smoke]]: data-only HTTP checks (DESIGN-v1 §5)."""
    if not isinstance(items, list) or len(items) > 20:
        raise ConfigError("app.toml: [[smoke]] is a list of at most 20 checks")
    out = []
    for i, c in enumerate(items):
        w = f"app.toml: smoke[{i}]"
        if not isinstance(c, dict) or set(c) - {"path", "status", "contains", "headers",
                                                 "headers_absent", "request_headers"}:
            raise ConfigError(f"{w}: keys are path, status, contains, headers, headers_absent, "
                              "request_headers")
        absent = c.get("headers_absent", [])
        if not isinstance(absent, list) or len(absent) > 10 or not all(
                isinstance(n, str) and HEADER_RE.match(n) for n in absent):
            raise ConfigError(f"{w}: headers_absent must be a list of header names")
        path = c.get("path")
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9/_.~%?=&+,;:@!$'()*-]{0,500}", path):
            raise ConfigError(f"{w}: path must start with / and be plain URL characters")
        status = c.get("status", 200)
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise ConfigError(f"{w}: status must be an HTTP status")
        contains = c.get("contains", "")
        if not isinstance(contains, str) or len(contains) > 500:
            raise ConfigError(f"{w}: contains must be a string (at most 500 characters)")
        hdrs = {}
        for key, kind in (("headers", "response"), ("request_headers", "request")):
            h = c.get(key, {})
            if not isinstance(h, dict) or len(h) > 10:
                raise ConfigError(f"{w}: {key} must be a table of at most 10 entries")
            for name, val in h.items():
                if not HEADER_RE.match(name) or not isinstance(val, str) or len(val) > 500 \
                        or any(ch in val for ch in "\r\n\0"):
                    raise ConfigError(f"{w}: {key}.{name}: plain header name and one-line value")
                if kind == "request" and name.lower() in FORBIDDEN_REQ_HEADERS:
                    raise ConfigError(f"{w}: request header {name} is set by huta")
            hdrs[key] = {k.lower(): v for k, v in h.items()}
        item = {"path": path, "status": status, "contains": contains,
                "headers": hdrs["headers"], "request_headers": hdrs["request_headers"]}
        if absent:
            item["headers_absent"] = sorted({n.lower() for n in absent})
        out.append(item)
    return out


DURATION_RE = re.compile(r"^(\d{1,5})(s|m|h)$")


def duration_s(v, where: str, hi: int) -> int:
    m = DURATION_RE.match(v) if isinstance(v, str) else None
    if not m:
        raise ConfigError(f"{where}: a duration like 90s, 10m or 1h")
    n = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    if not 1 <= n <= hi:
        raise ConfigError(f"{where}: {v} is out of range")
    return n


def _diag(items) -> list[dict]:
    """[[diag]]: read-only diagnostics a box may run by name (huta diag), in the live image,
    without network or secrets, with the data's last backup copy mounted read-only."""
    if not isinstance(items, list) or len(items) > 20:
        raise ConfigError("app.toml: [[diag]] is a list of at most 20 diagnostics")
    out, names = [], set()
    for i, j in enumerate(items):
        w = f"app.toml: diag[{i}]"
        if not isinstance(j, dict) or set(j) - {"name", "cmd", "timeout"}:
            raise ConfigError(f"{w}: keys are name, cmd, timeout")
        name = j.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name) or name in names:
            raise ConfigError(f"{w}: name must be a unique lowercase name")
        names.add(name)
        cmd = j.get("cmd")
        if not isinstance(cmd, list) or not 1 <= len(cmd) <= 64 or not all(
                isinstance(a, str) and len(a) <= 1024 and "\0" not in a for a in cmd):
            raise ConfigError(f"{w}: cmd must be an argv list of strings")
        out.append({"name": name, "cmd": list(cmd),
                    "timeout": duration_s(j.get("timeout", "1m"), f"{w}.timeout", 300)})
    return out


def _jobs(items) -> list[dict]:
    """[[jobs]]: scheduled commands in the live release's image (DESIGN-v1 §4)."""
    if not isinstance(items, list) or len(items) > 20:
        raise ConfigError("app.toml: [[jobs]] is a list of at most 20 jobs")
    out, names = [], set()
    for i, j in enumerate(items):
        w = f"app.toml: jobs[{i}]"
        if not isinstance(j, dict) or set(j) - {"name", "cmd", "schedule", "timeout",
                                                "after_release", "component"}:
            raise ConfigError(f"{w}: keys are name, cmd, schedule, timeout, after_release, "
                              "component")
        if "component" in j and not (isinstance(j["component"], str) and NAME_RE.match(j["component"])):
            raise ConfigError(f"{w}: component must be a component name")
        name = j.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name) or name in names:
            raise ConfigError(f"{w}: name must be a unique lowercase name")
        names.add(name)
        cmd = j.get("cmd")
        if not isinstance(cmd, list) or not 1 <= len(cmd) <= 64 or not all(
                isinstance(a, str) and len(a) <= 1024 and "\0" not in a for a in cmd):
            raise ConfigError(f"{w}: cmd must be an argv list of strings")
        sched = j.get("schedule")
        if not calendar_ok(sched):
            raise ConfigError(f"{w}: schedule {sched!r} is not a systemd OnCalendar expression")
        if calendar_gap(sched) < 300:
            raise ConfigError(f"{w}: schedule {sched!r} fires more often than every 5 minutes")
        timeout = duration_s(j.get("timeout", "10m"), f"{w}.timeout", 6 * 3600)
        after = j.get("after_release", False)
        if not isinstance(after, bool):
            raise ConfigError(f"{w}: after_release must be true or false")
        job = {"name": name, "cmd": list(cmd), "schedule": sched, "timeout": timeout}
        if after:
            # Only when set: a plan made before this key existed compares equal to one without
            # it, so an upgrade alone doesn't mark every release as changing its jobs.
            job["after_release"] = True
        if "component" in j:
            job["component"] = j["component"]     # only when set, as after_release
        out.append(job)
    return out
