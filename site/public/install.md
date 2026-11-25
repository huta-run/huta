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
