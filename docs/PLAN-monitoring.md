# Plan: monitoring hooks

Order among the post-v2 plans: 1 of 4 (monitoring → snapshot → unattended → plain TLS).

Status: **plan, reviewed** (codex, grok, agy; findings folded in below). Nothing is built;
everything below is *planned*.

## Goal

huta adds no monitoring stack. It wires each target into services the operator already runs:
healthchecks (job and uptime checks), Bugsink (errors, Sentry protocol) and Matomo (analytics).

- Per target, created once and found again by name: a healthchecks check per job, an uptime
  check fed by a probe of the public health URL, a Bugsink project and DSN, a Matomo site.
- Injected env: `SENTRY_DSN`, `SENTRY_RELEASE=<sha>`, `SENTRY_ENVIRONMENT=<target>`,
  `MATOMO_SITE_ID`.
- huta pings a check of its own.
- A **watch window** after each release: errors, checks, probes and local container signals
  compared with before the switch, a verdict recorded, a ↩️ button in Telegram. Unattended
  releases (plan 3) build on it.
- Every integration is optional. A monitoring API that fails never fails a release.

## Non-goals

- No metrics store, dashboards, alert routing or on-call logic: the services do that.
- huta doesn't install, upgrade or back up those services.
- No automatic rollback. `on_degraded = "rollback"` is a reserved value, refused by name.
- No unattended approval here; only the interface it needs.
- No deleting of remote checks, projects or sites, ever (a removed job's check is paused).
- Gateways get no monitoring in this plan. Backup timers get no check in this plan.
- A quiet watch proves only that the configured sources saw nothing; it doesn't prove the app's
  SDK works. `ok` is evidence, not a guarantee.

## API survey (milestone 0)

Read from the source of the versions an operator runs today: healthchecks v4.2, Bugsink 2.2,
Matomo 5.14. Milestone 0 repeats each call with the real tokens on a test host and records the
answers (field names only) in `tests/fixtures/monitor/shapes.json`, which the fake services in
`tests/fakemon.py` must match. Checked against running services so far: Bugsink's project and
team lists with a real token (fields, the DSN form with a 32-hex key) and Matomo's redirect
without `X-Forwarded-Proto` and its 401 error body. Still to check with real tokens: healthchecks
create/pause/flips and Matomo find/add (they need a healthchecks API key and a Matomo token),
and the Bugsink create (it writes, so it waits for a test project name).

**healthchecks** (API v3, header `X-Api-Key`; a key belongs to one healthchecks project, so every
huta check lands in that project):
- `POST /api/v3/checks/` with `unique: ["name"]` is find-or-create (an upsert): 201 when made,
  200 when a check of that name existed, which is then updated with the fields sent. 403 when the
  healthchecks project has no checks left.
- A check made through the API has **no notification channels** unless the call sends
  `channels` (`"*"` = every channel of the project). huta sends `channels` (default `"*"`) only
  on the first call for a check; later calls send only the fields huta owns (name, timeout or
  schedule+tz, grace, tags, desc), so changes made in the healthchecks UI stay.
- The answer carries `uuid` and `ping_url`. huta keeps only the uuid (matched by regex) and
  builds ping URLs itself from the configured ping base; a returned URL is never requested.
- `schedule` accepts cron or systemd `OnCalendar` expressions (with `tz`); `timeout` is the
  simple alternative. Both `timeout` and `grace` must be 60 s..365 d.
- `POST /api/v3/checks/<uuid>/pause`; a ping resumes a paused check. `GET .../<uuid>` → status
  (new, up, grace, down, paused, started); `GET .../<uuid>/flips/?start=<unix>` → the changes of
  state since a time (used for "down after the switch" and "already down at the switch").
- Pings: `<base>/<uuid>`, `/start`, `/fail`, `/<code>` with code 0..255; `?rid=<uuid>` pairs a
  start with its end.

**Bugsink** (`/api/canonical/0/`, `Authorization: Bearer`; a token is global and made by a
superuser, so the token that reads crashes can also create projects):
- `GET projects/` (cursor pages of 250, by name; soft-deleted hidden): `id` (int), `name`
  (unique), `dsn`, `team`. `POST projects/` with `{"team": <uuid>, "name"}` creates; its answer
  has no `dsn`, so huta lists again by name. A second create of the same name fails; huta then
  finds it by name. A project needs a team: `[monitoring.bugsink] team`.
- The DSN is `https://<key>@<BASE_URL host>/<id>`: huta checks all of it (below).
- Issues: `GET issues/?project=<id>&sort=last_seen&order=desc` (`first_seen`, `last_seen`).
- Events: only per issue, `GET events/?issue=<uuid>&order=desc`, with `timestamp`; the release
  is only in an event's detail. So the event signal counts by time, per issue seen in the window,
  with caps (see the watch). Newer Bugsink versions add hourly counts; not used.

**Matomo** (POST `/index.php`, form body `module=API&format=json&method=…&token_auth=…`; the
token never goes in a query string):
- `SitesManager.getSitesIdFromSiteUrl` (`url=https://<host>`) → `[{"idsite": "N"}]`, zero, one or
  several; `SitesManager.addSite` (`siteName`, `urls[0..]` = the target's hosts) → `{"value": N}`.
  `addSite` needs a superuser token.
- With `force_ssl` Matomo redirects plain-HTTP calls; huta's loopback call sends
  `X-Forwarded-Proto: https` (read when Matomo lists it in `proxy_scheme_headers`, the usual
  setup behind a proxy). A 3xx is an error that says this.
- Errors arrive as HTTP 200 or 401 with `{"result": "error", "message": …}`.

## Contract (all *planned*)

**`huta.toml`**. Each table is optional; a missing table turns that integration off host-wide.

```toml
[monitoring]
prefix = "huta"                    # remote names: <prefix>_<project>_<target>[_<job>]
                                   # (over 100 chars: cut, plus 8 hex of its sha256)
[monitoring.healthchecks]
url = "http://127.0.0.1:8000"      # management API; token: HEALTHCHECKS_API_KEY
ping = "https://hc.example.org/ping"   # ping base (hosted services use another host)
host = "hc.example.org"            # Host header for a loopback url (optional)
channels = "*"                     # sent only when a check is created
self = true                        # huta pings its own check, <prefix>__self_<platform.host>
[monitoring.bugsink]               # API address and Host come from [crashes]; token: BUGSINK_TOKEN
dsn_host = "bugs.example.org"      # where containers send events (granted as egress, :443)
team = "00000000-0000-0000-0000-000000000000"   # Bugsink team new projects belong to
[monitoring.matomo]
url = "http://127.0.0.1:8081"      # token: MATOMO_TOKEN
host = "stats.example.org"         # Host header, as [crashes] host
[monitoring.watch]
window = "15m"                     # default for targets; 5m..2h
button_ttl = "2h"                  # how long ↩️ stays after the window
```

URLs are `http://` on loopback or `https://` to a hostname. Calls: no proxy, no redirects, 10 s
per call with a wall-clock read deadline, 8 MiB (read max+1, refuse more). `dsn_host` must
resolve only to public addresses (the egress daemon refuses anything else, so a Bugsink on the
same host is reached through its public name, e.g. behind a CDN); `huta check` resolves it and
says so.

`prefix` is part of every remote name: changing it makes huta create new resources and leave the
old ones. `huta check` warns.

**Platform secrets** (`secrets/_platform/`): `HEALTHCHECKS_API_KEY`, `BUGSINK_TOKEN` (exists),
`MATOMO_TOKEN`. `huta secret set _platform <NAME>` is new (today `huta secret` takes only a
project). Platform tokens never reach a container.

**Generated values.** The DSN is stored as a project secret, `HUTA_SENTRY_DSN_<TARGET>`, so the
redactor and the secret machinery treat it as any secret. Check uuids (ping capabilities) are
kept in `huta.db` and also in `secrets/_monitor/CAPABILITIES` (root-only, one per line, written
atomically before the row); `cfg.redact` reads every secret file, so every process redacts
them, whole URL or bare uuid. Old values stay in the file after rotation.

**Registry** (`projects.d/<p>.toml`, per target):

```toml
[targets.prod]
monitor = { uptime = true, jobs = true, errors = true, analytics = true }
watch = { window = "15m", new_issues = 1, events = 10, factor = 3.0, on_degraded = "notify" }
matomo = 7                         # optional: adopt this Matomo site instead of creating one
[targets.prod.components.api]
monitor_env = true                 # this component also gets the Sentry variables
```

- A key that is `true` while its host table is missing: `huta check` refuses it.
- `crashes = <id>` stays. With `errors = true` huta adopts that Bugsink project, not a new one.
  `matomo = <id>` does the same for Matomo (and is the way to use it without a superuser token).
- A name the registry already sets (`SENTRY_DSN` in `env` or `secrets`) wins; huta injects
  nothing over it, `huta check` says so, and the errors source counts as ready only if
  `crashes` names the project that DSN reports to (huta can't check that; the operator says it).
- `watch` needs at least one signal source (`uptime`, `jobs`, `errors`, or the local signals,
  which every target has). `watch` changes are part of `effective`, so they show in plans.
- Injection reaches the public component, components with `monitor_env = true`, and jobs that
  run in one of those. Never a migration, never another component.

**`app.toml`** (the repo asks for what changes its container):

```toml
[monitor]
env = ["sentry", "matomo"]
```

Checks, probes and local signals need no ask: they don't touch the container. Env is injected
only when the repo asks **and** the registry grants. The DSN egress is granted only then too: a
target that doesn't send events needs no way out. Until its milestone, `parse_app_toml` refuses
`[monitor]` by name. A release must not need these variables to start: they can be absent.

**Store**: migration 4 adds
- `monitors` (project, target, kind, name, remote id, spec JSON — period, grace, paused —,
  state, updated; key project+target+kind+name). The spec lets the tick skip a call when
  nothing changed.
- `watches` (release id, since — the serving episode —, project, target, opened, until, state,
  reasons, coverage JSON, signals JSON, last tick, policy, notified state, button hash, button
  target, button expires, chat, message id, action, acked, acked by; key release id + since).

**Commands**: `huta monitor status|ensure <p> <t>`, `huta monitor tick` (timer), `huta watch
<release> [--ack]`; `huta status <p> <t>` and the `status` op answer with the watch verdict of the
live release, in the answer's text (the client prints only that).

## Design

Two new modules: `monitor.py` (clients, `ensure`, job checks, probe, self-ping) and `watch.py`
(rows, signals, verdicts, `blocks`, the ↩️ button). The Bugsink client moves from `observe.py`
into `monitor.py` and both use it.

### 1. Clients

One small client per service, each with `find(name)` and `create(name, …)`. Every answer is
untrusted: strict shapes, ids matched by regex, sizes and time bounded. Matches must be exact:
two remote resources with huta's name is an error (`monitor-fail`, "ambiguous"), never a guess.

- healthchecks: the upsert above; the uuid is kept, the URL built.
- Bugsink: find by name in the list, else create in `team`, then find again. The DSN must match
  `^https://[0-9a-f]{32}@<dsn_host>/<id>$` with `<id>` the project's id; anything else refuses.
- Matomo: `matomo = <id>` from the registry if set; else find by `https://<first host>`, else
  `addSite`. Several matches: refuse.

### 2. `ensure`: remote resources, idempotent

`monitor.ensure(platform, st, project, t, kinds)` creates what has no `monitors` row and repairs
what a row says exists but whose local half is missing (the DSN secret). It runs
under `<state_dir>/monitor-ensure.lock` (one ensure on the host at a time; not in `locks/`, so
it doesn't block an install), with one 30 s budget for the whole call.

Where it runs, and what each place may touch:

- **Planning** (`plan_release`, before `effective` is computed): `errors` and `analytics`, the
  kinds that change the container. So the plan shows exactly what the release will get: either
  the variables, or "not available yet (Bugsink: <reason>)" and no variable. Approval compares
  `effective` as today; nothing between plan and approve creates these, so a valid plan is not
  superseded by monitoring. Not under the secrets lock: the DSN secret is written with
  `secret_set` (atomic), only when absent or different.
- **The tick**: `uptime` and `jobs` checks (they don't touch the container).
- **`huta monitor ensure`**: everything, for the operator.

Any failure is an event `monitor-fail` (kind, error class, redacted) and the release goes on.

No phase file: nothing needs undoing. A crash between "created remotely" and "row written" is
healed by the next `find` (same name, exactly one match).

### 3. Env injection

`runtime.environment` adds the variables for the components above. In plans `SENTRY_DSN` shows
the secret placeholder, and `effective["monitor"]` lists the names, what is missing and why.
As built: the variables follow the state when a compose file is written (a release, a revert,
`huta start`); nothing between planning and approval creates the DSN or the site except the
operator's `huta monitor ensure`, which then supersedes a waiting plan. A DSN made later reaches
a live slot only when its compose is next written. The Matomo site id is kept in
`<state_dir>/monitor/<p>--<t>.json`, as the environment is built without the store.
`effective` gains a `monitor` key only when the target has `monitor`, so v1 dicts are unchanged.

`<dsn_host>:443` joins the egress of a release that gets `SENTRY_DSN`, and only then
(decision 3): `runtime.egress_for` gives a release's env and compose their list; the egress
policy, one file for every target, grants it while such a release of the target is live,
approved or running (`egress.monitor_grants`). The plan shows it on its `📈 monitoring:` line.
The DSN is in the plan's `secret_versions`, so a DSN replaced between plan and approval
supersedes the plan.

### 4. Job checks

- The tick converges job checks for every target with `jobs`: one check per job of the live
  release, period from the schedule, grace from the job's timeout + 2 min (the timer's
  `RandomizedDelaySec` and start-up). It calls the API only when the stored spec differs (new,
  changed, removed → paused, re-added → resumed by the next ping). `reconcile` makes no network
  call for this.
- Period: healthchecks reads systemd `OnCalendar` expressions itself (its `schedule` field, with
  `tz`). huta sends the job's schedule in systemd's normalized form (`systemd-analyze calendar`,
  so `hourly` arrives as `*-*-* *:00:00`) and the host's time zone, so a weekday-only job doesn't
  alert on weekends. If healthchecks refuses the expression (its parser covers less than
  systemd's), huta falls back to `timeout` = the **longest** gap over 64 firings (not
  `cfg.calendar_gap`, which returns the shortest of six), clamped to 60 s..365 d, and `huta
  monitor status` says so.
- `jobs.run_job` pings `/start?rid=<uuid>` right before the container starts and
  `/<code>?rid=…` after (code 0..255; a negative return code — a signal — sends 128+n; the
  timeout's 124 as is; an exception before the end sends `/fail`). 5 s each, in a thread with a
  deadline. A failed ping never changes the job's outcome; an event is written only when pinging
  starts or stops failing. A lost end ping makes the check go late: an alert that the evidence
  is missing, which is true.

### 5. The tick: probe, checks, self-ping, watch

`huta-monitor.timer` (every minute) → `huta monitor tick`, one at a time (`flock` on
`<state_dir>/monitor.lock`), holding no target lock and taking none, with a 45 s budget for the
whole tick; work left over waits for the next tick, targets in turn so none starves. Without
`[monitoring]` the tick does nothing and exits 0.

- **Probe.** huta probes by itself; healthchecks is optional (decision 11). `monitor.uptime` needs only `[monitoring]`; the samples live in
  huta's ring file and feed the watch's probe signal; with `[monitoring.healthchecks]` each
  result is also pinged to the target's uptime check. For each target with `uptime` and a live
  release: one request per host to the
  public health path, 5 s, `ok is true` and `sha` = the live sha (today's `public_check` checks
  only the sha; the probe checks both, and `public_check` is fixed to do the same). Before and
  after the request huta reads the live row (release id, since), the phase file and the stop
  marker; if anything differs, or a release was at work (a phase file), the sample is dropped.
  A held target lock alone is not a reason to skip: a backup or a one-writer job holds it for
  hours while the site serves. Otherwise success or `/fail` (short reason) is pinged to the
  uptime check, whose timeout is 5 min and grace 15 min: a release that keeps huta busy longer
  than that makes the check alert, which for a one-writer target is true downtime. The last
  60 samples (time, release, episode, result) go to `<state_dir>/monitor/<p>/<t>.json`
  (tmp + rename, 0600; a missing or broken file is an empty one).
- **Checks.** Job and uptime checks converge as in §4.
- **Self.** Registry loaded, store opened, tick finished within budget → ping the self check.
  Only that: needs-human already has its own Telegram message.
- **Watch.** Below.

### 6. Watch window

A watch is a row, not a process: nothing waits on it and no lock is held.

- **Episode.** A watch belongs to a serving episode: (release id, `live.since`). `make_live`
  and `revert_live` start a new episode. `live.since` is written in the same transaction as the
  switch is recorded, after the vhost switch; a recovery that records it later makes the
  episode start later than the traffic, and the watch says "started at recovery".
- **Open.** `recipe.run` (as built: `cmd_approve`, right after the outcome) opens the watch where it returns `live`, after all its steps (not at
  `make_live`, which comes before the public checks). The tick opens one for every live episode
  of a target with `watch` that has no row, when no phase file exists and the target lock is
  free. An episode first seen after its window ended gets a row in state `unknown` ("not
  watched: huta wasn't running"). Opening twice is a no-op (primary key).
- **Close.** A watch whose episode is no longer live becomes `closed` (keeping its signals and
  any verdict for `blocks`): in the tick, after an approval's outcome, and on every return path
  of `reconcile` (as built: not inside `make_live`/`revert_live`, which stay unchanged; a crash
  between them leaves a row the next tick closes). `huta stop` closes it with reason "stopped".
- **Signals** (sticky: once fired, kept), all from timestamps, so no baseline is captured at the
  switch:
  - *new issues*: Bugsink issues of the target's project with `first_seen` ≥ switch
    (≥ `new_issues`).
  - *event rate*: events since the switch against the `window` before it (served by the previous
    release), as rates: fires when after ≥ `events` and rate after ≥ `factor` × rate before
    (a before of zero counts as one event). Counted per issue with `last_seen` ≥ switch − window,
    at most 20 issues and 500 events a tick; past a cap the "after" count is a lower bound (it may
    still fire) and the tick's errors evidence is incomplete.
  - *checks*: a flip to `down` of one of the target's checks after the switch. A check already
    `down` at the switch makes the verdict at best `unknown` ("was already down"). Job checks are
    weak evidence in a short window (a daily job rarely runs in it): they can fire a signal but
    never count towards coverage.
  - *probe*: three failed samples in a row of this episode, each within 150 s of the last.
  - *local* (no network): a container of the live slot restarted (`RestartCount` up since the
    switch), was OOM-killed (`OOMKilled`, cgroup `memory.events` `oom_kill`), or a worker exited.
    Read through `runstate`.
- **Coverage.** Per source (errors, checks, probe, local), the ticks that got a complete answer,
  kept in the row. A source is covered when it answered in each third of the window.
- **States.** `watching` → `degraded` (a signal fired; reasons kept) | `ok` (window over, no
  signal, every configured source covered, nothing already down) | `unknown` (window over, no
  signal, a source not covered or something already down) | `closed`. The final evaluation runs
  once at `until` before any verdict. Verdict, reasons and event are written in one transaction,
  conditional on the row still being `watching` for that episode. Events: `watch-open`,
  `watch-signal`, `watch-ok`, `watch-degraded`, `watch-unknown`, `watch-closed`.
- **Telegram.** Text from an API (an issue title, a Matomo message) is untrusted: cut, redacted
  and HTML-escaped before it enters a message. The history message gains a line (`👀 watching until 14:32`, then `✓ quiet for
  15 min` or `⚠️ <reasons>`). `degraded` and `unknown` also send a reply, because an edit doesn't
  notify. Delivery is retried (the `notified` column trails the state), so a message may arrive
  twice but is never lost silently. `notify.history` passes the live keyboard with every edit
  (an edit without `reply_markup` removes it) and reads the watch row so later edits keep the
  line.
- **↩️ button.** While a watch is open, and for `button_ttl` after, the history message of the
  live release carries `↩️ Roll back` if an earlier release exists. At the moment the button is
  made, huta fixes the target of the rollback (the release `cmd_rollback` would pick) and stores
  it with the token's hash, *before* posting the keyboard. `callback_data` `huta:b:<token>`.
  `take_press` checks owner and chat before touching the database (a stranger can't burn it),
  then consumes it in one transaction and checks message and expiry. A press starts
  `huta rollback <p> <t> --press <watch>` in a transient unit, which under the target lock:
  (`take_press` gets its own branch for `b`; `cmd_press` today treats any kind but `a` as
  Reject, so a `b` press must never fall through to it.)
  1. refuses unless that episode is still live and the fixed target is still a retired release
     whose images are on the host;
  2. records `action = rollback-planned:<id>` and plans the rollback of exactly that release,
     then posts the plan with its normal buttons (so a unit that dies here leaves a plan the owner
     can approve);
  3. approves it at once only if the plan is **one-tap clean**: the target is concurrent-safe
     (no one-writer data), class code-only, no migration, restore or schema warning, no staged
     secrets, no external publishing, the history comparison known (`history_note` hides the
     downgrade line for rollbacks, so the check reads the comparison itself, not the rendered
     text), and nothing marked ⚠️. The
     approval goes through `cmd_approve`'s checks unchanged, recorded as "approved with ↩️".
     Otherwise the plan waits for its usual buttons.
  A refusal (the old code can't run on today's schema) edits the message with the reason and
  the `huta restore` line. The release rolled away from is recorded on the watch row, and
  `cmd_rollback` never picks a release that was rolled away from by ↩️. No ↩️ is offered when
  the live release is itself a rollback, so presses can't walk back through every retired
  release.

Known limit, as for approval buttons: the relay that delivers presses is trusted with the
presser's id (`docs/ARCHITECTURE.md`); anything running as the relay user can forge a press while
a button is out. `button_ttl` keeps that window short; one-tap only for clean, stateless plans
bounds what a forged press can do (it rolls code back one release).

### 7. Interface for unattended releases (plan 3 uses, this plan builds)

- `monitor.ready(platform, st, t) -> (bool, why)`: every source the target's `watch` needs is
  configured, has its remote resource, and (errors) the live release got the DSN. Resource
  existence is the minimum; plan 3 may add "an event was received".
- `watch.open(st, rid, since)`, `watch.verdict(st, rid, since) -> (state, reasons)`.
- `watch.blocks(st, project, target) -> why | None`, one rule: a watch still `watching`; a live
  episode of a target with `watch` and no row; or any `degraded` or `unknown` verdict, or any
  ↩️ press, not acknowledged with `huta watch <release> --ack` (root on the host only, never a
  Telegram button or a box request; recorded with who and when). A newer `ok` does **not**
  clear an older failure.
- Reasons carry the kind of each signal (`new-issues`, `event-rate`, `check`, `probe`, `local`),
  so plan 3 can choose which kinds may roll back (reviewers: a public probe alone can be the CDN,
  one new issue can be a new code path).
- A hook `watch.on_degraded(platform, st, row)` the tick calls when a watch turns `degraded` and
  `action` is empty; it is retried every tick until `action` is set, while the episode is live.
  Here it only records `action = notified`. The tick never waits on a target lock: plan 3's
  hook starts a transient unit, as the ↩️ press does.
- `on_degraded`: only `"notify"` is accepted; `"rollback"` is the seam for automatic rollback.
- `unknown` is never treated as `ok`.

## Milestone 5 build notes

Prepared after milestone 4, from what milestones 1–4 left in the code.

- **Sources a host may have.** A host can run with Bugsink only (no healthchecks, no Matomo).
  The sources are then `errors`, `probe` and `local`; `checks` exists only with healthchecks.
  Coverage asks only for the sources the target has: `errors` when `monitor.errors` and the
  release got `SENTRY_DSN`; `probe` when `monitor.uptime`; `checks` when healthchecks is
  configured and the target has a check; `local` always.
- **Local signals need a sampler.** The tick reads the live slot's containers
  (`docker inspect`: `RestartCount`, `OOMKilled`, `State.Running`; for workers, exited) and the
  cgroup's `memory.events` `oom_kill`, and stores the counts in the watch row at the first tick
  of the episode; later ticks compare against them. This is the only baseline the watch keeps,
  and it is local. Bounded: one `docker inspect` per tick per watched target.
- **Probe signal** reads the ring file (`monitor.samples`): samples of this episode (release id
  and `since` match), three failed in a row, each within 150 s of the last.
- **Errors signal.** New issues: `first_seen` ≥ switch. Event rate: per issue with `last_seen`
  ≥ switch − window, events by `timestamp` (caps: 20 issues, 500 events a tick, as planned).
  Bugsink calls share the tick's deadline, after the probes.
- **Order inside a tick:** probes first (cheap, bounded), then watches (open, evaluate,
  notify), then pings. A watch evaluation that runs out of time leaves coverage unchanged for
  that tick, which `unknown` then shows.
- **Config:** `[monitoring.watch]` (`window`, `button_ttl`) and the target's `watch` stop being
  refused; `on_degraded` accepts only `"notify"`.
- **Notification:** `notify.render` reads the live episode's watch row and adds its line;
  `degraded` and `unknown` send a reply to the history message (`notified` trails `state`). The
  ↩️ keyboard is milestone 6, so milestone 5 edits carry no `reply_markup` yet.
- **Gate on a test host:** turn `watch` on for the test target; release it; quiet window → `ok`.
  Release again and call its test-error endpoint 10+ times in the window → `degraded`
  (event rate). With Bugsink unreachable for the window (wrong `[crashes]` port on purpose) →
  `unknown`.

## Failure matrix

| Step | Recorded first | Crash right after → reconcile / next run | Outcome |
|---|---|---|---|
| remote create (check, project, site) | nothing; the name is deterministic | next `ensure` finds exactly one by name and writes the row; two matches → `monitor-fail` ambiguous, nothing guessed | release unaffected |
| capability written | — | row written by the next `ensure` | release unaffected |
| `monitors` row written | capability, remote resource | the DSN secret is repaired from the row's remote id by the next `ensure` | release unaffected |
| DSN secret written | row | — | release unaffected |
| plan made with or without the variables | the plan | as any plan | as today |
| job checks converged | `monitors` spec | next tick compares spec and calls again | live |
| job ping start / end | job event | none; the check goes late and alerts that evidence is missing | job's own |
| probe sample | — | dropped if anything changed around it | live |
| watch opened | release live | the tick opens it from `live.since`, or as `unknown` past the window | live, watched |
| tick evaluated, state changed | `watches` row, one transaction | next tick sees the state; notification resent until `notified` matches (may duplicate) | live |
| `on_degraded` hook | `degraded` row | `action` empty → hook runs again next tick | live |
| ↩️ button made | hash and fixed target in the row | keyboard missing: posted again by the next history edit; a stale keyboard's token no longer matches | live |
| ↩️ pressed | hash consumed + `button` event | the unit died before planning: nothing changed; the press is spent, `huta rollback` on the CLI works | live |
| ↩️ planned | `action` + plan with its buttons | not approved: the plan waits (30 min) with its buttons | live |
| ↩️ rollback running | an ordinary release (phase files) | exactly as a release: the existing matrix | live / nothing-activated |

`reconcile` gains only local work: closing watches whose episode is no longer live, on every
return path. It makes no network call and never fails on monitoring.

## Milestones

Each is one install; each leaves every target working. The rules of `BUILD-v2.md` apply.

| # | What | Gate | Tests | Status |
|---|---|---|---|---|
| 0 | API survey checked with real tokens on a test host, shapes kept as fixtures; fake services in `tests/fakehost.py`; `[monitor]`, `monitor`, `watch`, `matomo`, `monitor_env` refused by name | all tests green; golden unchanged | `test_app_toml` (refusals), `test_monitor` (fake API shapes) | built; real-token checks of the writing calls pending |
| 1 | Config, `huta secret set _platform`, migration 4, clients, `ensure`, check ids in the redactor, `huta monitor status\|ensure` | `ensure` twice on a test target creates each thing once; with the API down a release still goes live | unit: parsing, URL rules, hostile answers (oversize, redirect, wrong shape, two matches, bad DSN); redaction of a bare uuid; crash: `MonitorEnsure` (die after each create, capability, row and the DSN secret), API down; old golden cases byte-identical (the plan's `effective` doesn't change until milestone 2) | built; gate passed on a test host (Bugsink; healthchecks and Matomo not configured there) |
| 2 | Env injection, one effective egress list, `[monitor] env` and `monitor_env` accepted | a test release shows the variables, the plan shows the egress line; an event reaches Bugsink with the release sha | golden: new case `notes-monitored` (compose and effective), old cases byte-identical; unit: registry value wins, DSN missing → variable absent and plan says why, component filtering; crash: a monitored release with every service down plans and goes live | built; gate passed on a test host: the release's plan showed the variables and the egress line, and its test event reached Bugsink tagged with the release sha |
| 3 | Job checks (tick convergence, OnCalendar schedules) and pings | a job run turns its check up; a failing job turns it down; removing a job pauses it | unit: schedule, fallback period and grace (weekday schedule), exit code mapping; ping failure doesn't change the exit; crash: tick killed after a convergence call | |
| 4 | Tick: timer, budget, probe, self-ping; `public_check` requires `ok` | stop a test slot → uptime check down within its grace; a release in progress is skipped | unit: skip and revalidation rules, sample ring, budget; `test_monitor_tick` | built; gate passed on a test host: the live container stopped by hand gave `probe-down` (502) within a minute, started again gave `probe-up` |
| 5 | Watch: episodes, signals (local included), coverage, verdicts, history lines, `huta watch`, `status` | a release that throws on a test target ends `degraded` with reasons; a quiet one `ok`; API down for the window → `unknown` | unit: each signal, each state change, coverage thirds, re-evaluation from rows; crash: tick killed after each write, twice (`WatchTick`) | gate passed on a test host: a quiet release ended `ok`, a throwing one `degraded` (event rate), one with the error tracker unreachable `unknown` |
| 6 | ↩️ button and the press; `ready`, `blocks`, `--ack`, the `on_degraded` hook | one press rolls a stateless test target back; a forged, reused, foreign or expired press is refused; a one-writer target gets a plan with buttons, not a rollback | `test_press` (as the approval-button tests); crash: `WatchRollback` (unit dies before planning, after planning, after approval) | built; gate pending |

New durable steps become fake-host steps by wrapping `monitor.ensure`'s writes, the tick's
convergence and row updates, `watch.open` and the press unit's steps, so both crash tests cover
them. The fake services keep their state in files and count creates.

## Docs to update (same commit as the behavior)

- `docs/ARCHITECTURE.md`: contract blocks (huta.toml, registry, app.toml), "What huta
  generates" (timer, env), a "Monitoring" section, crash safety, commands, the relay limit for ↩️;
  "Not yet" loses monitoring hooks.
- `README.md`: status paragraph and layout (`monitor.py`, `watch.py`).
- `site/public/docs/contract.md` (keys), `runtime.md` (the variables), `commands.md`
  (`monitor`, `watch`, the status answer), `approval.md` (↩️ button, watch lines), `faq.md`,
  `rules.md`, `start.md`; `site/public/host.md` (tokens, tables, the timer).
- Their copies in `site/public/llms-full.txt` and `llms-full.md`, and the index lines in
  `llms.txt` / `llms.md` (`tests/test_site_docs.py` checks the twins).
- `systemd/huta-monitor.{service,timer}`; `install.sh` installs them.
- This file's status line and the milestones' status column.

## Decided

The owner decided the open questions on 2026-11-25, each as proposed:

1. **↩️** is one tap only for a one-tap-clean plan; otherwise the plan waits with its usual
   buttons. `button_ttl` default 2 h after the window.
2. **The repo asks** (`[monitor] env`) **and** the registry grants, as every other feature.
3. **DSN egress** is added by huta when the DSN is injected, and shown in the plan.
4. **Watch defaults** 15 min, 1 new issue, 10 events, ×3; a watch only where `watch` is set.
5. **`unknown`** alerts the owner (a reply in Telegram).
6. **Self check** says only that the tick ran.
7. **Staging** gets its own Bugsink project and Matomo site, per target.
8. **No `MATOMO_URL`**; the registry's `env` can carry a public tracking URL.
9. **Local signals** (restarts, OOM kills, worker exits) are in the watch now, milestone 5.
10. **Matomo sites** are created when `MATOMO_TOKEN` is set; `matomo = <id>` adopts one without
    it.
11. **The uptime probe doesn't need healthchecks** (decided during milestone 2): huta keeps the
    samples itself and pings a healthchecks check only when one is configured.

## How to build this

1. **Review first.** Give this file to three independent reviewers (codex, grok, agy), each
   without the others' notes. Fold the findings in, record the decisions the owner makes on the
   open questions under a "Decided" heading, and only then write code.
2. **Order:** milestones 0 → 6, one at a time; the next starts when the gate passed on a test
   target. Never start with a real production target.
3. **Before every commit:** `python3 -I -m unittest discover -s tests -t .` passes, golden
   included. Only the new monitored snapshot may be rewritten, and the commit says why.
4. **Every new durable step** is a fake-host step with its crash scenario before the code.
5. **Docs in the same commit** as the behavior, site docs and both `llms-full` copies
   included. Anything not built stays marked *planned*.
6. **Small commits**, present-tense subject, the why in the body, no AI attribution.
7. **This repo is public.** No real hostnames, addresses, ids, tokens, chat names or owner
   quotes in code, tests, docs or commit messages; examples use `example.org`. The operator's
   settings and host-specific checks stay in the operator's own records.
8. **Untrusted input:** every API answer. A monitoring failure is an event, never an outcome.
9. **Install by hand** from a commit, after the owner has seen the diff.
10. After milestone 6, the same three reviewers read the code before plan 2 starts.
