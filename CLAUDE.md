# huta: rules for agents

Read `README.md` and `docs/ARCHITECTURE.md` before changing anything.

## This repo is public

It lives at https://github.com/huta-run/huta, and every commit and commit message is public.

- Nothing operator-specific goes here, in files or in commit messages: no real hostnames, IPs,
  ports, uids, chat ids, account names, tokens, incident notes or owner quotes. Examples use
  `example.org` and made-up values. The operator's own records (decisions, notes, reviews, host
  config) live in a separate private repo.
- Secrets never go in the repo, commit messages, logs or output. Refer to them by name.
- Code assumptions about one particular host (the dev container's name, the `caddy` user, the
  Telegram adapter) belong in host config with sensible defaults, not hard-coded.

## Rules

- **huta is the trusted base.** It is never deployed by huta. It's installed on a host by hand
  (`sudo bash install.sh` from the checkout) after the owner has seen the diff, from a commit
  (a dirty tree installs as `<rev> (dirty)`).
- huta runs as root on production. Treat everything from a repo, an image, a submitter or a
  health endpoint as untrusted input: argument arrays only (no shell), strict schemas, no
  redirects, size caps, `O_NOFOLLOW`/`O_EXCL` for files root writes in directories others own.
- Every action that can fail must end in a recorded outcome (live, nothing-activated,
  needs-human). If you add a step, say what `reconcile` does when the process dies during it.
- **Keep the docs true.** Any change to commands, the `app.toml` contract, image requirements,
  transports, approval or outcomes updates `docs/ARCHITECTURE.md` **and** the agent-facing site
  docs (`site/public/llms.txt`, `llms-full.txt`, `site/public/docs/*.md`) in the same commit.
  Agents learn huta from the site, so a stale page is a bug. Mark anything not built yet as
  "planned".
- Write docs with Edit/Write, not through shell quoting.
- Commits: small, present-tense subject, why in the body.
