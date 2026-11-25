# huta

**huta** ([huta.run](https://huta.run), source: [github.com/huta-run/huta](https://github.com/huta-run/huta)) releases projects to one server you own, safely, with
one tap of approval.

Agents or people build container images on a development box. The host checks each release by
itself, plans exactly what will change, waits for the owner's approval, then swaps it in with
no dropped requests, and can put the old one back.

```
dev:   huta pack → image → huta submit prod
host:  huta verifies (sha on origin/main, CI, image id and labels, manifest read from git)
       → plans (what changes, effective config, plan hash)
owner: approves (CLI today; one-tap Telegram buttons next)
host:  start idle slot → verify /healthz reports the sha → switch the vhost → stop the old slot
```

Status: **v2**. The `web` recipe: one HTTP component or several per target (a slot is the whole
set, workers hand over after the switch), two loopback slots, Caddy as the edge, Cloudflare
Origin CA certificates and per-host SSL rules, crash recovery (`huta reconcile`, tested by
killing every step on a fake host). Per target, as the host grants: secrets and env, outbound
HTTPS by hostname allowlist and named host services (no DNS or route out otherwise), a data
volume with consistent backups shipped off the server, its own Postgres, schema migrations
(exclusive or online, with restore points and restore plans), scheduled jobs, smoke checks with
an automatic switch back, taking over a live site's vhost, gateways for services huta doesn't
run, and publishing to Cloudflare Pages and Workers (`docs/DESIGN-v1.md`, `docs/DESIGN-v2.md`).
Monitoring hooks (`docs/PLAN-monitoring.md`) are being built: the remote resources exist,
the rest is planned. Running in production on one host.

## Ideas it's built on

- **The repo asks, the host decides.** A project ships a declarative `deploy/app.toml`. The host
  registry (`/etc/huta/projects.d/<project>.toml`, root-owned) decides users, ports, hostnames,
  limits and policy. huta **generates** the compose file and the vhost from both, and never runs
  repo-supplied compose, Caddy config or shell as root.
- **Trust only the sha.** A submission says "project, target, sha, image". huta fetches the sha
  from GitHub itself, reads `app.toml` from that commit, and checks the image's id and labels.
- **No traffic before verification.** The vhost routes to exactly one slot. A new release is
  verified on its own port, then the vhost is switched in one validated, graceful reload.
- **Every failure ends in a recorded outcome:** live, nothing activated, rolled back, or needs a
  human.
  `huta reconcile` repairs state after a crash from what the vhost actually routes to.
- **Images are the only artifact,** built off the host and imported with every name stripped,
  so an archive can't overwrite another image's tag.

## Layout

```
src/huta/        the huta command (Python 3.11+, standard library only)
  cfg.py         host config, registry checks, app.toml schema (strict)
  store.py       SQLite release table and append-only event log
  ext.py         GitHub (read-only) and Cloudflare clients
  edge.py        certificates, generated vhosts, journaled edge changes
  images.py      importing images without trusting the archive
  recipe.py      the web recipe, smoke checks, revert, reconcile, pruning
  runtime.py     what a container gets: env, secrets, egress wiring, data mount, hardening
  egress.py      the egress daemon (CONNECT allowlist, upstreams) and its policy
  data.py        data volumes (loop-mounted ext4), consistent backups, import/export
  jobs.py        generated job and backup timers, running a job
  monitor.py     monitoring hooks: healthchecks, Bugsink and Matomo clients, ensure (partly built)
  cli.py         commands
examples/hello/  the smallest project: a static page in a ~14 MB image
examples/notes/  every v1 feature on one small Python app
site/            huta.run, the agent docs
install.sh       installs huta on a host (root)
```

## Install (host)

```sh
sudo bash install.sh       # from the checkout: copies src/huta to /opt/huta, installs /usr/local/sbin/huta
sudo huta check            # validates /etc/huta/huta.toml and projects.d/
```

Setting up a fresh server (Docker, Caddy, Tailscale, firewall, tokens, egress, registry, sshd):
[site/public/host.md](site/public/host.md) (https://huta.run/host.md), written for an agent
running on that server, which diagnoses first and asks before each change.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the model, the contract and the commands.

## License

MIT, see [LICENSE](LICENSE).
