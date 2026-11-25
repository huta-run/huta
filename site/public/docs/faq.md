# FAQ: refusals and questions

Host refusals reach you as `refused: <ExceptionType>: <message>`. Client errors start with
`huta: `. The messages below are the exact strings (with example values). A refused submit
creates no release; fix the cause, commit, push, `huta pack`, `huta submit <target>` again.

## `app.toml` refusals

**`ConfigError: app.toml: components.web has unknown keys ['order'] (top-level keys must come before the first [table])`**
`order` is written after a `[table]` header, so TOML put it inside that table. Move it above the
first `[...]` line. (The same message names any other unknown component key.)

**`ConfigError: app.toml: schema must be 1`** / **`ConfigError: app.toml: project must be 'myapp'`**
Either the key is missing or wrong, **or** it sits below a `[table]` header (the same TOML
pitfall: a misplaced `schema` or `project` shows up as this message, not as "unknown keys").
`project` must also equal the project name in the host registry.

**`ConfigError: app.toml: unknown top-level keys ['name']`**
Only `schema project order build images components state preflight smoke jobs external` exist.

**`ConfigError: app.toml: no components`**: add one `[components.<name>]`.

**`ConfigError: app.toml: top-level 'order' must list every component exactly once`**
`order` is missing, not an array, or does not match the `[components.*]` names.

**`ConfigError: app.toml: huta needs 1..8 [images.<name>] tables`**.

**`ConfigError: app.toml: images.web has unknown keys ['args']`**: only `dockerfile` and
`context`. **`ConfigError: app.toml: images.web.dockerfile is required`**.

**`ConfigError: app.toml: components.web.image must name one of [images]: ['web']`**
Set `image = "<the [images.*] name>"` in the component.

**`ConfigError: app.toml: [state] accepts only 'kind', got ['kind', 'path']`**,
**`ConfigError: app.toml: [build] accepts only 'test', got [...]`**: remove the other keys.

**`ConfigError: app.toml: 'preflight' is not supported by huta yet`** (also `external`),
**`ConfigError: app.toml: exactly one HTTP component is public`**: mark one `public = true`,
**`ConfigError: app.toml: components.web.upstream_api is not supported yet`**:
the project needs a feature that is planned. Do not delete the need to get past the check; tell
the human.

**`ConfigError: app.toml: state.kind must be "none" or "data"`**: `sqlite`/`postgres` are not
kinds; an SQLite file on a `data` volume works.

**`ConfigError: app.toml: [state] kind = "data" needs components.web.data`** /
**`components.web.data needs [state] kind = "data"`**: set both, or neither.

**`ConfigError: app.toml: components.web.data must be an absolute path like /data (not a system directory)`**:
for example `/data` or `/var/lib/myapp`; not `/`, `/tmp`, `/etc`, `/usr`, `/run`, ….

**`ConfigError: app.toml mounts data at /data, but the host registry gives myapp/prod no data`**:
the operator has to add `data = { size = "…" }` to the target. Give them the size you need.

**`ConfigError: secret API_TOKEN is not set: sudo huta secret set myapp API_TOKEN`**: the host
registry lists the secret but nobody has set its value. If you have the value, send it with
`huta secret set myapp API_TOKEN` (stdin) and submit again; otherwise tell the human that command.

**`ConfigError: myapp has no secret API_TOKEN in the host registry; ask the operator to add the name`**:
a box can send values only for names the registry already lists. Give the operator the name
(and which target needs it, or which job for `job_secrets`).

**`a secret value staged by a box changed since this plan was made; submit again`**: a new value
was sent after the plan was made, so the plan no longer says what would go live. Submit again.

**`a secret's live value changed since this plan was made; submit again`**: since planning, the
operator set a secret this target uses, or another target's release made a staged value live.
Submit again; the new plan is made against the current values.

**Can I set a secret from my box?** Yes, for a name the registry lists: `huta secret set
<project> <NAME>` with the value on stdin. It is staged and goes live with the next release
of a target that uses it, once the owner approves that release; the plan lists it. You can't
read a value back.

**`ConfigError: app.toml: smoke[0]: …`**: a check has a wrong key or value: `path` must start
with `/`, `status` is an int, `headers`/`request_headers` are tables of one-line strings,
`headers_absent` is a list of header names, and
`Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, `X-Forwarded-*` can't be sent.

**`ConfigError: app.toml: jobs[0]: schedule 'every 5 min' is not a systemd OnCalendar expression`**:
use OnCalendar syntax (`hourly`, `*-*-* 03:10:00`, `*:0/15`); test with
`systemd-analyze calendar '<expr>'`. **`… fires more often than every 5 minutes`**: the minimum
interval is 5 minutes.

**`RecipeError: sha256:…: the image declares VOLUME ['/data']`**: remove `VOLUME` from the
Dockerfile (and from base images you control). Persistent data is the target's data volume.

**`ConfigError: app.toml: web.port invalid`** / **`web.health invalid`**: port must be an int
1..65535; health a path matching `^/[A-Za-z0-9/_.-]*$`.

**`ExtError: GET /repos/example/myapp/contents/deploy/app.toml: HTTP 404`**
`deploy/app.toml` is not in the commit (or the registry's `app_toml` points elsewhere).

## Commit and CI refusals

**`ExtError: 0123456789ab is not on example/myapp main`**
The commit is on GitHub but not on `main`. Merge to `main`.

**`ExtError: example/myapp: compare 0123456789ab...main failed`**
GitHub does not know the commit: you did not push it.

**`ExtError: CI for 0123456789ab (push to main) is not green`**
Only when the target requires CI: among runs of the workflow whose top-level `name:` is `ci`,
for that sha, on a `push` to `main`, the **newest** is not `completed` with `success` (none yet,
still running, failed, a re-run in progress, or the workflow has another `name:`). Wait for it,
or fix it. A minimal workflow is in [start.md](start.md).

## Image refusals

**`RecipeError: sha256:4f2c9a1b0e7d: image runs as root (USER unset)`**
Add a non-root `USER` (numeric, e.g. `USER 20001:20001`) to the final stage.

**`RecipeError: sha256:4f2c9a1b0e7d: linux/arm64, expected linux/amd64`**
Build with `huta pack` (always `linux/amd64`), not plain `docker build`.

**`RecipeError: image labels don't name this project and a full sha; refused`**
Build with `huta pack`, which sets both labels.

**`ConfigError: image says 0123456789ab, request says fedcba987654`**
The image was built from another commit than `HEAD`. Run `huta pack` again, then submit.

**`RecipeError: docker save on devbox failed: ...`** (pull; the dev container's name appears
instead of `devbox`): the image is not in dev's Docker any more. Run `huta pack` again.

**`RecipeError: docker save on devbox took longer than 900 s`** (pull): the image is too big or
the dev box too slow. Make the image smaller.

**`RecipeError: image archive larger than 2 GiB; refused`**,
**`RecipeError: archive expands past 2 GiB; refused`**,
**`RecipeError: archive has more than 20000 entries; refused`** (pull): make the image smaller
(multi-stage build, slim base, fewer files).

**`RecipeError: archive entry '...' is sparse; refused`**, **`RecipeError: archive has '...' twice; refused`**,
**``RecipeError: archive entry '...' is not a plain relative file; refused``** (pull): the
`docker save` archive is malformed. Rebuild with `huta pack`; if it repeats, tell the human.

A refused submit untags the image the host imported; nothing is left behind to clean up.

**`RecipeError: image must be named huta-build/myapp:<12 hex>, got '...'`** (push) /
**`RecipeError: dev image must be named huta-build/myapp:<12 hex>, got '...'`** (pull): the image
name in the request is not the one `huta pack` makes. Do not hand-craft requests; use
`huta submit`.

## Host registry and edge

**`ConfigError: no project 'myapp' for box mybox`**: either the host registry has no entry, or
its `boxes` doesn't name this box (the host gives one answer for both, on purpose). Write the
registry request for the operator ([contract.md](contract.md)), or ask them to add your box to
`boxes`. `no release <id> for box mybox` from `huta log` is the same: a release of a project
this box isn't granted, or none at all.

**`ConfigError: myapp has no target 'staging' (targets: prod)`**: use one of the listed targets.

**``ConfigError: no valid certificate for myapp.example.org; run `huta edge myapp prod --apply` ``**
appears at approval: the operator must set up the edge first.

## Logs and status

**`ConfigError: myapp/prod: the host doesn't let boxes read its logs (logs.boxes = false)`**:
the operator turned box access off for that target; ask them.

**`ConfigError: myapp/prod: job output is not readable from a box (the host sets logs.jobs = true to allow it)`**:
`--job` needs the operator's `logs.jobs = true`; the slots' logs need nothing extra.

**`RequestError: bad since`** (also `bad until`, `bad slot`, `bad grep`, `tail must be 1..5000`):
times are `90s`, `15m`, `1h`, `2d` or a UTC time like `2026-11-25T10:00`; `--grep` is one line
of plain text up to 200 characters.

**`huta logs` shows `(no matching lines)` for a release that failed.** Widen `--since` (the
default is the last hour), or pass `--release <id>`: lines carry the time the app wrote them.
`huta log <release>` shows the slot's exit code and OOM kill as `slot-state`.

**`‹huta: earlier lines lost: Docker rotated its log before huta read it›`**: the app wrote
more than about 30 MB between two collections (one minute); log less.

**`ConfigError: myapp/prod has no Bugsink project (crashes = <id> in the host registry)`** /
**`this host has no [crashes] in huta.toml`**: the operator hasn't connected crash reports for
that target yet.

**`ConfigError: the live release has no diag 'db-check' (it has: …)`**: the name must be a
`[[diag]]` of the **live** release; a new one is available once a release with it is live.

**`ConfigError: a diag of myapp/prod is running; try again when it ends`**: one at a time.

**`ConfigError: /opt/… is not readable (…); its logs are not shown`**: a legacy service's env
file is needed to redact its logs; ask the operator.

**`ConfigError: at most 8 days at a time`**: narrow `--since`/`--until`.

**`status` says `now: not available`**: the host couldn't read the containers' state; the
release list above it is still right. Ask the operator if it persists.

## Transport errors

**`RequestError: this request id was already used`**: every request id is accepted once, across
both transports. Run the command again; the client makes a new id each time. Do not resend an
old request file or JSON line.

**`RequestError: the request id must match its file name`** (pull): the inbox file
`<id>.json` holds a different `id`. Use `huta submit`; do not write inbox files by hand.

**`RequestError: duplicate key 'id'`**, **`RequestError: request must be an object with v = 1`**,
**`RequestError: request is not JSON`**, **`RequestError: fields for submit: missing [...], unexpected [...]`**,
**`RequestError: bad project`** (also `bad target`, `bad sha`, `bad image`, `bad release`,
`bad digest`; `bad image` means the image is not `huta-build/<project>:<12 hex>`): the request is
malformed. Use the `huta` client, not hand-written JSON.

**`ConfigError: box 'mybox' is not allowed to push`** (push): the box is not enrolled: its user
`huta-mybox` is not in the host's `huta-push` group (and the box is not in the optional
`[transport.push] boxes`). Enroll it again (`sudo huta enroll`, see
[install.md](https://huta.run/install.md)).

**`ConfigError: socket peer uid 1234 is not a huta push user`** (push): the ssh login is not a
`huta-<box>` user. Your `client.toml` `ssh` must be `huta-<box>@<host>`; if it is, the operator's
setup is incomplete.

**`huta-ssh: no complete request within 30 s`**, **`huta-ssh: request too large`**,
**`huta-ssh: host service unavailable (...)`** (push): the forced command on the host could not
read your request or reach the host service. Retry once; if it repeats, tell the human.

**`huta: the host answered request <id>, not <id>`**, **`huta: the host's answer has no request id`**,
**`huta: the host's answer is not a huta answer`**: the answer does not belong to your request.
Retry once; if it repeats, tell the human.

**`huta: no answer over ssh within 20 minutes`** (push): the host is slow or stuck. Check
`huta status <project> <target>` before retrying.

**`ConfigError: a push submit must name the pushed digest`**,
**`ConfigError: digest is only used by the push transport`**,
**`RequestError: credentials is not available over pull`**: the request and the transport do
not match; use `huta submit` with the right `client.toml`.

**`huta: no answer over ssh: ...`** (push): ssh failed. Typical causes: the key is not yet added
on the host, the host key in `~/.config/huta/known_hosts` does not match, wrong `port`. Tell
the human the stderr shown.

**``huta: docker push failed (did you run `huta login`?): ...``** (push): run `huta login`, then
submit again.

**``huta: `huta login` is only for the push transport``**: on pull there is nothing to log in to.

**`huta: no answer after 10 minutes; check /huta/outbox/<id>.json later`** (pull): the host has
not answered (intake not running, or a slow import). Read that file later; if it never appears,
tell the human.

## Client errors

**`huta: the checkout has uncommitted changes; commit first (releases are commits)`**: commit.

**`huta: git archive failed: ...`** / **`huta: could not unpack the commit`**: `huta pack`
could not export `HEAD`. Check that you are in a git checkout with a commit.

**My image is missing files that are on my disk.** `huta pack` builds from `git archive HEAD`:
untracked and git-ignored files are not in the build. Commit them, or produce them in a build
stage of the Dockerfile.

**`huta: no deploy/app.toml in this checkout (use --app)`**: run from the repo, or pass
`--app <path>`.

**`ConfigError: app.toml at <sha> names 2 images and the request brought one`**: the client is
older than the host: `huta self-update`, then `huta pack` and `huta submit` again.

**``huta: huta-build/myapp:0123456789ab not built; run `huta pack` first``**: `HEAD` moved since
the last pack, or you never packed.

**``huta: no /home/agent/.config/huta/client.toml; see `huta --help` ``**: the client is not set
up. See the prerequisites in [start.md](start.md).

**`huta: unknown transport 'ftp'`**: `transport` must be `"pull"` or `"push"`.

## Telegram button replies (the owner sees these)

The relay plugin replies in the owner's chat only when a press did not work. If the human
quotes one:

**`this button is no longer valid`**: the button was already used, or the plan was decided
another way (CLI approve or reject, expiry on an approve attempt), or a newer message replaced
it. Check `huta status <project> <target>`; submit again if needed.

**`this plan expired; submit again`**: more than 30 minutes passed; the press marked the
release `superseded`. Run `huta submit <target>` again; a new release message with new buttons
arrives.

**`only the owner can approve`**: someone other than the owner pressed. Only the owner's
Telegram id is accepted, also in a group.

**`this button doesn't belong to this chat`**: the press came from a chat other than the
configured one (`history_chat` or the owner's own chat), or from a different chat than the
message that carried this button (for example a forwarded copy). The button is now used up;
the owner approves on the command line, or you submit again.

**`not a huta button`**: the press carried data huta does not recognize.

**`huta is unavailable; use sudo huta approve on the host`**: the relay could not reach the
host's press service. The owner approves on the command line instead.

**`huta: <ExceptionType>`**: the host failed while handling the press. The owner uses the command
line; tell the operator.

A successful press gets no reply: the release's message in the topic is edited to show the
release moving (deploying, then live or not activated), and its buttons are removed. There is
no pop-up notice.

## Other questions

**The release ended `nothing-activated`. Is the site down?** Traffic was never switched to the
new release. If an earlier release was live, it still serves. **On a first deploy nothing was
live: the site still answers 503 `not released yet`.** `huta log <release>` says why (usually
`/healthz` never reported the sha within 30 s: the server listens on `127.0.0.1` instead of
`0.0.0.0`, wrong port, wrong sha, `ok` not `true`, a redirect, or the app needs a writable path
other than `/tmp`).

**It ended `needs-human`.** Stop and give the human the release id and `huta log <release>`.

**My plan says `superseded`.** It was not approved within 30 minutes, or prod or host config
changed before approval. Submit again.

**It is more than 30 minutes old but still says `planned`.** Expiry is checked when someone
tries to approve (with the CLI or a late button press): the release stays `planned` until
then, and that attempt marks it `superseded` (`plan expired; submit again`). Treat it as
expired and submit again.

**`nothing-activated` with `127.0.0.1:18300/healthz never reported 0123456789ab (...)`**: the
container never answered with the sha in 30 s. Most often the server listens on `127.0.0.1`
inside the container; it must listen on `0.0.0.0` at the `app.toml` port. Other causes: wrong
port, `/healthz` reports another sha or `ok` not `true`, a redirect, a crash at start (for
example writing outside `/tmp`).

**Are requests dropped during a deploy?** The new release is verified on its own port, the vhost
switches in one graceful reload, and the old release keeps running for the target's `drain`
seconds (default 10) before it is stopped. Short requests are fine; long requests, streams and
websockets on the old release can be cut.

**Can I deploy a static site?** Yes: a tiny static-server image. See
[dockerfile.md](dockerfile.md).

**Can my app call an external API?** Yes, over HTTPS to the hostnames the operator allows
(`egress` in the host registry), through the proxy in `HTTPS_PROXY`. Your HTTP client must
honour that variable; names don't resolve inside the container otherwise. See
[Runtime](runtime.md#egress). A refused call returns `403` from the proxy; plain `http://`
returns `405`.

**Can my app reach a service on the host or the tailnet?** Only one the operator names as an
upstream (`HUTA_UPSTREAM_<NAME>` = `huta-egress:<port>`), or a literal `ip:port` egress entry.

**Can I store data?** Yes: `state.kind = "data"` and a mount path; the operator sets the size
and the backup schedule. See [Runtime](runtime.md#data). Everything else is read-only except
`/tmp`.

**My release ended `rolled-back`.** Its public checks failed after the switch, so huta
switched traffic back to the release before it. `huta log <release>` says which check failed.

**My release ended `needs-human` with "Not switched back automatically".** The target lets only
one release write its data at a time, and the new release had already written and served.
The human decides: `sudo huta rollback <project> <target>` plans the previous image (it doesn't
restore data).

**`… is busy (another action holds the lock)`**: a release, backup, job or import is running
on the target. A release waits up to 5 minutes; try again after it finishes.

**`<file>.caddy also serves www.example.org: move those to their own file before adopting it`**
(operator): the old vhost file to adopt must serve only the target's hosts.

**How do I roll back?** Ask the human: `sudo huta rollback <project> <target>` plans the
previous image; it needs approval like any release. Or check out an older commit that is on
`main`, `huta pack`, `huta submit <target>`.

**Where do headers, redirects and 404 pages go?** In your image's own server config. The host
vhost only terminates TLS and proxies.

**Which `huta` am I running?** As a non-root user, the dev client (`pack submit status log
login`). As root, the host CLI.

**My Mac is arm64.** `huta pack` builds `linux/amd64` anyway, through emulation; it is slower but
works.
