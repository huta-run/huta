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
