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
