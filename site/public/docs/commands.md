# Commands

There is one command name, `huta`. As a **normal user** (on dev) it is the client: it builds
and asks. As **root** (on the host) it is the platform: it verifies, plans and acts. Run the
client as a non-root user; as root you get the host commands instead.

## Dev side (you)

### `~/.config/huta/client.toml`

One of two transports. All values are set up by the operator (or by `install-client.sh` for
push); the ones below are examples, not defaults.

```toml
transport = "pull"            # a dev box on the same host
[pull]
inbox = "/huta/inbox"         # you write requests here
outbox = "/huta/outbox"       # the host writes answers here (read-only for you)
```

```toml
transport = "push"            # any dev box; written by install-client.sh
[push]
registry = "registry.example.org:4882"   # the host's registry
ssh = "huta-mybox@host.example.org"      # this box's own user on the host: huta-<box>
port = 22
key = "~/.ssh/id_huta_push"              # a key made for huta only
known_hosts = "~/.config/huta/known_hosts"  # the host key, pinned at install time
```

If `huta` is not on your `PATH` or `client.toml` is missing, ask the operator.

`huta` with no command prints a short overview: which server this box ships to (or that it isn't
set up yet) and the commands below. `huta <command> -h` shows a command's options.

### `huta login` (push only)

Gets this box's own registry login from the host over ssh (request op `credentials`) and passes
it to `docker login <registry> -u <box> --password-stdin`. huta never prints or stores the
password. Output: `logged in to <registry> as <box>`. Run it once after install, and again if
`docker push` says you are not logged in. On pull: `` `huta login` is only for the push transport``.

### `huta self-update`

Replaces this client with the one the host would install (request op `client`, over the box's
own transport). Output: `client <old> → <new>`, or `client already at <rev>`.

You rarely need it: **the client keeps itself at the host's version.** Every answer names the
host's revision; when it differs from this client's, the client updates itself after the
command and prints `huta: client updated to the server's version <rev>` on stderr. If the box's
`client.toml` has `auto_update = false`, it prints
`huta: the server runs <x>, this client <y>; update with huta self-update` instead. If an
update fails, the command's own result still stands; it says `couldn't update the client …`.

### `huta pack [--app deploy/app.toml]`

Builds the image for `HEAD` of the current git checkout.

- refuses uncommitted changes to tracked files (releases are commits);
- reads `--app` (default `deploy/app.toml`); needs exactly one `[images.*]`;
- exports the commit with `git archive HEAD` into a temporary directory and builds **from that
  export**, never from the working tree. Untracked and git-ignored files (a `.env`, a local
  build output, `node_modules`) never reach the image. Anything the image needs must be
  committed or produced by a build stage in the Dockerfile;
- runs `docker build -q -f <dockerfile> --platform linux/amd64 --provenance=false
  --build-arg RELEASE_SHA=<sha> --label org.opencontainers.image.revision=<sha>
  --label run.huta.project=<project> -t huta-build/<project>:<sha12> <context>`.
  Always `linux/amd64`, also on arm64 machines (emulated, slower).

Output:

```
building huta-build/myapp:0123456789ab from 0123456789ab (a clean export of the commit)
built huta-build/myapp:0123456789ab (sha256:4f2c9a1b0e7d)
next: push 0123456789ab to main if it isn't there, then `huta submit <target>`
```

### `huta submit <target> [--app deploy/app.toml] [--no-wait | --follow]`

Asks the host to plan `HEAD` for `<target>`. There is no default target. Needs
`huta-build/<project>:<sha12>` to exist locally (run `huta pack` first).

While it waits it says what is happening, in lines starting with `…`: when the host picks the
request up (it then checks the commit and imports the image, which can take a minute for a
large image), and `still working (N s)` every 30 s. `huta pack` prints `still building` the
same way.

With `--follow`, after the plan it keeps reporting until the release ends: the owner's
approval (button or CLI), each recorded step, then `outcome: <state>`. It exits 0 only for
`live`. Ctrl-C stops following, not the release. Use it when you want to tell the human the
result without asking them.

- **pull:** writes the request into the inbox and waits up to 10 minutes for the answer in
  the outbox. With `--no-wait` it prints
  `sent request <id>; the answer will be in <outbox>/<id>.json` and exits 0.
- **push:** first `docker push`es the image to the host's registry and prints
  `pushed huta-build/<project>:<sha12> (sha256:...)`, then sends the request, with the digest
  the registry reported, over ssh and waits for the answer (up to 20 minutes). `--no-wait` has
  no effect on push.

Each run sends a request with a fresh id; the host accepts an id only once. To retry, just run
the command again (and `huta pack` again first if `HEAD` moved).

Request:

```json
{"v": 1, "id": "<16 hex>", "op": "submit", "project": "myapp", "target": "<target>",
 "sha": "<40 hex>", "image": "huta-build/myapp:<12 hex>"}
```

(push adds `"digest": "sha256:<64 hex>"`.)

On success it prints the plan, then `waiting for the owner's approval`:

```
plan 1a2b3c4d · myapp → prod · state planned
  (nothing live) → 0123456789ab   image huta/myapp:0123456789ab-4f2c9a1b0e7d
  image id sha256:4f2c9a1b0e7d...
  hosts: myapp.example.org · uid 20001 · slots [18300, 18301] · limits {'memory': '64m', 'cpus': 0.5, 'pids': 64}
  env: PUBLIC_ORIGIN · secrets (names): API_TOKEN
  egress: api.example.com:443 · upstreams: vpn-exit
  data: 2g at /data · NOT concurrent-safe: the old slot stops first (a few seconds of 502) · backup hourly · local copy 0.4 h ago, last ship ok, off-site 0.4 h ago
  jobs: rotate (*-*-* 03:10:00) · smoke: 2 checks
  class: infra — first release of this target
  checks: on origin/main ✓ · CI green · built by dev
  plan hash 0f1e2d3c4b5a6978 · expires 14:32

waiting for the owner's approval
```

If the sha is older than the live one, or not based on it, the plan has a `⚠️ DOWNGRADE` or
`⚠️ NOT BASED ON LIVE` line under the shas, and the client repeats it on stderr after the plan.
That is usually a stale checkout or another session's older commit: if you didn't mean it, ask
the owner to reject the plan, then update and submit again. huta doesn't refuse it.

`1a2b3c4d` is the **release id** (8 hex). The `env`, `egress`, `data` and jobs lines appear when
the target has them. `class` is `code-only` when only the image changed, `infra` when anything
else changed; then it names what changed (`changed: egress, jobs`). `CI` reads `not required`
when the target does not require CI.

On refusal it prints `refused: <ExceptionType>: <reason>` to stderr and exits 1. See
[faq.md](faq.md).

### `huta status <project> <target>`

`status`, `log`, `jobs`, `logs`, `egress-log`, `traffic`, `crashes`, `diag` and `submit` work only for projects whose host registry names
this box in `boxes`; anything else answers `no project '<name>' for box <box>` (or `no release
<id> for box <box>`), exactly as if it didn't exist.

That target's **last 10 releases**, newest first: `id  MM-DD HH:MM  sha12  state`, then what its
containers are doing **now**:

- per slot: the release and sha it runs, live or idle, Docker's state, when it started, its
  restart count, its last exit code and time, and `OOM-KILLED` when the kernel killed it;
- for a running slot: memory now and peak against the limit with `memory.events` (`oom`,
  `oom_kill`, `max`), pids now and peak against the limit with `pids.events max` (forks refused
  at the limit), and cpu in cores over a 1 s sample against the limit;
- for the live slot: one health request now (`ok`, or what was wrong) and how long it took;
- with data: the volume's use and size, and the age of the last local copy and off-site ship;
- with egress or upstreams: open connections to the egress daemon against its cap of 256.

```
1a2b3c4d  11-09 14:02  0123456789ab  live
9f8e7d6c  11-08 10:15  fedcba987654  retired

now:
slot a (idle): release 9f8e7d6c sha fedcba987654 · exited · started 2026-11-08T09:15:02Z (1.2 d ago) · restarts 0 · last exit 0 at 2026-11-09T13:02:40Z (1.0 d ago)
slot b (live): release 1a2b3c4d sha 0123456789ab · running · started 2026-11-09T13:02:21Z (1.0 d ago) · restarts 0
  memory 99 MiB now, peak 101 MiB, limit 768 MiB · memory.events oom 0 oom_kill 0 max 0
  pids 18 now, peak 24, limit 128 · pids.events max 0 (forks refused at the limit)
  cpu 0.02 cores (1 s sample) of 1
  health /healthz now: ok in 11 ms
data: 49 MiB used of 1946 MiB (3 %)
backup: local copy 0.8 h ago, last ship ok, off-site 0.8 h ago
egress: 0 open connections (cap 256)
```

### `huta log <release>`

The **last 50 events** of one release, oldest first: `MM-DD HH:MM:SS  kind  message`.

```
11-09 14:02:10  planned            myapp/prod 0123456789ab
11-09 14:02:10  submitted-by       from devbox (pull)
11-09 14:05:40  button             Deploy pressed in Telegram
11-09 14:05:41  approved           approved with the Telegram button
11-09 14:05:41  running            starting slot b on 127.0.0.1:18301
11-09 14:05:44  live               live on slot b
11-09 14:05:58  public-ok          https://myapp.example.org/healthz reports 0123456789ab
```

### `huta logs <project> <target>`

The target's container output (stdout and stderr), redacted by the host. Options:

| option | meaning |
|---|---|
| `--since` | `90s`, `15m`, `1h`, `2d` ago, or a UTC time `2026-11-25` / `2026-11-25T10:00[:00]`; default `1h` |
| `--until` | the same forms; default now |
| `--grep` | a plain substring, case-insensitive, at most 200 characters (no regex) |
| `--tail` | the last N matching lines, 1..5000; default 500 |
| `--slot` | `a`, `b`, `live` or `previous` (the other slot) |
| `--release` | one release's lines (8 hex) |
| `--job` | a job's output instead of the slots' (only if the host sets `logs.jobs = true`) |

One line per output line, oldest first, times in UTC:
`<time> slot-<a|b> <release> <out|err> <text>` (or `job:<name>` instead of the slot).
A first line names the range and filters; a line in `‹…›` says when lines were left out (more
matches than `--tail` or the answer size of about 900 KB, a scan stopped after 20 s, or the
oldest hour still kept). Output with no newline is cut at 64 KiB, a line at 8192 characters.

```
myapp/prod logs 2026-11-25T09:00:00Z .. 2026-11-25T10:01:00Z (UTC), slot b
2026-11-25T09:58:01.204113Z slot-b 1a2b3c4d out listening on :8080
2026-11-25T09:59:12.880042Z slot-b 1a2b3c4d err fetch failed: token=‹redacted›
```

What you get and what you don't:

- **A slot that never became healthy** keeps its output: a release that ends
  `nothing-activated` or `needs-human` records the slot's state in `huta log <release>`
  (`slot-state`: exit code, OOM kill, restarts) and its lines are readable at once with
  `--slot` and `--release`.
- **Releases and slot swaps don't lose lines**: the host stores them every minute and right
  before it recreates a slot container.
- **Kept** 7 days and 200 MB per target by default (the host can set other values), and a
  host-wide cap; the oldest hours go first.
- **Redaction is a gate, before `--grep` and before any cut:** every live secret value of the
  host and every value replaced or removed in the last 30 days, also as parts (list entries,
  `k=v` values such as cookie pairs, every URL `user:pass@` inside it and its password) and as
  URL-encoded, JSON-escaped and base64 forms. Parts shorter than 8 characters are not redacted
  on their own, except `:pass@` and `user:pass@` (any length from 3), a bare `user:pass` (from
  6) and the base64 of `user:pass`.
  Lines holding one of the project's `log_drop` strings (host registry) are never stored.
  It is a second layer: don't print secrets.
- Every read is recorded on the host (who, which filters, how many lines, never the lines).
- Only stdout and stderr: a container has no syslog socket, so log to stdout.

### `huta egress-log <project> <target> [--since 1h] [--until …]`

What the egress proxy refused for this target, per minute: `<minute> <count> <what and why>`,
for example `api.example.com:443 is not on the allowlist`, `… resolves to a non-public
address`, `upstream vpn-exit unreachable: ConnectionRefusedError`, `too many open
connections`. A name that is not on the allowlist is shown masked (its last two labels and a
hash), since an app could put data in a name. No paths or payloads. At most 8 days at a time.

### `huta traffic <project> <target> [--since 1h] [--until …]`

An edge summary from the target's access log: per minute, requests by status class
(`2xx 41  4xx 2  5xx 1`), then each 5xx with its time, status, duration and method. A `502`
means the slot didn't answer. No client addresses, paths, queries or headers. Needs
`access_log = true` (the default).

### `huta crashes <project> <target> [--since 1d]`

`--since` looks back at most 90 days (the other reads: 8).

Crash reports from the host's Bugsink for the target's Bugsink project (the host registry's
`crashes`), newest first, at most 20 issues: type and message, count, first and last seen,
then the newest event's release, environment and stack trace (up to 80 lines). Redacted like
logs. The DSN is the owner's: send it as a secret only when the owner says so.

### `huta diag <project> <target> <name>`

Runs a `[[diag]]` of the live release ([Runtime](runtime.md#diagnostics)): no network, no
secrets, the data's last backup copy read-only. Prints one line with the exit status,
duration and the copy's age, then the output. Exits 1 when the diagnostic did.

### Services huta doesn't run (`legacy`)

When the host registry lists `[legacy.<name>]` for your project, `huta logs <project> <name>`
reads that service's journal (`--since`, `--until`, `--grep`, `--tail`; no slots or releases)
and `huta crashes <project> <name>` its Bugsink project. Its own env file's values are
redacted too; if the host can't read that file, nothing is shown. Read-only: huta never
changes that service.

### `huta jobs <project> <target>`

The live release's jobs (schedule, timeout, `after each release` when set, next run) and the
outcomes of the last 20 runs, newest first: `MM-DD HH:MM  kind  message`, where kind is
`job-ok`, `job-fail` or `job-queued` (started after a release). Exit status, duration and peak
memory against the limit (`OUT OF MEMORY` if the kernel killed in it) only; a job's output
stays on the host (`huta logs --job <name>` reads it where the host allows it). Read-only: there
is no dev-side way to run a job.

```
pulse                    *-*-* *:25:00            timeout 600 s · after each release · next Mon 2026-11-09 10:25:40 CET
rotate                   *-*-* 03:10:00           timeout 600 s · next Tue 2026-11-10 03:10:12 CET
last runs:
  11-09 09:25  job-ok     pulse: exit 0 in 5 s · peak 412 MiB of 768 MiB
  11-09 08:40  job-queued pulse: after the release
```

### `huta secret set <project> <NAME>` / `huta secret import <project> NAME… --from <file>`

Sends a value for a secret the host registry already lists for the project (request op
`secret`, over the box's own transport; the box must be in the project's `boxes`). `set` reads
the value from stdin (not echoed on a terminal; one trailing newline is dropped); `import`
reads named keys from an env file (`KEY=VALUE`, comments, `export `, simple quotes) and sends
one request per name. Never put a value on the command line.

The host **stages** the value (root-only, never answered, logged or shown) and answers:

```
API_TOKEN staged for myapp (used by prod). It goes live with the next release of prod the owner
approves; its plan lists it. With no code change, submit the live commit again.
```

The next plan for a target that uses the name lists
`🔑 new value for secret API_TOKEN, sent by a dev box 11-09 14:02; it goes live with this release`
(plus `and also reaches <targets>` when other targets use the name),
and the value replaces the live one only when the owner approves that plan. If that release
then ends `nothing-activated` or `rolled-back`, the old value is live again and the new one is
staged again (the release's log says so). Refusals: a name the registry doesn't list (`myapp
has no secret API_TOKEN in the host registry; ask the operator to add the name`), an empty
value, more than one line, a control character, or over 8192 characters (a multi-line secret,
such as a PEM key, is set by the operator on the host). While a release of the project is
applying values, the host answers `a release of myapp is applying secrets right now; send the
value again in a few minutes`. There is no way to read a value back.

### Answers

Each answer is one JSON object: `{"v": 1, "id": "<request id>", "ts": <unix>, "ok":
true|false, ...}`. `id` is `null` when the host could not parse the request. The client refuses
an answer whose id is not its request's. On `ok: false` the field `error` holds the reason.
Submit answers carry `release`, `state` and `text` (the plan). Push answers are exactly one
JSON line on the ssh connection; pull answers are `<outbox>/<id>.json`, kept up to 7 days.

## Release states

| State | Meaning |
|---|---|
| `planned` | verified and planned; waits for approval (30 min). Expiry is applied on the next approve attempt, so an old plan can still show `planned` |
| `approved` | approved; the recipe is about to run |
| `running` | the new slot is starting; no traffic yet |
| `live` | serving traffic |
| `retired` | was live, replaced by a newer release |
| `rejected` | the owner said no (Reject button or `huta reject`) |
| `superseded` | not approved in time, or prod or host config changed before approval: submit again |
| `nothing-activated` | **outcome:** traffic was never switched to it. If an earlier release was live it still serves; on a first deploy the site still answers 503 `not released yet` (or an adopted site's old config still serves) |
| `rolled-back` | **outcome:** it went live, its public checks failed, and huta switched traffic back to the release before it (or to an adopted site's old config) |
| `needs-human` | **outcome:** the operator must decide or repair (`huta log <release>` says why; `huta reconcile` repairs state from what the vhost routes to) |

Every **approved** release ends in one of four outcomes: `live`, `nothing-activated`,
`rolled-back`, `needs-human`. A submit refused before planning creates no release; a plan that is never
approved stays `planned` or becomes `superseded` (or `rejected`).

## Host side (operators, as root)

You, the agent, cannot run these. They are listed so you can tell the human exactly what to do.

| Command | Does |
|---|---|
| `huta check` | validate `/etc/huta/huta.toml` and every `projects.d/*.toml` (no shared uids, ports or hosts) |
| `huta edge <project> <target> [--apply]` | certificate, vhost, Cloudflare per-host SSL rule, and a proxied A record for hosts with no address record (A/AAAA/CNAME); dry run without `--apply` |
| `huta import-dev <project> <ref>` | import `huta-build/<project>:<sha12>` from the dev box by hand |
| `huta submit <project> <target> <sha> <image>` | verify and plan by hand |
| `huta show <release>` | print a plan |
| `huta approve <release> [--via cli\|telegram]` | re-check everything and run the release. `--via telegram` is what a Deploy button press runs (as a transient systemd unit); it only changes how the approval is recorded. Default `cli` |
| `huta reject <release>` | reject a planned release |
| `huta rollback <project> <target>` | **plan** a release of the previous image; it then needs `huta approve` |
| `huta reconcile <project> <target>` | repair state from what the vhost actually routes to |
| `huta status [project] [-n N]` | recent releases on the host (default 20) |
| `huta status <project> <target> [-n N]` | that target's releases, then its containers now (as on dev) |
| `huta logs <project> <target> [options]` | the target's logs, same options and redaction as on dev; collects first, so it is up to the moment |
| `huta egress-log` / `traffic` / `crashes <project> <target> [--since …]` | as on dev |
| `huta diag <project> <target> <name>` | as on dev; recorded with the operator's name |
| `huta logs-collect` | store new slot output of every target, then apply the size caps (run every minute by `huta-logs.timer`) |
| `huta log [release] [-n N]` | events (default 50) |
| `huta intake` | pull: process the inbox (run by `huta-intake.service`, triggered by `huta-intake.path`) |
| `huta push-request` | push: one request from `huta-ssh` (run by `huta-push.socket`). A refused request is answered and exits 0; only a huta error fails the unit |
| `huta enroll <box> --from <ip> --pubkey '<key line>'` | push: create or update a box (user `huta-<box>` in group `huta-push`, its key pinned with `from="<ip>"`, its registry login), restart the registry, print the client settings as one JSON line for `install-client.sh --settings`. Re-run to replace the key |
| `huta revoke <box>` | push: remove the box's user, its key and its registry login; restart the registry |
| `huta client-bundle` | write the client (`install-client.sh` + `src/huta`) as a `.tar.gz` to stdout: `ssh <host> sudo huta client-bundle \| tar -xz` |
| `huta press` | one Telegram button press from the relay plugin (run by `huta-press.socket`); checks owner, chat, secret, expiry, then approves (`--via telegram`) or rejects. A refused press (stale, reused, not the owner) exits 0; a wrong relay peer, bad config or huta error fails the unit |
| `huta secret list <project>` | secret names, when each was set, which the registry needs but are NOT SET, and which have a new value from a box waiting for an approved release; never values |
| `huta secret set <project> <NAME>` | set one secret; the value is read from stdin (not echoed on a terminal). Drops a value a box staged for that name |
| `huta secret import <project> NAME… --from <env file>` | copy named keys from an existing `.env` (without names: every secret the registry lists); drops staged values for those names |
| `huta secret rm <project> <NAME>…` | remove secrets (live and staged) |
| `huta secret discard <project> <NAME>…` | drop a value a box staged, keep the live one (a plan that listed it can no longer be approved) |
| `huta data status <project> <target>` | volume size, use, mount, last local and off-site backup |
| `huta data ensure <project> <target>` | create, mount or grow the volume as the registry says (a release does it too) |
| `huta data import <project> <target> <dir>` | copy a directory into the volume (nothing of the target may run), set ownership, back it up |
| `huta data export <project> <target> [path…] > out.tar` | a tar of the last backup copy (or parts of it) |
| `huta backup <project> <target>` | consistent copy, then the platform's ship command (the generated timer runs it on the target's schedule) |
| `huta job list <project> <target>` / `huta job run <project> <target> <name>` | the live release's jobs, next runs and recent results / run one now |
| `huta units <project> <target>` | rewrite the target's job and backup timers from the live release |
| `huta egress sync` | refresh the egress daemon's policy (releases do it too) and print it |
| `huta stop` / `start` / `restart <project> <target>` | stop the slots (the site answers 502), start the routed slot again, or recreate it with the current env and secrets |

CLI approve and reject retire the release's Telegram buttons.
