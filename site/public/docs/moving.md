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
