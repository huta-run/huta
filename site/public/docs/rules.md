# Rules for agents

## Releases are commits on `main`

- A release is one commit: the sha you submit must be on `main` of the GitHub repo in the host
  registry (`main` itself or an ancestor of it), checked by the host through the GitHub API
  at submit time and again at approval.
- The manifest is read from GitHub **at that sha**, at the path the host registry names
  (`app_toml`, usually `deploy/app.toml`). Local edits you did not push do not exist for the
  host. `huta pack --app` / `huta submit --app` only choose what the client reads to build;
  point them at the same file.
- `huta pack` refuses uncommitted changes to tracked files and builds from `git archive` of
  `HEAD`, so the image contains only what is committed. Build the commit you submit.
- If the target has `require_ci = true` (the default), the newest run of the GitHub Actions
  workflow whose top-level `name:` is `ci`, for that sha on a `push` to `main`, must be
  completed with `success` (minimal workflow in [start.md](start.md)).

## The server listens on `0.0.0.0`

- Your process must listen on `0.0.0.0` at the `port` in `app.toml`. Bound to `127.0.0.1`
  inside the container, it never answers the host's health check, and the release ends
  `nothing-activated` with `never reported <sha12>`.

## Never put secrets in the repo or the image

- Not in source, `app.toml`, Dockerfiles, build args, image layers, commit messages, logs or
  messages to the human. Refer to a secret by its name.
- A secret your app needs at run time is listed by name in the host registry (the operator
  adds names) and arrives as an environment variable
  ([Runtime](runtime.md#secrets-and-environment)). Its value is set either by the owner on
  the host (`sudo huta secret set <project> <NAME>`) or from your box with
  `huta secret set <project> <NAME>` (stdin) / `huta secret import … --from <file>`: the host
  stages it, and it goes live only with a release the owner approves. Never put a value on a
  command line, in chat or in a message to the human; give the human names only.

## You ask, the host decides

- Never write compose files, web-server config, systemd units or host registry files for the
  host. The host generates them from `app.toml` plus its own registry.
- You may write the registry *request* (the exact file) for the operator to review and
  install. See [contract.md](contract.md).
- Do not ask for a shell, Docker access or a general ssh login on the production host. The push
  transport's ssh key runs one forced command that only accepts huta requests; that is all
  you need.

## Only the owner approves

- Every release waits for the owner: the **✅ Deploy** button on the release's message in
  the configured Telegram topic, or `sudo huta approve <release>` on the host. Both run the same checks.
- Never try to press, forward, copy or relay a huta button, never post a lookalike button,
  and never ask the human to forward the approval message to you or elsewhere. The button
  strings are single-use secrets; only the owner's own press, on huta's own message in the
  configured chat, counts.
- Never write to the host's press socket or try to call `huta approve` yourself.

## Scheduled jobs

`[[jobs]]` in `app.toml` ([Contract](contract.md#jobs), [Runtime](runtime.md#jobs)):

- a job runs in the project's own image (the live release's), as the project's uid, with its
  limits, env, data and egress, never as root; never from host cron, a crontab in a container,
  or a scheduler loop inside a long-running container;
- `schedule` (systemd `OnCalendar` syntax) is required; `timeout` defaults to 10 minutes;
- minimum interval 5 minutes (huta refuses a schedule that fires more often); anything more
  frequent is the web process's own work;
- `after_release = true` runs a job once more after each release goes live; dev has no way to
  run a job on demand (ask the owner for `huta job run`);
- adding or removing a job, or changing its schedule, timeout or command, is an infra change
  and needs approval; changing only the image does not.

## Debug access to prod

**Available now**, each only for projects the host grants your box (`boxes`), read-only,
size-capped and recorded on the host (see [commands.md](commands.md)):

- `huta status <project> <target>`: the target's last 10 releases, then its containers now
  (per slot: release, state, restarts, last exit, OOM kill; memory, pids and cpu against the
  limits with the kernel's event counters; a health request now; data use, last backup and
  ship; open egress connections);
- `huta log <release>`: its last 50 events (a slot that failed to start: its exit code, OOM
  kill and restarts as `slot-state`);
- `huta jobs <project> <target>`: the live release's jobs, next runs and recent outcomes;
- `huta logs <project> <target> [--since 1h] [--grep text] [--slot live] [--release id]`: the
  target's container output, redacted by the host, kept 7 days by default, including a slot
  that never became healthy. A job's output too (`--job`) where the host allows it.

- `huta egress-log <project> <target>`: what the egress proxy refused, per minute;
- `huta traffic <project> <target>`: requests per minute by status class, and each 5xx;
- `huta crashes <project> <target>`: the target's Bugsink issues with stack traces;
- `huta diag <project> <target> <name>`: a `[[diag]]` from `app.toml`, run from the live image
  without network or secrets, against the last backup copy, read-only;
- for a service of the project that huta doesn't run (the registry's `[legacy.<name>]`):
  `huta logs` and `huta crashes` with that name.

*Planned:* `huta snapshot <project> <target>`, a sanitized copy of data that needs approval
every time.

Only `snapshot` is planned. Either way you never get a shell, Docker access
or the data directory. Data export exists on the host only: the owner runs `sudo huta data
export` and decides what to hand over.

## What "needs a human" means

Ask the human, and stop, when:

- the release outcome is `needs-human` (give the release id and `huta log <release>`);
- the outcome is `nothing-activated` on a first deploy: the site still answers 503
  `not released yet`, so nothing is serving;
- the project needs a host registry entry or a change to one;
- the project needs something the host registry doesn't grant yet (a secret, a data volume,
  an egress entry, an upstream): write the exact request for the operator;
- the project needs anything huta refuses: preflight checks, plain-HTTP egress, a data
  volume and a database together;
- a one-writer release ended `needs-human` after its switch: the human decides whether to roll
  back (`huta rollback`), because the data was already written by the new release;
- a DNS record or certificate is missing (`huta edge` is the operator's step);
- a plan expired or was superseded more than once (something is changing prod in between).

Do not work around a refusal by changing what the check looks at. Fix the cause.
