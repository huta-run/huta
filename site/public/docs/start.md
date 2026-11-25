# Start here: deploying a project with huta

You are an agent working on a project that deploys with huta. huta runs on the production
host. You work on a dev box. You build an image and **ask** for a release; the host checks
everything itself, writes a plan, and a human approves it. You never act on the host.

Status: huta is **v1**. Only what these pages call available works today. Anything marked
*planned* does not exist yet; do not try to use it.

In these pages `<project>` is your project's name and `<target>` is the deploy target the
operator gave you (often `prod`, but use the name you were given).

Index of all pages: [https://huta.run/llms.md](https://huta.run/llms.md). Everything in one
file: [https://huta.run/llms-full.md](https://huta.run/llms-full.md). (`/llms.txt` and
`/llms-full.txt` are the same content under the llms.txt-convention names.)

## 0. Check the prerequisites first

Do not start on the repo until all of these hold. If one fails, stop and tell the human which
one.

- **A non-root `huta` on your `PATH`.** Run `huta --help` as your normal user: it must list
  `pack submit status log login`. (As root, `huta` is the host CLI, not the client.)
- **Docker** that your user can run: `docker info` works without sudo.
- **A working `~/.config/huta/client.toml`** for one of the two transports:
  - **pull** (dev box on the same host): the operator set up the dev box (an Incus
    container on the host) and the shared inbox and outbox. `client.toml` has
    `transport = "pull"` and `[pull] inbox`, `outbox` with the paths the operator gave you
    (for example `/huta/inbox`, `/huta/outbox`). The inbox must exist and be writable by you.
  - **push** (any dev box):
    - Python 3.11+ (`python3 --version`) and `~/.local/bin` on your `PATH`;
    - the box is enrolled and the client installed: `install-client.sh --keygen-only`, then
      `sudo huta enroll <box> --from <ip> --pubkey '<key>'` on the host, then
      `install-client.sh --settings <the JSON it printed>`. If this is not done yet, follow
      [install.md](https://huta.run/install.md);
    - the host's registry has a certificate your Docker trusts, or your Docker daemon lists it
      under `insecure-registries`; otherwise `docker push` fails;
    - `huta login` printed `logged in to <registry> as <box>`.
- **The project is registered on the host** with a target for you (`huta status <project>
  <target>` answers instead of `no project ... for box <box>`). If not, you will write the
  registry request in step 5 and wait. The registry entry names the boxes that may use it
  (`boxes`): a project registered for another box answers the same way.
- **The operator has set up the host's firewall** so containers get no outbound network. huta
  relies on it; the manifest alone does not isolate anything.

Details: [transports.md](transports.md), [commands.md](commands.md).

## What huta can deploy

Exactly one HTTP container per project (the `web` recipe): a static site or an HTTP service.
The host can give it, when the operator grants them in the host registry:

- **secrets and settings** as environment variables;
- **outbound HTTPS** to an allowlist of hostnames, and named host services (upstreams);
- **a data volume** of a fixed size, mounted at one path, backed up on a schedule (an SQLite
  file on it is fine);
- **scheduled jobs** that run in the same image;
- **smoke checks** before traffic moves, and an automatic switch back when the public checks
  fail.

What each means for your code: [Runtime](runtime.md). Moving a site that already runs on the
server: [Moving a live site](moving.md). Not available: several containers per project,
Postgres as a managed service, preflight checks, plain-HTTP egress. If the project needs one
of those, tell the human.

## Checklist, in order

1. **Get the facts:** the project name (`^[a-z][a-z0-9-]{0,30}$`), the target name, the public
   hostname, whether the target requires CI (`require_ci`, on by default), and the manifest
   path the host registry names in `app_toml` (usually `deploy/app.toml`).
2. **Add an image** that meets the enforced rules: non-root `USER`, `linux/amd64` (`huta pack`
   always builds it, emulated on arm64 machines such as Apple Silicon Macs), and `GET /healthz`
   returning `{"ok":true,"sha":"<full 40-hex sha>"}`. The server must listen on **`0.0.0.0`**
   at the port in `app.toml`: a server bound to `127.0.0.1` inside the container never answers
   the host's check, and the release ends `nothing-activated` with
   `never reported <sha12>`. `huta pack` sets the labels. Copy a pattern from
   [dockerfile.md](dockerfile.md).
3. **Add the manifest** at the path the host registry names (`app_toml`; usually
   `deploy/app.toml`). Minimal valid file (top-level keys before the first `[table]`):

   ```toml
   schema = 1
   project = "myapp"
   order = ["web"]

   [images.web]
   dockerfile = "Dockerfile"   # relative to the repo root
   context = "."               # relative to the repo root

   [components.web]
   image = "web"
   kind = "http"
   port = 8080
   health = "/healthz"

   [state]
   kind = "none"
   ```
   Every key and what huta refuses: [contract.md](contract.md). The client's `--app` only
   chooses which file **it** reads to build; the host always reads the registry's `app_toml`
   from git at the sha. Use the same file for both.
4. **Add CI if the target requires it:** a GitHub Actions workflow whose top-level `name:` is
   exactly `ci`, triggered by `push` to `main`. The host looks at runs of that workflow for the
   exact sha (event `push`, branch `main`); the **newest** such run must be `completed` with
   conclusion `success`. Minimal `.github/workflows/ci.yml`:

   ```yaml
   name: ci
   on:
     push:
       branches: [main]
   jobs:
     build:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - run: docker build --build-arg RELEASE_SHA=${{ github.sha }} .
   ```
5. **If the project or target is new on the host,** write the registry request for the
   operator (exact file in [contract.md](contract.md#host-side-registry-the-operator-writes-this)).
   You cannot install it. Wait until the operator confirms it is installed and the edge is set
   up (`huta edge`).
6. **Commit everything that should ship.** `huta pack` refuses uncommitted changes to tracked
   files and builds only what is committed (from `git archive HEAD`; untracked and git-ignored
   files never reach the image, so anything the image needs must be committed or built in the
   Dockerfile).
7. **Test that commit locally, as the host will run it.** Take the sha in the repo, build from a
   clean export of the commit, like `huta pack` does:

   ```sh
   sha=$(git rev-parse HEAD)
   rm -rf /tmp/myapp-export && mkdir /tmp/myapp-export
   git archive HEAD | tar -x -C /tmp/myapp-export
   cd /tmp/myapp-export
   docker build --platform linux/amd64 --build-arg RELEASE_SHA="$sha" -t myapp-test .
   docker run --rm -d --name myapp-test -p 8080:8080 --read-only --tmpfs /tmp:size=16m \
     --cap-drop ALL --security-opt no-new-privileges --user 20001:20001 myapp-test
   curl -s localhost:8080/healthz      # must print {"ok":true,"sha":"<$sha>"}
   docker rm -f myapp-test
   cd -
   ```
   If it fails, fix, commit again, and repeat this step.
8. **Push to `main`.** The host reads the manifest from GitHub at that commit, not from your
   disk, and checks the sha is on `main`.
9. **Build:** `huta pack` (or `huta pack --app <path>`) in that checkout. It tags
   `huta-build/<project>:<sha12>`.
10. **Submit:** `huta submit <target>` (with the same `--app <path>` if you used one). It prints
    progress lines (`… …`) while the host imports the image, then the plan with a release id,
    or `refused: <reason>`. Fix refusals using [faq.md](faq.md). `huta submit <target>
    --follow` also waits for the approval and reports each step and the outcome.
11. **Tell the human** the release id and that it waits for approval (below). The owner
    approves within 30 minutes of planning: with the **✅ Deploy** button on the release's
    message in the configured Telegram topic (when the operator has turned buttons on), or with
    `sudo huta approve <release>` on the host. After 30
    minutes the release still shows `planned` until an approve attempt or a late button press
    marks it `superseded`;
    then submit again.
12. **Follow up:** `huta status <project> <target>` and `huta log <release>`. An approved
    release ends in one of three outcomes:
    - `live`: serving;
    - `nothing-activated`: traffic was never switched to it. If an earlier release was live,
      it still serves. **On a first deploy nothing was live, so the site still answers 503
      `not released yet`**;
    - `needs-human`: stop and hand over to the human.

## What you cannot do

- No shell, Docker or file access on the production host. You do not need them.
- No secrets in the repo, the image, commit messages or logs.
- No deploy without approval: every release waits for a human.
- No host-side files: you never write compose files, vhosts, systemd units or the host
  registry. The host generates them.
- No prod data from dev, and no logs beyond what the host gives you redacted: `huta status`,
  `huta log`, `huta jobs`, `huta logs`, `huta egress-log`, `huta traffic`, `huta crashes` and
  `huta diag` exist; `huta snapshot` is *planned*.
  Data export is the owner's step on the host (`sudo huta data export`).

## What to tell the human

After a successful submit, in this shape:

```
Release <release> for <project> → <target> is planned (commit <sha12>).
It waits for your approval for 30 minutes: tap Deploy on huta's message for this release
in the Telegram topic, or on the host run
  sudo huta show <release>
  sudo huta approve <release>
```

If the human says the button replied `huta is unavailable; use sudo huta approve on the host`
or `this button is no longer valid`, point them to the command line (or submit again if the
plan expired).

Also tell the human, without being asked, when:

- a prerequisite in step 0 is missing;
- the project needs something huta does not have (online migrations, preflight checks):
  name the feature and say it is planned in huta, not available;
- the host registry needs a new or changed entry (env, secrets, egress, upstreams, data): give
  them the exact file (jobs and smoke checks live in `app.toml`; the owner sees them in the
  plan);
- a secret's name isn't in the registry yet: give the name (once it is, you can send the value
  yourself with `huta secret set <project> <NAME>`; if you don't have the value, say
  `sudo huta secret set <project> <NAME>`);
- an outcome is `nothing-activated` on a first deploy (the site is still not released) or
  `needs-human`: give the release id and the `huta log <release>` output.

## Read next

- [contract.md](contract.md): `deploy/app.toml`, image rules, host registry fields
- [dockerfile.md](dockerfile.md): copy-paste Dockerfiles
- [commands.md](commands.md): every command, its output, release states
- [rules.md](rules.md): the rules for agents
- [faq.md](faq.md): refusals and what they mean
