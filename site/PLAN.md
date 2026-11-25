# huta.run: the site

## What it is

A static site at `https://huta.run`, deployed **by huta itself** as an ordinary project
(`huta-site`, `web` recipe), the same way `examples/hello` is. huta deploying its own front
door is the demo.

Two audiences, in this order:

1. **Coding agents** that land on it and must learn, quickly and exactly, what huta is and what
   to do to deploy a project with it. They get markdown: `/llms.md` (the entry point),
   `/llms-full.md` (everything in one file) and `/docs/*.md`. `/llms.txt` and `/llms-full.txt`
   carry the same content under the names the llms.txt convention probes for; they are aliases,
   and the `.md` names are the ones we link.
2. **People**, who get one landing page (`/`).

## What is built

```
site/
  PLAN.md            this file
  Dockerfile         busybox stamp stage + static-web-server 2.33, same pinned digests as hello
  sws.toml           static-web-server config: headers, CSP, content types, cache rules, 404
  deploy/app.toml    valid v0 manifest: project huta-site, one web component, port 8080
  public/
    index.html       landing page (no JS, no external resources)
    index.md         the landing page as markdown
    install.md       agent-guided install of the client on a dev box (push: enroll, settings)
    host.md          agent-guided setup of an empty VPS as a huta host (diagnose, ask, install)
    style.css        light and dark via prefers-color-scheme
    404.html
    favicon.svg
    llms.txt         agent entry point (the one hand-edited index): summary + links to the docs
    llms.md          copy of llms.txt (generated)
    llms-full.txt    llms.txt + every docs page, concatenated (generated)
    llms-full.md     copy of llms-full.txt (generated)
    docs/
      start.md       prerequisites, then the agent checklist, in order
      contract.md    app.toml keys, what v0 refuses, image rules, /healthz, host registry fields
      dockerfile.md  static site and small HTTP service patterns, local test
      commands.md    dev-side and host-side commands, outputs, release states
      transports.md  pull and push (both available), setup, request format
      approval.md    one-tap Telegram buttons, CLI approval (fallback), checks, notifications
      rules.md       rules for agents, jobs policy (planned), debug access
      faq.md         exact refusal strings from the code and what to do
```

`/healthz` is stamped at build time (`{"ok":true,"sha":"<RELEASE_SHA>"}`), like hello.

### The landing page

One release, walked through a small works drawn in CSS 3D on a tiled ground. The scene stays
pinned while five captions scroll past and drive it: the agent's image leaves the dev bench, the
gate checks it, it waits at a barrier while the release's Telegram message arrives, the owner's
tap presses the button and lifts the barrier, the image enters the idle bay, and the visitors'
road switches over to it. Where the browser has no scroll-driven animations the story loops;
with reduced motion it is a still. The top bar's first sentence points agents at `/llms.md`.

A second walk follows on its own scroll timeline (`--setup`), for `host.md`: an empty plot, an
agent arrives, a beam surveys it and four checks light, two keys drop into a safe (the tokens
the human types), foundations for Docker, Caddy and Tailscale, a fence with an undo lamp that
turns green, then the bays, gate and road. Every piece rests in its finished state, so reduced
motion and browsers without scroll-driven animations show the ready host.

After the walks: three lines to paste into an agent (on a new server, in a project, on a machine), the four
recorded outcomes, what exists today and what is planned, and the docs list. `index.md` is the
same page as Markdown and keeps the longer text (the five ideas, the approval message, the CLI
output); it also states the limit behind "without a gap": long requests, streams and websockets
still on the old slot when the drain ends are cut.

No images, no fonts, no scripts and no `style=` attributes: every box of the scene is an element
placed by a class in `style.css`, so the CSP stays as it is. The scene's state changes that must
happen in order (the button, the barrier, the switch) follow the scroll; only the message's own
arrival and tap play in real time. No cookies, no tracking. Analytics only if the owner wants it
(self-hosted, cookie-free), and then the CSP must be widened on purpose.

### Serving rules (`sws.toml`)

- `*.md` → `text/markdown; charset=utf-8`, `*.txt` → `text/plain; charset=utf-8`,
  `*.html` and `/` → `text/html; charset=utf-8`, `/healthz` → `application/json` + `no-store`.
- Security headers on every response: CSP for a no-JS page (`default-src 'none'; style-src
  'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, hence
  no inline styles or `style=` attributes), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, COOP `same-origin`, a
  `Permissions-Policy`, HSTS (one year, no subdomains, no preload).
- `Cache-Control`: 5 minutes for pages and docs, 1 hour for css/svg (files are not
  fingerprinted), `no-store` for `/healthz`. sws's own `cache-control-headers` is off.
- **How the header rules are written (learned live):** sws 2.33 did not merge several matching
  `[[advanced.headers]]` rules: with a catch-all `"**"` rule plus per-type rules, `/` arrived
  with only some of the security headers. So there is no catch-all any more. There is one rule
  per file type (`/`, `**/*.html`, `**/*.md`, `**/*.txt`, `**/*.{css,svg}`, `/healthz`), and
  each repeats the **full** security header set. Where two rules can match the same path (`/`
  and `/index.html`) they carry identical headers, so it doesn't matter which one wins. **A new
  file type needs its own rule**, or it gets no security headers. After a change, check with
  `curl -sI` on `/`, `/llms.md`, `/llms.txt`, `/docs/start.md`, `/style.css`, `/favicon.svg`,
  `/healthz` and a missing path.

### Known

- **404 under a typed path:** a missing `*.md` or `*.txt` path (e.g. `/docs/nope.md`) returns
  the 404 page with that rule's `Content-Type` (`text/markdown`). sws picks headers by request
  path, not by the file it serves, so this can't be avoided cleanly. `404.html` is therefore
  kept tiny: it reads fine as text and names the full URLs.
- **Cloudflare's browser TTL:** `style.css` arrives with `Cache-Control: max-age=14400`. That is
  Cloudflare's browser cache TTL overriding ours on static assets, not a bug here. Change it in
  the zone settings if it matters.

### Generating `llms.md`, `llms-full.txt` and `llms-full.md`

`llms.txt`, `host.md`, `install.md` and the docs pages are the only hand-edited sources for the agent
files. Everything else is generated, so the copies can't drift. `llms-full` is `llms.txt`, then
`host.md`, then `install.md`, then every docs page in llms.txt order, separated by `---` and a comment naming
the page's URL. From `site/public`:

```sh
cp llms.txt llms.md
{ cat llms.txt
  printf '\n\n---\n\n<!-- https://huta.run/host.md -->\n\n'; cat host.md
  printf '\n\n---\n\n<!-- https://huta.run/install.md -->\n\n'; cat install.md
  for f in start contract runtime moving dockerfile commands transports approval rules faq; do
  printf '\n\n---\n\n<!-- https://huta.run/docs/%s.md -->\n\n' "$f"; cat "docs/$f.md"; done; } > llms-full.txt
cp llms-full.txt llms-full.md
```

Run it after any change to a docs page or `llms.txt`, and commit the results: `huta pack` builds
from `git archive HEAD`, so uncommitted files are not in the image.

## Keeping the docs true

Agents learn huta from this site, so a stale page is a bug. Any change to the commands, the
`app.toml` contract, image requirements, transports, approval or outcomes updates
`site/public/docs/*.md` and `llms.txt`, then regenerates the other `llms*` files, **in the same
commit**, alongside `docs/ARCHITECTURE.md` (the rule is also in the repo's `CLAUDE.md`).
Anything not built yet is marked "planned"; when it ships, remove the label in the same commit.
Error strings in `faq.md` are copied from the code: if a message changes, change it there too.

## How it ships

- Pack from the repo root: `huta pack --app site/deploy/app.toml`, then
  `huta submit <target> --app site/deploy/app.toml` (target `prod`).
- The host registry has `projects.d/huta-site.toml` with `app_toml = "site/deploy/app.toml"`
  (not the usual path), target `prod`, host `huta.run`, `cloudflare = "orange"`,
  `egress = "none"`, small limits (the image is ~14 MB). Later perhaps `www.huta.run` → redirect,
  which needs a redirect feature or a rule in sws.

## Status

Live at `https://huta.run`, deployed by huta from `site/`.
