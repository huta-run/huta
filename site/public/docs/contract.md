# The contract: `deploy/app.toml`, the image, the host registry

Three pieces decide how a release runs:

| Piece | Who writes it | Where |
|---|---|---|
| `deploy/app.toml` | you (the repo) | the repo, read by the host **from GitHub at the release sha** |
| the image | you (built on dev) | `huta-build/<project>:<sha12>` in dev's Docker |
| the host registry | the operator | `/etc/huta/projects.d/<project>.toml` on the host, root-owned |

The repo asks, the host decides: users, ports, hostnames, limits, secrets, data size, egress and
policy come only from the host registry. Nothing in the repo can widen them. What the repo
declares (a data mount path, smoke checks, jobs) runs inside those limits.

What each runtime feature means for your app (proxy variables, per-slot files, how jobs run,
what is backed up) is in [Runtime](runtime.md). Moving a site that already runs somewhere:
[Moving a live site](moving.md).

## `deploy/app.toml`

Data only. Strict:

- **top level:** only the keys in the table below; anything else is refused;
- **`[components.<name>]`**, **`[images.<name>]`**, **`[state]`**, **`[build]`**,
  **`[[smoke]]`**, **`[[jobs]]`**, **`[[diag]]`:** only their listed keys; anything else is refused;
- `preflight` and `external` are known keys but must be absent or empty (*planned*).

### TOML pitfall: top-level keys first

In TOML, every `key = value` after a `[table]` header belongs to that table. So this is wrong:

```toml
schema = 1
project = "myapp"

[components.web]
image = "web"
order = ["web"]     # WRONG: this is components.web.order
```

huta refuses it with:
`app.toml: components.web has unknown keys ['order'] (top-level keys must come before the first [table])`.
A misplaced `schema` or `project` is reported differently, because those are checked first:
`app.toml: schema must be 1` or `app.toml: project must be 'myapp'`. Put `schema`, `project`
and `order` at the top, before any `[...]` line.

### A stateless file

```toml
schema = 1
project = "myapp"
order = ["web"]

[images.web]
dockerfile = "Dockerfile"
context = "."

[components.web]
image = "web"
kind = "http"
port = 8080
health = "/healthz"

[state]
kind = "none"
```

### A file with data, checks and a job

```toml
schema = 1
project = "myapp"
order = ["web"]

[images.web]
dockerfile = "Dockerfile"

[components.web]
image = "web"
port = 8080
health = "/healthz"
data = "/data"              # the target's data volume is mounted here, read-write
concurrent_safe = false     # true only if two releases may write the same data at once

[state]
kind = "data"

[[smoke]]                   # run on the new slot before the switch, and publicly after it
path = "/"
status = 200
contains = "<title>myapp</title>"
headers = { "content-security-policy" = "frame-ancestors" }

[[smoke]]
path = "/"
request_headers = { accept = "text/markdown" }
headers = { "content-type" = "text/markdown" }
headers_absent = ["x-robots-tag"]   # the markdown twin must not be noindex

[[jobs]]
name = "rotate"
cmd = ["node", "jobs/rotate.mjs"]
schedule = "*-*-* 03:10:00"   # systemd OnCalendar
timeout = "10m"
```

### Top-level keys

| Key | Type | Notes |
|---|---|---|
| `schema` | int | required, must be `1` |
| `project` | string | required; must equal the project name in the host registry |
| `order` | array of strings | required; every component exactly once, no duplicates |
| `images` | table of tables | required; 1..8 `[images.<name>]` |
| `components` | table of tables | required; 1..8 components, exactly one of them the public HTTP one (below) |
| `state` | table | optional; `kind` (`"none"` default, or `"data"`), and with data `schema` and `runs_on` (below) |
| `migrate` | table | optional; a migration this release runs before its code starts (below) |
| `build` | table | optional; only `test` (accepted; `huta pack` does not run it yet) |
| `smoke` | array of tables | optional; at most 20 checks (below) |
| `jobs` | array of tables | optional; at most 20 jobs (below) |
| `diag` | array of tables | optional; at most 20 diagnostics (below) |
| `preflight` | array of tables | must be absent or empty (*planned*) |
| `external` | table | must be absent or empty (*planned*) |

Any other top-level key: `app.toml: unknown top-level keys ['<key>']`.

### `[images.<name>]`

Name: `^[a-z][a-z0-9-]{0,30}$`. Only these keys:

| Key | Type | Default | Notes |
|---|---|---|---|
| `dockerfile` | string | required | path relative to the repo root |
| `context` | string | `"."` | build context, relative to the repo root |

### `[components.<name>]`

Name: `^[a-z][a-z0-9-]{0,30}$`. Only these keys:

| Key | Type | Default | Notes |
|---|---|---|---|
| `image` | string | | required: must name one of the `[images.<name>]` tables |
| `kind` | string | `"http"` | `"http"`, or `"worker"` (serves nothing: no `port`, `health` or `public`) |
| `public` | bool | `false` | the component the site routes to; needed only when there is more than one HTTP component |
| `cmd` | array of strings | | the command (argv, no shell) instead of the image's own; the image's entrypoint still applies |
| `port` | int | `8080` | 1..65535; the port your server listens on **inside** the container, on `0.0.0.0` |
| `health` | string | `"/healthz"` | must match `^/[A-Za-z0-9/_.-]*$` |
| `data` | string | | with `state.kind = "data"`: the absolute mount path, `^/[a-z0-9][a-z0-9/_.-]*$`, not a system directory (`/etc`, `/tmp`, `/usr`, …). Only components that name it get the volume; with `kind = "data"` at least one must, without it none may |
| `concurrent_safe` | bool | `false` | `true`: two releases may run on the same data at once. It only takes effect when the host allows it too (`data.concurrent` in the registry) |
| `upstream_api` | string | | must be absent or empty (*planned*) |

### Several components

A target can run up to 8 components from up to 8 images: a web front, an API, a worker.

```toml
order = ["web", "api", "worker"]          # start order

[images.web]
dockerfile = "web/Dockerfile"
[images.api]
dockerfile = "api/Dockerfile"

[components.web]
image = "web"
public = true                             # the site routes here
[components.api]
image = "api"
port = 8081
data = "/data"
[components.worker]
image = "api"
kind = "worker"
cmd = ["python", "-m", "app.worker"]
data = "/data"
```

- **A slot is the whole set.** Slot a is web + api + worker of one release, on its own network:
  the components reach each other by name (`http://api:8081`). The other slot's release can't
  reach them. The switch moves everything at once, so versions never mix.
- Only the public component is published (on the slot's loopback port, as with one component).
  Every other HTTP component must answer its `health` path with `ok` and the sha too; the host
  checks it on its slot address before the switch.
- **Workers are single.** A new release's workers start only after the switch and the public
  checks, once the old release's workers have stopped. They must still be running after the
  host's `worker_liveness` (default 10 s); otherwise the release goes back (when the old slot is
  still there) or ends `needs-human`.
- One writer is per volume: every component that mounts the data counts. Two releases overlap
  only if all of them are `concurrent_safe` and the host allows it.
- The host's secrets reach the public component; other components get only what the registry
  names for them (`[targets.<t>.components.<c>]`). Jobs and diagnostics take
  `component = "<name>"` (default: the public one) for their image and mount.
- `huta pack` builds every image (each labelled `run.huta.image=<name>`); the public
  component's image keeps the name `huta-build/<project>:<sha12>`, the others are
  `huta-build/<project>/<image>:<sha12>`. `huta submit` sends them all.

### `[state]`

`kind`: `"none"` (default), `"data"` or `"postgres"` (below). With `"data"` the host gives the target its own
filesystem (a fixed size, set by the operator), mounted at `components.<c>.data`, backed up on a
schedule. A plan whose app.toml asks for data while the host registry gives the target none is
refused: `app.toml mounts data at /data, but the host registry gives myapp/prod no data`.
With `"postgres"` the release uses the target's own database server (the host must grant
`postgres` in the registry):

- huta runs one Postgres per target, from an image the operator pins by digest, on its own
  volume and its own internal networks (no route out). Your containers reach it as
  `postgres:5432`, slot a and slot b each on their own network to it.
- Your components get `DATABASE_URL` (`postgresql://app:…@postgres:5432/app`). The `app` role
  may read and write rows but not change the schema: create and change tables in `[migrate]`,
  which gets the `migrate` role (it owns the schema) and reaches only the database.
- Releases without a migration overlap as usual (both slots on the database); a migration runs
  exclusive, with a dump pinned as its restore point, as for data.
- Backups are `pg_dump` files on the target's schedule, shipped like data copies.
- A data volume and a database on one target together are not supported yet.

With data, two optional keys version it:

```toml
[state]
kind = "data"
schema = 4            # the schema this release leaves the data at (1..1000000)
runs_on = [3, 4]      # schemas this code runs on; must include schema (default: [schema])
```

The host records the schema the data is at. At plan time: the same schema runs as usual; a
higher one needs `[migrate]`; a lower one is refused (going back is a restore, never a migration
down); a target whose data has a schema refuses a release that declares none. The first release
that declares a schema without `[migrate]` just starts the record.

### `[migrate]`

```toml
[migrate]
image = "web"                                 # one of [images]
cmd = ["python", "manage.py", "migrate"]      # argv, no shell; the image's entrypoint applies
timeout = "10m"                               # default 10m, at most 6h
```

When a release raises the schema, huta runs it **exclusive**: every writer stops (the site
answers 502 until the switch), the data is copied and the copy is pinned as a restore point for
this release, then `cmd` runs once in its own container from that image: the target's limits and
data volume, **no network**, and only the secrets the operator lists in `migrate_secrets`. Its
environment also has `HUTA_SCHEMA_FROM` and `HUTA_SCHEMA_TO`. Exit 0 within `timeout` records the
new schema; then the new code starts and is checked as usual.

- The migration fails, or the new code fails its checks before the switch: the copy goes back,
  the schema is unchanged, the old release runs again: `nothing-activated`. Nothing is lost,
  because nothing but the migration wrote since the copy.
- huta dies mid-way: `huta reconcile` does the same (whether the migration committed doesn't
  matter: the copy goes back).
- The public checks fail after the switch: the new code keeps serving (`needs-human`), as for
  any one-writer release; going back means a restore.
- `online = true` (migrating while the old release serves) and a result `check` are *planned*.

Going back across a schema: `huta rollback` refuses code whose `runs_on` doesn't include the
data's current schema. `huta restore <project> <target> --to <release>` plans putting back the
restore point pinned before the migration (huta keeps the newest `keep_releases`, at least 2),
loses every write since, and needs `--confirm-data-loss` or the Telegram button; huta checks the
point's fingerprint at approval. The target then stays stopped until the release of that code,
which is planned for you, is approved.

### `[[smoke]]`

HTTP checks, data only. Each check is a GET; redirects are not followed (a 3xx can be the
expected status); bodies are read up to 1 MiB; 10 s each.

| Key | Type | Default | Notes |
|---|---|---|---|
| `path` | string | required | starts with `/`; plain URL characters (query strings allowed) |
| `status` | int | `200` | the exact status expected |
| `contains` | string | | a substring the body must contain (at most 500 characters) |
| `headers` | table | | response header name → a substring its value must contain |
| `headers_absent` | array of strings | | response header names that must **not** be present (for example `["x-robots-tag"]` on a page that must stay indexable) |
| `request_headers` | table | | sent with the request; not `Host`, `Content-Length`, `Transfer-Encoding`, `Connection` or `X-Forwarded-*` |

When they run: on the new slot over loopback **before** the switch (with `Host:` the target's
first host and `X-Forwarded-Proto: https`; any failure ends the release `nothing-activated`
and nothing changed), and through Cloudflare at `https://<host>` **after** the switch (see
[Runtime](runtime.md#smoke-checks-and-automatic-revert) for what a failure does then).

### `[[jobs]]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | `^[a-z][a-z0-9-]{0,30}$`, unique |
| `cmd` | array of strings | required | the command (argv) run in your image; no shell unless you call one |
| `schedule` | string | required | a systemd OnCalendar expression (`hourly`, `*-*-* 03:10:00`, `Mon *-*-* 04:00`); checked with `systemd-analyze calendar` |
| `timeout` | string | `"10m"` | `90s`, `10m`, `2h`; at most `6h`; the job is killed after it |
| `after_release` | bool | `false` | also run once each time a release of this target goes live (a rollback included), after it has finished; the plan says so |

A job runs in the **live** release's image, with the same user, limits, environment, secrets,
data mount and egress as the web container. Details: [Runtime](runtime.md#jobs).

### `[[diag]]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | `^[a-z][a-z0-9-]{0,30}$`, unique |
| `cmd` | array of strings | required | the command (argv) run in your image |
| `timeout` | string | `"1m"` | `90s`, `5m`; at most `5m` |

A diagnostic a dev box runs by name with `huta diag <project> <target> <name>`: in the live
release's image, **without network or secrets**, with the data's **last backup copy** mounted
read-only at the data path. Its output (256 KiB at most) passes the host's log redaction.
Adding or changing one is a change the plan lists. Details: [Runtime](runtime.md#diagnostics).

```toml
[[diag]]
name = "db-check"
cmd = ["node", "scripts/db-check.js"]   # integrity check, migration version, row counts
timeout = "2m"
```

## The image

### Enforced (the host refuses the release otherwise)

| Rule | Exact requirement |
|---|---|
| name on dev | `huta-build/<project>:<first 12 hex of the sha>` (`huta pack` sets it) |
| label | `org.opencontainers.image.revision=<full 40-hex sha>`, equal to the submitted sha (`huta pack` sets it) |
| label | `run.huta.project=<project>` (`huta pack` sets it) |
| user | `USER` set and not `root`, `0` or empty |
| platform | `linux/amd64` (`huta pack` always builds it) |
| no `VOLUME` | the image declares no `VOLUME` (Docker would attach unbounded writable storage); persistent data is the data volume |
| pull archive | one image; `docker save` output within 15 min, at most 2 GiB logical size and 20000 entries, no sparse files or duplicate paths |
| push | pulled by the exact digest you pushed |
| listen | the server listens on `0.0.0.0` at the `app.toml` `port`. Bound to `127.0.0.1` inside the container it never answers the host, and the release ends `nothing-activated` with `never reported <sha12>` |
| `/healthz` | answers within 30 s of start with `{"ok":true,"sha":"<full sha>"}` (details below) |

### Recommended (patterns, not checked)

- `ARG RELEASE_SHA` used to stamp `/healthz` at build time (`huta pack` passes
  `--build-arg RELEASE_SHA=<sha>`), or read `RELEASE_SHA` from the environment at run time;
- a numeric `USER` such as `20001:20001`;
- `LABEL` lines for the two labels too, so a plain `docker build` matches;
- base images pinned by digest; multi-stage builds; a small final image.

### How the container runs (generated by the host, not configurable from the repo)

- as the project's uid from the host registry (`user: <uid>:<uid>`), **not** your `USER`.
  Make files readable by any user; do not depend on file ownership;
- read-only root filesystem; `/tmp` is a tmpfs (16 MB unless the operator sets `limits.tmp`);
  the data volume (if any) is the only other writable place;
- `cap_drop: ALL`, `no-new-privileges`;
- memory, CPU and pids limits from the host registry (defaults `256m`, `1`, `128`);
- environment: the host registry's `env` and `secrets`, then `RELEASE_SHA`, `HUTA_PROJECT`,
  `HUTA_TARGET`, `HUTA_SLOT` (`a` or `b`; `job` in a job, plus `HUTA_JOB`), and the egress
  variables when the target has egress ([Runtime](runtime.md#egress));
- **no DNS**: names don't resolve inside the container (Docker's resolver would otherwise be a
  way out). Outbound HTTPS goes through the egress proxy, which resolves names itself;
- the container port is published on a loopback port of the host only; the host's web server
  proxies to it;
- each target gets its own Docker bridge with **no route out**; the only thing it can reach is
  the host's egress daemon, and only for what the target is allowed;
- the old slot gets SIGTERM after the switch plus `drain` seconds, then is killed 10 s later.

### `/healthz`

After start, the host polls `http://127.0.0.1:<slot port><health>` for up to **30 s**. It passes
only when:

- the status is 2xx (redirects are **not** followed: a redirect fails);
- the body is at most 64 KiB of JSON;
- `ok` is JSON `true` and `sha` equals the full 40-hex release sha.

```json
{"ok":true,"sha":"0123456789abcdef0123456789abcdef01234567"}
```

Serve it with `Cache-Control: no-store`: after the switch the host fetches
`https://<host><health>` through Cloudflare and expects the new sha.

Keep it shallow: don't make it depend on an outside API, or that API's outage blocks releases.

### HTTP behaviour belongs in the image

The generated vhost does TLS, compression, a Cloudflare-only IP filter, and a `reverse_proxy` to
the live slot. Nothing else. Your headers (CSP, caching), redirects, rewrites and 404 pages go
in your own server's config inside the image. What the vhost passes to your app:
[Runtime](runtime.md#what-the-web-server-passes-to-your-app).

## Host side: registry (the operator writes this)

You cannot install it. When a project or target is new, give the operator this file, filled
in. File: `/etc/huta/projects.d/<project>.toml`.

```toml
schema = 1
project = "myapp"
repo = "example/myapp"           # GitHub owner/name; the host reads it with a read-only token
app_toml = "deploy/app.toml"     # path of the manifest in the repo
boxes = ["mybox"]                # dev boxes that may submit it and read its status, log, jobs, logs
uid = 20001                      # 20000..29999, unique across projects
log_drop = ["enter setup code"]  # optional: log lines holding one of these are never stored

[legacy.myapp-old]               # optional: a service of this project that huta doesn't run
unit = "myapp.service"           # its journal is readable with huta logs myapp myapp-old
env_file = "/opt/myapp/.env"     # its values are redacted too; unreadable: nothing is shown
crashes = 12                     # optional: its Bugsink project id (huta crashes)

[targets.prod]
hosts = ["myapp.example.org"]    # must be in a Cloudflare zone the host allows
cloudflare = "orange"            # "orange" only
slots = [18300, 18301]           # two free loopback ports, 1025..65535, unique on the host
limits = { memory = "256m", cpus = 1, pids = 128, tmp = "64m" }
approve = "always"
require_ci = true
keep_releases = 5
drain = 10                       # seconds the old slot keeps running after the switch

# Optional, as the project needs:
env = { PUBLIC_ORIGIN = "https://myapp.example.org" }   # not secret
secrets = ["API_TOKEN"]          # names; values set with `huta secret set` on the host
job_secrets = { watch = ["CHAT_TOKEN"] }   # secrets only that job gets, never the web container
egress = ["api.example.com:443", "*.cdn.example.net:443"]   # or "none" (default)
upstreams = ["vpn-exit"]         # named host services from huta.toml [upstreams.*]
data = { size = "2g", backup = "hourly", concurrent = false }
access_log = true                # false: no access log for this site
logs = { days = 7, max = "200m", boxes = true, jobs = false }   # huta logs (these are the defaults)
crashes = 11                     # its Bugsink project id, for huta crashes (needs [crashes] in huta.toml)
adopt = "myapp.example.org.caddy"   # only when moving a live site (moving.md)
```

`boxes` names the dev boxes that may use this project: pull, the dev container's name; push,
the name the box was enrolled under. Any other box gets `no project '<name>' for box <box>`,
the same answer as for a project that doesn't exist, for submit, `status`, `log`, `jobs` and
`logs`.
Without `boxes`, only the host's own CLI can act on the project. Several projects on one box
see each other: the box is the unit huta can tell apart.

| Field | Type | Default | Checked |
|---|---|---|---|
| `schema` | int | | must be `1` |
| `project` | string | | must equal the file name |
| `repo` | string | | required |
| `app_toml` | string | | required |
| `uid` | int | | 20000..29999, unique across projects |
| `log_drop` | array | `[]` | up to 32 strings of 3..200 characters, matched case-insensitively; a log line holding one is never stored or shown (for values the app makes at run time, such as a pairing code) |
| `targets.<name>` | table | | name `^[a-z][a-z0-9-]{0,30}$`; **unknown keys refused** |
| `hosts` | array of strings | | valid hostnames, unique across the registry |
| `slots` | array of 2 ints | | 1025..65535, unique across the registry |
| `cloudflare` | string | `"orange"` | `"orange"` only (Origin CA certificate, Cloudflare-only filter); plain mode with ACME is *planned* |
| `approve` | string | `"always"` | `"always"` only; unattended releases are *planned* |
| `limits` | table | `{memory="256m", cpus=1, pids=128, tmp="16m"}` | only these four keys; `tmp` 1m..4g (it counts as memory) |
| `require_ci` | bool | `true` | the newest run of the GitHub Actions workflow named `ci` for the sha, on `push` to `main`, is completed with success |
| `keep_releases` | int | `5` | old images beyond this are untagged |
| `drain` | int | `10` | 0..300 seconds |
| `env` | table | `{}` | names `^[A-Z][A-Z0-9_]{0,63}$`, not `HUTA_*`, proxy or loader variables; one-line string values |
| `secrets` | array | `[]` | names as for `env`, not also in `env`; every secret must be set or the plan is refused |
| `job_secrets` | table | `{}` | job name → secret names given **only** to that job's runs (not to the web container); names as for `secrets`, not also in `env` or `secrets` |
| `egress` | `"none"` or array | `"none"` | entries `host:port`, `*.suffix:port`, `*:port` or a literal `ip:port` (`[v6]:port`) |
| `upstreams` | array | `[]` | names defined in `huta.toml` `[upstreams.<name>]` |
| `data` | table | | `size` 64m..512g (required), `backup` `hourly`/`daily`/`off`/an OnCalendar (default `daily`), `concurrent` bool (default `false`). Needs `[backup]` in `huta.toml` (its directory holds the copies; `backup = "off"` only turns off the schedule and shipping) |
| `access_log` | bool | `true` | |
| `crashes` | int | | the target's Bugsink project id (`huta crashes`) |
| `legacy.<name>` | table | | project level: `unit` (`*.service`), `env_file` (absolute; required), `crashes` (optional); the name may not also be a target |
| `logs` | table | `{days=7, max="200m", boxes=true, jobs=false}` | `days` 1..90, `max` 16m..20g; `boxes`: the project's boxes may read the slots' logs; `jobs`: and the jobs' output |
| `adopt` | string | | a file name in Caddy's sites directory (`*.caddy`) |
| `health_timeout` | int | `30` | 5..900 seconds a new slot has to report healthy on `/healthz` |
| `migrate_secrets` | array | `[]` | secret names `[migrate]` gets (it gets no others) |
| `components.<name>` | table | | for a component of an app with several: `limits`, `env`, `secrets` (the target's own secrets reach only the public component) |
| `postgres` | table | | `version` (a name in `huta.toml` `[postgres.images]`), `size` (256m..2t), `backup` (`hourly`/`daily`/`off`/an OnCalendar, default `daily`), `limits` (default 1g memory, 2 cpus). Needs `[backup]` in `huta.toml`. A target named `<this>-pg` can't exist next to it |
| `worker_liveness` | int | `10` | 1..300 seconds a new worker must stay up after the switch |
| `edge` | table | `{}` | `body` (request body limit, 1m..64g) and `timeout` (upstream read/write timeout, like `90s`, `10m`, `2h`; at most 24h). Unset: nothing is added to the vhost |
| `kind` | string | `"app"` | `"gateway"`: a hostname huta proxies to a service it doesn't run (below) |

Before the first release the operator runs `huta edge <project> <target> --apply` (and approves the plan)
(certificate, vhost answering 503 `not released yet`, Cloudflare per-host SSL rule; with
`adopt`, only the certificate: the old site keeps serving). If a host has no address record (A,
AAAA or CNAME; MX, TXT and the like don't count) and no covering wildcard, `--apply` creates a
**proxied A record** to the host's
`platform.public_ipv4`. It never edits or deletes an existing record.

### Gateways: a hostname for a service huta doesn't run

A target with `kind = "gateway"` runs nothing. huta does its edge exactly as for an app (DNS
record, Origin CA certificate, strict SSL rule, a vhost with the Cloudflare-only filter and the
access log, plan and approval, journaled change, `huta traffic`) and proxies to an upstream the
operator names. It doesn't build, release, check or roll back that service.

```toml
[targets.media]
kind = "gateway"
hosts = ["media.example.org"]
gateway = { to = "100.64.0.5:8080", secret = "GATEWAY_KEY", header = "X-Gateway-Key" }
edge = { body = "1g", timeout = "10m" }
```

- `to` is an IP address and port in loopback, private (10/8, 172.16/12, 192.168/16), CGNAT
  (100.64/10) or ULA (fc00::/7) space. Names and public addresses are refused: the host never
  proxies to the internet.
- `secret` and `header` (together, optional): huta sends that header with the secret's value to
  the upstream. The value lives in its own file (`/etc/caddy/huta-secrets/<p>--<t>.caddy`,
  root and the `caddy` group only) that the vhost imports; the vhost itself never holds it.
  Values with quotes, braces, `$` or spaces are refused. Set it with `huta secret set`.
- A gateway accepts only `kind`, `hosts`, `gateway`, `edge`, `cloudflare` and `access_log`. A
  project of gateways only needs no `repo`, `app_toml` or `uid`.
- `huta edge <p> <t> --apply` (then `huta approve <id>`) creates or changes it. Release
  commands (`submit`, `status` of a target, `stop`, `logs`, jobs) refuse a gateway.
