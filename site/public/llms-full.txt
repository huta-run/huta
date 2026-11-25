# huta

> huta releases projects to one server you own. An agent or person builds a container image on a dev box and asks for a release; the host verifies the commit (on `main`, and CI green when the target requires it), reads `deploy/app.toml` from git at that sha, checks the image's labels, user and platform, writes a plan, and waits for a human to approve. Then it starts the new release on an idle slot, checks that `/healthz` reports the sha, switches the vhost in one graceful reload, and stops the old slot after a short drain (long requests, streams and websockets can be cut). Status: v2. Available: the `web` recipe (one HTTP container, or several components per target, below), with what the host's operator grants per target: secrets and settings as environment variables (a dev box can send a secret's value; it goes live with the next release the owner approves); outbound HTTPS through an allowlisting proxy by hostname, and named host services (upstreams), with no DNS or other route out; a data volume of a fixed size mounted at one path, with consistent backups shipped off the server; scheduled jobs in the live release's image, optionally also run after each release; smoke checks on the new slot before the switch and public checks after it, with an automatic switch back; adopting a live site's vhost at the first release. Two transports (pull: a dev box on the same host with a shared inbox; push: any dev box over an ssh forced command plus a registry on the host), approval by the owner with one tap (✅ Deploy / ❌ Reject buttons on the release's Telegram history message, single-use secrets) or with `huta approve` on the host's command line (the fallback), and a Telegram history message per release, edited as it moves. Read-only debug access from a box: `huta status` (releases, and each slot's state, memory, pids and cpu against the limits, health now, data, backups, egress connections), `huta log`, `huta jobs`, `huta logs` (the target's container output, redacted by the host, kept 7 days, including a slot that never became healthy), `huta egress-log` (what the egress proxy refused), `huta traffic` (requests per minute by status class, each 5xx), `huta crashes` (Bugsink issues), and `huta diag` (a `[[diag]]` from app.toml, run without network or secrets against the last backup copy). Also available (v2): several containers per target (a web, an API, workers; one slot is the whole set), schema migrations that run exclusive with a pinned restore point and restore plans to go back, gateways (a public hostname for a service huta doesn't run), and Postgres per target (huta runs it: pinned image, own volume and networks, app and migrate roles, dumps, planned upgrades). and publishing to Cloudflare Pages and Workers as a step of a release (files copied out of the image without running it, a pinned wrangler with egress to Cloudflare only). Migrations can also run online (the old release serving) when the repo, the host and the old release's runs_on all allow it. Monitoring hooks, in part: a Bugsink project and Matomo site per target, made by the host, and their variables (`SENTRY_DSN`, `SENTRY_RELEASE`, `SENTRY_ENVIRONMENT`, `MATOMO_SITE_ID`) when app.toml asks (`[monitor] env`). Planned, not available: unattended releases, `huta snapshot`, the rest of monitoring (job checks, a watch window after each release; the per-minute probe of each site is available), plain-HTTP egress.

If you are an agent running on an empty server and asked to **set it up as a huta host**, follow https://huta.run/host.md step by step (it diagnoses the server first, then asks the human before each change). If you are an agent asked to **install the huta client** on a machine, follow https://huta.run/install.md step by step (it tells you what to ask the human and what to confirm before touching the server). If you are an agent asked to deploy a project with huta, read the start page first, check its prerequisites, then follow it in order. Everything is also in one file: https://huta.run/llms-full.md

This index is served as https://huta.run/llms.md; `/llms.txt` and `/llms-full.txt` are the same content under the llms.txt-convention names.

## Docs

- [Set up a host](https://huta.run/host.md): for an agent on an empty VPS: read-only diagnosis, what to ask, Docker, Caddy, Tailscale, firewall with a timed undo, installing huta, the two tokens, egress, registry and ssh for dev boxes, optional Telegram and backups, verify
- [Install the client](https://huta.run/install.md): for an agent asked to set up a dev box: what to ask, what to confirm, `huta enroll` on the server, `install-client.sh --settings`
- [Start here](https://huta.run/docs/start.md): prerequisites to check first, then the checklist: what to add to the repo, how to test, build and submit, what you cannot do, what to tell the human
- [Contract](https://huta.run/docs/contract.md): every `deploy/app.toml` key (`[state]`, `data`, `concurrent_safe`, `[[smoke]]`, `[[jobs]]`) and what huta refuses, the TOML top-level-keys pitfall, enforced image rules vs recommended patterns, `/healthz`, the host registry fields the operator fills in
- [Runtime](https://huta.run/docs/runtime.md): what the app gets at run time and how to write it: secrets and env, egress through `HTTPS_PROXY` (no DNS), upstreams, the data volume (one writer vs concurrent, per-slot files, backups), jobs, smoke checks and automatic revert, the headers the app receives (`CF-Connecting-IP`)
- [Moving a live site](https://huta.run/docs/moving.md): taking over a site that already runs on the server: what to move into the image, data import, secrets import, adopting the old vhost at the first release
- [Dockerfile patterns](https://huta.run/docs/dockerfile.md): copy-paste Dockerfiles for a tiny static site and a small HTTP service, and a local test
- [Commands](https://huta.run/docs/commands.md): `huta pack / submit / status / log / jobs / logs / egress-log / traffic / crashes / diag / login`, `client.toml`, host commands, what each prints, release states and the three outcomes
- [Transports](https://huta.run/docs/transports.md): pull and push, setup for each (push: one host user `huta-<box>` per box), the request format, single-use request ids
- [Approval](https://huta.run/docs/approval.md): one-tap Deploy / Reject buttons in Telegram and the CLI fallback, the checks both run, what a plan shows, 30-minute expiry, notifications
- [Rules](https://huta.run/docs/rules.md): releases are commits on main, secrets only on the host (never in the repo or image; a box may send a value with `huta secret set`, staged until an approved release), scheduled jobs policy, debug access (status, log, jobs, logs, egress-log, traffic, crashes and diag now; snapshot planned), only the owner approves, when to ask a human
- [FAQ](https://huta.run/docs/faq.md): exact refusal messages and Telegram button replies, and what to do about each

## Optional

- [Everything in one file](https://huta.run/llms-full.md): this index and all pages above, concatenated (alias: `/llms-full.txt`)
- [Landing page](https://huta.run/): the human overview


---

<!-- https://huta.run/host.md -->

# Set up a huta host

> For an agent running **on an empty server** and asked something like **"set this server up
> for huta, https://huta.run/host.md"**. The human installed you on a fresh VPS so you can do
> this for them. Follow the steps in order. Steps marked **ASK** need the human. Steps marked
> **CONFIRM** change the server: show the exact commands, say what they do, and wait for a
> clear yes. Never paste a token into chat or a command line: the human types each one at a
> hidden prompt.

What you're setting up: this server becomes a **huta host**. It runs projects as containers
behind Caddy and Cloudflare, and releases them only after the owner approves. Dev boxes (a
laptop, a build machine) reach it over Tailscale; each is set up later with
[install.md](https://huta.run/install.md).

What this guide sets up: the push transport (dev boxes over ssh and a registry), approval with
`sudo huta approve` on the host, and optionally Telegram history messages. Not here: the pull
transport (a dev container on this same host, see
[transports.md](https://huta.run/docs/transports.md)) and one-tap Telegram buttons (they need a
chat gateway plugin, see [approval.md](https://huta.run/docs/approval.md)).

## Ground rules

- **Never lock the human out.** Before any change to ssh or the firewall, ask the human to keep a
  second ssh session open, apply the change with a timed undo (shown below), test a new login,
  and only then cancel the undo.
- Before editing a file under `/etc`, copy it next to itself as `<file>.bak-huta-<date>`.
- Never `flush ruleset` in nftables: it would also delete Docker's and Tailscale's rules.
- Never print a secret. To check one exists: `sudo test -s <file> && echo set`.
- Never use `StrictHostKeyChecking=no`, `curl | sh` from a site you weren't told to use, or
  `insecure-registries`.
- If a check fails and the fix isn't in this guide, stop and tell the human what you found.

## 1. Diagnose (read-only)

Run this block. It changes nothing (`sudo -n` fails instead of asking for a password):

```sh
echo "== os";      . /etc/os-release; echo "$PRETTY_NAME"; uname -r; uname -m
echo "== virt";    systemd-detect-virt || true
echo "== me";      id; sudo -n true && echo "sudo ok" || echo "NO passwordless sudo"
echo "== size";    nproc; free -h | sed -n 2p; df -h / | tail -1
echo "== cgroup";  stat -fc %T /sys/fs/cgroup
echo "== loop";    sudo -n losetup -f 2>&1
echo "== tools";   for c in python3 git openssl nft docker caddy tailscale fsfreeze mkfs.ext4 resize2fs; do printf '%-10s %s\n' "$c" "$(command -v $c || echo MISSING)"; done
python3 -c 'import sys; print("python", sys.version.split()[0], "ok" if sys.version_info >= (3, 11) else "TOO OLD")'
docker version --format 'docker {{.Server.Version}}' 2>&1; docker compose version 2>&1
echo "== ports";   sudo -n ss -ltnup | awk 'NR==1 || /LISTEN|UNCONN/' | head -40
echo "== firewall"; sudo -n nft list tables 2>&1; command -v ufw >/dev/null && sudo -n ufw status 2>&1 | head -1; systemctl is-active firewalld 2>/dev/null
echo "== ssh";     sudo -n sshd -T 2>/dev/null | grep -Ei '^(port|permitrootlogin|passwordauthentication|allowusers|allowgroups) '
grep -n '^Include' /etc/ssh/sshd_config
echo "== addr";    ip -4 -br addr | grep -v '^lo'; ip -6 -br addr | grep -v '^lo'
echo "== huta";    ls -la /etc/huta /opt/huta 2>&1 | head
```

Read the result against this table. **Blockers** stop the setup; tell the human.

| Check | Needs | If not |
|---|---|---|
| os | Debian 12/13 or Ubuntu 22.04/24.04 (systemd, apt) | other distros work in principle; say this guide assumes apt and ask before going on |
| arch | `x86_64` | **blocker**: huta runs `linux/amd64` images only |
| virt | `kvm`, `xen`, `vmware`, `microsoft`, `none` | `openvz`, `lxc`: **blocker** (no own kernel: Docker, nftables and loop devices don't work properly) |
| sudo | `sudo ok`, or you are root | ask the human to run you as a user with passwordless sudo |
| size | 2 GB RAM and 20 GB free at least | small sites fit in 1 GB; say it's tight |
| cgroup | `cgroup2fs` | **blocker** (status and limits read cgroup v2) |
| loop | prints `/dev/loopN` | data volumes won't work; projects without `data` still do. Tell the human |
| python | 3.11 or newer | Debian 12 / Ubuntu 24.04 have it; Ubuntu 22.04 needs a newer Python: ask |
| ufw / firewalld | not active | ask before replacing it with nftables (step 4) |
| ssh | keys only (`passwordauthentication no`) | not a blocker; recommend it in the report |
| huta | nothing there | if `/etc/huta` exists, this host is already set up: stop and ask what they want |

Note the ssh `port`, any `allowusers` line, the public IPv4 address (`<ipv4>`) and whether the
server has IPv6 (`<ipv6>`).

## 2. ASK: what you need, in one message

1. **Domains.** Which domains will sites live on? Each must be a zone on the human's Cloudflare
   account (huta only serves sites through Cloudflare, proxied, "orange"). These become
   `<zones>`.
2. **A name for this host**, short, lower case (for example `web1`). This is `<host>`.
3. **Tailscale.** Do they have a tailnet? Dev boxes reach the host over it. If not, they need
   a free account at tailscale.com first.
4. **Which huta to install**: the public repository https://github.com/huta-run/huta (the
   default), or their own fork or copy.
5. **Optional, Telegram:** do they want a message per release in a Telegram chat? Then they need
   a bot (from @BotFather) that can post there, and the chat's id.
6. **Optional, backups:** will any project keep data (a SQLite file, uploads)? Then where should
   copies go off this server (another server, S3, a NAS)? You can set this up later.

## 3. Base packages (CONFIRM)

```sh
sudo apt-get update
sudo apt-get install -y python3 git openssl nftables e2fsprogs util-linux curl ca-certificates gnupg
```

**Docker** (from Docker's own apt repository; the distro's `docker.io` is often too old):

```sh
sudo install -m 0755 -d /etc/apt/keyrings
. /etc/os-release
curl -fsSL "https://download.docker.com/linux/$ID/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

Then `/etc/docker/daemon.json` (write it with your file tool, then `sudo systemctl restart
docker`):

```json
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" }, "live-restore": true }
```

**Caddy** (the stock package is enough: huta brings its own certificates):

```sh
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy
```

Replace `/etc/caddy/Caddyfile` (back up the package's one first) with:

```
# Sites are one file each in Caddyfile.d. huta writes its own there (they start with
# "# managed-by: huta"); add hand-written ones beside them, never edit huta's.
import /etc/caddy/Caddyfile.d/*.caddy
```

```sh
sudo install -d -m 0755 /etc/caddy/Caddyfile.d
sudo install -d -m 0755 -o caddy -g caddy /var/log/caddy
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

**Tailscale:**

```sh
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up
```

`tailscale up` prints a login URL: give it to the human and wait until they've logged in.
Then `tailscale ip -4` (the host's tailnet address) and `tailscale status --self --json | python3
-c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'` (its tailnet
name, `<host>.<tailnet>.ts.net`, call it `<tsname>`). Ask the human to check that **MagicDNS** and
**HTTPS certificates** are on in the Tailscale admin console (DNS page): the registry uses them.

## 4. Firewall (CONFIRM, timed undo)

The firewall lets in ssh, the web ports and Tailscale, and drops everything else. Docker and
Tailscale keep their own tables; this file only resets its own.

`/etc/nftables.conf` (replace `22` with the ssh port from step 1):

```
#!/usr/sbin/nft -f
# Resets only our tables: never `flush ruleset` (Docker and Tailscale have their own).
table inet filter
delete table inet filter
table inet filter {
	chain input {
		type filter hook input priority filter; policy drop;
		ct state established,related accept
		ct state invalid drop
		iif "lo" accept
		iifname "tailscale0" accept
		meta l4proto { icmp, ipv6-icmp } accept
		tcp dport 22 accept comment "ssh"
		tcp dport { 80, 443 } accept comment "caddy (huta vhosts also refuse non-Cloudflare peers)"
		udp dport 41641 accept comment "tailscale direct"
		iifname "dp-*" tcp dport 4880-4899 accept comment "huta egress daemon"
	}
	chain forward {
		type filter hook forward priority filter; policy accept;
	}
}
include "/etc/nftables.d/*.nft"
```

`/etc/nftables.d/80-huta-egress.nft`: the guard table from the repo's
`systemd/nftables-huta-egress.nft.example`, **only the `table inet huta_guard` part** (the
`inet filter` accept is already above).

If ufw or firewalld is active, ask before disabling it (`sudo ufw disable` /
`sudo systemctl disable --now firewalld`); two firewalls fight.

Apply with an undo that fires in 5 minutes unless you cancel it:

```sh
sudo install -d -m 0755 /etc/nftables.d
sudo nft -c -f /etc/nftables.conf
sudo systemd-run --unit=huta-fw-undo --on-active=5min /usr/sbin/nft delete table inet filter
sudo nft -f /etc/nftables.conf && sudo systemctl enable nftables
```

Ask the human to open a **new** ssh session. Only when it works:
`sudo systemctl stop huta-fw-undo.timer`. If it doesn't, wait: the rules go away by themselves.

## 5. Install huta (CONFIRM)

```sh
sudo install -d -m 0755 /opt/src && sudo chown "$USER" /opt/src
git clone https://github.com/huta-run/huta /opt/src/huta && cd /opt/src/huta
git log -1 --format='%h %s'
```

Tell the human which commit this is. huta runs as root and is never updated by itself: it's
installed from a checkout the owner has seen. Then:

```sh
sudo bash install.sh            # prints "installed huta <rev>"
sudo install -d -m 0755 /etc/huta /etc/huta/projects.d
sudo install -d -m 0700 /etc/huta/secrets /etc/huta/secrets/_platform
```

### Two tokens (the human types them)

**GitHub, read-only.** huta checks for itself that a commit is on `main` and that CI passed.
Ask the human to create a fine-grained token: github.com → Settings → Developer settings →
Personal access tokens → Fine-grained → Generate. Resource owner: the account or org that owns
the projects. Repository access: the project repos (or all). Permissions: **Contents: Read**,
**Actions: Read** (Metadata: Read comes with them). Nothing else.

**Cloudflare.** dash.cloudflare.com → My Profile → API Tokens → Create Token → Custom token.
Five permissions, all **Zone**: Zone · Read, DNS · Edit, SSL and Certificates · Edit, Zone
Settings · Edit, Config Rules · Edit. Zone resources: the `<zones>` from step 2. Client IP
filtering (recommended): `<ipv4>`, and `<ipv6>` if the server has one (it may call the API over
IPv6).

The human runs each line in **their own** ssh session, not through you:

```sh
sudo bash -c 'umask 077; IFS= read -rs -p "GitHub token: " t; echo; printf %s "$t" > /etc/huta/secrets/_platform/GITHUB_TOKEN'
sudo bash -c 'umask 077; IFS= read -rs -p "Cloudflare token: " t; echo; printf %s "$t" > /etc/huta/secrets/_platform/CLOUDFLARE_API_TOKEN'
```

Check without printing them:

```sh
sudo bash -c 'curl -fsS -H "Authorization: Bearer $(cat /etc/huta/secrets/_platform/GITHUB_TOKEN)" https://api.github.com/rate_limit >/dev/null && echo github ok'
sudo bash -c 'curl -fsS -H "Authorization: Bearer $(cat /etc/huta/secrets/_platform/CLOUDFLARE_API_TOKEN)" https://api.cloudflare.com/client/v4/user/tokens/verify | grep -o "\"status\":\"[a-z]*\""'
```

The second must print `"status":"active"`.

### `/etc/huta/huta.toml`

Write it with your file tool (root-owned, 0644), using the values gathered so far:

```toml
schema = 1

[platform]
host = "<host>"
public_ipv4 = "<ipv4>"          # new DNS records that `huta edge --apply` creates point here

[cloudflare]
zones = ["example.org"]         # <zones>: huta refuses any Cloudflare call outside this list

[transport.push]
registry_local = "127.0.0.1:5000"
registry_public = "<tsname>:4882"
ssh_host = "<tsname>"
ssh_port = 22                   # the ssh port from step 1
registry_project = "huta-registry"
```

Every key is in [transports.md](https://huta.run/docs/transports.md) and
[contract.md](https://huta.run/docs/contract.md#host-side-registry-the-operator-writes-this).
Then `sudo huta check` must end with `registry ok`.

## 6. Egress (CONFIRM)

huta containers have no DNS and no route out; the egress daemon is their only way out, and only
to the hostnames a target allows.

```sh
sudo huta egress sync                     # writes the (empty) policy and prints it
sudo systemctl enable --now huta-egress
sudo nft list table inet huta_guard >/dev/null && echo guard ok
```

## 7. Push transport: registry and ssh (CONFIRM, timed undo for ssh)

**Registry.** `/opt/huta-registry/compose.yaml`:

```yaml
# Image registry for huta's push transport. Loopback only; published to the tailnet by
# `tailscale serve`. Never remove `name:` (it prefixes the volume names).
name: huta-registry
services:
  registry:
    image: registry:2
    restart: unless-stopped
    ports: ["127.0.0.1:5000:5000"]
    environment:
      REGISTRY_AUTH: htpasswd
      REGISTRY_AUTH_HTPASSWD_REALM: huta
      REGISTRY_AUTH_HTPASSWD_PATH: /auth/htpasswd
      REGISTRY_STORAGE_DELETE_ENABLED: "true"
      REGISTRY_HTTP_RELATIVEURLS: "true"
    volumes:
      - /var/lib/huta/registry:/var/lib/registry
      - /etc/huta/registry/htpasswd:/auth/htpasswd:ro
    read_only: true
    tmpfs: [/tmp]
    security_opt: ["no-new-privileges:true"]
    cap_drop: [ALL]
    mem_limit: 512m
```

```sh
sudo install -d -m 0755 /etc/huta/registry /var/lib/huta/registry
sudo touch /etc/huta/registry/htpasswd
cd /opt/huta-registry && sudo docker compose up -d
sudo tailscale serve --bg --https=4882 http://127.0.0.1:5000
curl -sS -o /dev/null -w '%{http_code}\n' https://<tsname>:4882/v2/    # 401: up, and asks for a login
```

(Pin `registry:2` by digest once it runs: `docker inspect --format '{{index .RepoDigests 0}}'
registry:2`.) `huta enroll` fills `htpasswd` and restarts the registry.

**The request socket:**

```sh
sudo groupadd -f --system huta-push
sudo install -m 0755 -o root -g root systemd/huta-ssh /usr/local/sbin/huta-ssh
sudo install -m 0644 systemd/huta-push.socket 'systemd/huta-push@.service' /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now huta-push.socket
```

**sshd.** Copy `systemd/sshd-huta-push.conf.example` to `/etc/ssh/sshd_config.d/50-huta-push.conf`.
`/etc/ssh/sshd_config` must have `Include /etc/ssh/sshd_config.d/*.conf` (step 1 showed it). If
step 1 showed an `allowusers` line, add `huta-*@100.64.0.0/10` **to that line** (never a new
`AllowUsers` line listing only `huta-*`: it would lock everyone else out). Without one, leave
the example's line commented. Then:

```sh
sudo sshd -t
sudo sshd -T -C user=huta-test,addr=100.64.0.1,host=x | grep -i forcecommand   # /usr/local/sbin/huta-ssh
sudo systemd-run --unit=huta-ssh-undo --on-active=5min /bin/sh -c 'mv /etc/ssh/sshd_config.d/50-huta-push.conf /root/ && systemctl reload ssh'
sudo systemctl reload ssh
```

New ssh session from the human works → `sudo systemctl stop huta-ssh-undo.timer`.

## 8. Optional: Telegram history

With a bot token in a root-only JSON file, huta posts one message per release (waiting for
approval, live, failed) and edits it as the release moves. The human writes the token
(hidden prompt again):

```sh
sudo bash -c 'umask 077; IFS= read -rs -p "Bot token: " t; echo; printf "{\"bot\": {\"token\": \"%s\"}}\n" "$t" > /etc/huta/secrets/_platform/telegram.json'
```

```toml
[telegram]
history_chat = -1001234567890   # the chat id
# history_thread = 12           # a forum topic, if any
token_json = "/etc/huta/secrets/_platform/telegram.json"
token_keys = ["bot", "token"]
```

The owner then approves with `sudo huta approve <release>` (the message shows the command). Buttons
in the message need more: [approval.md](https://huta.run/docs/approval.md).

## 9. Optional: backups

Targets with a data volume need a `[backup]` section: huta makes a consistent copy in `dir`
and runs `ship` to send it off the server. `ship` is the operator's own command; it gets
`HUTA_BACKUP_DIR`, `HUTA_PROJECT`, `HUTA_TARGET` and exits 0 (sent), 75 (destination
unreachable, try later) or anything else (failed). Write it with the human for the destination
from step 2 (restic, rsync, rclone…), test it by hand, then:

```toml
[backup]
dir = "/var/backups/huta"
ship = ["/usr/local/sbin/huta-ship"]
```

Without it, skip: projects without `data` don't need it.

Targets with their own Postgres (`postgres = { … }` in the registry) need `[backup]` too (the
dumps live there; `HUTA_TARGET` is then `<target>-pg`) and the images they may run, pinned by
digest (get one with `docker pull postgres:17` and `docker image inspect --format
'{{index .RepoDigests 0}}' postgres:17`):

```toml
[postgres.images]
"17" = "postgres@sha256:<64 hex>"
```

Targets that publish to Cloudflare (`external` in the registry) need the publisher image,
built from `systemd/wrangler.Dockerfile.example` and pinned by digest, and its token as a
platform secret (`/etc/huta/secrets/_platform/<NAME>`, root 0600):

```toml
[external]
wrangler = "<repo>@sha256:<64 hex>"
```

Targets with several components need the firewall fragment's `huta_intra` set (in
`systemd/nftables-huta-egress.nft.example`; load it again if yours predates it, then
`sudo huta egress sync`).

Monitoring hooks (optional; built so far: the remote resources, the variables they give
containers (`[monitor]` in the contract), and a probe of each monitored site every minute
(`huta-monitor.timer`, installed and enabled by `install.sh`); job checks and watch windows are
planned):
if the human already runs healthchecks, Bugsink or Matomo, huta can make a check, a project or
a site per target that asks for it (`monitor = { … }` in the registry). Each table is
optional; leave out the services they don't run.

```toml
[monitoring]
prefix = "huta"
[monitoring.healthchecks]
url = "http://127.0.0.1:8000"          # its API, loopback http or https://<host>
ping = "https://hc.example.org/ping"   # its ping base
[monitoring.bugsink]                   # needs [crashes]
dsn_host = "bugs.example.org"          # the host in its DSNs (Bugsink's BASE_URL)
team = "<team uuid>"
[monitoring.matomo]
url = "http://127.0.0.1:8081"
host = "stats.example.org"
```

The human sets the tokens (a healthchecks project's read-write API key, Bugsink's token,
a Matomo superuser token if huta should create sites): `sudo huta secret set _platform
HEALTHCHECKS_API_KEY` (and `BUGSINK_TOKEN`, `MATOMO_TOKEN`); the value is read without echo.
Check a target with `sudo huta monitor ensure <project> <target>`.

## 10. Verify

```sh
sudo huta check
systemctl is-active docker caddy nftables tailscaled huta-egress huta-logs.timer huta-push.socket
sudo docker compose -p huta-registry ps --format '{{.Service}} {{.State}}'
cat /opt/huta/REVISION
```

Everything `active`, the registry `running`, `huta check` ending in `registry ok`.

## 11. First project (if the human has one ready)

A project needs a host entry, `/etc/huta/projects.d/<project>.toml`
([contract.md](https://huta.run/docs/contract.md#host-side-registry-the-operator-writes-this)).
Pick a uid from 20001 up and two free loopback ports (for example 18300/18301), one hostname in
`<zones>`, and the box names that may deploy it in `boxes`. Then:

```sh
sudo huta check
sudo huta edge <project> prod            # dry run: what it would create
sudo huta edge <project> prod --apply    # plans: Origin CA certificate, vhost (503 "not released yet"), SSL rule, DNS record
sudo huta approve <id>                   # CONFIRM: applies that plan (or `huta edge … --now` at once)
```

The site now answers 503 `not released yet` until its first approved release.

## 12. Report to the human

Say what is installed and running, the huta revision, the zones, and what's left:

- **Each dev box:** run an agent there with "install the huta client, https://huta.run/install.md"
  (server: `<tsname>` or an ssh name for it). Every box must be on the tailnet.
- **Each project:** an entry in `projects.d` naming its boxes, then `huta edge`; the project's
  agent follows [start.md](https://huta.run/docs/start.md).
- **Approving:** `sudo huta approve <release>` on this server (or Telegram buttons, later).
- **Updating huta:** `git pull` in the checkout, review the diff, `sudo bash install.sh`. It
  refuses while a release or recovery is in progress (finish or `huta reconcile` first). Boxes'
  clients follow by themselves. A huta older than the one that last wrote `huta.db` refuses to
  start; to go back, restore the database copy from before the upgrade.
- Anything from step 1 they should fix (password ssh login, low memory, no loop devices).

## Troubleshooting

| You see | Meaning | Do |
|---|---|---|
| `huta check`: `… must be …` | a key in `huta.toml` has the wrong form | fix that key; the message names it |
| `caddy validate` fails on a huta vhost | a cert file missing or unreadable by the `caddy` group | `sudo huta edge <p> <t> --now` again |
| Cloudflare `Authentication error` | a permission row or a zone is missing from the token, or the IP filter lacks the address it calls from | edit the token (the value stays the same) |
| registry `curl` gives `000` or a TLS error | `tailscale serve` not running, or HTTPS certificates off in the tailnet | `tailscale serve status`; turn HTTPS on in the admin console |
| `huta enroll` fails restarting the registry | no single `registry` container in project `huta-registry` | `cd /opt/huta-registry && sudo docker compose up -d` |
| a box gets `Permission denied (publickey)` | it was enrolled with a non-tailnet address, or sshd's `AllowUsers` lacks `huta-*@100.64.0.0/10` | see [install.md](https://huta.run/install.md#troubleshooting) |
| containers can reach the internet directly | the guard table isn't loaded | `sudo nft list table inet huta_guard`; reload nftables |


---

<!-- https://huta.run/install.md -->

# Install the huta client

> For an agent asked something like **"install the huta client, https://huta.run/"**.
> Follow these steps in order. Steps marked **ASK** need the human. Never run a command on the
> deploy server without first showing the human the exact command and getting a clear yes.

What you're setting up: this machine becomes a **box** that can ask a huta host (the "deploy
server") for releases over the **push** transport: it builds images, pushes them to the
server's registry, and sends requests over ssh to a forced command. Prod still waits for the
owner's approval.

## 1. ASK: two facts, in one message

Ask the human:

1. **Which deploy server?** An ssh name you can already reach (for example a `Host` entry in
   `~/.ssh/config`), or "no access" if you can't ssh there.
2. **What should this box be called?** Suggest this machine's short hostname in lower case
   (letters, digits, dashes, starting with a letter, at most 27 characters), for example
   `laptop`.

Use the answers as `<server>` and `<box>` below.

## 2. Check this machine

```sh
python3 -c 'import sys; assert sys.version_info >= (3, 11)'   # Python 3.11+
docker version --format '{{.Server.Version}}'                  # Docker running
tailscale ip -4 | head -n 1                                    # this box's tailnet address
```

`tailscale ip -4` gives `<ip>`: the server pins this box's key to that address, so it must be
the address the server sees this box connect from (its tailnet address).

**`<ip>` must be this box's tailnet address: one address, the first line `tailscale ip -4`
prints.** Never use
a public-IP lookup service (ipify, ifconfig.me, a "what is my ip" page, the operator's own IP
service, and so on). The box reaches the server over the tailnet, the key is pinned to the
address the server sees, and with a public IP every login fails with
`Permission denied (publickey)`. If any check
fails, stop and tell the human what is missing. Make sure `~/.local/bin` is on `PATH` (the
client is installed there).

## 3. Get the client

**With server access:** the server hands out its own client, matching its version. This is
the first command on the server: **show it to the human and get a yes** (it changes nothing
there, but its output is code you'll run here):

```sh
mkdir -p ~/.huta-client-src && cd ~/.huta-client-src
ssh <server> sudo huta client-bundle | tar -xz
cd huta-client
```

If ssh says the server's host key is unknown and asks whether to trust it, **stop**: show the
human the fingerprint and continue only once they confirm it is the server's (for example
against `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run on the server). Everything after
this, including the host key huta pins, comes over this connection. Never pass
`StrictHostKeyChecking=no`. If `sudo` asks for a password or is refused, go to **without server
access**.

**Without server access:** clone the public repository
(`git clone https://github.com/huta-run/huta`) and `cd` into it. Its client must match the
server's huta version closely enough; if the server runs an older huta, ask the human for the
bundle from someone who can run `sudo huta client-bundle` there instead.

## 4. Make this box's key

```sh
./install-client.sh --box <box> --keygen-only
```

It prints one line, `ssh-ed25519 AAAA… huta-push@<box>`: the **public** key, safe to share. The
private key stays in `~/.ssh/id_huta_push`. Never print or send the private key.

## 5. Enroll the box on the server (CONFIRM first)

The server command is:

```sh
mkdir -p ~/.config
ssh <server> "sudo huta enroll <box> --from <ip> --pubkey '<the public key line>'" > ~/.config/huta-enroll.json
```

Keep the `>` outside the quotes (the JSON must land here, not on the server). The box name and
the key line must contain no quotes or `;`: they come from steps 1 and 4, so they don't, unless
something is wrong.

Before running it, **show it to the human and ask for a yes**, and say what it does on the
server:

- creates the user `huta-<box>`, which can only run huta's request command (no shell, no sudo:
  the key itself is restricted to that command);
- lets that user log in only with this key, only from `<ip>`;
- creates this box's registry login (its password reaches only this box, through step 7);
- restarts the server's registry for a few seconds.

It's safe to run again: it replaces the key, and a new key also gets a new registry login
(run `huta login` again after).

**Without server access:** give the human that same command without the `ssh <server>` part,
so they can run it on the server as root (or hand it to the server's agent), and ask them to
paste the one-line JSON it prints back to you. Save that as `~/.config/huta-enroll.json`.

The JSON looks like
`{"box": "…", "ssh": "huta-<box>@…", "port": …, "registry": "…", "host_key": "ssh-ed25519 …"}`.
If the server answers `no [transport.push] in huta.toml` or `ssh_host is not set`, push isn't
set up on that server yet: stop and tell the human.

## 6. Install with the server's settings

```sh
./install-client.sh --box <box> --settings ~/.config/huta-enroll.json
```

This writes `~/.config/huta/client.toml` and pins the server's ssh host key in
`~/.config/huta/known_hosts`, so huta's own connections can't be redirected. (The pin came over
your ssh to the server, which is why step 3 checks that connection's host key.)

## 7. Log in and test

```sh
huta login                       # the box's registry login, fetched over ssh, given to docker on stdin
huta status <project> <target>   # any project the server hosts; proves ssh + identity work
```

`huta login` prints `logged in to <registry> as <box>`. If you don't know a project, ask the
human for one, or skip the status check.

You install once: from then on the client keeps itself at the server's version (it updates
itself after a command when the server has moved on, and says so). To get only a notice instead,
add `auto_update = false` at the top of `~/.config/huta/client.toml`.

## 8. Report to the human

Say the box is enrolled, which server it talks to, that the client updates itself, and that the
operator still has to add the box to `boxes` in each project's registry entry it should deploy
(until then `huta status` answers `no project … for box <box>`). Then how to use it: from a project checkout,
`huta pack --app <path to deploy/app.toml>`, then `huta submit <target> --app <the same path>`
(both default to `deploy/app.toml`). Prod waits for the
owner's approval (one tap in Telegram, or `sudo huta approve` on the server). Point them to
[start.md](https://huta.run/docs/start.md) for preparing a project.

## Troubleshooting

| You see | Meaning | Do |
|---|---|---|
| `huta: no answer over ssh: … Permission denied (publickey)` from `huta login` | the key isn't enrolled, or `<ip>` was wrong | re-run step 5 with the address from `tailscale ip -4` |
| `Permission denied (publickey)` although the box was enrolled | the box was enrolled with its **public** IP (from a lookup service) instead of its tailnet address, so `from=` never matches | re-run step 5 with `--from "$(tailscale ip -4)"`; it replaces the key line |
| `Host key verification failed` | the pinned host key doesn't match | stop; tell the human (never disable host-key checking) |
| `docker push failed (did you run huta login?)` | no registry login on this machine | `huta login` |
| `docker login failed: … x509` or `… certificate` | Docker doesn't trust the registry's TLS certificate | stop and tell the human: Docker must trust that certificate (install its CA); don't switch to `insecure-registries` |
| `socket peer uid … is not a huta push user` / `box … is not allowed to push` | the box user is missing on the server | re-run step 5 |
| `settings are for box 'x', not 'y'` | wrong JSON for this box | use the JSON from enrolling this box |

To remove a box later, on the server: `sudo huta revoke <box>`.


---

<!-- https://huta.run/docs/start.md -->

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

An HTTP service or a static site (the `web` recipe): one container, or several components per
target (a web front, an API, workers) that release together. The host can give it, when the
operator grants them in the host registry:

- **secrets and settings** as environment variables;
- **outbound HTTPS** to an allowlist of hostnames, and named host services (upstreams);
- **a data volume** of a fixed size, mounted at one path, backed up on a schedule (an SQLite
  file on it is fine);
- **scheduled jobs** that run in the same image;
- **smoke checks** before traffic moves, and an automatic switch back when the public checks
  fail;
- **its own Postgres**, run by huta, with an `app` role for the code and a `migrate` role for
  schema changes;
- **schema migrations** (`[migrate]`), exclusive by default, online when everyone allows it,
  with a pinned restore point and restore plans to go back;
- **publishing to Cloudflare** Pages or Workers as a step of the release.

What each means for your code: [Runtime](runtime.md) and [Contract](contract.md). Moving a site
that already runs on the server: [Moving a live site](moving.md). Not available: preflight
checks, plain-HTTP egress, a data volume and a database on one target together, unattended
releases. If the project needs one of those, tell the human.

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
- the project needs something huta does not have (preflight checks, unattended releases):
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


---

<!-- https://huta.run/docs/contract.md -->

# The contract: `deploy/app.toml`, the image, the host registry

Three pieces decide how a release runs:

| Piece | Who writes it | Where |
|---|---|---|
| `deploy/app.toml` | you (the repo) | the repo, read by the host **from GitHub at the release sha** |
| the image | you (built on dev) | `huta-build/<project>:<sha12>` in dev's Docker |
| the host registry | the operator | `/etc/huta/projects.d/<project>.toml` on the host, root-owned |

The repo asks, the host decides: users, ports, hostnames, limits, secrets, data size, egress and
policy come only from the host registry. Nothing in the repo can widen them. What the repo
declares (a data mount path, smoke checks, jobs) runs inside those limits.

What each runtime feature means for your app (proxy variables, per-slot files, how jobs run,
what is backed up) is in [Runtime](runtime.md). Moving a site that already runs somewhere:
[Moving a live site](moving.md).

## `deploy/app.toml`

Data only. Strict:

- **top level:** only the keys in the table below; anything else is refused;
- **`[components.<name>]`**, **`[images.<name>]`**, **`[state]`**, **`[build]`**,
  **`[[smoke]]`**, **`[[jobs]]`**, **`[[diag]]`:** only their listed keys; anything else is refused;
- `preflight` is a known key but must be absent or empty (*planned*).

### TOML pitfall: top-level keys first

In TOML, every `key = value` after a `[table]` header belongs to that table. So this is wrong:

```toml
schema = 1
project = "myapp"

[components.web]
image = "web"
order = ["web"]     # WRONG: this is components.web.order
```

huta refuses it with:
`app.toml: components.web has unknown keys ['order'] (top-level keys must come before the first [table])`.
A misplaced `schema` or `project` is reported differently, because those are checked first:
`app.toml: schema must be 1` or `app.toml: project must be 'myapp'`. Put `schema`, `project`
and `order` at the top, before any `[...]` line.

### A stateless file

```toml
schema = 1
project = "myapp"
order = ["web"]

[images.web]
dockerfile = "Dockerfile"
context = "."

[components.web]
image = "web"
kind = "http"
port = 8080
health = "/healthz"

[state]
kind = "none"
```

### A file with data, checks and a job

```toml
schema = 1
project = "myapp"
order = ["web"]

[images.web]
dockerfile = "Dockerfile"

[components.web]
image = "web"
port = 8080
health = "/healthz"
data = "/data"              # the target's data volume is mounted here, read-write
concurrent_safe = false     # true only if two releases may write the same data at once

[state]
kind = "data"

[[smoke]]                   # run on the new slot before the switch, and publicly after it
path = "/"
status = 200
contains = "<title>myapp</title>"
headers = { "content-security-policy" = "frame-ancestors" }

[[smoke]]
path = "/"
request_headers = { accept = "text/markdown" }
headers = { "content-type" = "text/markdown" }
headers_absent = ["x-robots-tag"]   # the markdown twin must not be noindex

[[jobs]]
name = "rotate"
cmd = ["node", "jobs/rotate.mjs"]
schedule = "*-*-* 03:10:00"   # systemd OnCalendar
timeout = "10m"
```

### Top-level keys

| Key | Type | Notes |
|---|---|---|
| `schema` | int | required, must be `1` |
| `project` | string | required; must equal the project name in the host registry |
| `order` | array of strings | required; every component exactly once, no duplicates |
| `images` | table of tables | required; 1..8 `[images.<name>]` |
| `components` | table of tables | required; 1..8 components, exactly one of them the public HTTP one (below) |
| `state` | table | optional; `kind` (`"none"` default, or `"data"`), and with data `schema` and `runs_on` (below) |
| `migrate` | table | optional; a migration this release runs before its code starts (below) |
| `external` | table of tables | optional; files to publish to Cloudflare Pages or Workers after the switch (below) |
| `build` | table | optional; only `test` (accepted; `huta pack` does not run it yet) |
| `smoke` | array of tables | optional; at most 20 checks (below) |
| `jobs` | array of tables | optional; at most 20 jobs (below) |
| `diag` | array of tables | optional; at most 20 diagnostics (below) |
| `monitor` | table | optional; `env`, the monitoring variables your app wants (below) |
| `preflight` | array of tables | must be absent or empty (*planned*) |

Any other top-level key: `app.toml: unknown top-level keys ['<key>']`.

### `[images.<name>]`

Name: `^[a-z][a-z0-9-]{0,30}$`. Only these keys:

| Key | Type | Default | Notes |
|---|---|---|---|
| `dockerfile` | string | required | path relative to the repo root |
| `context` | string | `"."` | build context, relative to the repo root |

### `[components.<name>]`

Name: `^[a-z][a-z0-9-]{0,30}$`. Only these keys:

| Key | Type | Default | Notes |
|---|---|---|---|
| `image` | string | | required: must name one of the `[images.<name>]` tables |
| `kind` | string | `"http"` | `"http"`, or `"worker"` (serves nothing: no `port`, `health` or `public`) |
| `public` | bool | `false` | the component the site routes to; needed only when there is more than one HTTP component |
| `cmd` | array of strings | | the command (argv, no shell) instead of the image's own; the image's entrypoint still applies |
| `port` | int | `8080` | 1..65535; the port your server listens on **inside** the container, on `0.0.0.0` |
| `health` | string | `"/healthz"` | must match `^/[A-Za-z0-9/_.-]*$` |
| `data` | string | | with `state.kind = "data"`: the absolute mount path, `^/[a-z0-9][a-z0-9/_.-]*$`, not a system directory (`/etc`, `/tmp`, `/usr`, …). Only components that name it get the volume; with `kind = "data"` at least one must, without it none may |
| `concurrent_safe` | bool | `false` | `true`: two releases may run on the same data at once. It only takes effect when the host allows it too (`data.concurrent` in the registry) |
| `upstream_api` | string | | must be absent or empty (*planned*) |

### Several components

A target can run up to 8 components from up to 8 images: a web front, an API, a worker.

```toml
order = ["web", "api", "worker"]          # start order

[images.web]
dockerfile = "web/Dockerfile"
[images.api]
dockerfile = "api/Dockerfile"

[components.web]
image = "web"
public = true                             # the site routes here
[components.api]
image = "api"
port = 8081
data = "/data"
[components.worker]
image = "api"
kind = "worker"
cmd = ["python", "-m", "app.worker"]
data = "/data"
```

- **A slot is the whole set.** Slot a is web + api + worker of one release, on its own network:
  the components reach each other by name (`http://api:8081`). The other slot's release can't
  reach them. The switch moves everything at once, so versions never mix.
- Only the public component is published (on the slot's loopback port, as with one component).
  Every other HTTP component must answer its `health` path with `ok` and the sha too; the host
  checks it on its slot address before the switch.
- **Workers are single.** A new release's workers start only after the switch and the public
  checks, once the old release's workers have stopped. They must still be running after the
  host's `worker_liveness` (default 10 s); otherwise the release goes back (when the old slot is
  still there) or ends `needs-human`.
- One writer is per volume: every component that mounts the data counts. Two releases overlap
  only if all of them are `concurrent_safe` and the host allows it.
- The host's secrets reach the public component; other components get only what the registry
  names for them (`[targets.<t>.components.<c>]`). Jobs and diagnostics take
  `component = "<name>"` (default: the public one) for their image and mount.
- `huta pack` builds every image (each labelled `run.huta.image=<name>`); the public
  component's image keeps the name `huta-build/<project>:<sha12>`, the others are
  `huta-build/<project>/<image>:<sha12>`. `huta submit` sends them all.

### `[state]`

`kind`: `"none"` (default), `"data"` or `"postgres"` (below). With `"data"` the host gives the target its own
filesystem (a fixed size, set by the operator), mounted at `components.<c>.data`, backed up on a
schedule. A plan whose app.toml asks for data while the host registry gives the target none is
refused: `app.toml mounts data at /data, but the host registry gives myapp/prod no data`.
With `"postgres"` the release uses the target's own database server (the host must grant
`postgres` in the registry):

- huta runs one Postgres per target, from an image the operator pins by digest, on its own
  volume and its own internal networks (no route out). Your containers reach it as
  `postgres:5432`, slot a and slot b each on their own network to it.
- Your components get `DATABASE_URL` (`postgresql://app:…@postgres:5432/app`). The `app` role
  may read and write rows but not change the schema: create and change tables in `[migrate]`,
  which gets the `migrate` role (it owns the schema) and reaches only the database.
- Releases without a migration overlap as usual (both slots on the database); a migration runs
  exclusive, with a dump pinned as its restore point, as for data.
- Backups are `pg_dump` files on the target's schedule, shipped like data copies.
- A data volume and a database on one target together are not supported yet.

With data, two optional keys version it:

```toml
[state]
kind = "data"
schema = 4            # the schema this release leaves the data at (1..1000000)
runs_on = [3, 4]      # schemas this code runs on; must include schema (default: [schema])
```

The host records the schema the data is at. At plan time: the same schema runs as usual; a
higher one needs `[migrate]`; a lower one is refused (going back is a restore, never a migration
down); a target whose data has a schema refuses a release that declares none. The first release
that declares a schema without `[migrate]` just starts the record.

### `[migrate]`

```toml
[migrate]
image = "web"                                 # one of [images]
cmd = ["python", "manage.py", "migrate"]      # argv, no shell; the image's entrypoint applies
timeout = "10m"                               # default 10m, at most 6h
```

When a release raises the schema, huta runs it **exclusive**: every writer stops (the site
answers 502 until the switch), the data is copied and the copy is pinned as a restore point for
this release, then `cmd` runs once in its own container from that image: the target's limits and
data volume, **no network**, and only the secrets the operator lists in `migrate_secrets`. Its
environment also has `HUTA_SCHEMA_FROM` and `HUTA_SCHEMA_TO`. Exit 0 within `timeout` records the
new schema; then the new code starts and is checked as usual.

- The migration fails, or the new code fails its checks before the switch: the copy goes back,
  the schema is unchanged, the old release runs again: `nothing-activated`. Nothing is lost,
  because nothing but the migration wrote since the copy.
- huta dies mid-way: `huta reconcile` does the same (whether the migration committed doesn't
  matter: the copy goes back).
- The public checks fail after the switch: the new code keeps serving (`needs-human`), as for
  any one-writer release; going back means a restore.
- **Online** (`online = true`): the migration runs while the live release keeps serving, for
  changes its code tolerates (adding a column or a table). It needs all of: `online = true`
  here, `migrate_online = true` from the host, the live release's `runs_on` including the new
  schema, and releases allowed to overlap. Otherwise it runs exclusive. A reference copy is
  pinned (for a restore plan), but **nothing is restored automatically**, because the live
  release kept writing: a failed migration ends `needs-human` while the old release serves; a
  new release that fails its checks afterwards ends `nothing-activated` and the schema stays
  migrated (the old code runs on it).
- `check = ["python", "manage.py", "schema"]` (optional, argv): prints the data's schema
  number. huta runs it when an online migration's result is unknown (it failed, or huta died
  during it). Without it such a crash ends `needs-human`.

Going back across a schema: `huta rollback` refuses code whose `runs_on` doesn't include the
data's current schema. `huta restore <project> <target> --to <release>` plans putting back the
restore point pinned before the migration (huta keeps the newest `keep_releases`, at least 2),
loses every write since, and needs `--confirm-data-loss` or the Telegram button; huta checks the
point's fingerprint at approval. The target then stays stopped until the release of that code,
which is planned for you, is approved.

### `[external.<name>]`

Files your build put in one of the release's images, published to Cloudflare after the switch
and the public checks, as a step of the same release (one plan, one approval):

```toml
[external.site]
kind = "cloudflare-pages"          # static files
image = "web"
dir = "/out/site"                  # where they are inside the image

[external.edge]
kind = "cloudflare-worker"         # one prebuilt bundle (huta does not bundle)
image = "web"
dir = "/out/worker"
main = "index.js"                  # the bundle, inside dir
```

- The host copies `dir` out of the image **without running it** and refuses anything but
  regular files and directories (no links or devices), more than 20000 files, files over
  25 MiB or 512 MiB in all. The plan shows the file count and a content hash; publishing
  checks the hash again.
- Pages files are static: `_worker.js`, `functions/`, `wrangler.*` and `package.json` are code
  and refused (publish code as a worker).
- The operator decides where it goes (`[targets.<t>.external.<name>]`: the Pages project or
  Worker name, the account, the token, a Worker's routes and compatibility date). Your repo's
  `wrangler.toml` is never used.
- A failed publish leaves the site live and ends the release `needs-human`, naming the step.
  After a crash huta asks Cloudflare whether this commit's files are already there before
  sending them again. `huta external <p> <t> rollback <name>` plans going back to the previous
  deployment.

### `[[smoke]]`

HTTP checks, data only. Each check is a GET; redirects are not followed (a 3xx can be the
expected status); bodies are read up to 1 MiB; 10 s each.

| Key | Type | Default | Notes |
|---|---|---|---|
| `path` | string | required | starts with `/`; plain URL characters (query strings allowed) |
| `status` | int | `200` | the exact status expected |
| `contains` | string | | a substring the body must contain (at most 500 characters) |
| `headers` | table | | response header name → a substring its value must contain |
| `headers_absent` | array of strings | | response header names that must **not** be present (for example `["x-robots-tag"]` on a page that must stay indexable) |
| `request_headers` | table | | sent with the request; not `Host`, `Content-Length`, `Transfer-Encoding`, `Connection` or `X-Forwarded-*` |

When they run: on the new slot over loopback **before** the switch (with `Host:` the target's
first host and `X-Forwarded-Proto: https`; any failure ends the release `nothing-activated`
and nothing changed), and through Cloudflare at `https://<host>` **after** the switch (see
[Runtime](runtime.md#smoke-checks-and-automatic-revert) for what a failure does then).

### `[[jobs]]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | `^[a-z][a-z0-9-]{0,30}$`, unique |
| `cmd` | array of strings | required | the command (argv) run in your image; no shell unless you call one |
| `schedule` | string | required | a systemd OnCalendar expression (`hourly`, `*-*-* 03:10:00`, `Mon *-*-* 04:00`); checked with `systemd-analyze calendar` |
| `timeout` | string | `"10m"` | `90s`, `10m`, `2h`; at most `6h`; the job is killed after it |
| `after_release` | bool | `false` | also run once each time a release of this target goes live (a rollback included), after it has finished; the plan says so |

A job runs in the **live** release's image, with the same user, limits, environment, secrets,
data mount and egress as the web container. Details: [Runtime](runtime.md#jobs).

### `[[diag]]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | `^[a-z][a-z0-9-]{0,30}$`, unique |
| `cmd` | array of strings | required | the command (argv) run in your image |
| `timeout` | string | `"1m"` | `90s`, `5m`; at most `5m` |

A diagnostic a dev box runs by name with `huta diag <project> <target> <name>`: in the live
release's image, **without network or secrets**, with the data's **last backup copy** mounted
read-only at the data path. Its output (256 KiB at most) passes the host's log redaction.
Adding or changing one is a change the plan lists. Details: [Runtime](runtime.md#diagnostics).

```toml
[[diag]]
name = "db-check"
cmd = ["node", "scripts/db-check.js"]   # integrity check, migration version, row counts
timeout = "2m"
```

### `[monitor]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `env` | array of strings | `[]` | `"sentry"` (`SENTRY_DSN`, `SENTRY_RELEASE`, `SENTRY_ENVIRONMENT`), `"matomo"` (`MATOMO_SITE_ID`) |

Your app gets these only if the host also grants them (`monitor` on the target in the
registry), and only once the host has made the Bugsink project or Matomo site: the plan says
which are set and why any are not. Never require them to start; treat them as optional. With
`"sentry"`, the host lets the target reach the Bugsink host on port 443 through the egress proxy
(the SDK must honour `HTTPS_PROXY`; there is no DNS in the container). Details:
[Runtime](runtime.md#monitoring).

```toml
[monitor]
env = ["sentry"]
```

## The image

### Enforced (the host refuses the release otherwise)

| Rule | Exact requirement |
|---|---|
| name on dev | `huta-build/<project>:<first 12 hex of the sha>` (`huta pack` sets it) |
| label | `org.opencontainers.image.revision=<full 40-hex sha>`, equal to the submitted sha (`huta pack` sets it) |
| label | `run.huta.project=<project>` (`huta pack` sets it) |
| user | `USER` set and not `root`, `0` or empty |
| platform | `linux/amd64` (`huta pack` always builds it) |
| no `VOLUME` | the image declares no `VOLUME` (Docker would attach unbounded writable storage); persistent data is the data volume |
| pull archive | one image; `docker save` output within 15 min, at most 2 GiB logical size and 20000 entries, no sparse files or duplicate paths |
| push | pulled by the exact digest you pushed |
| listen | the server listens on `0.0.0.0` at the `app.toml` `port`. Bound to `127.0.0.1` inside the container it never answers the host, and the release ends `nothing-activated` with `never reported <sha12>` |
| `/healthz` | answers within 30 s of start with `{"ok":true,"sha":"<full sha>"}` (details below) |

### Recommended (patterns, not checked)

- `ARG RELEASE_SHA` used to stamp `/healthz` at build time (`huta pack` passes
  `--build-arg RELEASE_SHA=<sha>`), or read `RELEASE_SHA` from the environment at run time;
- a numeric `USER` such as `20001:20001`;
- `LABEL` lines for the two labels too, so a plain `docker build` matches;
- base images pinned by digest; multi-stage builds; a small final image.

### How the container runs (generated by the host, not configurable from the repo)

- as the project's uid from the host registry (`user: <uid>:<uid>`), **not** your `USER`.
  Make files readable by any user; do not depend on file ownership;
- read-only root filesystem; `/tmp` is a tmpfs (16 MB unless the operator sets `limits.tmp`);
  the data volume (if any) is the only other writable place;
- `cap_drop: ALL`, `no-new-privileges`;
- memory, CPU and pids limits from the host registry (defaults `256m`, `1`, `128`);
- environment: the host registry's `env` and `secrets`, then `RELEASE_SHA`, `HUTA_PROJECT`,
  `HUTA_TARGET`, `HUTA_SLOT` (`a` or `b`; `job` in a job, plus `HUTA_JOB`), and the egress
  variables when the target has egress ([Runtime](runtime.md#egress));
- **no DNS**: names don't resolve inside the container (Docker's resolver would otherwise be a
  way out). Outbound HTTPS goes through the egress proxy, which resolves names itself;
- the container port is published on a loopback port of the host only; the host's web server
  proxies to it;
- each target gets its own Docker bridge with **no route out**; the only thing it can reach is
  the host's egress daemon, and only for what the target is allowed;
- the old slot gets SIGTERM after the switch plus `drain` seconds, then is killed 10 s later.

### `/healthz`

After start, the host polls `http://127.0.0.1:<slot port><health>` for up to **30 s**. It passes
only when:

- the status is 2xx (redirects are **not** followed: a redirect fails);
- the body is at most 64 KiB of JSON;
- `ok` is JSON `true` and `sha` equals the full 40-hex release sha.

```json
{"ok":true,"sha":"0123456789abcdef0123456789abcdef01234567"}
```

Serve it with `Cache-Control: no-store`: after the switch the host fetches
`https://<host><health>` through Cloudflare and expects the new sha.

Keep it shallow: don't make it depend on an outside API, or that API's outage blocks releases.

### HTTP behaviour belongs in the image

The generated vhost does TLS, compression, a Cloudflare-only IP filter, and a `reverse_proxy` to
the live slot. Nothing else. Your headers (CSP, caching), redirects, rewrites and 404 pages go
in your own server's config inside the image. What the vhost passes to your app:
[Runtime](runtime.md#what-the-web-server-passes-to-your-app).

## Host side: registry (the operator writes this)

You cannot install it. When a project or target is new, give the operator this file, filled
in. File: `/etc/huta/projects.d/<project>.toml`.

```toml
schema = 1
project = "myapp"
repo = "example/myapp"           # GitHub owner/name; the host reads it with a read-only token
app_toml = "deploy/app.toml"     # path of the manifest in the repo
boxes = ["mybox"]                # dev boxes that may submit it and read its status, log, jobs, logs
uid = 20001                      # 20000..29999, unique across projects
log_drop = ["enter setup code"]  # optional: log lines holding one of these are never stored

[legacy.myapp-old]               # optional: a service of this project that huta doesn't run
unit = "myapp.service"           # its journal is readable with huta logs myapp myapp-old
env_file = "/opt/myapp/.env"     # its values are redacted too; unreadable: nothing is shown
crashes = 12                     # optional: its Bugsink project id (huta crashes)

[targets.prod]
hosts = ["myapp.example.org"]    # must be in a Cloudflare zone the host allows
cloudflare = "orange"            # "orange" only
slots = [18300, 18301]           # two free loopback ports, 1025..65535, unique on the host
limits = { memory = "256m", cpus = 1, pids = 128, tmp = "64m" }
approve = "always"
require_ci = true
keep_releases = 5
drain = 10                       # seconds the old slot keeps running after the switch

# Optional, as the project needs:
env = { PUBLIC_ORIGIN = "https://myapp.example.org" }   # not secret
secrets = ["API_TOKEN"]          # names; values set with `huta secret set` on the host
job_secrets = { watch = ["CHAT_TOKEN"] }   # secrets only that job gets, never the web container
egress = ["api.example.com:443", "*.cdn.example.net:443"]   # or "none" (default)
upstreams = ["vpn-exit"]         # named host services from huta.toml [upstreams.*]
data = { size = "2g", backup = "hourly", concurrent = false }
access_log = true                # false: no access log for this site
logs = { days = 7, max = "200m", boxes = true, jobs = false }   # huta logs (these are the defaults)
crashes = 11                     # its Bugsink project id, for huta crashes (needs [crashes] in huta.toml)
adopt = "myapp.example.org.caddy"   # only when moving a live site (moving.md)
```

`boxes` names the dev boxes that may use this project: pull, the dev container's name; push,
the name the box was enrolled under. Any other box gets `no project '<name>' for box <box>`,
the same answer as for a project that doesn't exist, for submit, `status`, `log`, `jobs` and
`logs`.
Without `boxes`, only the host's own CLI can act on the project. Several projects on one box
see each other: the box is the unit huta can tell apart.

| Field | Type | Default | Checked |
|---|---|---|---|
| `schema` | int | | must be `1` |
| `project` | string | | must equal the file name |
| `repo` | string | | required |
| `app_toml` | string | | required |
| `uid` | int | | 20000..29999, unique across projects |
| `log_drop` | array | `[]` | up to 32 strings of 3..200 characters, matched case-insensitively; a log line holding one is never stored or shown (for values the app makes at run time, such as a pairing code) |
| `targets.<name>` | table | | name `^[a-z][a-z0-9-]{0,30}$`; **unknown keys refused** |
| `hosts` | array of strings | | valid hostnames, unique across the registry |
| `slots` | array of 2 ints | | 1025..65535, unique across the registry |
| `cloudflare` | string | `"orange"` | `"orange"` only (Origin CA certificate, Cloudflare-only filter); plain mode with ACME is *planned* |
| `approve` | string | `"always"` | `"always"` only; unattended releases are *planned* |
| `limits` | table | `{memory="256m", cpus=1, pids=128, tmp="16m"}` | only these four keys; `tmp` 1m..4g (it counts as memory) |
| `require_ci` | bool | `true` | the newest run of the GitHub Actions workflow named `ci` for the sha, on `push` to `main`, is completed with success |
| `keep_releases` | int | `5` | old images beyond this are untagged |
| `drain` | int | `10` | 0..300 seconds |
| `env` | table | `{}` | names `^[A-Z][A-Z0-9_]{0,63}$`, not `HUTA_*`, proxy or loader variables; one-line string values |
| `secrets` | array | `[]` | names as for `env`, not also in `env`; every secret must be set or the plan is refused |
| `job_secrets` | table | `{}` | job name → secret names given **only** to that job's runs (not to the web container); names as for `secrets`, not also in `env` or `secrets` |
| `egress` | `"none"` or array | `"none"` | entries `host:port`, `*.suffix:port`, `*:port` or a literal `ip:port` (`[v6]:port`) |
| `upstreams` | array | `[]` | names defined in `huta.toml` `[upstreams.<name>]` |
| `data` | table | | `size` 64m..512g (required), `backup` `hourly`/`daily`/`off`/an OnCalendar (default `daily`), `concurrent` bool (default `false`). Needs `[backup]` in `huta.toml` (its directory holds the copies; `backup = "off"` only turns off the schedule and shipping) |
| `access_log` | bool | `true` | |
| `crashes` | int | | the target's Bugsink project id (`huta crashes`); with `monitor.errors` huta uses this project instead of making one |
| `monitor` | table | | `{ uptime, jobs, errors, analytics }` booleans: what huta wires the target into (needs `[monitoring]` in `huta.toml`; `errors` needs `[monitoring.bugsink]`, `jobs` `[monitoring.healthchecks]`; `jobs` and the uptime probe are *planned*). With `errors`, a release that gets `SENTRY_DSN` may also reach the Bugsink host on 443 |
| `matomo` | int | | a Matomo site id to use instead of making one (needs `monitor.analytics`) |
| `components.<name>.monitor_env` | bool | `false` | this component also gets the monitoring variables (the public one always does) |
| `legacy.<name>` | table | | project level: `unit` (`*.service`), `env_file` (absolute; required), `crashes` (optional); the name may not also be a target |
| `logs` | table | `{days=7, max="200m", boxes=true, jobs=false}` | `days` 1..90, `max` 16m..20g; `boxes`: the project's boxes may read the slots' logs; `jobs`: and the jobs' output |
| `adopt` | string | | a file name in Caddy's sites directory (`*.caddy`) |
| `health_timeout` | int | `30` | 5..900 seconds a new slot has to report healthy on `/healthz` |
| `migrate_secrets` | array | `[]` | secret names `[migrate]` gets (it gets no others) |
| `migrate_online` | bool | `false` | `true` lets a release migrate online when it asks (`[migrate] online = true`), the live release runs on the new schema, and releases may overlap |
| `components.<name>` | table | | for a component of an app with several: `limits`, `env`, `secrets` (the target's own secrets reach only the public component) |
| `external.<name>` | table | | where an `[external.<name>]` goes: `name` (Pages project or Worker), `account` (32 hex), `token` (a platform secret name; Cloudflare tokens are account-wide), Workers also `routes` (like `example.org/api/*`, in an allowed zone, not another target's host) and `compatibility_date`. Needs `[external] wrangler` in `huta.toml` |
| `postgres` | table | | `version` (a name in `huta.toml` `[postgres.images]`), `size` (256m..2t), `backup` (`hourly`/`daily`/`off`/an OnCalendar, default `daily`), `limits` (default 1g memory, 2 cpus). Needs `[backup]` in `huta.toml`. A target named `<this>-pg` can't exist next to it |
| `worker_liveness` | int | `10` | 1..300 seconds a new worker must stay up after the switch |
| `edge` | table | `{}` | `body` (request body limit, 1m..64g) and `timeout` (upstream read/write timeout, like `90s`, `10m`, `2h`; at most 24h). Unset: nothing is added to the vhost |
| `kind` | string | `"app"` | `"gateway"`: a hostname huta proxies to a service it doesn't run (below) |

Before the first release the operator runs `huta edge <project> <target> --apply` (and approves the plan)
(certificate, vhost answering 503 `not released yet`, Cloudflare per-host SSL rule; with
`adopt`, only the certificate: the old site keeps serving). If a host has no address record (A,
AAAA or CNAME; MX, TXT and the like don't count) and no covering wildcard, `--apply` creates a
**proxied A record** to the host's
`platform.public_ipv4`. It never edits or deletes an existing record.

### Gateways: a hostname for a service huta doesn't run

A target with `kind = "gateway"` runs nothing. huta does its edge exactly as for an app (DNS
record, Origin CA certificate, strict SSL rule, a vhost with the Cloudflare-only filter and the
access log, plan and approval, journaled change, `huta traffic`) and proxies to an upstream the
operator names. It doesn't build, release, check or roll back that service.

```toml
[targets.media]
kind = "gateway"
hosts = ["media.example.org"]
gateway = { to = "100.64.0.5:8080", secret = "GATEWAY_KEY", header = "X-Gateway-Key" }
edge = { body = "1g", timeout = "10m" }
```

- `to` is an IP address and port in loopback, private (10/8, 172.16/12, 192.168/16), CGNAT
  (100.64/10) or ULA (fc00::/7) space. Names and public addresses are refused: the host never
  proxies to the internet.
- `secret` and `header` (together, optional): huta sends that header with the secret's value to
  the upstream. The value lives in its own file (`/etc/caddy/huta-secrets/<p>--<t>.caddy`,
  root and the `caddy` group only) that the vhost imports; the vhost itself never holds it.
  Values with quotes, braces, `$` or spaces are refused. Set it with `huta secret set`.
- A gateway accepts only `kind`, `hosts`, `gateway`, `edge`, `cloudflare` and `access_log`. A
  project of gateways only needs no `repo`, `app_toml` or `uid`.
- `huta edge <p> <t> --apply` (then `huta approve <id>`) creates or changes it. Release
  commands (`submit`, `status` of a target, `stop`, `logs`, jobs) refuse a gateway.


---

<!-- https://huta.run/docs/runtime.md -->

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


---

<!-- https://huta.run/docs/moving.md -->

# Moving a live site onto huta

For a site that already serves its hostname from the same server some other way (its own vhost
file, a systemd unit, a cron job, a data directory). huta takes it over without a gap and
without risking the only copy: the old config keeps serving until huta's first release has
passed its checks, and goes back if the first public checks fail.

## What to prepare in the repo (you)

1. Everything in [Start here](start.md): a Dockerfile, `/healthz`, `deploy/app.toml`.
2. **Move the site's HTTP rules into the image.** huta's vhost only does TLS, compression,
   the Cloudflare filter and the proxy. Headers (CSP, caching), redirects, rewrites, content
   negotiation, static files: your own server inside the image (for example Caddy or nginx in
   front of your app, in the same container).
3. **Persistent state** → `state.kind = "data"`, one mount path (`data = "/data"`). Make your
   app's data directory configurable (an env var such as `DATA_DIR`) and point it there.
4. **Timers and cron jobs** → `[[jobs]]`, as commands in your image (a script, not a shell
   one-liner in a unit file).
5. **Secrets** → environment variables listed by name for the operator (values never in the
   repo). Once the names are in the registry, you can send the values yourself with
   `huta secret import <project> NAME… --from <old .env>` if you can read the old `.env`; the
   first release's plan lists them for the owner's approval.
6. **Outbound calls** → a list of `host:port` for the operator, and make your HTTP client use
   the proxy variables ([Runtime](runtime.md#egress)). Calls through an existing SOCKS or VPN
   exit → an upstream (`HUTA_UPSTREAM_<NAME>`).
7. **Checks** → `[[smoke]]` for what must be true before traffic moves: the canonical page, the
   headers you rely on, content negotiation, `noindex`, a 404.
8. If something checks the public site from outside (an uptime probe, a watcher), it can stay
   where it is; it doesn't need to run on the server.

Then write `docs/huta-migration.md` in the repo for the operator: env names (secret or not),
data path and size, egress list, upstreams, jobs, whether two releases may share the data, and
the old pieces to retire.

## What the operator does (on the host)

1. **Registry:** `/etc/huta/projects.d/<project>.toml` with the target's hosts, slots, `env`,
   `secrets`, `egress`, `upstreams`, `data`, `access_log`, and
   `adopt = "<the old vhost file>.caddy"` (a file in Caddy's sites directory that serves
   **only** this target's hosts; huta refuses one that serves others).
2. **Secrets:** `sudo huta secret import <project> NAME… --from /path/to/old/.env`, or
   `sudo huta secret set <project> NAME` for each (unless the agent already sent them from its
   box: `sudo huta secret list <project>` shows them as staged).
3. **Edge:** `sudo huta edge <project> <target> --apply`, then `sudo huta approve <id>`. With `adopt` it only gets the
   certificate: the old file keeps serving and the SSL mode is left alone.
4. **Data:** stop the old app's writes (stop its service and timers), then
   `sudo huta data import <project> <target> /old/data/dir`. That creates the volume, copies the
   files in, sets ownership to the project uid and takes the first backup. Anything the old app
   writes after the import is not carried over.
5. **First release:** the agent submits; the owner approves. huta starts the new slot, checks
   `/healthz` and the smoke checks on loopback while the old site still serves, then in **one
   validated reload** installs huta's vhost and takes the old file out (a copy is kept in
   huta's edge journal, `/var/lib/huta/edge/adopted/`), sets the per-host strict SSL rule, and
   runs the public checks. If they fail, the old file goes back and the old site serves again
   (`rolled-back`).
6. **Retire the old pieces** once the release is live: the old service and timers, the old
   deploy script. Keep the old data directory until the operator is satisfied with backups.

## Requirements and limits

- The old site must already serve a certificate Cloudflare accepts in strict mode (an Origin CA
  certificate or a public one): huta sets the strict rule at the switch, and the old site must
  keep working with it if huta switches back.
- Between the data import and the switch, the old app must not write (or its writes are lost).
- After a successful first release, going back to the old setup is a manual step: the saved
  file is in `/var/lib/huta/edge/adopted/`.


---

<!-- https://huta.run/docs/dockerfile.md -->

# Dockerfile patterns

**Enforced by the host:** a non-root `USER`, `linux/amd64`, the labels
`org.opencontainers.image.revision=<sha>` and `run.huta.project=<project>`, and `GET /healthz`
returning `{"ok":true,"sha":"<full sha>"}`. `huta pack` takes care of the platform (it always
builds `--platform linux/amd64 --provenance=false`, emulated on arm64 machines) and the labels.

**What the runtime needs:** the app must work with a read-only root filesystem (only `/tmp`
writable) and without outbound network.

**Recommended patterns** (not checked): `ARG RELEASE_SHA` to stamp `/healthz` (`huta pack` passes
`--build-arg RELEASE_SHA=<full sha>`), a numeric `USER`, `LABEL` lines for the two labels so a
plain `docker build` matches, base images pinned by digest (`image:tag@sha256:...`). The
digests below are the ones huta's own example uses.

## 1. Static site (about 14 MB)

A tiny static server ([static-web-server](https://static-web-server.net/) 2.33) plus your files
plus your own HTTP rules. `/healthz` is a file stamped at build time in a busybox stage.

Layout:

```
Dockerfile
sws.toml
site/            your built files (index.html, ...)
deploy/app.toml
```

`Dockerfile`:

```dockerfile
FROM busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662 AS stamp
ARG RELEASE_SHA=unknown
RUN mkdir /out && printf '{"ok":true,"sha":"%s"}\n' "$RELEASE_SHA" > /out/healthz

FROM joseluisq/static-web-server:2.33.0@sha256:04def7ef00cfb0dd7fb869e68a76140ce79cc607f0d5773b873c8d69d4f6fdbf
ARG RELEASE_SHA=unknown
LABEL org.opencontainers.image.revision=$RELEASE_SHA \
      run.huta.project=myapp
COPY site/ /public/
COPY --from=stamp /out/healthz /public/healthz
COPY sws.toml /etc/sws.toml
ENV SERVER_CONFIG_FILE=/etc/sws.toml
USER 20001:20001
EXPOSE 8080
```

`sws.toml`:

```toml
[general]
host = "0.0.0.0"
port = 8080
root = "/public"
log-level = "info"
compression = true
cache-control-headers = true
directory-listing = false

[advanced]

[[advanced.headers]]
source = "**"
[advanced.headers.headers]
X-Content-Type-Options = "nosniff"
Referrer-Policy = "strict-origin-when-cross-origin"

[[advanced.headers]]
source = "/healthz"
[advanced.headers.headers]
Content-Type = "application/json"
Cache-Control = "no-store"
```

`deploy/app.toml`:

```toml
schema = 1
project = "myapp"
order = ["web"]

[images.web]
dockerfile = "Dockerfile"
context = "."

[components.web]
image = "web"
kind = "http"
port = 8080
health = "/healthz"

[state]
kind = "none"
```

If your site needs a build step (a bundler, a static-site generator), add a build stage before
`stamp` and `COPY --from=build /app/dist/ /public/` instead of `COPY site/`.

## 2. Small HTTP service (Python, standard library)

The host also sets `RELEASE_SHA` in the environment at run time (same value), so reading it
from the environment is correct both locally and on the host.

`Dockerfile`:

```dockerfile
# Pin this by digest: python:3.12-slim@sha256:<digest>
FROM python:3.12-slim
ARG RELEASE_SHA=unknown
ENV RELEASE_SHA=$RELEASE_SHA \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.revision=$RELEASE_SHA \
      run.huta.project=myapp
WORKDIR /app
COPY app.py /app/app.py
USER 20001:20001
EXPOSE 8080
CMD ["python", "/app/app.py"]
```

`app.py`:

```python
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHA = os.environ.get("RELEASE_SHA", "unknown")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps({"ok": True, "sha": SHA}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        else:
            body = b"hello\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
```

Install dependencies in a build stage (`pip install --target` or a venv copied in); nothing can
be installed at run time.

## Advice for any image

- Report `"ok": true` only when the service can serve. huta switches traffic to the new
  release only after `/healthz` passes, within 30 s of start.
- Write only to `/tmp` (16 MB, lost on restart). There is no persistent storage in v0.
- Make application files world-readable: the container runs as the project's uid from the
  host registry, which may differ from your `USER`.
- Handle SIGTERM. After the switch the old release keeps running for the target's `drain`
  seconds (default 10), then gets SIGTERM and 10 s more before it is killed. Long requests,
  streams and websockets on the old release can be cut.
- No secrets in build args, layers or files. Secrets reach the app as environment variables at
  run time ([Runtime](runtime.md#secrets-and-environment)).
- `huta pack` builds from `git archive HEAD`, not your working tree: only committed files
  exist in the build. Generated files (a built `dist/`, `node_modules`) must be produced by a
  build stage in the Dockerfile, not copied from your disk.
- Listen on `0.0.0.0` at the port in `app.toml`. A server bound to `127.0.0.1` inside the
  container is unreachable from the host's check; the release ends `nothing-activated` with
  `never reported <sha12>`.
- Test the committed state as the host will run it: take the sha in the repo, export the
  commit with `git archive` (like `huta pack`), build from the export:

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

  To prove the app starts without outbound network, run it once more with `--network none`
  (no port is reachable then; check the logs instead).


---

<!-- https://huta.run/docs/commands.md -->

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
- reads `--app` (default `deploy/app.toml`) and builds every `[images.*]` (with several, each
  labelled `run.huta.image=<name>`);
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
| `--component` | one component's lines, on a target with several |

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
| `huta edge <project> <target> [--apply\|--now]` | certificate, vhost, Cloudflare per-host SSL rule, and a proxied A record for hosts with no address record (A/AAAA/CNAME); dry run by default. `--apply` makes an edge plan, applied by `huta approve <id>`; `--now` applies at once (for the console; recorded) |
| `huta import-dev <project> <ref>` | import `huta-build/<project>:<sha12>` from the dev box by hand |
| `huta submit <project> <target> <sha> <image> [--image <name>=<image> …]` | verify and plan by hand (`--image` for every image of a project with several) |
| `huta show <release>` | print a plan |
| `huta approve <id> [--via cli\|telegram] [--confirm-data-loss]` | re-check everything and run the release, or apply an edge or restore plan. `--via telegram` is what a button press runs (as a transient systemd unit); it only changes how the approval is recorded. A restore plan needs `--confirm-data-loss` on the CLI |
| `huta reject <release>` | reject a planned release |
| `huta rollback <project> <target>` | **plan** a release of the previous image; it then needs `huta approve`. Refused when that code can't run on the data's current schema (its `runs_on` lacks it): then `huta restore` |
| `huta postgres <project> <target> [status\|upgrade]` | the target's database: what runs, its schema, whether an upgrade is pending; `upgrade` **plans** applying a changed image, version or limits (the site is down meanwhile: the app stops, a dump is pinned, the new server starts, restored from the dump when the version changed; any failure puts the old one back). huta never applies such a change by itself |
| `huta external <project> <target> [status\|rollback <name>]` | what releases published to Cloudflare; `rollback` **plans** going back to the deployment before the last one (the host target doesn't change) |
| `huta restore <project> <target> --to <release>` | **plan** putting the data back to the restore point pinned before the migration that left a schema `<release>` runs on. Every write since that point is lost, and the plan says so first. Approve with `huta approve <id> --confirm-data-loss` (or the Telegram button). The target stops and stays stopped; a release of `<release>`'s code is then planned for approval |
| `huta reconcile <project> <target>` | repair state from what the vhost actually routes to |
| `huta status [project] [-n N]` | recent releases on the host (default 20) |
| `huta status <project> <target> [-n N]` | that target's releases, then its containers now (as on dev) |
| `huta logs <project> <target> [options]` | the target's logs, same options and redaction as on dev; collects first, so it is up to the moment |
| `huta egress-log` / `traffic` / `crashes <project> <target> [--since …]` | as on dev |
| `huta monitor status\|ensure <project> <target>` | monitoring hooks: what is wired up for the target (a Bugsink project and its DSN, a Matomo site, a healthchecks check), and `ensure` makes what is missing. Failures are events, never release outcomes. `status` also shows the last probe |
| `huta monitor tick` | run every minute by `huta-monitor.timer` (does nothing without `[monitoring]`): probes the public health URL of every target with `monitor.uptime` (`ok: true` and the live sha), keeps the last 60 results, records `probe-down` / `probe-up` events, and pings healthchecks checks when configured |
| `huta secret list\|set\|rm _platform [NAME]` | the host's own tokens (`GITHUB_TOKEN`, `CLOUDFLARE_API_TOKEN`, `BUGSINK_TOKEN`, `HEALTHCHECKS_API_KEY`, `MATOMO_TOKEN`); `set` reads the value from stdin; containers never get them |
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


---

<!-- https://huta.run/docs/transports.md -->

# Transports: how a request reaches the host

A transport carries two things from dev to the host: a small JSON request and the image. Both
transports are available, use the same request format and feed the same verify → plan →
approve → run path. The dev-side commands are the same (`huta pack / submit / status / log`);
the client picks the transport from `~/.config/huta/client.toml`. Who sent a request is never
read from the request itself.

| | pull | push |
|---|---|---|
| For | a dev box on the same host (v0: an Incus container) | any dev box with ssh to the host |
| Request | a file in a shared inbox directory | over ssh, one request per connection |
| Sender known from | the configured dev container | the box's own system user `huta-<box>` (kernel peer credentials) |
| Image | the host runs `docker save` inside the dev box | dev pushes to a registry on the host; the host pulls it **by digest** |
| Dev-side setup | the operator mounts the inbox/outbox into the dev box | `install-client.sh --keygen-only`, `sudo huta enroll` on the host, `install-client.sh --settings`, `huta login` ([install.md](https://huta.run/install.md)) |
| `--no-wait` | supported | ignored (push always waits) |

## Request format and rules (both)

One JSON object, at most 16 KiB:

```json
{"v": 1, "id": "<16 hex>", "op": "submit", "project": "myapp", "target": "<target>",
 "sha": "<40 hex>", "image": "huta-build/myapp:<12 hex>"}
{"v": 1, "id": "<16 hex>", "op": "submit", ..., "digest": "sha256:<64 hex>"}   (push)
{"v": 1, "id": "<16 hex>", "op": "status", "project": "myapp", "target": "<target>"}
{"v": 1, "id": "<16 hex>", "op": "log", "release": "<8 hex>"}
{"v": 1, "id": "<16 hex>", "op": "jobs", "project": "myapp", "target": "<target>"}
{"v": 1, "id": "<16 hex>", "op": "logs", "project": "myapp", "target": "<target>",
 "since": "1h", "until": "...", "grep": "...", "tail": 500, "slot": "live",
 "release": "<8 hex>", "job": "<name>"}                  (all but project and target optional)
{"v": 1, "id": "<16 hex>", "op": "egress-log", "project": "myapp", "target": "<target>"[, "since", "until"]}
{"v": 1, "id": "<16 hex>", "op": "traffic", "project": "myapp", "target": "<target>"[, "since", "until"]}
{"v": 1, "id": "<16 hex>", "op": "crashes", "project": "myapp", "target": "<target>"[, "since"]}
{"v": 1, "id": "<16 hex>", "op": "diag", "project": "myapp", "target": "<target>", "diag": "<name>"}
{"v": 1, "id": "<16 hex>", "op": "credentials"}                                (push only)
{"v": 1, "id": "<16 hex>", "op": "client"}                                     (huta self-update)
{"v": 1, "id": "<16 hex>", "op": "secret", "project": "myapp", "name": "API_TOKEN",
 "value": "<one line, 1..8192 characters>"}              (huta secret set; staged until an approved release)
```

- duplicate keys are refused; `v` must be the integer `1`; fields are exact per op (no extra,
  none missing); names and hex values must match in full;
- `digest` is required on a push submit and refused on pull;
- a `secret` value is never echoed in the answer, an event or an error; over pull it sits in the
  inbox only until the host claims the request (seconds), then the host deletes it;
- **a request id can be used only once**, across both transports. A retry needs a new id; the
  client makes a new one for every command, so just run the command again;
- every answer is `{"v": 1, "id": "<id>", "ts": <unix>, "ok": true|false, ...}`, with `id`
  `null` if the request could not be parsed. The client refuses an answer whose id is not its
  request's;
- every answer also carries `"huta": "<host revision>"`; the client uses it to keep itself at
  the host's version (see `huta self-update` in [commands](https://huta.run/docs/commands.md)).

Everything in a request is a claim; the host re-checks each one itself.

## pull

1. `huta submit` writes `<inbox>/.<id>.json.tmp`, then renames it to `<inbox>/<id>.json`.
2. On the host, a systemd path unit (`huta-intake.path`) starts `huta intake`.
3. `huta intake` treats only names `<16 hex>.json` as requests. Anything else in the inbox is
   removed (temp files once they are older than 10 minutes). It handles at most 50 requests
   per run, one at a time: each is first claimed by renaming it into a root-only work
   directory, then checked (regular file owned by the dev user, at most 16 KiB), parsed, and
   **the id inside must equal the file name**.
4. For a submit, the host runs `docker save huta-build/<project>:<sha12>` inside the dev box
   (for each image, when the project has several)
   (15-minute deadline) and checks the archive: exactly one image, plain relative entries, at
   most 20000 entries and 2 GiB logical size, no sparse files, no duplicate paths. It **strips
   every image name**, loads the image by id and names it `huta/<project>:<sha12>-<id12>`
   itself, so an archive can never overwrite another image's tag.
5. Then the checks and the plan (below), and the answer in `<outbox>/<id>.json` (writable only
   by root; answers are kept up to 7 days).

Dev-side config (paths are whatever the operator mounted; these are examples):

```toml
transport = "pull"
[pull]
inbox = "/huta/inbox"
outbox = "/huta/outbox"
```

Host-side config, in `/etc/huta/huta.toml` (the operator's part, example values):

```toml
[transport.pull]
inbox = "/srv/huta-dev/inbox"     # the same directory, seen from the host
outbox = "/srv/huta-dev/outbox"
container = "devbox"              # the dev container's name
owner_uid = 1001000               # the dev user's uid as seen on the host
# work = "/srv/huta-dev/work"     # root-only claim directory; default: next to the inbox
```

## push

### Setup, once per dev box

Needs on the dev box: Python 3.11+, Docker, `~/.local/bin` on your `PATH`, and a registry
certificate your Docker trusts. If the host's registry has no publicly trusted TLS certificate,
your Docker daemon must list it under `insecure-registries` (or trust its CA), or `docker push`
fails.

The step-by-step, agent-guided version (with the questions to ask the human) is
[install.md](https://huta.run/install.md). In short:

1. **Get the client.** With ssh access to the host: `ssh <host> sudo huta client-bundle | tar -xz`
   gives `huta-client/` (`install-client.sh` + `src/huta`), matching the host's version.
   Otherwise use a clone of the huta repo.
2. **Make the key**, as your normal user, in that directory:

   ```sh
   ./install-client.sh --box mybox --keygen-only
   ```
   It makes `~/.ssh/id_huta_push` (for huta only) once and prints the **public** line.
3. **Enroll the box on the host** (the operator, or you with the human's explicit yes):

   ```sh
   sudo huta enroll mybox --from <this box's tailnet ip> --pubkey 'ssh-ed25519 AAAA... huta-push@mybox'
   ```
   It creates the system user `huta-<box>` in group `huta-push`, writes its key as
   `restrict,from="<ip>" <key>`, creates the box's registry login, restarts the registry for a
   few seconds, and prints one JSON line of client settings:
   `{"box": ..., "ssh": "huta-<box>@<host>", "port": ..., "registry": ..., "host_key": ...}`.
   Re-running it replaces the key. It needs `[transport.push] ssh_host` on the host.

   `--from` must be the box's **tailnet** address, exactly as `tailscale ip -4` prints it on the
   box. Never use a public-IP lookup service (ipify, ifconfig.me, an operator's own "what is my
   ip" service, and so on): the box reaches the host over the tailnet, the key is pinned to the
   address the host sees, and with a public IP every login fails with
   `Permission denied (publickey)`. To fix a box enrolled that way, enroll it again with the
   tailnet address.
4. **Install with those settings:**

   ```sh
   ./install-client.sh --box mybox --settings enroll.json
   ```
   It refuses settings made for another box, installs the client in
   `~/.local/lib/huta-client` with a launcher in `~/.local/bin/huta`, pins the host key in
   `~/.config/huta/known_hosts` (no trust-on-first-use) and writes `client.toml`.
   (By hand instead: `--ssh huta-<box>@<host> --port <port> --registry <host:port>
   --host-key "ssh-ed25519 AAAA..."`; `--ssh` must be `huta-<box>@<host>`.)
5. `huta login`. Then `huta pack` and `huta submit <target>` as usual.

To remove a box: `sudo huta revoke <box>`. It removes the key, the push permission and the
registry login first, then ends the box's sessions and removes the user; the registry restarts.

sshd needs **no per-box change**. One static drop-in covers every box:
`AllowUsers huta-*@<tailnet CIDR>` (added to the host's existing `AllowUsers` line, if it has
one) plus `Match User huta-*` (every huta-* login, whatever its groups) →
`ForceCommand /usr/local/sbin/huta-ssh` with forwarding, TTY and the rest turned off, closed
with `Match all`. Each key line also carries `command="/usr/local/sbin/huta-ssh"` and is pinned
to its box's address with `from=`. The
repo has an example: `systemd/sshd-huta-push.conf.example`.

### What happens on submit

1. The client tags the image `<registry>/huta-build/<project>:<sha12>`, runs `docker push`, and
   takes the digest the registry reports.
2. It sends the request (with `"digest"`) over ssh. Whatever command you ask ssh to run, the host
   runs `huta-ssh` as your box's user. It reads one request (at most 16 KiB, within 30 s) and
   passes it to the root service `huta push-request` over a local socket.
3. The root service learns the box from the **socket peer's uid** (the user `huta-<box>`), not
   from the request, and refuses boxes that are not enrolled (user not in `huta-push`, and not
   in the optional `boxes` list).
4. It pulls exactly `<registry>/huta-build/<project>@<digest>` from its local registry, drops
   the registry name, and names the image `huta/<project>:<sha12>-<id12>`.
5. Then the same checks and plan (the project must name the box in its `boxes`). The answer is **exactly one JSON line** on the ssh
   connection, at most 2 MiB (`huta-ssh` refuses a larger one rather than pass half of it on;
   `huta logs` answers stay under 1 MB).

Host-side config (the operator's part, example values):

```toml
[transport.push]
registry_local = "127.0.0.1:5000"               # where the host pulls from
registry_public = "registry.example.org:4882"   # what dev boxes push to
ssh_host = "host.example.org"                   # what `huta enroll` tells boxes to ssh to
ssh_port = 22                                   # default 22
registry_project = "huta-registry"              # compose project of the registry (restarted on enroll/revoke)
# boxes = ["mybox"]                             # optional, older configs only
```

A box may push when its user `huta-<box>` exists and is in the `huta-push` group (what
`huta enroll` sets up), or when it is listed in the optional `boxes`. Enrolling lets a box talk
to the host; which projects it may submit and read is each project's own `boxes` list
([contract.md](contract.md)), so after enrolling, add the box there.

## After the image is in (both transports)

The host checks the image's labels against the request (`image says X, request says Y` if the
sha differs), then the sha on `main`, CI when the target requires it, `deploy/app.toml` from
GitHub at that sha and the image (user, platform, labels), and records a plan. A refused submit
untags the image it imported. The owner is told about the plan (see
[approval.md](approval.md#notifications)).


---

<!-- https://huta.run/docs/approval.md -->

# Approval

Production waits for a human. You cannot approve, and nothing you send through huta can
approve (see the known limit below for what the operator accepts with buttons).

There are two ways for the owner to approve a plan, and both run the same checks:

- **one tap in Telegram** (available when the operator has turned the buttons on): **✅ Deploy**
  or **❌ Reject** on the release's message in the configured Telegram topic;
- **the host's command line** (always works, and is the fallback):
  `sudo huta approve <release>` or `sudo huta reject <release>`.

## One tap in Telegram

There is **one message per release, in one place**: the release's history message in the
configured Telegram chat or forum topic (`[telegram] history_chat` / `history_thread`).

1. For every new plan (a submit over either transport, a CLI submit, or a rollback) the host
   posts the release's history message and attaches two inline buttons to it, ✅ Deploy and
   ❌ Reject. huta sends and edits it itself through the Bot API; no chat assistant writes it.
   (The chat gateway's bot account can see it; see the known limit below.)
2. Each button carries its own capability string as `callback_data`: `huta:a:<22 chars>` for
   Deploy, `huta:r:<22 chars>` for Reject. That is 128 random bits each, single use, expiring
   with the plan (30 minutes). The host stores only their SHA-256 hashes. The strings are part of
   the message's keyboard: chat apps don't show them, but the bot account and any API client
   in the chat can read them, which is why only the owner's id is accepted.
3. A press reaches the chat gateway that polls the bot. A small deterministic relay plugin
   (`huta-press`, no LLM, never logs the data) claims the `huta` namespace, so the press never
   reaches a chat agent as text. It passes `{v, data, from_id, chat_id, message_id}` to the
   host's local press socket. The host accepts only connections from the configured relay
   user.
4. The host checks that the presser is the owner (`owner_id`), that the press came from the
   configured chat (`history_chat`, or the owner's own chat), that the secret matches an open
   plan and is unused (it is consumed atomically), that the press came from the same chat as
   that plan's message, and that the plan has not expired. A press after the 30 minutes marks
   the release `superseded`.
5. **Deploy** then runs `huta approve <release> --via telegram` as its own transient systemd
   unit, with exactly the command-line checks below (still planned, prod unchanged, sha on
   `main`, CI). **Reject** rejects the plan.
6. The message is edited as the release moves (deploying, live, not activated, needs a human,
   rejected), and the buttons are removed once the plan is decided.
7. If huta refused the press, the plugin replies in the chat with the reason:
   `this button is no longer valid`, `only the owner can approve`,
   `this button doesn't belong to this chat`, `not a huta button`,
   `this plan expired; submit again`. If huta could not be reached:
   `huta is unavailable; use sudo huta approve on the host`. Otherwise it says nothing; the
   gateway answers the button press itself, so there is no pop-up notice.

A CLI approve or reject, and an approve attempt that finds the plan expired, retire the
buttons: the keyboard is taken off the message and the strings stop working.

What this protects against: only the owner's Telegram id is accepted, and the press must come
from the same chat and the same message that carried the button. A lookalike button posted by
another bot or member can't approve anything, a used or expired string does nothing, and a
Reject string can't approve.

What it does **not** protect against (known limit): huta trusts the relay to report who
pressed. The secret strings are in the message's keyboard, which the bot account and every
chat client can see, and the relay may keep incoming updates on disk. So anything that runs as
the relay's Unix user (for example an AI agent of the chat bot with a shell) can read a Deploy
string and send a forged press with the owner's id. Use buttons only where that user runs
nothing you don't trust as much as an approver. Planned: a receiver that only huta controls (its
own bot token, readable by root only), so presses don't pass through the chat agent at all.

Operator setup (not something you do): `[telegram] history_chat` (and `history_thread`),
`owner_id`, `relay_user` and `buttons = true` in the host's `huta.toml`. Install the relay plugin
from the huta repo's [`openclaw-plugin/`](https://github.com/huta-run/huta/tree/main/openclaw-plugin) (see its README). Add the gateway's user to the
`huta-press` group and restart that user's systemd manager so the new group applies (a plain
service restart keeps the old groups). The buttons stay off until `buttons = true`, because an
unclaimed press would reach the chat agent as text, secret included.

## On the host's command line

1. Your `huta submit` produces a plan with a release id, and the owner is told (see
   [Notifications](#notifications)), including the line `sudo huta approve <release>`.
2. The owner reads it (`sudo huta show <release>`) and runs `sudo huta approve <release>` or
   `sudo huta reject <release>`.
3. Under the target's lock, `huta approve` checks everything again before acting (a button
   Deploy runs the same command):
   - the release is still `planned`;
   - it is less than **30 minutes** old (otherwise: state `superseded`,
     `plan expired; submit again`);
   - what is live is still what the plan was made against (otherwise: `superseded`,
     `prod changed since this plan was made; submit again`);
   - the effective config (host registry plus manifest) is unchanged (otherwise:
     `superseded`, `host config changed since this plan was made; submit again`);
   - the sha is still on `main` and CI is still green;
   - the certificate for the target is valid.
4. Then the `web` recipe runs: start the idle slot (no traffic) → `/healthz` must report the
   sha → the smoke checks pass on that slot over loopback → switch the vhost in one validated,
   graceful reload → the public `/healthz` and smoke checks pass through Cloudflare (otherwise
   traffic goes back to the old slot: `rolled-back`) → keep the old slot running for the
   target's `drain` seconds (default 10) → stop it. A target whose data allows one writer at a
   time stops the old slot first and copies the data before the new slot starts (a few
   seconds of 502; see [Runtime](runtime.md#one-writer-or-two-at-once)). Long requests,
   streams and websockets on the old slot can be cut.
5. The approved release ends in `live`, `nothing-activated`, `rolled-back` or `needs-human`.
   If it fails before the switch on a first deploy, the site keeps answering 503
   `not released yet`.

The log records how it was approved: `approved with the Telegram button` or
`approved on the host by <the sudo user>` (or `on the host (as root)`).

There is no automatic replan. After `superseded`, run `huta submit <target>` again.
Expiry is applied on an approve attempt: until then an old plan still shows `planned`.

The host registry accepts only `approve = "always"`: every release waits for a human.
Unattended releases are *planned*.

## What a plan shows

```
plan 1a2b3c4d · myapp → prod · state planned
  9f8e7d6c5b4a → 0123456789ab   image huta/myapp:0123456789ab-4f2c9a1b0e7d
  3 commits ahead of the live 9f8e7d6c5b4a
  image id sha256:4f2c9a1b0e7d...
  hosts: myapp.example.org · uid 20001 · slots [18300, 18301] · limits {...}
  egress: none
  class: code-only
  checks: on origin/main ✓ · CI green · built by dev
  plan hash 0f1e2d3c4b5a6978 · expires 14:32
```

- from → to: the live sha (or `(nothing live)`) and the new sha, and how they relate on GitHub:
  `N commits ahead of the live …`, `the same commit as the live … (a redeploy)`, or a warning:
  `⚠️ DOWNGRADE: goes back N commits from the live …`, `⚠️ NOT BASED ON LIVE: drops N commits
  of the live … and adds M`, `⚠️ can't compare: the live … is not in the repo (it moved, or its
  history was replaced)`, `⚠️ can't compare: shares no history with the live …`, or
  `⚠️ couldn't compare with the live …: <error>` when GitHub couldn't be asked. huta only warns;
  whether going back is right is the approver's call. A rollback always starts that line with
  `rollback: ` and never warns, whatever the comparison shows;
- the image name and immutable image id the host imported;
- the effective runtime config, decided by the host: env and secret names, egress, upstreams,
  the data volume (size, mount, one writer or concurrent, backup schedule, last backup), jobs,
  smoke checks, adopt, access log;
- `class`: `code-only` (only the image changes) or `infra` with the reasons;
- checks done; the plan hash; when it expires.

The Telegram message is shorter: project → target, release id, short shas, class, where it came
from, main and CI, the ⚠️ line when the plan goes back or isn't based on live (and the rollback
line), the state, and the expiry.

A rollback (`huta rollback <project> <target>`) is planned the same way, with
`· rollback to <release>` in the first line, and needs the same approval (buttons included).

## Notifications

Configured by the operator, all available:

- **Telegram history** (`[telegram] history_chat, history_thread, token_json, token_keys` in the
  host's `huta.toml`): the host keeps **one message per release** in a chat or forum topic and
  edits it as the release moves: waiting for approval (with `sudo huta approve <release>` and
  the expiry time), approved / deploying, live (with links to the hosts and the slot), not
  activated / needs a human / rejected / expired (with the reason), and replaced when a newer
  release goes live. With buttons turned on, this same message carries ✅ Deploy / ❌ Reject
  while the plan waits (above).
- **Fallback** `platform.notify` (an argv that gets a plain-text message on stdin when a plan
  waits), used only when no Telegram history is configured.

A failure to notify never affects a release.


---

<!-- https://huta.run/docs/rules.md -->

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


---

<!-- https://huta.run/docs/faq.md -->

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
