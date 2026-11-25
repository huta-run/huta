# huta v1: what stateful, connected projects need

Status: built (32551a0, e2288e4 and later). Reviewed as a design by codex and grok before the
code; the decisions they changed are folded into the text below and listed at the end.

v0 runs one stateless HTTP container with no egress, no secrets and no data. v1 adds what real
projects need. The test cases are a small public site with a tiny but irreplaceable data corpus
and scheduled rotations (project M), and a media hub with SQLite, many secrets and outbound
calls through VPN SOCKS exits (project T). Everything here is general: no project gets a
special case.

The rule from v0 still holds: **the host decides** users, ports, hosts, secrets, data, egress,
limits and policy (`/etc/huta/projects.d/<p>.toml`). The repo's `deploy/app.toml` only says what
the app *is*: its mount path, its checks and its jobs. A repo can never widen what the host
allows.

## 1. Secrets and environment

Host side, per target:

```toml
[targets.prod]
env = { PUBLIC_ORIGIN = "https://t.example.org", LOG_LEVEL = "info" }   # not secret
secrets = ["API_TOKEN", "MASTER_KEY"]                                  # names only
job_secrets = { watch = ["CHAT_TOKEN"] }   # only that job's runs get these, never the slots
```

- A secret is one file, `/etc/huta/secrets/<project>/<NAME>` (root 0600, directories 0700).
  `sudo huta secret set <p> <NAME>` reads the value from stdin (no echo on a tty; never from
  the command line). `huta secret import <p> <env-file> NAME…` copies named keys from an
  existing `.env`. `huta secret list <p>` shows names and modification times, never values.
- The container gets `env` and every listed secret as environment variables: in the generated
  compose file (root 0600, `$` doubled so compose never interpolates a value) for the slots,
  and through `docker run --env NAME` with the value in the client's own environment (never on
  a command line) for jobs. Plans store a placeholder, never the value.
  Names must match `^[A-Z][A-Z0-9_]{0,63}$`; `HUTA_*`, `RELEASE_SHA`, proxy variables and
  `PATH`-like names are reserved.
- A plan lists secret names (never values or hashes). A missing secret refuses the plan.
  Adding or removing a name, or changing `env`, is an `infra` change. Changing a secret's value
  takes effect on the next release (or `huta restart <p> <t>`), and is not a plan change.
- Every stored or printed string is already redacted against every file in
  `/etc/huta/secrets/` (v0).

## 2. Egress

v0: `egress = "none"` (enforced by the host firewall: `dp-*` bridges get no forward accept).

v1, host side, per target:

```toml
egress = ["api.example.com:443", "*.cdn.example.net:443", "100.64.1.2:8443"]
upstreams = ["vpn-ua"]       # named TCP services the platform defines (below)
```

**Allowlist (HTTPS by hostname).** One host daemon, `huta-egress.service` (`huta egress-proxy`,
an unprivileged dynamic user), is an HTTP `CONNECT` proxy on port 4880.

- The container gets `HTTPS_PROXY`/`https_proxy` = `http://huta-egress:4880`,
  `NO_PROXY=localhost,127.0.0.1`, `NODE_USE_ENV_PROXY=1` (Node ≥ 22.21 / 24 honour the
  variables then), and an `/etc/hosts` entry `huta-egress` → the host gateway.
- The proxy knows which target a connection comes from by its **source address**: each target
  has its own Docker network and subnet. huta writes the policy (subnet → target → allowlist
  and upstreams) to `/run/huta-egress/policy.json` whenever a network is created or the
  registry changes; the proxy re-reads it when it changes. A source outside every huta subnet
  is refused. Containers run with `cap_drop: ALL` (no raw sockets), so they can't spoof.
- `CONNECT host:port` is allowed when an entry matches: exact name, `*.suffix` (one or more
  labels), or `*` (any public host, operator's explicit choice). The proxy resolves the name
  itself and **refuses non-global addresses** (loopback, private, link-local, CGNAT, …) so an
  allowed name can't be pointed at the host or the LAN. It connects to the address it checked
  (no second lookup). An entry that is a literal IP (for example a tailnet service) is allowed
  as written and is the only way to reach a non-global address.
- Only `CONNECT` (HTTPS and anything else that tunnels). Plain-HTTP forwarding is not offered.
- Every connection is logged (target, host:port, address, bytes, duration); every refusal is
  logged with the reason. Limits: 256 open tunnels per target, 30 s connect deadline, 10 min
  idle timeout.

**Upstreams (named host services).** The platform defines them; a target opts in by name:

```toml
# huta.toml
[upstreams.vpn-ua]
to = "127.0.0.1:12080"   # a host-local SOCKS proxy, a tailnet API, …
port = 4881              # the port containers connect to (4881..4899)
```

The same daemon listens on each upstream port and forwards plain TCP to `to`, only for sources
whose target lists that upstream. The container gets `HUTA_UPSTREAM_VPN_UA=huta-egress:4881`.
The app connects there as it would to the original (SOCKS, HTTP, a database…).

**No DNS.** Docker's embedded resolver forwards names from the host's namespace, a side channel
around any egress rule (`<secret>.attacker.example`). Every huta container gets
`dns: 127.0.0.254`, a loopback resolver nobody listens on: every external lookup fails. The
daemon resolves names itself; `huta-egress` comes from `/etc/hosts` (`extra_hosts`).

**Firewall.** One accept in the host's input chain (`iifname "dp-*" tcp dport 4880-4899`) plus
a guard table at a higher priority that drops everything else arriving from `dp-*` bridges, on
input and forward, whatever later rules (open public ports, Docker's forward accepts) would
allow (`systemd/nftables-huta-egress.nft.example`). The only way out is through the daemon.

**The daemon refuses** a name that resolves to a non-global address, to any address of this
host, or into any Docker subnet (the policy carries that list), and any all-numeric host that
isn't a plain IP (`2130706433`, `0177.0.0.1`, `127.1`).

The plan shows the allowlist and upstreams; changing them is `infra`.

## 3. Persistent data

Repo side:

```toml
[state]
kind = "data"               # none | data

[components.web]
data = "/data"              # where the data directory is mounted (absolute path)
concurrent_safe = false     # true: two releases may run on the same data at once
```

Host side, per target:

```toml
data = { size = "2g", backup = "hourly", concurrent = false }
# backup: hourly | daily | an OnCalendar | "off"; concurrent: may two releases share the data
```

- **Volume.** Each target's data is its own ext4 filesystem in a sparse image file,
  `/var/lib/huta/data/<p>/<t>.img`, loop-mounted by a generated systemd mount unit at
  `/var/lib/huta/data/<p>/<t>/` (`nodev,nosuid,noexec`). That gives a real size limit and a
  filesystem huta can freeze for consistent backups. The filesystem root is owned by the
  project uid, mode 0700. The mount point underneath is root-owned mode 0000, so if the volume
  is ever not mounted the app gets "permission denied" instead of writing to the root disk.
  Growing: raise `size`; the next release (or `huta data grow`) grows it online. Shrinking is
  refused.
- **Mounting.** The volume is bind-mounted read-write at `components.<c>.data` in the slots and
  in every job.
- **Two releases at once** only when both the repo (`concurrent_safe = true`) and the host
  (`data.concurrent = true`) allow it: the release works as for stateless apps (the new slot
  starts while the old still serves; both use the data during the checks and the drain).
  Otherwise (the default) **one writer**: stop the old slot (and prove it stopped), take a quiet
  local copy of the data (nothing runs, so no freeze), start the new one, health, smoke,
  switch. The site answers 502 for those seconds. If the new slot fails before the switch, it
  is stopped, **the copy is restored** (nothing is lost: it was taken after the old slot
  stopped), and the old slot starts again (`nothing-activated`). After the switch there is no
  automatic switch back for one-writer targets: the new release has written and served, so
  old code on that data is a human decision (`needs-human`, `huta rollback`).
- **Backups.** A state target needs a platform backup hook, or it is refused (`backup = "off"`
  is an explicit operator choice and is printed on every plan):

  ```toml
  # huta.toml
  [backup]
  dir = "/var/backups/huta"             # root 0700
  ship = ["/usr/local/sbin/huta-ship"]  # gets HUTA_BACKUP_DIR, HUTA_PROJECT, HUTA_TARGET
  ```

  `huta backup <p> <t>` (a generated timer per target, and before every concurrent release)
  builds a **new generation** `<dir>/<p>/<t>/next/`: hard links to the last generation for
  unchanged files, one `rsync` pass while the app runs, then `fsfreeze` of the volume (only
  after checking it is this target's loop mount) and a second pass with `--checksum` (content,
  not just size and time; mtimes compared to the nanosecond), thaw. Only then is it published:
  `current/` becomes `previous/`, `next/` becomes `current/`. A failure leaves `current/` as the
  last good copy. A transient systemd timer thaws the volume after 90 s even if huta dies while
  it is frozen; reconcile thaws too. The copy is crash-consistent (SQLite in WAL mode recovers
  from it as after a power cut). Then `ship` runs on `current/`; exit 0 is "shipped", 75 "target
  unavailable, skipped" (recorded, not an error), anything else fails the unit. The local copy
  and the last off-site copy are recorded separately; the plan shows both ages.
- **Seeding.** `huta data import <p> <t> <dir>` copies an existing directory into the volume
  (only while the target runs nothing; ownership set to the project uid), then backs it up.
- **Getting data out.** `huta data export <p> <t> [path…] > file.tar` (owner on the host)
  writes a tar of the last backup copy. Agents ask the owner; there is no agent-side export.

## 4. Jobs

Repo side:

```toml
[[jobs]]
name = "rotate-telemetry"
cmd = ["node", "jobs/rotate.mjs", "telemetry"]
schedule = "*-*-* 03:10:00"       # systemd OnCalendar, checked with systemd-analyze
timeout = "10m"                   # s/m/h, at most 6h
after_release = false             # true: also run once after each release goes live
```

- A job runs the **live release's image** with the live release's job list, as the project
  uid, with the same hardening, limits, env, secrets, data mount and network policy as the
  slots. A rollback brings the old jobs back.
- huta writes `huta-job_<p>_<t>_<name>.timer` and `.service` (`huta job run <p> <t> <name>`)
  when a release goes live, and removes the units of jobs that no longer exist.
- A job runs next to the live slot. On one-writer data it holds the target lock while it runs
  (a release waits up to 5 min for it, a job up to 10 min for a release); otherwise only while it
  starts. A timed-out job is killed; stopping its unit removes its container
  (`ExecStopPost=docker rm -f`), so a killed wrapper can't leave it running.
- The minimum interval is 5 minutes (checked with `systemd-analyze calendar --iterations`). Output
  goes to the journal; every run is an event (`huta job list <p> <t>` on the host, `huta jobs
  <p> <t>` on dev, show the last runs and the next times). A failed run fails its unit, so host alerting sees it.
- `after_release = true`: once the release (or rollback) is live and its units are synced, huta
  starts the job's unit with `systemctl start --no-block` (event `job-queued`). The run waits
  for the target lock the release still holds, so it starts after the release has finished;
  its outcome is the job's own. A crash between the switch and that call skips the run
  (reconcile doesn't replay it; the schedule runs it next). `restart`/`start` don't trigger it.
  The key is stored only when true, so older plans compare equal.
- `huta job run <p> <t> <name>` runs one now (owner). Dev has no way to run a job.
- Dev reads jobs with the `jobs` request (`huta jobs <p> <t>`): schedules, next runs, the last
  20 outcomes (exit status, duration, peak memory), never output.
- Peak memory: while a job runs, a thread samples its container's cgroup v2 (found through the
  container's pid, `/proc/<pid>/cgroup`): `memory.peak`, `memory.max`, `oom_kill` in
  `memory.events`. Best effort: nothing is recorded if the cgroup can't be found or the job
  ends before the first sample.
- Adding, removing or changing a job is `infra`.

## 5. Smoke checks and automatic revert

Repo side, data only:

```toml
[[smoke]]
path = "/"
status = 200
contains = "<title>"                                      # optional body substring
headers = { "content-security-policy" = "frame-ancestors" } # optional: header contains
request_headers = { accept = "text/markdown" }           # optional
headers_absent = ["x-robots-tag"]                        # optional: these must not be present
```

- **Before the switch**: every check runs against the new slot on loopback with `Host:` set to
  the target's first host and `X-Forwarded-Proto: https`. One failure: the slot is stopped,
  `nothing-activated`, nothing changed.
- **After the switch**, while the old slot still runs: the public health check (`/healthz`
  reports the new sha) and every smoke check against `https://<host>` (identity encoding). One
  failure on a **concurrent** target: **automatic revert** — the vhost goes back to the old
  slot, the new one stops, state `rolled-back`. On a one-writer target: `needs-human` (see §3).
  A first release has nothing to go back to and ends `needs-human`, except an adopted site
  (§6), which goes back to its old config. If huta dies mid-revert, reconcile finishes it as
  `rolled-back` (never `retired`, which would make the failed release a rollback target).
- Only after that does the drain run and the old slot stop.
- At most 20 checks; GET only; no redirects followed (a 3xx can be the expected status); bodies
  capped at 1 MiB; 10 s each.
- **Rollback** stays `huta rollback <p> <t>`: plans the previous image (no new commit, no CI),
  approved like any release.

## 6. Moving a live site onto huta (adopt)

Host side, per target: `adopt = "<file>.caddy"`, a vhost file in Caddy's sites directory that
huta doesn't manage and that serves the target's hosts today.

- The file must serve **only** the target's hosts (huta parses its top-level site addresses and
  refuses otherwise), and the old site must serve a certificate Cloudflare's strict mode
  accepts.
- `huta edge <p> <t> --apply` does the certificate and DNS, but leaves the old file serving and
  the SSL mode alone (it says so); the per-host strict rule is set right after the switch.
- The first release starts and is checked (health and smoke on loopback, as above) while the
  old site serves. The switch is one edge transaction: write huta's vhost **and** move the old
  file into huta's journal (`/var/lib/huta/edge/adopted/`), validate, reload. A failed validation
  reverts both. A failed post-switch check reverts both (the old site serves again).
- After the first successful release the `adopt` key is inert (the file is gone). To go back by
  hand, the saved file is in the journal.

## 7. Edge guarantees (the documented contract)

- Request headers reach the app unchanged, including every `cf-*` header; hop-by-hop headers
  are dropped. **The visitor's address is `CF-Connecting-IP`** (only Cloudflare can connect).
- No `trusted_proxies`: `remote_ip` stays the TCP peer, so the Cloudflare-only filter keeps
  working. `X-Forwarded-For` is therefore the Cloudflare edge address; `X-Forwarded-Proto` is
  `https`, `X-Forwarded-Host` the host.
- Access logs: `access_log = false` per target writes none (`true`, the default, writes one file
  per host in the Caddy log directory). The plan shows it.
- `/tmp` size: `limits.tmp` (default 16m, at most 4g).
- Images that declare `VOLUME` are refused (Docker would attach unbounded writable storage).
- Creating or growing a volume needs the disk to hold it full plus a 20 GiB reserve.

## 8. Plans

A plan shows everything above that decides how the release runs: env names, secret names,
egress, upstreams, data size and mount, concurrent-safe, backup schedule and last good backup,
jobs, smoke count, adopt. All of it is part of the effective config: if it changes between plan
and approval, the plan is superseded.

## Not in v1

Several components, `pg`, migrations with schema versions, CI-attested images and unattended
releases, agent-side data export, plain-HTTP egress.

## Review decisions (2026-11-09)

From codex's and grok's design reviews, what changed against the first draft: Docker DNS
disabled for huta containers (also closes a v0 side channel); the guard table; host addresses
and Docker subnets refused by the daemon; strict numeric-host parsing; upstreams documented as
capabilities of their own; host-controlled concurrency; one-writer order stop → quiet copy →
start, restore on a failure before the switch, no automatic revert after it; backup generations
with a checksum pass under the freeze and a thaw watchdog; `VOLUME` refused; disk reserve; job
units remove their containers and jobs hold the lock on one-writer data; adopt checks the file's
hosts and defers the SSL rule; no `trusted_proxies`; reconcile restarts a routed slot a crash
left stopped and finishes interrupted reverts. Not taken: per-target listener addresses and a
separate network namespace for the daemon (source-subnet attribution with `cap_drop: ALL` and
the guard table is enough on one host), filesystem snapshots instead of freeze (no snapshot-
capable storage on the target host), secrets as files (apps read environment variables), one
static job timer (generated units are validated names and calendar strings only).
