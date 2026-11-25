# huta

Coding agents release to a server you own, without a shell on it. **Agents build, your server
checks, you approve.**

A coding agent (or a person) builds a container image on a dev box and *asks* for a release.
The host checks every claim itself, writes down exactly what will change, and waits. You
approve. The new release is verified on its own slot before traffic moves to it, and the old
one can be put back.

> **Are you an agent?** Start at [https://huta.run/llms.md](https://huta.run/llms.md)
> On an empty server, asked to set it up for huta? Follow [https://huta.run/host.md](https://huta.run/host.md).
> Asked to install the client on a machine? Follow [https://huta.run/install.md](https://huta.run/install.md).
>
> The exact contract, commands and refusal messages, in Markdown. Read the index, then the
> start page, then follow it in order:
>
> ```sh
> curl -s https://huta.run/llms.md        # the index: what to read, in order
> curl -s https://huta.run/docs/start.md  # prerequisites, then the checklist
> huta --help                             # must list: pack submit status log login
> ```
>
> Everything in one file: [https://huta.run/llms-full.md](https://huta.run/llms-full.md). The
> same content under the llms.txt names: [/llms.txt](https://huta.run/llms.txt),
> [/llms-full.txt](https://huta.run/llms-full.txt).

## How a release moves

```text
dev    pack ── submit
                  │
host          verify ── plan
                           │
owner                  approve
                           │
host         idle slot ── /healthz = sha ── switch ── drain ── live
```

1. **dev:** `huta pack` builds the image from a clean export of the commit, with the sha baked
   in. `huta submit <target>` sends the image and asks for a release. Nothing else crosses
   over: no shell on the server.
2. **host:** verifies: the sha is on `main`, CI is green when the target requires it,
   `deploy/app.toml` is read from git at that sha, and the image's id, labels, user and
   platform match.
3. **host:** plans: what changes, the effective runtime config, a plan hash, and a 30-minute
   expiry.
4. **owner:** approves. Everything is checked again under a lock first.
5. **host:** starts the idle slot, waits until `/healthz` reports the new sha, switches the
   vhost in one validated, graceful reload, keeps the old slot running for a short drain
   (10 s by default), then stops it.

No traffic reaches a release before it is verified. Visitors move over without a gap: new
requests go to the new release, and requests already in flight finish on the old one during the
drain. Long requests, streams and websockets still on the old slot when the drain ends are cut.
A rollback is a plan of the previous image, approved the same way.

## Setting up the server

Before the first release, the server itself. Install any coding agent on a fresh VPS and give it
one line; it follows [host.md](https://huta.run/host.md):

1. **vps:** an empty server (amd64, KVM or similar, Debian or Ubuntu) and an agent on it.
2. **agent:** a read-only diagnosis first: architecture, virtualization, cgroup v2, loop
   devices, Python, open ports, firewall, ssh. A blocker stops it, with the reason.
3. **you:** answer a few questions (domains on Cloudflare, a name for the host, your tailnet)
   and type the GitHub and Cloudflare tokens yourself at a hidden prompt; the agent never sees
   them. Docker, Caddy and Tailscale go in.
4. **agent:** firewall and ssh changes, each applied with a five-minute undo that is cancelled
   only once your new login works.
5. **host:** huta installed from a commit you have seen, the egress guard on, a registry and an
   ssh door on the tailnet for dev boxes. Each dev box then follows
   [install.md](https://huta.run/install.md).

## Say it to your agent

Paste one line into Claude Code, Codex or any agent with a shell:

- On a new server: `Set this server up for huta. Follow https://huta.run/host.md`
- In a project: `Get this project ready to deploy with huta. Start at https://huta.run/llms.md`
- On your machine: `Set this machine up to release with huta. Follow https://huta.run/install.md`

---

## Five ideas

- **The repo asks, the host decides.** A project ships `deploy/app.toml`: data only, strict
  schema. The host sets users, ports, hostnames, limits and policy, and generates the
  container config and vhost itself. It never runs a repo's compose files, web-server config
  or shell as root.
- **Trust only the sha.** A request names a project, target, sha and image. huta fetches the
  commit from GitHub itself, reads the manifest from that commit, and checks the image's
  labels against it.
- **No traffic before verification.** The vhost routes to exactly one slot. A new release is
  checked on its own loopback port, then traffic moves in one graceful reload.
- **Every approved release ends in a recorded outcome:** `live`, `nothing-activated` (traffic
  never moved; what was live keeps serving), `rolled-back` (public checks failed; traffic went
  back) or `needs-human`. After a crash,
  `huta reconcile` rebuilds state from what the vhost actually routes to.
- **Images arrive nameless.** They are built off the host. huta loads an image by id, from a
  name-stripped archive or by digest from its own registry, and names it itself, so a
  submission can never overwrite another image's tag.

---

## What an approval looks like

### One tap (available)

One message per release, in one place: the release's message in the configured Telegram topic
carries two buttons, **Deploy** and **Reject**, while the plan waits. huta posts and edits it
itself; no chat assistant writes or reads it. Each button carries its own single-use secret (128
random bits, stored only as a hash, expiring with the plan). Only the owner's tap counts, even
in a group: a lookalike button from another bot carries no valid secret. A tap runs the same
checks as the command line, and the message is edited as the release moves.

```text
huta
🚀 myapp → prod · 1a2b3c4d
9f8e7d6 → 0123456 · code-only
from mybox (push) · main ✓ · CI ✓
⏳ waiting for approval · until 14:32
sudo huta approve 1a2b3c4d
[   ✅ Deploy   ]  [   ❌ Reject   ]
```

The buttons come off once the plan is decided; the message stays as the release's history. The
operator turns the buttons on once the chat relay is installed.

### The command line (always works)

The owner can read the plan and approve on the host; this is also the fallback when chat is
down.

```console
$ sudo huta show 1a2b3c4d
plan 1a2b3c4d · myapp → prod · state planned
  9f8e7d6c5b4a → 0123456789ab   image huta/myapp:0123456789ab-4f2c9a1b0e7d
  hosts: myapp.example.org · slots [18300, 18301]
  egress: api.example.com:443
  data: 1g at /data · concurrent-safe · backup hourly · local copy 0.3 h ago, last ship ok
  class: code-only
  checks: on origin/main ✓ · CI green · built by dev
  plan hash 0f1e2d3c4b5a6978 · expires 14:32

$ sudo huta approve 1a2b3c4d
slot b started, verifying /healthz reports 0123456789ab (no traffic yet)
smoke on the new slot: 3 checks passed
vhost switched, caddy reloaded
public check ok: https://myapp.example.org/healthz
public smoke: 3 checks passed
live on slot b; old slot a stopped
outcome: live
```

---

## Status

Running in production on one server. This list is kept honest: anything not marked available
does not exist yet.

### Available now

- [x] Recipe `web`: one HTTP container per project, on two loopback slots
- [x] Secrets and settings from the host, as environment variables; a dev box can send a
  secret's value, which goes live with the next release the owner approves
- [x] Outbound HTTPS by hostname allowlist, and named host services (upstreams); no DNS or
  route out otherwise
- [x] A data volume per target (fixed size), consistent backups shipped off the server, import
  and export on the host
- [x] Scheduled jobs in the live release's image
- [x] Smoke checks before the switch, public checks after it, automatic switch back
- [x] Moving a live site without a gap (adopt its vhost file at the first release)
- [x] Transport *pull*: a dev box on the same host, through a shared inbox
- [x] Transport *push*: any dev box, over an ssh forced command plus a registry on the host
- [x] One-tap Deploy / Reject buttons on the release's message in the Telegram topic (owner only)
- [x] Approval with `huta approve` on the host (the fallback); a Telegram history message per
  release, edited as it moves
- [x] Read-only debug access for agents: `huta status`, `log`, `jobs`, `logs`, `egress-log`,
  `traffic`, `crashes`, `diag`
- [x] Caddy as the edge with generated vhosts, Cloudflare Origin CA certificates, per-host SSL
  rules
- [x] Crash recovery with `huta reconcile`; rollback as an approved plan
- [x] v2: several containers per target (a slot is the whole set; workers hand over after the
  switch)
- [x] v2: Postgres per target, run by huta (pinned image, own volume and networks, app and
  migrate roles, dumps, planned upgrades)
- [x] v2: schema migrations, exclusive (with a pinned restore point) or online; restore plans
- [x] v2: gateways (a hostname for a service huta doesn't run); edge changes as approved plans
- [x] v2: publishing to Cloudflare Pages and Workers as a step of a release

### Planned (not yet)

- [ ] `huta snapshot`: a sanitized copy of data for debugging
- [ ] Unattended releases
- [ ] Monitoring hooks; plain (non-Cloudflare) TLS

---

## Docs

Written for agents, in Markdown.

- [Set up a host](https://huta.run/host.md): an empty VPS, step by step
- [Install the client](https://huta.run/install.md): a dev box or laptop
- [Start here](https://huta.run/docs/start.md): prerequisites, then the checklist
- [Contract](https://huta.run/docs/contract.md): every `app.toml` key, image rules
- [Runtime](https://huta.run/docs/runtime.md): secrets, egress, data, jobs, checks
- [Moving a live site](https://huta.run/docs/moving.md): taking over a site without a gap
- [Dockerfiles](https://huta.run/docs/dockerfile.md): copy-paste patterns
- [Commands](https://huta.run/docs/commands.md): what each prints, release states
- [Transports](https://huta.run/docs/transports.md): pull and push
- [Approval](https://huta.run/docs/approval.md): plans, expiry, notifications
- [Rules](https://huta.run/docs/rules.md): what agents may and may not do
- [FAQ](https://huta.run/docs/faq.md): exact refusals and what to do

---

This page is packaged as an ordinary huta project and deployed by huta itself.
[/healthz](https://huta.run/healthz) names the commit it came from. No cookies, no tracking,
no scripts. The HTML version is at [https://huta.run/](https://huta.run/).
