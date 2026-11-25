# Runtime: secrets, egress, data, jobs, checks

What your app gets at run time, and how to write it so it works on huta. The keys themselves
are in [Contract](contract.md). Everything here is decided by the operator's host registry; you
declare needs, the operator grants them.

## Secrets and environment

- Non-secret settings come from the target's `env` in the host registry; secrets from its
  `secrets` list. Both arrive as **environment variables**. Values are never in the repo, the
  image, a plan or a log line huta writes.
- The operator sets a secret on the host with `sudo huta secret set <project> <NAME>` (value
  from stdin), or copies named keys from an existing `.env` with
  `sudo huta secret import <project> NAME… --from <file>`. A plan lists secret **names** only;
  a missing secret refuses the plan (`secret API_TOKEN is not set: sudo huta secret set …`).
- **From a dev box**, for a name the host registry already lists for the project:
  `huta secret set <project> <NAME>` (value from stdin, not echoed on a terminal) or
  `huta secret import <project> NAME… --from <env file>`. The host **stages** the value: it is
  never answered, logged or shown, and it goes live only with the next release of a target
  that uses the name, **after the owner approves it**. That plan lists
  `🔑 new value for secret <NAME>, sent by a dev box …`. With no code change, submit the live
  commit again (a redeploy) to get such a plan. A staged value also counts as set, so a first
  release doesn't need the operator to set it first. A box can't add names (the operator does,
  in the registry), can't read values back, and can't touch other projects. If the value is
  sent again before approval, the plan can't be approved (`a secret value staged by a box
  changed since this plan was made`): submit again. A project's secrets are shared by its
  targets: once one target's release applies the value, the other targets' jobs get it at
  their next run, and their web containers at their next release or restart (the plan line
  names those targets); their plans made before that can't be approved (`a secret's live
  value changed since this plan was made`). Values go live **with the release or not at
  all**: if it ends `nothing-activated` or `rolled-back`, the old values are live again and
  the new ones staged again. While it runs, the project's jobs already read the new values.
- A box sends one line (up to 8192 characters, no control characters). A multi-line secret,
  such as a PEM key, is set by the operator on the host.
- Never pass a value as a command-line argument or paste it into chat: pipe it in
  (`printf %s "$VALUE" | huta secret set …`) or point `--from` at a file that is not in the repo.
- A secret only a job needs (a chat token for a watcher, say) goes in `job_secrets` for that
  job: its runs get it, the web container's environment never does. Ask for that instead of a
  target-wide secret whenever the web process doesn't need the value. It limits exposure to
  the running web process, not to your code: the job runs your image and could write the
  value to the data volume. When a release adds such a job or changes its command, the plan
  and the approval message show `🔑 job <name> gets <secrets> — <command>` (also when an
  existing job newly gets a secret). Such a job runs exactly its `cmd`: the image's
  `ENTRYPOINT` is replaced by `cmd[0]`, so give the full command (`["node", "jobs/watch.mjs"]`).
- A new value takes effect on the next release, or with `sudo huta restart <project> <target>`
  (recreates the live slot: a few seconds of 502).
- Trailing newlines and surrounding whitespace of a value are trimmed.
- Your app can read every secret it is given and could send it anywhere it can reach: egress
  rules limit where, not what.

**Ask the operator for:** each variable name and whether it is secret (they add names to the
registry). Values: send them yourself with `huta secret set` when you have them, or the
operator sets them on the host. Never in chat, the repo or a commit.

## Monitoring

If the repo asks (`[monitor] env` in `app.toml`) and the host grants (`monitor` on the target),
the public component (and components the registry marks `monitor_env`), and jobs that run in
them, get:

| Variable | Value |
|---|---|
| `SENTRY_DSN` | the target's own Bugsink project's DSN (a secret: redacted in logs) |
| `SENTRY_RELEASE` | the release's sha |
| `SENTRY_ENVIRONMENT` | the target's name (`prod`, `staging`) |
| `MATOMO_SITE_ID` | the target's Matomo site id |

- Each target has its own Bugsink project and Matomo site (staging never mixes with prod).
- The host makes them when it plans a release. If that fails (the service is down), the plan
  says which variables are not set and why, and the release goes ahead without them; they
  arrive with a later release. Your app must start without them.
- A value the host registry sets itself (`SENTRY_DSN` in `env` or `secrets`) wins.
- Events go out through the egress proxy to the Bugsink host on 443 (`HTTPS_PROXY` is set; no
  DNS). Most Sentry SDKs honour `HTTPS_PROXY`; check yours.
- Migrations and diagnostics never get these variables.
- Planned: per-release watch windows that read Bugsink, checks and probes after a release.

## Egress

By default a target has **no way out**: no route to the internet, the LAN or the host, and no
DNS. Two kinds of exception exist, both granted by the operator.

### Allowlisted HTTPS (`egress = ["api.example.com:443", …]`)

A proxy on the host (the egress daemon) opens tunnels only to the listed `host:port` entries.
Your container gets:

```
HTTPS_PROXY=http://huta-egress:4880   https_proxy=…   HTTP_PROXY=…   http_proxy=…
NO_PROXY=localhost,127.0.0.1,huta-egress   ALL_PROXY=   NODE_USE_ENV_PROXY=1
```

- **Your HTTP client must use the proxy variables.** curl, Python `requests`/`httpx`/`urllib`,
  Go's `net/http`, Node ≥ 22.21 / 24 built-in `fetch` and `http(s)` (with
  `NODE_USE_ENV_PROXY=1`, set for you) do. Raw sockets, `aiohttp` without `trust_env=True`,
  and libraries with their own agent don't: those connections fail (there is no other route).
- Only `CONNECT` tunnels (HTTPS, or anything that tunnels). Plain `http://` requests are
  refused with 405.
- Names are resolved by the proxy. A name that resolves to a non-public address (loopback,
  private, link-local, CGNAT/tailnet) or to the host itself is refused, so an allowed name
  can't be pointed at the LAN. A literal `ip:port` entry (written by the operator) is the only
  way to reach a non-public address.
- Entries: `host:port`, `*.suffix:port` (any subdomain, not the bare suffix), `*:port` (any
  public host; the operator's explicit choice).
- Refusals are logged on the host (`journalctl -u huta-egress`) with the reason; your client
  sees `403` (not allowed), `502` (unreachable) or `405` (not CONNECT).
- Hostnames behind a CDN are fine: the check is by name, not by IP.

### Upstreams (`upstreams = ["vpn-exit"]`)

Named services the operator defines on the host (a SOCKS exit, a tailnet API, …). For each,
your container gets `HUTA_UPSTREAM_<NAME>=huta-egress:<port>` (name upper-cased, `-` → `_`),
and connects there exactly as it would to the original service. It is a plain TCP forward,
so an HTTP service behind an upstream is called as `http://huta-egress:<port>/…`; `huta-egress`
is in `NO_PROXY`, so proxy-aware clients dial it directly.

**An upstream is a capability of its own.** If it is a SOCKS or HTTP proxy, it can reach
whatever *it* can reach; the allowlist above doesn't apply to it. The plan shows each upstream.

### No DNS

Names don't resolve inside the container. Don't resolve hostnames yourself before connecting;
let the proxy-aware client send the name. Health checks and your own `/healthz` must not depend
on outside services.

## Data

With `state.kind = "data"` and `components.<c>.data = "/data"`, the target has its own
filesystem of a fixed size (the operator's `data.size`), mounted read-write at that path in the
components that name it and in every job that runs in one of them.

- It is owned by the project uid, mode 0700. It contains an empty `lost+found` (root's, leave it).
- When it is full, writes fail with "no space left". `huta data status` shows the use. The
  operator can grow it (never shrink); the next release or `huta data ensure` applies it.
- Nothing else is writable except `/tmp` (tmpfs, lost on restart).

### Postgres

With `state.kind = "postgres"` (and `postgres = { … }` granted in the registry), every component
gets `DATABASE_URL=postgresql://app:…@postgres:5432/app`. The `app` role reads and writes rows;
it can't create or change tables. Schema changes go in `[migrate]`, whose container gets
`DATABASE_URL` for the `migrate` role (it owns the schema) and reaches only the database. Your
code should connect with that variable as given (it contains the password; never log it).

### Several components

Each component also gets `HUTA_COMPONENT=<its name>` and reaches the others of its slot by name
(`http://api:8081`); they are excluded from the egress proxy (`NO_PROXY`). Only the public
component receives the target's own secrets; others get what the registry names for them.

### One writer, or two at once

During a release the old and the new version can overlap. Which order huta uses:

| | concurrent (`concurrent_safe = true` in **both** the live and the new release's app.toml, **and** host `data.concurrent = true`) | one writer (the default) |
|---|---|---|
| order | new slot starts while the old serves; switch; old stops after `drain` | old slot **stops**, a quiet local copy of the data is taken, new slot starts, health, smoke, switch. A running job of the target makes the release end `nothing-activated` before anything stops (submit again later) |
| downtime | none | the site answers 502 from the stop until the switch (seconds) |
| both versions write the data | yes, during start-up checks and the drain | never |
| new slot fails before the switch | it stops; the old one kept serving | it stops, **the copy is restored**, the old slot starts again: `nothing-activated` |
| public checks fail after the switch | switched back automatically: `rolled-back` | stays live: `needs-human` (the new release has written data and served traffic; going back is a human decision, `huta rollback`) |

To be concurrent-safe, make the two versions never write the same file: name files per slot
(`HUTA_SLOT` is `a` or `b`), for example `events/2026-11-09.a.ndjson`, and read `*.ndjson*`.
SQLite with two processes on one file is not concurrent-safe across schema changes: leave it
one-writer.

### Backups

- On the operator's schedule (`data.backup`: `hourly`, `daily` or an OnCalendar), and before
  every concurrent release, huta makes a **consistent copy**: one `rsync` pass while the app
  runs, then the filesystem is frozen for a second pass that copies everything that differs,
  then thawed. Writers block for that moment (typically well under a second); reads continue.
  The copy is crash-consistent: an SQLite database in WAL mode opens from it as after a power
  cut. Keep the `-wal` file next to the database (huta copies it).
- A one-writer release copies while nothing runs (no freeze).
- A new copy replaces the previous one only when complete; the one before is kept too. Then the
  operator's ship command sends it off the server. `huta data status` shows when the last local
  copy and the last off-site copy were made; a plan shows it too.
- **Restore is manual**: the operator stops the target and restores from the copy or the
  off-site store. If huta itself is interrupted in the middle of a one-writer release, the next
  huta command on the target finishes the recovery: it stops the new slot, puts the
  pre-release copy back and starts the old slot. huta never restores data on its own, except the case above (a one-writer
  release that failed before its switch).
- Seeding from an existing server: the operator runs `huta data import` ([Moving a live site](moving.md)).
  Until an import has completed, nothing of the target starts (an interrupted import has to be
  run again).
- Getting data out: the owner runs `sudo huta data export <project> <target> [path…] > out.tar`
  on the host (a tar of the last copy). There is no agent-side export: ask the human, or
  publish what you need (for example an anonymised rollup written by a job).

## Jobs

- A job runs on its schedule in the **live** release's image (after a rollback, the old
  release's jobs), as the project uid, with the same limits, env, secrets, data mount and
  egress as the web container, plus `HUTA_SLOT=job`, `HUTA_JOB=<name>` and that job's own
  `job_secrets`.
- Scheduled work that belongs to the project runs here, not in CI: a watcher that checks the
  public site and alerts a chat is a job with its token in `job_secrets` and the chat API in
  `egress`.
- It runs **next to** the live web container. With one-writer data it holds the target's lock
  while it runs: a release waits (up to 5 minutes) for it, and it waits (up to 10 minutes) for a
  release. Write jobs that are safe to run while the app serves (for example rotate by renaming
  a file the app reopens).
- Killed after `timeout`. Exit status 0 is success; anything else fails the run, and host
  alerting sees the failed unit. Each run records its peak memory against the target's limit
  (sampled every 0.5–2 s, so a final spike or a very short job may not show), and
  `OUT OF MEMORY` when the kernel killed a process in it: the limit is per target, so a job
  that needs much more than the web container raises it for both. Output goes to the host's journal and to the target's logs
  (`huta logs --job <name>`, readable from a box only where the host sets `logs.jobs = true`),
  with known secret values replaced (best effort: don't print secrets).
- One run at a time per job; a run that would overlap the previous is skipped by systemd.
- `after_release = true` also runs the job once whenever a release of the target goes live
  (rollbacks too), for work that should match the new code at once, such as regenerating a
  published rollup. It starts after the release has finished and released the target's lock;
  its result is the job's, never the release's (a failure fails the job's unit, the release
  stays live). If the host dies between the switch and that start, the run is skipped and the
  schedule runs it next. Not on `huta restart`/`start`.
- `huta jobs <project> <target>` on dev (and `huta job list` on the host) shows the schedule,
  next run and the outcome of recent runs (exit status and duration, no output); the owner
  can run one now with `huta job run` on the host. Dev can't trigger a run.
- Adding, removing or changing a job is an infra change in the plan.

## Logs

Write logs to **stdout and stderr**, one event per line. The host keeps them for `huta logs`
(7 days and 200 MB per target unless the host registry says otherwise), across releases and
slot swaps, including a slot that never became healthy. There is no syslog socket and no log
file the host reads: a file under `/tmp` or the data volume stays yours.

Every line passes the host's redaction before it is stored: each secret value, its parts (list
entries, `k=v` values, every URL `user:pass@` inside it, wherever it sits) and their
URL-encoded, JSON-escaped and base64 forms become `‹redacted›`; lines with one of the project's
`log_drop` strings are dropped. Parts under 8 characters are not caught on their own, except in
URL form: `:pass@` and `user:pass@` at any length (from 3 characters), a bare `user:pass` from
6, and the base64 of `user:pass` (Basic auth). A value the app makes at run time is not a known
secret: redact in the app first, and name such lines in `log_drop`.

## Diagnostics

`[[diag]]` ([Contract](contract.md#diag)) declares checks a dev box may run by name:
`huta diag <project> <target> <name>`.

- They run in the **live** release's image, as the project's user, with the web container's
  limits, a read-only root and `/tmp`.
- **No network** (not even the egress proxy) and **no secrets**: only the registry's
  non-secret `env`, `RELEASE_SHA`, `HUTA_PROJECT`, `HUTA_TARGET`, `HUTA_DIAG`.
- With data, the **last backup copy** (not the live volume) is mounted **read-only** at the
  data path; the answer says how old it is. Without a backup yet, it runs without data and
  says so.
- **SQLite in WAL mode:** copy the database **and its `-wal` file** to `/tmp` and open the
  copy. On the read-only mount, `immutable=1` ignores the `-wal` (rows committed there are
  missing) and `mode=ro` can't create the `-shm` it needs. `/tmp` counts against the target's
  `limits.tmp`; a database larger than that needs a bigger `tmp` from the operator.
- Bounded: its `timeout` (at most 5 minutes), 256 KiB of output, one diag per target at a
  time. Output passes the same redaction as logs; every run is an event on the host.
- Good uses: an integrity check, the schema/migration version, row counts per table, a config
  sanity check. Print counts and states, not records.

## Smoke checks and automatic revert

`[[smoke]]` checks ([Contract](contract.md#smoke)) run twice:

1. **Before the switch**, on the new slot over loopback, with `Host:` the target's first host
   and `X-Forwarded-Proto: https`. A failure stops the new slot; nothing changed
   (`nothing-activated`).
2. **After the switch**, through Cloudflare at `https://<host>`, together with the public
   `/healthz` check, while the old slot still runs. A failure reverts automatically for
   concurrent targets (`rolled-back`, the old release serves again) and for an adopted site's
   first release (the old config serves again). For one-writer targets it ends `needs-human`
   (see the table above). A first release with nothing to go back to ends `needs-human` too.

Write checks that hold both on loopback and through Cloudflare: Cloudflare may add headers and
replace `Server`, and you shouldn't let it cache what a check looks at.

## What the web server passes to your app

- Every request header, unchanged, including `cf-*`. Hop-by-hop headers are dropped.
- **The visitor's address is `CF-Connecting-IP`.** Only Cloudflare can reach the site (other
  peers are dropped), and Cloudflare sets that header.
- `X-Forwarded-For` is the Cloudflare edge address that connected (huta's web server doesn't
  trust incoming forwarding headers); don't use it as the visitor.
  `X-Forwarded-Proto` is `https`, `X-Forwarded-Host` the requested host.
- Access logs: one file per host on the server, unless the operator sets `access_log = false`
  (then none). If your privacy notes depend on "no access logs", ask for that and check the
  plan says `access log off`.
