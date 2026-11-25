# Plan: monitoring hooks

Order among the post-v2 plans: 1 of 4 (monitoring → snapshot → unattended → plain TLS).

Status: **plan**, nothing built. Everything below is *planned*. To be reviewed by three
independent reviewers before any code (see "How to build this").

## Goal

huta adds no monitoring stack. It wires each target into services the operator already runs:
healthchecks (job and uptime checks), Bugsink (errors, Sentry protocol) and Matomo (analytics).

- Per target, created once and found again by name: a healthchecks check per job, an uptime
  check fed by a probe of the public health URL, a Bugsink project and DSN, a Matomo site.
- Injected env: `SENTRY_DSN`, `SENTRY_RELEASE=<sha>`, `SENTRY_ENVIRONMENT=<target>`,
  `MATOMO_SITE_ID`.
- huta pings a check of its own.
- A **watch window** after each release: error rates and failed checks compared with before the
  switch, a verdict recorded, a ↩️ button in Telegram. Unattended releases (plan 3) build on it.
- Every integration is optional. A monitoring API that fails never fails a release.

## Non-goals

- No metrics store, dashboards, alert routing or on-call logic: the services do that.
- huta doesn't install, upgrade or back up those services.
- No automatic rollback. `on_degraded = "rollback"` is a reserved value, refused by name.
- No unattended approval here; only the interface it needs.
- No deleting of remote checks, projects or sites, ever (a removed job's check is paused).
- Gateways get no monitoring in this plan.

## Contract (all *planned*)

**`huta.toml`**. Each table is optional; a missing table turns that integration off host-wide.

```toml
[monitoring]
prefix = "huta"                    # remote names: <prefix>-<project>-<target>[-<job>]
[monitoring.healthchecks]
url = "https://hc.example.org"     # management API and ping base; token: HEALTHCHECKS_API_KEY
self = true                        # huta pings its own check, <prefix>-self-<platform.host>
[monitoring.bugsink]               # API address and Host come from [crashes]; token: BUGSINK_TOKEN
dsn_host = "bugs.example.org"      # where containers send events (granted as egress, :443)
[monitoring.matomo]
url = "http://127.0.0.1:8081"      # token: MATOMO_TOKEN
host = "stats.example.org"         # Host header, as [crashes] host
[monitoring.watch]
window = "15m"                     # default for targets; 1m..2h
```

URLs are `http://` on loopback or `https://` to a hostname. Calls: no proxy, no redirects,
10 s, 8 MiB, as `observe._bugsink` today.

**Platform secrets** (`secrets/_platform/`, set with `huta secret set _platform <NAME>`):
`HEALTHCHECKS_API_KEY`, `BUGSINK_TOKEN` (exists), `MATOMO_TOKEN`. Generated per target, stored
as project secrets like the Postgres passwords: `HUTA_SENTRY_DSN_<TARGET>`. Ping URLs are
capability URLs: kept in `huta.db` (root-only), added to the redaction needles, never printed.

**Registry** (`projects.d/<p>.toml`, per target):

```toml
[targets.prod]
monitor = { uptime = true, jobs = true, errors = true, analytics = true }
watch = { window = "15m", new_issues = 1, events = 10, factor = 3.0, on_degraded = "notify" }
```

- A key that is `true` while its host table is missing: `huta check` refuses it.
- `crashes = <id>` stays. With `errors = true` huta adopts that Bugsink project, not a new one.
- A name the registry already sets (`SENTRY_DSN` in `env` or `secrets`) wins; huta injects
  nothing over it and `huta check` says so.
- `watch` needs at least one signal source (`uptime`, `jobs` or `errors`).

**`app.toml`** (the repo asks for what changes its container):

```toml
[monitor]
env = ["sentry", "matomo"]
```

Checks and the probe need no ask: they don't touch the container. Env is injected only when
the repo asks **and** the registry grants. Until its milestone, `parse_app_toml` refuses
`[monitor]` by name. A release must not need these variables to start: they can be absent.

**Store**: migration 4 adds `monitors` (project, target, kind, name, remote id, ping URL,
state, updated; key project+target+kind+name) and `watches` (release id, project, target,
opened, until, state, reasons, last tick, button hash, chat, message id, policy).

**Commands**: `huta monitor status|ensure <p> <t>`, `huta monitor tick` (timer),
`huta watch <release>`; the `status` op answers with the watch verdict of the live release.

## Design

### 1. Clients (`monitor.py`, new)

One small client per service, each with `find(name)` and `create(name, …)`. Every answer is
untrusted: strict shapes, ids matched by regex.

- healthchecks: `POST /api/v3/checks/` with `unique: ["name"]` is find-or-create in one call.
- Bugsink: find the project by name, else create; read its DSN. Its host must equal `dsn_host`.
- Matomo: find the site by its main URL, else `SitesManager.addSite` (POST, token in the body).

Milestone 0 checks each call against the real API. Where one doesn't exist, the fallback is
an id the operator puts in the registry.

### 2. `ensure`: remote resources, idempotent

`monitor.ensure(platform, st, project, t, app)` runs in `cmd_approve`, under the target lock,
after the checks and before `recipe.run`; also from `huta monitor ensure`. For each granted
kind without a `monitors` row: find by name, else create, then write the row (and the DSN
secret file). Budget: 30 s in all, so a hung API can't hold the lock.

Any failure is an event `monitor-fail` (kind and error class, redacted) and the release goes
on. It is retried by the next release, by `ensure`, and by the tick (at most hourly).

No phase file: nothing needs undoing. The remote name is deterministic, so a crash between
"created remotely" and "row written" is healed by the next `find`.

### 3. Env injection

`runtime.environment` adds the four variables for components the grants reach (the public one,
plus components whose registry entry names them). In plans `SENTRY_DSN` shows the secret
placeholder. A DSN that isn't available yet is left out and the plan says "when available";
it arrives with the next release, never by restarting a live slot.

With `errors = true`, `<dsn_host>:443` joins the target's egress allowlist; the plan shows it
as a change. `effective` gains a `monitor` key only when the target has `monitor`, so v1
dicts are unchanged.

### 4. Job checks

- `monitor.sync_jobs` runs beside `jobs.sync_units` (release step 5, `reconcile`, `huta units`):
  a check per job of the live release, period from `cfg.calendar_gap`, grace from the job's
  timeout. A removed job's check is paused.
- `jobs.run_job` pings `/start` before the container and `/<exit code>` after, 5 s timeout each.
  A failed ping never changes the job's outcome; an event is written only when pinging starts
  or stops failing.

### 5. The tick: probe, self-ping, watch

One host timer, `huta-monitor.timer` (every minute) → `huta monitor tick`, one at a time
(`flock`), holding no target lock:

- **Probe.** For each target with `uptime` and a live release: `public_check` on each host
  (expects the live sha). Success or `/fail` (short reason) is pinged to the uptime check.
  Targets with a phase file, a stop marker or a held lock are skipped: a release is at work.
  The last 60 results per target are kept in `<state_dir>/monitor/<p>/<t>.json`.
- **Self.** Registry loaded and store opened → ping the self check.
- **Watch.** Below.

### 6. Watch window

A watch is a row, not a process: nothing waits on it and no lock is held.

- **Open.** The tick opens a watch for every live release with `watch`, no row yet and
  `live.since` inside the window. `recipe.run` calls the same function when it returns `live`,
  so the row usually exists at once. Opening twice is a no-op (primary key).
- **Signals**, all computed from timestamps, so no baseline is captured at the switch:
  - *errors*: Bugsink issues first seen since the switch (≥ `new_issues`); events since the
    switch for this release against the equal window before it (≥ `events` and ≥ `factor` ×
    before). Paging is capped.
  - *checks*: a check of the target that is `down` and was up at the switch.
  - *probe*: three failed probes in a row since the switch.
- **States.** `watching` → `ok` (window over; no signal; every configured source answered at
  least once in the last third of the window) | `degraded` (a signal fired; reasons kept) |
  `unknown` (window over; a source never answered) | `closed` (the release stopped being live).
  Events: `watch-open`, `watch-signal`, `watch-ok`, `watch-degraded`, `watch-unknown`,
  `watch-closed`.
- **Telegram.** The history message gains a line (`👀 watching until 14:32`, then `✓ quiet for
  15 min` or `⚠️ <reasons>`). `degraded` and `unknown` also send a reply to it, because an edit
  doesn't notify. As ever, a failed notification changes nothing.
- **↩️ button.** While a watch is open, and for `button_ttl` (24 h) after, the live message
  carries `↩️ Roll back` if an earlier release exists. `callback_data` `huta:b:<token>`, stored
  hashed in the watch row, single use, checked like the approval buttons (owner, chat, own
  message). A press starts `huta rollback <p> <t> --from <release> --via telegram` in a
  transient unit: refused unless that release is still live; the rollback is planned as today
  and, if the plan is code-only with no warning, approved by the same press. Otherwise the plan
  waits with its normal buttons. A refusal (schema can't run the old code) edits the message
  with the reason and the `huta restore` line.

### 7. Interface for unattended releases (plan 3 uses, this plan builds)

- `monitor.ready(platform, t) -> (bool, why)`: every source the target's `watch` needs is
  configured and has its remote resource. Not ready → not eligible for unattended.
- `watch.open(st, rid)`, `watch.verdict(st, rid) -> (state, reasons)`.
- `watch.blocks(st, project, target) -> why | None`: a watch still `watching`, or a last
  verdict `degraded`/`unknown` that nobody cleared (`huta watch <release> --ack`, or a newer
  `ok`). Unattended refuses to start while it returns a reason.
- `on_degraded`: only `"notify"` is accepted; `"rollback"` is the seam for automatic rollback.
- For `docs/PLAN-unattended.md` (built after this): also provide a hook the tick calls when a
  watch turns `degraded`, and an `action` column on the watch row (what was done about it). Build
  both here, unused, so the unattended plan only fills them in.
- `unknown` is never treated as `ok`.

## Failure matrix

| Step | Recorded first | Crash right after → reconcile / next run | Outcome |
|---|---|---|---|
| remote create (check, project, site) | nothing; the name is deterministic | next `ensure` finds it by name, writes the row; never a second one | release unaffected |
| `monitors` row / DSN secret written | the remote resource exists | row and file are written again from `find` (same value) | release unaffected |
| env injected (compose written) | row + secret | as any release: compose is rewritten by the next run | as today |
| job checks synced | units synced | `reconcile` syncs units, then checks, for the live release | live |
| job ping start / exit | job event | none; the check goes late and alerts, which is true | job's own |
| watch opened | release live | the tick opens it from `live.since` | live |
| tick evaluated, state changed | `watches` row, one transaction, then the notification | next tick sees the state; a lost message is resent once (`notified` flag) | live |
| ↩️ pressed | hash consumed + `button` event | the unit died before planning: nothing changed; the press is spent, `huta rollback` on the CLI works | live |
| ↩️ rollback running | an ordinary release (phase files) | exactly as a release: the existing matrix | live / nothing-activated |

`reconcile` gains two things only: `sync_jobs` after `sync_units`, and closing watches whose
release is no longer live. It makes no other network call and never fails on monitoring.

## Milestones

Each is one install; each leaves every target working. The rules of `BUILD-v2.md` apply.

| # | What | Gate | Tests |
|---|---|---|---|
| 0 | API survey: the exact calls, written into this file; fake services in `tests/fakehost.py`; `[monitor]`, `monitor`, `watch` refused by name | all tests green; golden unchanged | `test_app_toml` (refusals), `test_monitor` (fake API shapes) |
| 1 | Config, secrets, migration 4, `monitor.py` clients, `ensure`, `huta monitor status\|ensure` | `ensure` twice on a test target creates each thing once; with the API down a release still goes live | unit: parsing, URL rules, hostile answers (oversize, redirect, wrong shape); crash: `MonitorEnsure` (die after each create and each row), `MonitorApiDown`; golden: new case `notes-monitored`, old cases byte-identical |
| 2 | Env injection, egress grant, `[monitor] env` accepted | a test release shows the variables, the plan shows the egress line; an event reaches Bugsink with the release sha | golden: compose and effective for `notes-monitored`; unit: registry value wins, DSN missing → variable absent; crash: `MonitoredRelease` (full scenario with monitoring on) |
| 3 | Job checks and pings | a job run turns its check up; a failing job turns it down; removing a job pauses it | unit: period and grace; ping failure doesn't change the exit; crash: die after `sync_jobs` |
| 4 | Tick: timer, probe, self-ping | stop a test slot → uptime check down within its grace; a release in progress is skipped | unit: skip rules, result ring; `test_monitor_tick` |
| 5 | Watch: rows, signals, verdicts, history lines, `huta watch`, `status` op | a release that throws on a test target ends `degraded` with reasons; a quiet one `ok`; API down for the window → `unknown` | unit: each signal, each state change, stateless re-evaluation; crash: tick killed after each write, twice (`WatchTick`) |
| 6 | ↩️ button and the rollback press; `ready`, `blocks`, `--ack` | one press rolls a stateless test target back; a forged, reused or foreign press is refused; a schema-bound target gets the reason | `test_press` (as the approval-button tests); crash: `WatchRollback` (press unit dies before and after planning) |

New durable steps become fake-host steps by wrapping `monitor.ensure`'s writes, `sync_jobs`,
`watch.open` and the tick's row update, so both crash tests cover them. The fake services keep
their state in files and count creates.

## Docs to update (same commit as the behavior)

- `docs/ARCHITECTURE.md`: contract blocks (huta.toml, registry, app.toml), "What huta
  generates" (timer, env), a "Monitoring" section, crash safety, commands; "Not yet" loses
  monitoring hooks.
- `README.md`: status paragraph and layout (`monitor.py`).
- `site/public/docs/contract.md` (keys), `runtime.md` (the variables), `commands.md`
  (`monitor`, `watch`, the status answer), `approval.md` (↩️ button, watch lines), `faq.md`,
  `rules.md`, `start.md`; `site/public/host.md` (tokens, tables, the timer).
- Their copies in `site/public/llms-full.txt` and `llms-full.md`, and the index lines in
  `llms.txt` / `llms.md` (`tests/test_site_docs.py` checks the twins).
- `systemd/huta-monitor.{service,timer}`; `install.sh` installs them.
- This file's status line and a status column for the milestones.

## Open questions for the owner

1. **One tap or two for ↩️?** Proposed: one tap when the rollback plan is code-only and clean,
   otherwise the plan waits with its usual buttons.
2. **Does the repo have to ask** (`[monitor] env`) for the variables, or is the registry grant
   enough? Proposed: both, as every other feature.
3. **Egress for the DSN** added by huta when `errors = true`, or listed by hand in `egress`?
   Proposed: added, and shown in the plan.
4. **Watch defaults**: 15 min, 1 new issue, 10 events and ×3. Should a watch be on for every
   target with `monitor`, or only where `watch` is set? Proposed: only where set.
5. **`unknown`**: alert the owner, or only record? Proposed: alert, since unattended treats it
   as "not ok".
6. **Self check**: only "the tick ran", or `/fail` while any target is `needs-human`?
7. **Staging**: its own Bugsink project and Matomo site per target (proposed), or one per
   project split by `SENTRY_ENVIRONMENT`?
8. **`MATOMO_URL`** as a fifth variable? Server-side tracking would also need egress; not
   proposed.
9. **Local signals** in the watch (container restarts, OOM kills from `runstate`): now or later?

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
