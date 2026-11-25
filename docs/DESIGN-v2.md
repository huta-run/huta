# huta v2: several containers, Postgres, migrations, gateways, external publishing

Status: **planned**, being built in the order of `docs/BUILD-v2.md`. Each section says what is
built; anything not marked built is planned. Reviewed as a plan by three independent reviewers
(2026-11-25) before any code; their findings are folded into the text, and the decisions they
split on were made by the operator (listed at the end).

v1 runs one HTTP container per target, with secrets, egress, a data volume, jobs and smoke
checks. Real projects also need: a worker or a second service next to the web container, a
database, schema migrations that can fail safely, a public hostname for a service huta doesn't
run, and a static site or edge worker published to Cloudflare. Everything here is general: no
project gets a special case.

The v1 rules hold unchanged: **the repo asks, the host decides**; huta generates every compose
file and vhost and never runs repo-supplied compose, Caddy config or shell as root; every action
ends in a recorded outcome (`live`, `nothing-activated`, `rolled-back`, `needs-human`); and
`reconcile` finishes or undoes whatever a crash interrupted.

## Compatibility: v1 targets don't change

A target that uses no v2 feature runs byte for byte as before: the same compose document, the
same vhost, the same plan text, the same records. `tests/golden/` pins that output and every
step's tests run against it.

- **v1 is v2 with one component.** One parser reads every `app.toml`, old or new, into one
  release description; a v1 manifest becomes one image, one public HTTP component, no schema,
  no migration. There is no separate v1 code path.
- **Every feature is opt-in twice** (gateways once): the repo asks in `app.toml`, the registry
  grants it. Without both, nothing about the target changes.
- **Formats only grow.** `app.toml` stays `schema = 1` with optional new keys (an older host
  refuses them by name, so a v2 repo fails cleanly there). The request stays `v = 1` with an
  optional `images` map, sent only by projects with more than one image. `huta.db` gains
  columns and tables, never renames or drops. Phase records gain a version for new stages.
- **No silent downgrade.** `huta.db` carries a schema version; a huta that finds a newer one
  refuses to run instead of misreading it.
- New vhost knobs default to today's output, so no vhost is rewritten by an upgrade.
- Backup layout (`current/`, `previous/`, `last.json`) stays as it is; new restore points live
  beside it, so tools that read it keep working.

## 0. Foundation

What every later step stands on.

- **Database versioning.** `PRAGMA user_version` with forward-only migrations in `store.py`.
  A newer database than the code knows: huta refuses to start and says which version it found.
- **Install guard.** `install.sh` refuses while any target has a phase file, a running or
  approved release, or a held target lock.
- **Release records v2.** Releases gain `images` (name → image id; a v1 release is a map of one)
  and `schema` (the data schema this release leaves). `image_id` keeps its meaning: the public
  component's image, so reconcile's match, rollback and history keep working.
- **Resources.** A `resources` row per stateful thing a target has (its data volume, its
  Postgres): kind, current schema, and the release that last changed it. **Restore points**:
  a copy or dump taken for one release, pinned (never rotated away by the hourly backup),
  with a fingerprint (sha256 of the copy's manifest or the dump file), kept for the last
  `keep_releases` migrations.
- **Plans that aren't releases.** A `plans` table for *infra* plans (edge changes, gateways,
  Postgres upgrades) and *restore* plans, approved like releases (same buttons, same expiry),
  bound to what they change (the resource revision, the live release).
- **Phase records v2.** New stages carry `"v": 2` and a stage name. `reconcile` resolves every
  stage it knows; one it can't read ends `needs-human` instead of being cleared.
- **Edge changes are plans.** `huta edge --apply` makes an infra plan and asks for approval.
  `--now` applies at once for an operator at the console, and is recorded as an event.
- **Approval timeout per plan.** The approval unit's limit is computed from the plan (migrate
  timeout + health timeout + drain + copy), at least today's 20 minutes.
- **Notifications** put data-loss and migration lines first, before the 🔑 lines.

## 1. Gateways: a hostname for a service huta doesn't run (built)

A target with no container: huta does the edge (DNS record, Origin CA certificate, strict SSL
rule, generated vhost with the Cloudflare-only filter and access log, plan, approval, journal,
history, `huta traffic`) and proxies to an upstream the operator names. It doesn't build,
release, health-check or roll back the upstream. Only the registry changes a gateway.

```toml
[targets.media]
kind = "gateway"
hosts = ["media.example.org"]
gateway = { to = "100.64.0.5:8080", secret = "GATEWAY_KEY", header = "X-Gateway-Key" }
edge = { body = "1g", timeout = "10m" }    # also valid on app targets
```

- `to` is an IP literal and port in loopback, private (RFC 1918), CGNAT (100.64.0.0/10) or
  ULA space. No DNS name, no public address: the host must not become an open proxy.
- The secret header's value lives in a separate file (`<caddy_secrets>/<p>--<t>.caddy`,
  root:caddy 0640) that the vhost `import`s; the vhost itself stays world-readable. The value
  is checked: no quotes, braces, `$`, whitespace or control characters.
- `edge.body` (request body limit) and `edge.timeout` (upstream read/write timeout) render
  nothing when unset, so v1 vhosts don't change.
- A project of gateways only needs no `repo`, `app_toml` or `uid`. `huta submit` to a gateway
  target is refused. `routed_port` ignores gateways, so app recovery never takes one for an app.
- Handing a hostname from a gateway to an app (hostname first, code later) is an explicit
  registry change, planned and approved like any edge change.

## 2. Migrations, exclusive mode (built)

```toml
# app.toml
[migrate]
image = "web"                         # one of [images]
cmd = ["python", "manage.py", "migrate"]
timeout = "10m"

[state]
kind = "data"                         # or "postgres"
schema = 4                            # the schema this release migrates to
runs_on = [3, 4]                      # schemas this code runs on (must contain schema)
```

- The target's recorded schema `S` (resource row) and the release's `M`: `M == S` skips the
  migration; `M > S` (any increase) migrates; `M < S` is refused (going back is a restore
  plan, below). A target that has a schema refuses a release that declares none.
- **Exclusive order** (the default, and the only mode until step 6): stop every writer (both
  slots, workers; a running job blocks the release) → copy the data or dump the database as
  a **pinned restore point** → phase `migrating` → run the migrate container once → record
  `S = M` together with phase `migrated` → start the new slot → health (`health_timeout`) →
  smoke → switch → public checks.
- The migrate container runs from the release's named image with the target's limits, the
  data volume or a migrate-role database URL, **no egress, no target secrets** (only names
  listed in the registry's `migrate_secrets`), argv only, entrypoint as the image sets it,
  output captured and redacted. Exit 0 within `timeout` is success.
- **Failure before the switch** (migrate fails, the new slot never gets healthy, smoke fails):
  restore the pinned point, `S` back, start the old slot: `nothing-activated`. This is
  lossless: nothing but the migration wrote since the copy.
- **Crash while migrating:** whether the migration committed is unknown, but nothing else ran
  since the pinned point, so reconcile restores it, puts `S` back and starts the old slot:
  `nothing-activated`. No guessing is needed in exclusive mode.
- **After the switch:** public checks failing leave the release live: `needs-human` (as v1
  one-writer targets).
- `health_timeout` (registry, per target, default 30 s, at most 900 s) replaces the fixed
  30 s, for apps whose start includes slow work.
- **Going back across a schema.** `huta rollback` refuses a release whose `runs_on` lacks the
  current schema and names the restore plan instead: `huta restore <p> <t> --to <release>`
  restores the restore point pinned before the migration that left that schema, then
  releases that code. The plan says, first, what is lost ("writes since <time>"). Approval:
  `huta approve <id> --confirm-data-loss`, or the Telegram button; huta checks at approval
  that the restore point's fingerprint is unchanged.

## 3. Several containers per target (built)

```toml
# app.toml
order = ["web", "api", "worker"]

[images.web]
dockerfile = "web/Dockerfile"
[images.api]
dockerfile = "api/Dockerfile"

[components.web]
image = "web"
kind = "http"
port = 8080
health = "/healthz"
public = true                  # the one the vhost routes to

[components.api]
image = "api"
kind = "http"
port = 8081
health = "/healthz"
data = "/data"                 # only components that name it get the volume

[components.worker]
image = "api"
kind = "worker"
cmd = ["python", "-m", "app.worker"]
```

- **A slot is the whole set.** Slot a = web + api + worker of one release, on its own network
  (ICC on inside the slot, component names as aliases: `http://api:8081`). The two slots'
  networks are separate, so the old and new releases never talk to each other. The switch moves
  everything at once; versions never mix.
- **One public component** (`public = true`, or the only HTTP one). Only it is published on the
  slot's loopback port; the vhost routes to it, as in v1. Other HTTP components are checked on
  their slot-network address from the host before the switch.
- **Workers are single.** A new worker starts only after the switch, once the old worker has
  stopped; it must still be running after the liveness window (registry `worker_liveness`,
  default 10 s). A worker failing then counts as a failed public check (concurrent: switch
  back, old worker restarted; one-writer: `needs-human`). Restart policy `no` during the
  window, `unless-stopped` after.
- **One writer is per resource.** The components that mount the data volume (or the database
  in exclusive mode) are the writers; the one-writer order stops all of them.
- **Per-component host settings:** `[targets.<t>.components.<c>]` with `limits`, `env` and
  `secrets`. Target-level secrets reach the public component only; others get what their own
  entry names. Target env reaches every component.
- **Images:** pack builds every image; the request carries `images` (name → digest). Extra
  images are tagged `huta-build/<p>/<image>:<sha12>` and carry `run.huta.image`; each is
  checked like the v1 image. `prune_images` and `rollback` work on the whole map.
- Labels `run.huta.component` and `run.huta.release` on every container. A container without
  a component label is the public component (v1 containers). `status`, `logs`, `diag` take
  `--component`; jobs take `component = "<name>"` (default: the public one).
- Compose services are `slot-a-<component>`; a one-component target keeps `slot-a`/`slot-b`
  and its single network, unchanged.

## 4. Postgres, one per target

```toml
# huta.toml (host)
[postgres.images]
"17" = "postgres@sha256:<digest>"

# projects.d/<p>.toml
[targets.prod]
postgres = { version = "17", size = "10g", backup = "hourly" }
```

```toml
# app.toml
[state]
kind = "postgres"
schema = 1
runs_on = [1]
```

- **Its own compose project** (`huta_<p>_<t>_pg`), outside the release lifecycle: a release
  never recreates it. Its own loop-mounted volume, `PGDATA` in a subdirectory of it.
- **Database-only networks.** Three internal Docker networks (`…_pga`, `…_pgb`, `…_pgm`, no
  route out, not in the egress policy): slot a's components join `pga`, slot b's `pgb`, the
  migrate and dump containers `pgm`. Postgres is on all three, reachable as `postgres:5432`.
- **Roles:** an owner role (huta's, for dumps, restores and role setup), `migrate` (owns the
  schema; only the migrate step gets it) and `app` (data access only, by default privileges
  the migrate role grants). Passwords are generated by huta and kept as root-only secrets. The
  app gets `DATABASE_URL` for `app`; plans show the name, never the value.
- **Hardening** as for slots where Postgres allows it: the project uid, no capabilities,
  `no-new-privileges`, read-only root with tmpfs for its socket and temp, a pinned image
  digest from the host's list, `shm_size` set.
- **Backups:** `pg_dump -Fc` in a one-off container on `pgm`, on schedule and before every
  migration (pinned). Same rotation, shipping and `last.json` as data volumes; the restore
  drill restores the dump into a scratch database and checks it.
- **Upgrades** are infra plans: a digest change within a major version dumps first, then
  recreates the container; a major version dumps, initialises a new volume, restores, and
  keeps the old volume until the operator removes it.
- `reconcile` starts a target's Postgres if it is stopped, before anything else of the target.
- Bring-your-own stays possible: a `DATABASE_URL` secret plus egress to the database host.

## 5. External publishing (Cloudflare Pages and Workers)

```toml
# app.toml
[external.site]
kind = "cloudflare-pages"          # or "cloudflare-worker"
image = "site"                     # one of [images]
dir = "/out"                       # where the built files are in that image
main = "index.js"                  # worker only: the prebuilt bundle in dir

# projects.d/<p>.toml
[targets.prod.external.site]
name = "my-site"                   # Pages project or Worker name
account = "<account id>"
token = "CF_PAGES_TOKEN"           # a platform secret name
routes = ["example.org/*"]         # worker only
compatibility_date = "2026-11-01"  # worker only
```

- **A step of the target's own release**: one plan, one approval. It runs after the switch
  and the public checks, bound to that release.
- **Files come out without running the image:** `docker create` + `docker cp` streams the
  directory as tar, read through a validator (regular files and directories only; no links,
  devices or absolute paths; count and size caps). Their content hash is in the plan and
  checked again at the step.
- Two contracts: **static** (refuses `_worker.js`, `functions/`, `wrangler.*`, `package.json`:
  those are code) and **worker** (one prebuilt bundle, uploaded without bundling).
- `wrangler` runs in a container pinned by digest in host config, with only that token, egress
  to `api.cloudflare.com` only, the files read-only, and a config huta generates (the repo's
  `wrangler.toml` is never used). Routes and names come from the registry, checked against
  `[cloudflare] zones` and every other target's hosts.
- **Intent before every call:** the phase records name, content hash and the previous
  deployment id first. After a crash, reconcile asks Cloudflare whether a deployment with that
  commit exists: if so it records it, otherwise it retries once, then `needs-human`. The host
  target stays live either way: a failed external step is `needs-human` naming the step.
- **Rollback** by the recorded deployment (Pages) or version (Workers), as its own approved
  plan. Cloudflare tokens are account-wide; the plan says so.

## 6. Migrations, online mode

For changes the running code tolerates (adding a column, a table): the old release keeps
serving while the migration runs. Three yeses are needed: the repo's `[migrate] online = true`,
the host's `migrate = { online = true }`, and the live release's `runs_on` containing the new
schema. Otherwise the release runs exclusive.

- Order: dump (a reference copy, never restored automatically) → phase `migrating` → migrate
  → record `S = M` → start the new slot → checks → switch → drain.
- **Never restore under a live writer.** A failed migration is `needs-human`; a crash while
  migrating runs the repo's optional `[migrate] check` argv (prints the schema number) to learn
  the result, and without it ends `needs-human`. A new slot that fails after a good migration
  leaves the schema at `M` (the old code runs on it) and ends `nothing-activated`.

## Failure matrix

Every durable step, what is recorded before it, and what `reconcile` does when huta dies right
after it. "Lost writes" means writes made by a running release that recovery discards.

| Step | Recorded first | Crash right after → reconcile | Lost writes | Outcome |
|---|---|---|---|---|
| stop old writers (exclusive) | phase `stopped` | start the old slot again | none | nothing-activated |
| pinned copy / dump | phase `copied`, restore point row | old slot back, point kept | none | nothing-activated |
| migrate (exclusive) | phase `migrating` | restore the point, `S` back, old slot back | none | nothing-activated |
| migrate done | `S = M` + phase `migrated` | restore the point, `S` back, old slot back | none | nothing-activated |
| new slot started | phase | stop it, restore if it could write, old back | none | nothing-activated |
| vhost switch | phase `switching` + previous vhost | by the route installed: finish the checks | none | live / rolled-back / needs-human |
| worker handoff | phase `workers` | old worker stopped → start the new one, check | none | live / needs-human |
| external publish | phase `external` + intent | ask Cloudflare; record, or retry once | n/a | live / needs-human |
| migrate (online) | phase `migrating` | run `check`; without it a human decides | none | needs-human |
| restore plan | phase `restoring` + fingerprint | redo the restore from the point | since the point, by approval | live |
| infra plan (edge, gateway, pg) | phase / edge journal | edge journal recovery; pg: start it | none | applied / needs-human |

## Decided (2026-11-25)

- **Order:** foundation (tests, database versioning and install guard, refactor onto one
  release description, records) → gateways → exclusive migrations → several containers →
  Postgres → external publishing → online migrations.
- **Data-losing restores** can be approved from the CLI (`--confirm-data-loss`) or with the
  Telegram button, which shows the warning first and checks the restore point's fingerprint.
- **Postgres** on database-only internal networks, not through the egress daemon.
- **Compatibility** as described above: `schema = 1` grows; one engine; the database version
  check ships alone first; the v1 snapshots are the contract.
