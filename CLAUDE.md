# huta: rules for agents

Read `README.md` and `docs/ARCHITECTURE.md` before changing anything.

## This repo is public

It lives at https://github.com/huta-run/huta, and every commit and commit message is public.

- Nothing operator-specific goes here, in files or in commit messages: no real hostnames, IPs,
  ports, uids, chat ids, account names, tokens, incident notes or owner quotes. Examples use
  `example.org` and made-up values. The operator's own records (decisions, notes, reviews, host
  config) live in a separate private repo.
- **The operator's settings stay local, never in this repo.** Real `huta.toml`,
  `projects.d/*.toml`, `client.toml`, enroll JSON, secrets, tokens, host firewall or sshd files,
  notes about a particular host: they live on that host (`/etc/huta`, `~/.config/huta`) or in the
  operator's private records. Anything here is a generic example with made-up values. Before every
  commit, check the staged diff and the message for real names, addresses or ids.
- Commits carry the author's GitHub noreply address, not a personal email.
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
- **Run the tests before every commit:** `python3 -I -m unittest discover -s tests -t .`
  (standard library only), or the same tests in parallel: `python3 -I tests/run.py`
  (`-j N` workers, default all cores; `-v` prints each unit's time; a pattern argument keeps
  matching test ids). Serially the suite takes about four and a half minutes, almost all of it
  `tests/test_crash.py`; in parallel about one. `tests/golden/` holds what huta generates today for v1 targets
  (compose, vhost, the plan's `effective`, names). Those snapshots are the compatibility
  contract: a target that uses no new feature must run exactly as before. Rewrite them
  (`HUTA_UPDATE_GOLDEN=1`) only for an intended change, and say why in the commit. Test
  fixtures use made-up values only, like everything else here.
- `tests/test_crash.py` kills a release after every step (a forked child, `os._exit`) on the
  fake host in `tests/fakehost.py`, runs `reconcile` and checks the invariants. A new durable
  step in a release gets recorded there as a step, so it is crashed after too.
- A new phase stage goes into `recipe.KNOWN_STAGES` together with what `reconcile` does with
  it; a stage `reconcile` doesn't know stops it with needs-human (it never clears a record it
  can't resolve). Add a crash scenario for it in `tests/test_crash.py`.
- Site docs: `site/public/llms-full.{txt,md}` contain every page in `site/public/docs/` plus
  `host.md` and `install.md` verbatim, and `llms.txt` equals `llms.md`. Edit a page and its copies
  in the same change (a test checks it).
- Commits: small, present-tense subject, why in the body.
