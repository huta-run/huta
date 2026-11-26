# huta architecture

## Roles

| Role | Where | Trusted with |
|---|---|---|
| **dev** | a build box or container, often with coding agents | building images, running tests, asking for a release |
| **host** | the production server, `huta` as root | everything: it verifies, plans and acts |
| **owner** | a person | approving production releases |

dev is trusted to *ask*, never to act. The host trusts only what it checks itself.

## A release

1. **pack** (dev): refuse a dirty tree (tracked changes), export the commit with `git archive`
   (untracked and git-ignored files never reach the build), build `linux/amd64` with
   `RELEASE_SHA` and labels `org.opencontainers.image.revision=<sha>` and
   `run.huta.project=<project>`, tag `huta-build/<project>:<sha12>`.
2. **import** (host): pull transport: `docker save` from the dev box, the archive is checked and
   every name stripped; push transport: pulled from the host's registry **by digest**. Either
   way the image is loaded by id and named `huta/<project>:<sha12>-<id12>`.
3. **submit** (host): check the sha is on `origin/main` (and, if required, that the newest
   GitHub Actions run of the workflow named `ci` for that sha on a push to `main` is completed
   with success), read
   `deploy/app.toml` from git at that sha, check the image (id, labels, non-root, linux/amd64),
   compute the effective config, compare the sha with the live one (GitHub compare: ahead,
   behind, diverged, identical; or unrelated, when the live commit isn't in the repo or shares
   no history with it), record a plan with a hash. A plan that goes back or isn't based
   on live carries a warning (plan text, Telegram, the submit answer's `warning`); it never
   refuses: going back on purpose is the owner's call. A refused submit untags its image.
4. **approve** (owner): with the ✅ Deploy button on the release's message in the Telegram topic, or
   `huta approve <release>` on the host (both end up in `cmd_approve`). Re-check everything
   under the target lock (state, 30-minute expiry, what is live, effective config, main, CI),
   then run the recipe. Expiry is applied here: an old plan stays `planned` until an approve
   attempt (CLI, or a late button press) marks it `superseded`.
5. **run** (`web` recipe): start the idle slot, verify `/healthz` reports `{"ok": true,
   "sha": "<sha>"}` on its own loopback port (the app must listen on `0.0.0.0` at the
   `app.toml` port inside the container), switch the vhost to it (journaled edge
   transaction, one graceful reload), keep the old slot running for the target's `drain`
   seconds, stop it, check the public URL. Long requests, streams and websockets on the old slot
   can be cut.

Every **approved** release ends in an outcome: `live`, `rolled-back` (it went live, its public
checks failed, traffic went back), `nothing-activated` (traffic never moved
to it: the previous release keeps serving, or on a first deploy the vhost still answers 503
`not released yet`), `needs-human`. A refusal before approval leaves no release; an unapproved
plan stays `planned` or becomes `superseded` / `rejected`.

## The contract

**Repo side** (`deploy/app.toml`, data only). Strict where it matters: unknown top-level keys,
unknown keys in `[components.*]`, `[images.*]` (`dockerfile`, `context`), `[state]` (`kind`:
`none` or `data`), `[build]` (`test`), `[[smoke]]` and `[[jobs]]` are errors; `preflight` and
`external` must be empty; `components.*.image` must name the one `[images.*]` table. v1 keys
(data mount, `concurrent_safe`, smoke checks, jobs) are specified in `DESIGN-v1.md` and the
agent docs (`site/public/docs/contract.md`, `runtime.md`).

```toml
schema = 1
project = "hello"
order = ["web"]            # top-level keys come before the first [table]

[images.web]
dockerfile = "examples/hello/Dockerfile"
context = "examples/hello"

[components.web]
image = "web"
kind = "http"
port = 8080
health = "/healthz"

[state]
kind = "none"
```

**Host side** (`/etc/huta/projects.d/<project>.toml`, root-owned; unknown target keys are
errors, uids unique across projects, ports and hosts unique across targets):

```toml
schema = 1
project = "hello"
repo = "example/hello"
app_toml = "deploy/app.toml"
uid = 20001
boxes = ["devbox"]             # dev boxes that may submit it and read status/log/jobs/logs (none: CLI only)
log_drop = ["enter setup code"]   # log lines holding one (case-insensitive) are never stored
[legacy.hello-old]             # a service of the project huta doesn't run: huta logs/crashes read it
unit = "hello.service"
env_file = "/opt/hello/.env"   # its values are redacted too; unreadable: nothing is shown
crashes = 12                   # optional Bugsink project id

[targets.prod]
hosts = ["hello.example.org"]
cloudflare = "orange"          # v0: orange only (Origin CA cert + Cloudflare-only filter)
slots = [18300, 18301]         # two loopback ports
egress = "none"                # or ["api.example.com:443", "*.cdn.example.net:443"]
limits = { memory = "64m", cpus = 0.5, pids = 64 }   # also tmp = "64m"
approve = "always"             # v0: always only
require_ci = true
keep_releases = 5
drain = 10                     # seconds the old slot keeps running after the switch (0..300)
# v1, optional (DESIGN-v1.md):
env = { PUBLIC_ORIGIN = "https://hello.example.org" }
secrets = ["API_TOKEN"]        # /etc/huta/secrets/<project>/<NAME>, set with `huta secret set`
upstreams = ["vpn-exit"]       # names from huta.toml [upstreams.*]
data = { size = "2g", backup = "hourly", concurrent = false }
access_log = true
logs = { days = 7, max = "200m", boxes = true, jobs = false }   # huta logs (the defaults)
crashes = 11                   # Bugsink project id (huta crashes)
adopt = "hello.example.org.caddy"   # moving a live site: its vhost file, replaced at the first release
edge = { body = "1g", timeout = "10m" }   # v2: request body limit, upstream timeouts (unset: unchanged)
# Monitoring hooks (docs/PLAN-monitoring.md; built: remote resources and the variables):
monitor = { uptime = true, errors = true, analytics = true }   # jobs = true: planned
matomo = 7                     # optional: adopt this Matomo site instead of creating one
watch = { window = "15m", new_issues = 1, events = 10, factor = 3.0 }   # after each release
# [targets.prod.components.api] monitor_env = true   also gives that component the variables

# v2: a gateway, a hostname for a service huta doesn't run (DESIGN-v2 §1). No slots, uid or repo.
[targets.media]
kind = "gateway"
hosts = ["media.example.org"]
gateway = { to = "100.64.0.5:8080", secret = "GATEWAY_KEY", header = "X-Gateway-Key" }
```

A gateway's upstream is an IP in loopback, private, CGNAT or ULA space. Its secret header
lives in `<platform.caddy_secrets>/<p>--<t>.caddy` (default `/etc/caddy/huta-secrets`,
root:`caddy_group` 0640, directory 0750), imported by the vhost, so the vhost stays
world-readable without the value. `reconcile`, release, job and log commands skip or refuse
gateways; `huta edge` and `huta traffic` work.

**Platform** (`/etc/huta/huta.toml`):

```toml
schema = 1
[platform]
host = "myhost"
notify = ["/usr/local/bin/notify-owner"]   # optional fallback, used only without [telegram]
caddy_secrets = "/etc/caddy/huta-secrets"  # v2 gateways' header files (the default)
caddy_group = "caddy"                       # the group Caddy runs as (the default)
[postgres.images]              # v2: the only Postgres images targets may run, pinned by digest
"17" = "postgres@sha256:<64 hex>"
[external]                     # v2: the image huta publishes to Cloudflare with (pinned)
wrangler = "<repo>@sha256:<64 hex>"
[cloudflare]
zones = ["example.org"]        # huta refuses any Cloudflare call outside this list
[transport.pull]               # optional
inbox = "/srv/huta-dev/inbox"
outbox = "/srv/huta-dev/outbox"
container = "devbox"
owner_uid = 1001000
[transport.push]               # optional
registry_local = "127.0.0.1:5000"
registry_public = "registry.example.org:4882"
ssh_host = "host.example.org"  # what `huta enroll` tells boxes to ssh to
ssh_port = 22
registry_project = "huta-registry"   # compose project restarted on enroll/revoke
# boxes = ["mybox"]            # optional, older configs; enrolled boxes need no entry here
                               # (which projects a box may use: each project's own `boxes`)
[telegram]                     # optional: history messages and one-tap approval
history_chat = -1001234567890
history_thread = 12
token_json = "/path/to/bot-config.json"
token_keys = ["bot", "token"]
owner_id = 123456789           # the owner's Telegram user id; the only one who can press
relay_user = "chatgw"          # the only user allowed on the press socket
buttons = true                 # only once the relay plugin is installed
[backup]                       # needed by targets with data (unless their backup = "off")
dir = "/var/backups/huta"
ship = ["/usr/local/sbin/huta-ship"]   # sends <dir>/<p>/<t>/current off the server; 75 = skipped
[logs]                         # optional
max_total = "2g"               # all targets' stored logs together; oldest hours go first
syslog = false                 # true: also send each stored (redacted) line to syslog
[crashes]                      # optional: Bugsink on this host, for huta crashes
url = "http://127.0.0.1:8000"  # loopback only; token: secrets/_platform/BUGSINK_TOKEN
host = "bugs.example.org"      # the Host header its ALLOWED_HOSTS accepts
[monitoring]                   # optional: wire targets into services the operator runs
prefix = "huta"                # remote names <prefix>_<project>_<target>[_<job>]
[monitoring.healthchecks]      # token: secrets/_platform/HEALTHCHECKS_API_KEY
url = "http://127.0.0.1:8000"  # management API: loopback http or https://<host>
ping = "https://hc.example.org/ping"   # ping base; huta builds ping URLs from it
host = ""                      # Host header for a loopback url (optional)
channels = "*"                 # sent only when a check is created
[monitoring.bugsink]           # API from [crashes]; token: BUGSINK_TOKEN (global, can create)
dsn_host = "bugs.example.org"  # the host in DSNs; must resolve to public addresses
team = "<team uuid>"           # the Bugsink team new projects belong to
[monitoring.matomo]            # token: MATOMO_TOKEN (superuser, to create sites)
url = "http://127.0.0.1:8081"
host = "stats.example.org"
[monitoring.watch]
window = "15m"                 # default watch window for targets, 5m..2h
[upstreams.vpn-exit]           # a host service containers may reach (opt-in per target)
to = "127.0.0.1:1080"
port = 4881                    # 4881..4899
```

Secrets live in `/etc/huta/secrets/` (root 0700), one file each. A value a dev box sent (op
`secret`) waits in `secrets/<project>/.staged/<NAME>` until a release that uses it is approved
(see "Secrets from a box" below). The platform ones are
`_platform/GITHUB_TOKEN` (read-only) and `_platform/CLOUDFLARE_API_TOKEN` (Zone Read, DNS Edit,
SSL and Certificates Edit, Zone Settings Edit, Config Rules Edit), and with `[monitoring]`
`HEALTHCHECKS_API_KEY`, `BUGSINK_TOKEN`, `MATOMO_TOKEN`; set them with `huta secret set
_platform <NAME>`. Push boxes' registry passwords are `registry/<box>`. huta writes two kinds
itself: `<project>/HUTA_SENTRY_DSN_<TARGET>` (a target's Bugsink DSN) and
`_monitor/CAPABILITIES` (healthchecks check ids, which work as ping capabilities; kept there so
the redactor hides them in every process).

**Setting up a host.** What a host needs besides huta itself (an amd64 KVM-class server with
cgroup v2 and loop devices; Docker with the compose plugin; Caddy importing
`<caddy_sites>/*.caddy`; nftables with the egress guard; Tailscale for boxes and the registry;
the registry compose project; the push socket and sshd drop-in; the two platform tokens) and the
order to do it in: `site/public/host.md` (https://huta.run/host.md), written for an agent on
that server.

## What huta generates

- **compose** per target (`/var/lib/huta/compose/<p>/<t>/compose.yaml`): services `slot-a` and
  `slot-b` running the image **by id**, as the project uid, read-only root filesystem, `/tmp`
  tmpfs, `cap_drop: ALL`, `no-new-privileges`, memory/cpu/pids limits, ports on 127.0.0.1 only,
  env from the registry (`env`, `secrets`) plus `RELEASE_SHA`, `HUTA_PROJECT`, `HUTA_TARGET`,
  `HUTA_SLOT`, `dns: 127.0.0.254` (no name resolution: Docker's resolver would be a way out), a
  bridge named `dp-<hash>`; with egress or upstreams the proxy variables and
  `huta-egress:host-gateway`; with data the volume bind-mounted at the manifest's path;
  `logging` pinned to `json-file` 10m×3, uncompressed (the collector reads those files).
  The host firewall gives `dp-*` bridges nothing but the egress daemon's ports
  (`systemd/nftables-huta-egress.nft.example`).
  With several components (DESIGN-v2 §3) the services are `slot-<s>-<component>`, each slot on
  its own network `huta_<p>_<t>_<s>2` (bridge `dp-<hash of the slot>`, ICC on, component names
  as aliases), labels `run.huta.component` (and `run.huta.public` on the public one), only the
  public component published, per-component limits, env and secrets, data only where named,
  workers with restart `no` until they have held for `worker_liveness` after the switch. The
  guard's `huta_intra` set lets exactly each slot bridge talk to itself; `huta egress sync`
  keeps it filled. Releases carry an image map (`releases.images`); every image is checked,
  with `run.huta.image` naming which it is.
- **egress policy** (`/run/huta-egress/policy.json`, root-written, world-readable): subnet →
  target, allowlist, upstreams, plus the host's own addresses and Docker subnets to refuse.
  `huta-egress.service` (DynamicUser) reads it.
- **data volume** per target: `/var/lib/huta/data/<p>/<t>.img` (ext4, sparse), a generated
  mount unit (`Before=docker.service`) at `/var/lib/huta/data/<p>/<t>/`; backups in
  `[backup] dir`/`<p>/<t>/{current,previous}`.
- **timers** per target: `huta-job_<p>_<t>_<job>.{timer,service}` and
  `huta-backup_<p>_<t>.{timer,service}` in `/etc/systemd/system` (marker line; only huta's own
  names are touched).
- **vhost** per target (`<caddy sites dir>/<host>.caddy`): TLS with the target's Origin CA
  certificate, the Cloudflare-only filter, and `reverse_proxy` to the **one** live slot (or a
  503 `not released yet` before the first release).
- **Cloudflare**: one Origin CA certificate per target covering all its hosts; a per-host
  Configuration Rule `ssl = strict` (zone-wide settings are never changed, foreign rules are
  never rewritten and are re-read to prove it).

## Logs (`logs.py`) and status now (`runstate.py`)

- **Capture.** Slots log to `json-file` (pinned in compose). `huta logs-collect`
  (`huta-logs.timer`, every minute) reads each slot container's files from a cursor (container
  id → inode and offset, `<state_dir>/logs/<p>/<t>/.cursor.json`), follows Docker's rotation,
  joins Docker's 16 KiB partial entries into lines (cut at 64 KiB without a newline), and
  appends records to `<state_dir>/logs/<p>/<t>/<YYYY-MM-DDTHH>.jsonl` (UTC hour files, root
  0600). The cursor only moves past complete lines. `recipe.compose("up", …)` collects the
  target first, because `up` may recreate a slot container and its log; a failed start is
  collected right after its slot stops, and its container state goes into the release's events
  (`slot-state`). Jobs (`--log-driver none`) write their relayed output there too.
  One collector per target at a time (`flock` on `.lock`), separate from the target lock.
- **Gate at ingest, again at read.** A line holding a project `log_drop` string (raw text,
  lowercase) is dropped; otherwise `cfg.redact`, then cut at 8192 characters. Nothing
  unredacted is stored. A read redacts again (values learned since) and re-applies `log_drop`,
  then `--grep` (substring, lowercase, after redaction), then `--tail`, then the answer budget
  (900 000 bytes of JSON-encoded text; newest lines win; every cut is said in a `‹…›` line). A
  read scans at most 20 s.
- **Retention.** Per target `logs.days` (hour files older go) and `logs.max` (oldest hours go,
  never the current one); then `[logs] max_total` over all targets.
- **Access.** Op `logs` has the same `boxes` check as `status`; then `logs.boxes` for slot
  output and `logs.jobs` for `--job`. Each read is an event `logs-read` with the box, the
  filters and the line count. The operator's `huta logs` collects first.
- **syslog** (`[logs] syslog = true`): each stored line, after the gate, also goes to syslog
  (ident `huta/<p>/<t>`, facility daemon; stderr at notice). Off by default: the journal is
  readable by more users than huta's store.
- **Status now** (op `status`, and `huta status <p> <t>`): per slot container `docker inspect`
  (state, started, restarts, exit code, OOMKilled), its cgroup v2 files (found from its pid and
  checked by name: `memory.current/peak/max/events`, `pids.current/peak/max/events`,
  `cpu.stat` over 1 s, `cpu.max`), one health request to the live slot (`recipe._get_json`:
  no proxy, no redirect, capped, 3 s), `statvfs` of the data mount, `last.json` of the backup,
  and established connections from the target's subnet to ports 4880..4899 (`ss`). Failure to
  read any of it never fails the status.

## Other reads for boxes (`observe.py`, `diag.py`)

All take the same `boxes` check, are bounded (20 s of scanning, 64 MiB read, the logs answer
budget), redacted, and recorded as events (`<op>-read`, `diag-start`/`diag`), never with their
content.

- **egress-log** (op `egress-log`): `journalctl -u huta-egress.service` in the window (at most
  8 days), lines `refused <p>/<t> …: <why>` and `<p>/<t> → upstream <u> unreachable`, counted
  per minute and reason. The peer address is dropped; names off the allowlist are already
  masked by the daemon.
- **traffic** (op `traffic`): the target's Caddy access logs (`<caddy_log_dir>/<host>.log` and
  rolled `<host>-*.log[.gz]` newer than the window). Only `ts`, `status`, `duration` and the
  method (from a fixed list) are read out: per-minute status classes and the 5xx list.
- **crashes** (op `crashes`): Bugsink's API (`[crashes] url`, loopback only, `Host` from
  `[crashes] host`, no proxy, no redirect, 8 MiB per answer) with the platform token
  `BUGSINK_TOKEN`. Bugsink tokens are global, which is why boxes never get one: the host reads
  only the target's (or legacy entry's) project id. Issues by `last_seen` since the window, at
  most 20; for each, the newest event's release, environment and `stacktrace_md` (4000
  characters, 80 lines). Every line passes the log gate.
- **legacy** (`[legacy.<name>]`): `huta logs <p> <name>` reads `journalctl -u <unit>`; first every
  value in `env_file` becomes a redaction needle (opened `O_NOFOLLOW`, regular, ≤ 1 MiB); if it
  can't be read nothing is shown. Nothing writes to the unit, its files or its database.
- **diag** (op `diag`): one per target at a time (`Store.acquire(<p>, "_diag-<t>")`).
  `docker run --rm --name huta-diag_<p>_<t> --network none`, the slot hardening, `--log-driver
  none`, env = registry `env` + `RELEASE_SHA`, `HUTA_PROJECT`, `HUTA_TARGET`, `HUTA_DIAG`
  (no secrets). With data, `cp -al` of `<backup dir>/<p>/<t>/current` to `.diag-<random>` in the
  same directory (hard links: a backup rotating `current` can't pull files from under the
  run), bind-mounted read-only at the data path, removed afterwards. `timeout` from
  `[[diag]]` (≤ 5 min), output ≤ 256 KiB, then the log gate. `[[diag]]` is part of
  `recipe.effective`, so adding or changing one shows in the plan.
- **Crash:** a dead huta leaves the container and the snapshot; the next diag of the target
  removes both (`docker rm -f` by name, `rm -rf` of `.diag-*`).

## Monitoring (`monitor.py`)

Built so far (docs/PLAN-monitoring.md milestones 1, 2, 4 and 5): the remote resources, the
variables they give containers, the probe and the watch window. The rest of the plan (job
checks, the ↩️ button, `ready`/`blocks`/`--ack`) is *planned*.

- `ensure` makes what a target's `monitor` grants, once, and finds it again by name
  (`<prefix>_<project>_<target>`): a Bugsink project and its DSN (`errors`; `crashes = <id>`
  adopts that project instead), a Matomo site for `https://<host>` (`analytics`; `matomo = <id>`
  adopts), a healthchecks check fed by the uptime probe (`uptime`; the probe is planned).
- It runs at planning time for `errors` and `analytics` (the kinds that will change the
  container), and for everything with `huta monitor ensure`. One ensure on the host at a time
  (`<state_dir>/monitor-ensure.lock`, outside `locks/`), 30 s for all its calls.
- Calls: no proxy, no redirect, 8 MiB, and 10 s each for the whole exchange (connect,
  headers, body: the request runs in a thread that is abandoned at the deadline, since socket
  timeouts only bound each wait). Answers are checked: ids by regex, exactly one remote
  resource per name (two is `ambiguous`, nothing is guessed; a listing with an entry or a page
  link huta can't read is refused rather than taken as "not there", so it never leads to a
  create), the DSN must be `https://<32 hex>@<dsn_host>/<project id>`. healthchecks' returned
  ping URL is never used; huta builds ping URLs from `ping` and the checked id. Matomo calls are
  POST with the token in the body and `X-Forwarded-Proto: https` (Matomo with `force_ssl`
  redirects plain loopback calls otherwise).
- A failure is an event `monitor-fail` (kind and a short reason) and never fails a release.
- **Variables** (`runtime.environment`, via `monitor.env_for`): only when `app.toml` asks
  (`[monitor] env = ["sentry", "matomo"]`) and the registry grants (`monitor.errors`,
  `monitor.analytics`): `SENTRY_DSN` (the DSN secret), `SENTRY_RELEASE` (the sha),
  `SENTRY_ENVIRONMENT` (the target), `MATOMO_SITE_ID` (`matomo = <id>`, else the site ensure
  made, kept in `<state_dir>/monitor/<p>--<t>.json` since the environment is built without the
  store). A name the registry sets wins. The public component gets them, other components only
  with `monitor_env = true`, jobs as their component; migrations and diag never. A value that
  doesn't exist yet is left out. They follow the state at the time a compose file is written
  (a release, a revert, `huta start`), so a DSN made by `huta monitor ensure` appears in a live
  slot only when its compose is next written.
- `effective` gains `monitor` (kinds, the variable names, what is missing and why, the egress
  added) only for targets with `monitor`; the plan says it as a `📈 monitoring:` line.
- **Probe** (`huta monitor tick`, `huta-monitor.timer` every minute; nothing without
  `[monitoring]`): one tick at a time (`<state_dir>/monitor.lock`), no target lock, 45 s for all
  of it, targets in turn so a slow one can't starve the rest. For each target with
  `monitor.uptime` and a live release: one GET of `https://<host><health>` per host, all hosts
  at once, 5 s, at most 64 KiB, passing only with `ok: true` and the live sha. Every call
  (probe, ping, making a check) gets only the time left in the tick, with 2 s kept for writing
  its state; the self check is made under ensure's lock, as every capability is. Skipped while a phase file or the stop marker
  exists, and dropped if the live release, its episode or those changed during the request (a
  held target lock alone isn't a reason: a backup holds it for hours while the site serves).
  The last 60 samples go to `<state_dir>/monitor/<p>--<t>.probe.json`; a change between ok and
  failing is an event (`probe-down`, `probe-up`). With `[monitoring.healthchecks]` each result
  is also pinged to the target's uptime check (made by the tick if missing, at most hourly), and
  with `self = true` huta pings its own check every tick.
- `recipe.public_check` (after a switch) now also requires `ok: true`, as the slot check does.
- **Watch window** (`watch.py`; targets with `watch`): a row in `watches` per serving episode
  (release id, `live.since`). `cmd_approve` opens it when the outcome is live, after its other
  steps; the tick opens one for a live episode that has none (as `unknown` if its window is
  already over: nobody looked). Opening takes the local baseline in the same write: zero for
  containers the release made (any restart or OOM kill of them counts), what an older container
  shows for one from before. Sources come from what the release got (its plan's
  `effective.monitor`), not from today's files. The tick
  evaluates open rows after the probes, within its deadline: `local` (the live slot's
  containers: restarts, OOM kills, stopped), `probe` (three failed samples of the episode in a
  row, each within 150 s), `errors` (Bugsink issues first seen since the switch; events since
  against the window before, per issue, at most 20 issues and 500 events), `checks` (the uptime
  check's flips, with healthchecks). A signal makes the row `degraded` at once; at the window's
  end it is `ok` if every source answered in each third of the window, else `unknown`. One
  transaction writes state, signals and events, only if the live episode and slot are the ones
  looked at and no release of the target is approved or running; a look during which the episode
  changed or a release started is dropped. Evidence that is incomplete (Docker or an OOM counter
  not readable, a capped Bugsink count, an answer huta can't read) leaves that source without an
  answer for the tick. Rows of an episode no longer live
  are `closed` by the tick, by approval and by every `reconcile`; `huta stop` closes the live
  one. The history message shows the watch line; `degraded` and `unknown` also send a reply
  (retried until sent, so possibly twice; a missing history message is sent first; random order,
  10 a tick, each Telegram call bounded by the tick's time; given up after 24 h with an event); `watch.on_degraded` records `action = notified`
  (the seam for automatic rollback). `huta status` and the box `status` op end with the line.
- **Egress:** `<dsn_host>:443` is added only for a release that gets `SENTRY_DSN` (the repo
  asks, the registry grants `errors`, the DSN exists): `runtime.egress_for` for its env and
  compose (proxy variables, the egress host), and in the egress policy only while such a
  release of the target is live (or named by the live pointer), approved or running
  (`egress.monitor_grants`, read-only on
  `huta.db`), resynced after every approval's outcome and every `reconcile` of such a target,
  so a grant goes when the release that needed it does. `Target.egress_allow` stays the
  registry's own list.
- The DSN is listed in the plan's `secret_versions`, so approval refuses a plan whose DSN
  changed since; `ensure` writes it under the project's secrets lock.
- Order and crashes: remote create → capability (check ids) → `monitors` row → DSN secret. A
  crash after a create is healed by the next `find` (same name); a row whose DSN secret is
  missing is repaired from the project id. A check is looked up by name before the upsert;
  only one that doesn't exist yet is created with `channels` (healthchecks gives API-made checks
  no channels otherwise), so a create whose answer was lost never overwrites channels the
  operator changed since. Any error in ensure, including its lock file or recording the event,
  stays inside it.

## Crash safety

- Every vhost change is journaled (`/var/lib/huta/edge/*.pending` plus `.prev`), validated as
  the `caddy` user and reverted on any failure. `recover()` undoes a half-done change.
- `reconcile` (run before every action, and as `huta reconcile`) treats the slot the vhost routes
  to as the truth: it repairs the live pointer, marks interrupted releases `nothing-activated`
  and stops slots nothing routes to.
- Every host helper command (docker, incus, git) runs with a deadline.
- Edge changes outside a release are plans too: `huta edge --apply` records an edge plan bound
  to what it surveyed (hosts, DNS, certificate, the installed vhost); `huta approve <id>`
  applies it if nothing changed, else it is superseded. `--now` applies at once and is recorded.
  Telegram buttons for these plans are planned (DESIGN-v2 milestone 2).
- Phase records this huta can't read, or with a stage or version it doesn't know, stop
  `reconcile` with `needs-human`; the record is kept, never cleared.
- `huta.db` carries its own version (`PRAGMA user_version`); migrations run forward, one
  transaction each, and a huta that finds a newer database refuses to run. `install.sh`
  refuses while a phase file, a running or approved release, or a held target lock exists.
- The log collector writes the lines, then its cursor (atomic rename): a crash between the two
  stores at most one run's lines twice; it never loses or exposes any.

## Commands (host, as root)

```
huta check                               validate the whole registry
huta edge <project> <target> [--apply|--now]   certificate, vhost, Cloudflare rule, A record if no
                                         A/AAAA/CNAME; --apply plans it (huta approve <id>), --now applies
huta import-dev <project> <ref>          import huta-build/<project>:<sha12> from the dev box
huta submit <project> <target> <sha> <image> [--image name=image …]
huta show <release> | reject <release>
huta approve <id> [--via cli|telegram] [--confirm-data-loss]   a release, or an edge or
                                         restore plan; restore needs --confirm-data-loss on the CLI
huta rollback <project> <target>         refused if that code can't run on the current schema
huta restore <project> <target> --to <release>   plan: data back to a restore point (loses writes)
huta reconcile <project> <target>
huta status [project [target]] | log [release]   with a target: its containers now (runstate.py)
huta logs <project> <target> [--since --until --grep --tail --slot --release --job]
huta logs-collect                        store new slot output, apply caps (huta-logs.timer, every minute)
huta egress-log|traffic <project> <target> [--since --until]   egress refusals / edge summary
huta crashes <project> <target|legacy> [--since]              Bugsink issues
huta diag <project> <target> <name>      a [[diag]] of the live release (diag.py)
huta intake                              pull: process the inbox (huta-intake.path/.service)
huta push-request                        push: one request per connection (huta-push.socket); exits 0 when
                                         it answered, refusals included; 1 only on a huta error
huta press                               one Telegram button press (huta-press.socket); a refused press
                                         exits 0, a wrong peer, bad config or huta error exits 1
huta enroll <box> --from <ip> --pubkey '<key>'   push: add or update a box, print its settings
huta revoke <box>                        push: remove a box
huta client-bundle                       the client as a .tar.gz on stdout
huta secret list|set|import|rm|discard <project> [NAME…] [--from <env file>]   set/import drop a staged value
huta secret list|set|rm _platform [NAME]   the host's own tokens (never given to a container)
huta monitor status|ensure <project> <target>   what is wired up (and the last probe) / make what is missing
huta monitor tick                        probe public health, watch windows, ping checks (huta-monitor.timer)
huta watch <release>                     the release's watch window and why
huta data status|ensure|import|export <project> <target> [dir | path…]
huta backup <project> <target>           consistent copy + ship (the timer runs it)
huta job list|run <project> <target> [name]
huta units <project> <target>            rewrite job and backup timers from the live release
huta egress sync                         refresh the egress daemon's policy
huta stop|start|restart <project> <target>
```

The `web` recipe with v1 (details in `DESIGN-v1.md`): concurrent targets start the new slot,
check health and smoke on loopback, switch, run the public checks while the old slot runs
(failure: switch back, `rolled-back`), drain, stop the old. One-writer targets (data without
host and repo both allowing concurrency) stop the old slot, copy the data, start the new one
(failure before the switch: restore the copy, start the old slot, `nothing-activated`; after:
`needs-human`). `reconcile` additionally thaws a frozen volume, starts the routed slot of the
live release if a crash left it stopped, and finishes an interrupted revert as `rolled-back`.
Migrations (DESIGN-v2 §2): a release whose `[state] schema` is higher than the target's
recorded one runs exclusive: stop every writer → copy (quiet) → pin the copy as a restore point
(`<backup dir>/<p>/<t>/pins/<release>/`, hard links, a fingerprint in `restore_points`) → phase
`migrating` → the `[migrate]` container (release image, no network, `migrate_secrets` only) →
schema recorded, phase `migrated` → start, check, switch. Any failure before the switch, or a
crash, restores the copy and the schema: nothing but the migration wrote since the copy.
Online migrations (DESIGN-v2 §6; `recipe.online_ok`): when the repo asks, the host's
`migrate_online` allows it, the live release's `runs_on` includes the new schema and the two
releases may overlap, the migration runs with the old slot serving: reference copy pinned →
phase `migrating` (`online`) → `[migrate]` → schema recorded → the release continues as an
ordinary overlapping one. Never restored automatically: a failure is `needs-human` (the repo's
`check` tells the schema when it can); reconcile waits for a migration still running, asks
`check`, stops the new slot, and records `nothing-activated`, or `needs-human` when the result
can't be known.
`health_timeout` (registry) sets how long a new slot has to get healthy.
Postgres (DESIGN-v2 §4, `pg.py`): one server per target in compose project
`huta_<p>_<t>_pg`, image from `[postgres.images]`, its own loop volume (`<t>-pg.img`, PGDATA
`pgdata-<version>`), the project uid, read-only root, internal networks `…_pga`/`_pgb`/`_pgm`
(slot a, slot b, the migration; bridges `dp-<hash>`, in the guard's `huta_intra` set). Roles
`huta_owner`, `migrate` (owns the schema) and `app` (rows only), passwords as project secrets
`HUTA_PG_<TARGET>_<ROLE>`. A release that asks for it: `PG.ensure` (create or repair; a changed
definition is refused: `huta postgres upgrade` plans it), slots join their network with
`DATABASE_URL`, `[migrate]` runs on `…_pgm` with the migrate role; copies, pins and restores are
`pg_dump -Fc` / `pg_restore --single-transaction` under `<backup>/<p>/<t>-pg/`. Jobs join the
live slot's database network as a second `--network` (Docker 25+). reconcile starts a stopped
server first; an interrupted upgrade puts the old definition back.
External publishing (DESIGN-v2 §5, `external.py`): at planning, each `[external.<name>]` dir is
copied out of its image (`docker create` + `docker cp`, tar through a validator) into
`/var/lib/huta/external/<p>/<t>/<sha12>/<name>/`, hashed, and the plan says where it goes.
After the switch, checks, workers and drain, each one runs: phase `external` (name, content
hash, the deployment before) → `E.find` (already there after a crash?) or wrangler in the
`[external] wrangler` image on network `huta_external` (bridge `dp-…`, policy target
`huta/external`, egress to api.cloudflare.com:443 only), the registry's token only, files
read-only, a generated config → `external_deploys` row. A failure leaves the site live,
`needs-human`; reconcile finishes an interrupted publish and publishes what a live release
never recorded. `huta external rollback` plans going back by deployment or version id.
Restore plans (`huta restore`) put a kept restore point back for code that can't run on the
current schema: stop marker, stop every slot, rsync the pin (its fingerprint checked again), the
point's schema recorded, phase `restoring` so reconcile redoes it after a crash. The target stays
stopped; a release of that code is planned. A release that fails on a stopped target leaves it
stopped. Plans (edge, restore) get Telegram history messages and buttons like releases; data-loss
and migration lines come first in every message.
A switch back replays the vhost files Caddy served before the switch (kept in the phase record
when the switch starts), so it needs no certificate check and no call to Cloudflare; if it
can't be done, the release ends `needs-human` with the reason. `tests/test_crash.py` kills a
release after every step, and `reconcile` after each of its own, on a fake host
(`tests/fakehost.py`) and checks these rules.

## Transports: how dev asks

Both are available and carry the same request (`src/huta/proto.py`) into the same verify → plan
→ approve path.

**Request rules (both):** one JSON object, at most 16 KB, duplicate keys refused, `v` must be
the integer `1`, fields exact per op (`submit`, `status`, `log`, `jobs`, `secret`; `credentials` push only;
`digest` required on a push submit and refused on pull), names matched in full. A request id
(16 hex) can be used **once**, across both transports; a retry needs a new id (the client makes
one per call). Every answer is `{"v": 1, "id": <id or null if unparsable>, "ts": <unix>, "ok":
..., ...}`; the client refuses an answer whose id is not its request's. Who sent a request is
never read from it. `submit`, `status`, `jobs` and `log` (through the release's project) need
the box in the project's `boxes`; otherwise the answer is the same as for a missing project or
release, so a box can't learn what else the host runs.

**pull:** for a dev box on the same host (an Incus container in v0).

- an inbox directory owned by the dev box's user (as the host maps it) and an outbox only root
  can write, both mounted into the dev box (for example `/huta/inbox` read-write, `/huta/outbox`
  read-only), plus a root-only work directory on the same filesystem (`work`, default
  `<inbox>/../work`);
- the client writes `.<id>.json.tmp` and renames it to `<id>.json`. `huta-intake.path` runs
  `huta intake`, which only accepts names `<16 hex>.json`, removes anything else (stale temp
  files after 10 minutes), handles at most 50 requests per run, one at a time, claims each by
  renaming it into the work directory before opening it (O_NOFOLLOW, regular file of the dev
  uid, size cap), and requires the id inside to equal the file name;
- the image comes from `incus exec <container> -- docker save` (15-minute deadline), checked:
  one image, plain relative entries, at most 20000 entries and 2 GiB logical size, no sparse
  files or duplicate paths; then every name is stripped and it is loaded by id;
- answers are written to the outbox (kept 7 days, at most 500).

**push:** for any dev box with ssh to the host.

- each box has its own system user `huta-<box>` in group `huta-push`, home
  `/var/lib/huta-ssh/<box>` (root-owned), `authorized_keys` line
  `restrict,command="/usr/local/sbin/huta-ssh",from="<ip>" <key>` (the key alone can't get a
  shell, even without the sshd block);
- sshd needs **no per-box change**: one static drop-in with `AllowUsers huta-*@<tailnet CIDR>`
  (on the host's existing `AllowUsers` line, if any) and `Match User huta-*` →
  `ForceCommand /usr/local/sbin/huta-ssh`, no TTY or forwarding, closed with `Match all`
  (`systemd/sshd-huta-push.conf.example`); each key is pinned to its box with `from=`;
- `huta-ssh` (as `huta-<box>`) reads one request (16 KB, 30 s) and passes it to
  `huta-push.socket` (`/run/huta/push.sock`, group `huta-push`). `huta push-request` (root)
  learns the box from the socket peer's uid (`SO_PEERCRED`), accepts it when `huta-<box>` is
  in the `huta-push` group (or the box is in the optional `[transport.push] boxes`), and
  writes exactly one JSON line back; logs go to the journal;
- `huta login` (op `credentials`) hands the box its own registry login (`secrets/registry/<box>`)
  for `docker login --password-stdin`; `huta submit` pushes to the registry and sends the
  reported digest; the host pulls `<registry_local>/huta-build/<project>@<digest>`;
- **enrolling a box** (operator, as root):
  - `huta enroll <box> --from <ip> --pubkey '<key line>'` creates the group and user if
    needed, writes the pinned key, creates the registry login, rebuilds the registry's
    htpasswd from `secrets/registry/*` and restarts the registry (compose project
    `registry_project`), then prints one JSON line:
    `{"box", "ssh": "huta-<box>@<ssh_host>", "port": <ssh_port>, "registry": <registry_public>,
    "host_key": <the host's ed25519 key>}`. Re-running replaces the key (and fixes the user's
    home, shell and group); a different key also gets a new registry password. Keys:
    `ssh-ed25519` or `ecdsa-sha2-nistp256`, parsed with `ssh-keygen -l`; `--from` a plain IP;
    box names at most 27 characters (`huta-<box>` ≤ 32). The registry restart needs exactly one
    `registry` service container in `registry_project`, or enroll fails;
  - `huta revoke <box>` cuts access first (key, push group, registry login), then ends the
    box's sessions, removes the user and its home, and restarts the registry; a box that is
    mid-request can't block it;
  - `huta client-bundle` writes the installed client (`install-client.sh` + `src/huta`) as a
    `.tar.gz` to stdout, so a box can get it with `ssh <host> sudo huta client-bundle | tar -xz`;
- **on the box:** `install-client.sh --box <box> --keygen-only` (makes `~/.ssh/id_huta_push`,
  prints the public line), then `install-client.sh --box <box> --settings <enroll JSON>`
  (refuses settings for another box; pins the host key; writes `client.toml`). The manual form
  `--ssh huta-<box>@<host> --port <port> --registry <host:port> --host-key "<key>"` still works.
  The box needs Python 3.11+, Docker, `~/.local/bin` on `PATH`, and a registry its Docker trusts
  (real TLS, or listed in `insecure-registries`). Agent-guided flow: `site/public/install.md`
  (https://huta.run/install.md).

On the dev box, `huta` (the same command, as a normal user) is the client:

```
huta pack   [--app deploy/app.toml]       # dirty tree refused; builds from git archive of HEAD
huta submit <target> [--app …] [--no-wait | --follow]   # --no-wait: pull only; --follow: until the outcome
huta status <project> <target>            # the target's last 10 releases, then its containers now
huta log    <release>                     # its last 50 events
huta jobs   <project> <target>            # live jobs, next runs, last 20 outcomes (no output)
huta logs   <project> <target> [--since 1h --until … --grep text --tail N --slot a|b|live|previous
            --release id --job name]       # redacted container output (or a legacy unit's journal)
huta egress-log|traffic <project> <target> [--since 1h --until …]
huta crashes <project> <target|legacy> [--since 1d]
huta diag   <project> <target> <name>
huta secret set <project> <NAME>          # value from stdin; staged until an approved release
huta secret import <project> NAME… --from <env file>
huta login                                # push only
huta self-update                          # replace this client with the host's version now
```

with `~/.config/huta/client.toml` (`transport = "pull"` plus `[pull] inbox, outbox`, or
`transport = "push"` plus `[push] registry, ssh, port, key, known_hosts`; optional top-level
`auto_update = false`).

**Secrets from a box.** Op `secret` {project, name, value} (both transports; the value is one
line of 1..8192 characters with no control characters, so line-by-line job-log redaction
catches it; multi-line secrets are set by the operator; never quoted in an error). The box must
be in the project's `boxes`, and the name must already be in the registry (`secrets` or
`job_secrets` of any target): a box can't add names, read values or reach other projects. The
host writes the value root-only to `secrets/<p>/.staged/<NAME>` (unique `O_EXCL|O_NOFOLLOW`
temp file, rename, directory fsync; over pull the client writes the request 0600 and removes
it if the host never claims it) and records an event with the box but not the value. Nothing
reads staged values except planning and approval.

Every step that touches a project's secret files (staging, the operator's `secret` commands,
planning, approval from its checks until the release's outcome, settling) holds the project's
**secrets lock** (`Store.lock(<p>, "_secrets")`, taken after the target lock). So no value
changes between a plan's checks and the moment its containers read their environment. A box
that sends a value while a release of the project runs waits up to 20 s, then is told to send
it again.

- `plan_release` treats a staged value as set (a first release needs no operator step), lists
  `🔑 new value for secret <NAME>, sent by a dev box <time>` in the plan's changes (so the plan
  is `infra`; the line names the project's other targets that use the name), and stores
  `staged_secrets` = {name: inode:mtime_ns:size} and `secret_versions` (the same fingerprint of
  every live secret the target uses).
- `huta approve` supersedes the plan when a live fingerprint differs (another target's release
  applied a value, or the operator set one). Before recording the approval it **claims** every
  planned file (rename to `.claim-<rid>-<NAME>`; a value a box sends meanwhile lands beside it
  and stays staged) and checks each fingerprint on the claimed file; on a mismatch the claims
  go back and the plan is superseded (the approval isn't used up). After the approval is
  recorded, each claimed value goes live with the old one kept aside first
  (`.prev-<rid>-<NAME>`, a hard link, or `.none-<rid>-<NAME>` when there was none), with
  directory fsyncs and an event before and after.
- **Values go live with the release or not at all.** After the outcome, `live` and
  `needs-human` (the new slot may be serving) keep the new values and drop the old copies;
  `nothing-activated` and `rolled-back` put the old values back and stage the new ones again
  (unless a newer one was sent meanwhile), with an event. While the release runs, the
  project's jobs already read the new values.
- **Crash:** the journal files are named by release id. `reconcile`, `submit` and `rollback`
  settle every journal whose release has an outcome (`settle_journal`, under the secrets
  lock, so no apply is in progress): kept for `live`, `retired`, `needs-human`; reverted for
  `nothing-activated`, `rolled-back`, `rejected`, `superseded` and `planned` (claims taken
  before a crash during approval); left alone while the release is still `approved` or
  `running` until its own target's `reconcile` decides it. A crash between keeping the old
  value and the rename is told apart by inode, so the old value is never staged as new.
- A project's secrets are shared by its targets: a value applied by one target's release
  reaches the other targets' jobs at their next run (the plan says so), and their pending
  plans can no longer be approved.
- The operator's `huta secret set/import` drop a staged value for the same name (theirs is
  newer); `huta secret discard` drops one and keeps the live value; `list` shows staged ones.
- Redaction (`cfg.redact`) reads every live value under `secrets/` but **not** staged values,
  journals or temp files (dot names): a box chooses those, and as needles they could blank out
  plan text the owner must read. Every value `read_secret` returns for use is added to the
  process's cache, so a value applied by another process after the cache loaded is still
  redacted. Each value becomes several needles (`cfg.needles`): its parts (entries split on
  whitespace `, ; & |`, `k=v` values, every URL userinfo found anywhere in the value, its
  password) and each of those URL-encoded, JSON-escaped and base64 at all three alignments;
  under 8 characters a part is skipped, except the context-bound forms `:pass@`, `user:pass@`
  and the base64 of `user:pass` (from 3 characters) and a bare `user:pass` (from 6).
- A live value that is replaced or removed (`huta secret set/import/rm`, an approved release's
  apply, a revert) is first hard-linked into `secrets/.retired/<p>.<NAME>.<unix time>.<rand>`
  (`runtime.retire`); `cfg.redact` keeps those as needles for 30 days, then they are deleted.
  These were live, so a box didn't choose them.

**Client updates.** Every answer carries `"huta": "<host revision>"` (`/opt/huta/REVISION`). The
client records its own in `~/.local/lib/huta-client/REVISION` (written by `install-client.sh`).
After any command, if they differ, the client sends op `client` (both transports): the host
answers with the same bundle as `huta client-bundle` plus `REVISION`, base64 with its sha256.
The client checks the hash and that every tar entry is a regular file in a fixed list
(`src/huta/*.py`, `install-client.sh`, `REVISION`), unpacks next to the install and swaps the
directory in (two renames). With `auto_update = false` it only prints
`huta: the server runs <x>, this client <y>; update with huta self-update`. Only per-user
installs update themselves (never `/opt/huta`). The client trusts the host already: a pinned
ssh host key (push) or a root-owned read-only outbox (pull).

## Notifications

With `[telegram]`, the host keeps **one history message per release** (HTML) and edits it as
the release moves: waiting for approval (with the approve command and the expiry), approved /
deploying, live (hosts and slot), not activated / needs a human / rejected / expired (with the
reason), replaced. Failures to notify never affect a release. Without `[telegram]`,
`platform.notify` (an argv that gets the text on stdin) is told when a plan waits.

## One-tap approval

With `history_chat`, `owner_id`, `relay_user` and `buttons = true`, every new plan (submit over
either transport, CLI submit, rollback) gets two inline buttons on **its history message in the
configured topic** (`notify.ask_owner` edits that message's `reply_markup`). One message per
release, in one place:

- `callback_data` is `huta:a:<22 chars>` (Deploy) / `huta:r:<22 chars>` (Reject): 128 random
  bits each, single use, expiring with the plan (30 min), stored only as SHA-256 hashes
  (`approvals` table, with the message's chat and id).
- The chat gateway (OpenClaw) receives the press. The `huta-press` plugin
  (`openclaw-plugin/`, deterministic, no LLM, never logs the data) claims the `huta`
  namespace and writes `{v, data, from_id, chat_id, message_id}` to `/run/huta/press.sock`
  (root, group `huta-press`, 0660). `huta-press.socket` runs `huta press`, which also checks
  the peer is `relay_user` (`SO_PEERCRED`).
- `notify.take_press` checks: presser is `owner_id`; chat is `history_chat` (or `owner_id`);
  the hash matches an open plan and is consumed atomically; the press came from that plan's
  own message (same chat and message id); not expired (a late press marks the release `superseded`). Deploy runs
  `huta approve <id> --via telegram` as a transient systemd unit (same checks as the CLI);
  Reject rejects. The message is edited as the release moves; the keyboard is removed when the
  plan is decided. CLI approve/reject and expiry retire the buttons (`close_ask`).
- Safe in a group against other members and bots: only `owner_id` is accepted, and only huta's
  own message carries the secret, so a lookalike button can't approve anything.
- **Known limit:** huta trusts `from_id` from the relay. The secrets sit in the message's
  keyboard (visible to the bot account and chat clients) and the gateway may store updates
  durably, so anything running as `relay_user` (for example the chat bot's own AI agents) can
  read a Deploy string and forge an owner press. Operators accept this when they enable
  `buttons`. Planned: a receiver only huta controls (its own bot token, root-only), so presses
  don't pass through the chat agent.
- The plugin replies in chat only on refusal (`this button is no longer valid`,
  `only the owner can approve`, `this button doesn't belong to this chat`,
  `not a huta button`, `this plan expired; submit again`) or if huta is unreachable
  (`huta is unavailable; use sudo huta approve on the host`). OpenClaw answers the callback
  itself, so there is no toast.
- Operator setup: install `openclaw-plugin/` as a local plugin (its README), add the gateway
  user to `huta-press`, restart that user's systemd manager (a service restart keeps the old
  groups), then set `buttons = true`. Until then the buttons stay off: an unclaimed press would
  reach the chat agent as text.

The CLI `huta approve` remains the fallback.

## Not yet

Unattended releases, plain (non-Cloudflare) TLS, read-only debug access for agents beyond what
exists (`snapshot`), monitoring hooks beyond their remote resources (docs/PLAN-monitoring.md),
plain-HTTP egress.

v2 is built (`DESIGN-v2.md`, `BUILD-v2.md`): several containers per target, Postgres per
target, migrations with schema versions (exclusive and online), gateways, and publishing to
Cloudflare Pages and Workers; targets that use none of it run exactly as in v1. Not yet in v2:
a data volume and a database on one target together, `run.huta.release` labels, a restore
drill for database dumps, `--component` on `status` and `diag` (logs and jobs have it).
