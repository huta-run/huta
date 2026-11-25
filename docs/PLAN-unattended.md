# Plan: unattended releases

Order among the post-v2 plans: 3 of 4 (monitoring → snapshot → unattended → plain TLS); needs
the monitoring watch window first.

Status: **plan, nothing built.** Everything under "Contract" is *planned*. To be reviewed by
independent reviewers before any code (see "How to build this").

## Goal

A target that opts in can go live without the owner's tap when all of this holds: the image was
built by GitHub Actions and huta itself verified where it came from; the plan is code-only; the
target is healthy and not blocked; the rate limit allows it. The owner is told at once, gets a
↩️ button, and a watch window after the release decides whether huta puts the old release back.

Anything else produces the same plan as today and **asks**. Unattended mode never refuses a
release that an owner could approve; it only declines to approve it by itself.

## Non-goals

- No unattended release of an image built on a dev box, ever.
- No unattended migrations, restores, edge, gateway, Postgres or external-publishing plans.
- No other CI systems or signers than GitHub Actions in this plan.
- huta doesn't judge the code. A bad commit merged to `main` ships; the watch window and ↩️
  are the answer to that, not the verifier.
- No automatic resume after a failure: the target asks again until the owner acknowledges it.

## Threat model

What unattended mode removes is the owner's look at the plan. So the checks below must stand
in for it, and what they can't cover is stated.

| Attacker | Can | Cannot |
|---|---|---|
| **A dev box** (any agent on it, its transport key) | Ask for an unattended release of a sha on `main` that is ahead of live; choose when, within the rate limit. Send staged secrets (which make the next plan ask). | Supply the image, its digest or any claim about its origin: the request carries a sha and nothing else. Release a branch, an old sha, or a dev build without approval. Acknowledge a failed watch, switch unattended on, change limits, press ↩️. |
| **A GitHub account that can push to `main`** | Ship code to an opted-in target with no approval. This is the accepted risk and the reason opt-in is per target. A dev box holding such a token is this attacker. | Change what the host grants (secrets, egress, hosts, limits, data): those plans ask. Change the workflow or `deploy/app.toml` unseen: a diff that touches `.github/` or changes the manifest asks. Skip the watch window or the notification. |
| **A GitHub account without push to `main`** (PR author, fork) | Nothing: a PR or fork workflow signs with another ref, trigger or repository id. | — |
| **Compromised CI** (a poisoned action or build dependency inside the pinned workflow) | Produce a validly attested malicious image for a real sha; it ships. Same blast radius as a malicious commit: the container's own secrets, data and egress allowlist. | Leave the container's limits; touch other targets; use a self-hosted runner; make another workflow, tag overwrite or registry account pass as the pinned one. |
| **The image registry** (or its account) | Withhold or swap images. | Get a swapped image accepted: the digest must carry an attestation for this sha. |

The signature chain and the run record (by API) are checked independently, so forging a
release needs both. Opt in only where `main` is protected and actions are pinned by commit;
huta can't enforce the second, the first is an open question.

## Contract (*planned*)

Host, `huta.toml`:

```toml
[unattended]                    # absent = off for the whole host
mode = "shadow"                 # "off" | "shadow" (only says what it would do) | "on"
max_per_hour = 4                # host-wide
verifier = ["/usr/local/bin/gh", "attestation", "verify"]   # pinned binary, see Design
trusted_root = "/etc/huta/sigstore/trusted_root.jsonl"      # for offline verification
```

Registry, `projects.d/<project>.toml`:

```toml
[ci]                                      # where CI-built images of this project come from
registry = "ghcr.io/example-org/myapp"    # image <registry>[/<image>]:<full sha>
workflow = ".github/workflows/release.yml"
repo_id = 123456789                       # numeric: a renamed or re-created repo doesn't match
token = "CI_REGISTRY_TOKEN"               # optional secret name, read-only pulls

[targets.prod]
approve = "code-only"                     # default "always", unchanged
unattended = { max_per_day = 6, min_gap = "15m" }
```

`approve = "code-only"` is refused unless `[ci]`, `[unattended]`, the target's watch window
(monitoring plan) and the notification buttons are all configured. Gateways stay `always`.

Request (`proto.py`, still `v = 1`): `submit` gains optional `built: "ci"`; with it, `image`,
`images` and `digest` are refused. An older host refuses the field by name. Client:
`huta submit <target> --ci [sha]` (no `pack`).

Commands (host): `huta unattended status | off [<project> [<target>]] | on [...]`. A file
`/etc/huta/unattended.off` turns everything off without the database. Boxes can't call these.

Repo side: a workflow at the pinned path, on `push` to `main`, on a GitHub-hosted runner, that
builds `linux/amd64` with the labels `pack` sets today, pushes one plain manifest per image
tagged with the full sha, and attests the pushed digest with GitHub's build-provenance action
(`id-token: write`, `attestations: write`). `examples/` gets a copy.

## Design

**Arrival.** `built: "ci"` → `images.import_from_ci`: resolve `<registry>:<sha>` to a digest
with the registry API (size caps, no redirects off the registry's hosts), verify the digest
(below), pull **by digest** with a throwaway Docker config, then the existing checks (labels,
sha, non-root, platform) and the usual `huta/<project>:…` name. The tag is only a lookup; the
attestation is what binds digest to sha.

**Verification** (`attest.py`, a pure function of bundle + expectations + trusted root, no
network):

1. Fetch the bundles for the digest from GitHub's attestations API with the existing read-only
   client (capped, at most N bundles).
2. Check the Sigstore bundle offline with the pinned verifier binary, run by argument array
   as an unprivileged user with the bundle and trusted root as files. huta is standard-library
   only and has no X.509 or ECDSA code; vendoring one is the alternative (open question).
   The verifier's exact flags are to be confirmed against the pinned version in milestone U1.
3. Don't rely on verifier flags alone: read its structured output and require, exactly: OIDC
   issuer is GitHub Actions; repository id = `repo_id`; signer and build config = the pinned
   workflow path in that repo at `refs/heads/main`; source digest = the full sha; trigger
   `push`; runner GitHub-hosted; SLSA provenance predicate; subject digest = the image digest.
4. Cross-check by API: the run named in the certificate exists in that repo, is for this sha
   on `main`, by `push`, of that workflow, completed with success.

Any miss refuses the submit (as a failed image check does today). The result is stored in the
plan as `provenance` (digest, run id, workflow, verifier version) and `built_by = "ci"`;
`approve` re-checks that the image ids still match it.

**Eligibility.** One function, `unattended_blockers(plan, app, live) -> [reasons]`, an
allow-list on top of the existing class. Empty only when:

- `class` is `code-only` (no change line at all: no first release, config, secrets, egress,
  jobs, smoke, edge, data, staged value, migration, external publishing);
- the parsed `app.toml` equals the live release's, and neither has several components,
  `state.schema` changes, `migrate` or `external`;
- `built_by` is `ci` with a stored provenance, for the live release too after the first;
- history is `ahead` of live (not identical, behind, diverged or unknown), and the compare's
  file list is complete and doesn't touch `.github/`;
- it isn't a rollback; no staged secret exists for the project; no other plan is open;
- live is settled: no phase file, last outcome `live`, `monitor.ready` is true and
  `watch.blocks` returns nothing;
- not switched off, mode `on`, rate limits not reached (counted from the releases table).

The plan text and message say `unattended: yes` or `asks because: <reasons>`. In `shadow`
mode it says `would release unattended` and asks.

**Flow.** submit → gates → verify → plan (the decision is inside the hashed plan) → huta
starts `huta approve <id> --via auto` as a transient unit. `cmd_approve` runs every check it
runs today under the same locks, then re-evaluates the blockers, kill switch and limits; on a
miss the plan simply stays `planned` with ✅/❌ buttons. On success the transition is recorded
as `approved automatically (code-only, CI run <n>)`. It refuses to claim or promote staged
secrets on this path, as an assertion, not only as a blocker. The recipe is unchanged.

**After live.** The monitoring plan's watch opens (it builds the row, the verdict and the ↩️
button; this plan only uses them). The message reads "released automatically" next to the
watch line. A ↩️ press rolls back as that plan describes, and its unacknowledged verdict then
blocks further unattended releases.

**Interface needed from the monitoring plan** (names as in its draft):

- `monitor.ready(platform, t)`: not ready → the plan asks.
- `watch.open(st, rid)` called where the recipe records `live`, and re-opened by the tick from
  the live time, so an automatic release is never live and unwatched for longer than a tick.
- `watch.verdict(st, rid)` → `ok` | `degraded(reasons)` | `unknown`, recorded once, surviving
  a crash or reboot; `unknown` is never `ok`.
- `watch.blocks(st, project, target)`: a running watch, or a `degraded`/`unknown` verdict
  nobody acknowledged (`huta watch <release> --ack`). This is the pause after a failure; this
  plan adds no second one.
- `on_degraded = "rollback"`: the seam this plan fills. Needed from monitoring: the tick calls
  one hook when a watch turns `degraded`, under the target lock, and the watch row has an
  `action` column (a release id) so the hook is idempotent.
- A ↩️ press on any release counts as a block until acknowledged.

**Acting on the verdict.** `ok`: nothing. `degraded` with `on_degraded = "rollback"` (accepted
only with `approve = "code-only"`): for a stateless or concurrent target, plan a rollback to
the previous release, write its id into the watch row, approve it automatically
(`automatic rollback: <reasons>`) and run it through the normal recipe; the bad release ends
`rolled-back`, so it is never a rollback candidate again. If the rollback plan is not
code-only, the target is one-writer, or the rollback doesn't end `live`: `needs-human`, with
the reason and the ↩️ button. `unknown`: stays live, blocked, the owner is told. A rollback's
own watch never rolls back again. A sha that was rolled back is never released unattended.

**Compatibility.** Default `approve = "always"`; targets without opt-in produce byte-identical
plans, compose and vhosts (`tests/golden/`). Dev-built submits are untouched. `huta.db` only
grows (one table for the kill switch). No new phase stage in the recipe.

## Failure matrix

| Step | Recorded first | Crash right after → reconcile | Outcome |
|---|---|---|---|
| pull + verify CI image | nothing | no release; the unnamed image is pruned like any unreferenced import | no release |
| plan with `unattended: yes` | release `planned` | nothing: reconcile never starts a release; buttons are attached, plan expires as usual | planned → superseded |
| automatic approve | `approved`, how | as today: approved but never run | nothing-activated |
| recipe steps | as today | as today | as today |
| live, open watch | `live`; the watch row right after | no row: the monitoring tick opens it from the live time | live, watched |
| watch turns `degraded` | watch row (monitoring) | no action id: the hook runs again at the next tick if that release is still live | as below |
| plan automatic rollback | rollback `planned`, its id in the watch row | planned but never approved: approve it if the verdict is under 10 minutes old, else needs-human | rolled-back / needs-human |
| rollback runs | as any release | as today; not `live` → needs-human, still blocked | live / needs-human |
| ↩️ press | monitoring's step | as the monitoring plan says | — |
| `unattended off/on` | one row or file | atomic; nothing to repair | — |

## Milestones

Rules as in `BUILD-v2.md`: the whole suite passes before every commit, each new durable step is
a step in `tests/fakehost.py`, docs in the same commit, v1 snapshots unchanged.

| | What | Gate |
|---|---|---|
| U0 | This plan reviewed; watch interface agreed with the monitoring plan | findings folded in |
| U1 | `attest.py` and its fixtures; no behavior change | verifier tests |
| U2 | CI-built images arrive (`built: "ci"`, `[ci]`), still always ask | a test target released from CI with a tap |
| U3 | Blockers, plan line, `shadow` mode, config accepts `code-only` | shadow plans read by the owner over real releases |
| U4 | Kill switch, rate limits, `unattended status` | each one turns a `shadow` yes into asks |
| U5 | Automatic approve, watch, automatic rollback, mode `on` | crash tests; a forced bad release rolls itself back |
| U6 | Later: several components, a polling trigger, reusable workflows | each its own plan |

Tests:

- **Verifier (U1), recorded fixtures** in `tests/fixtures/attest/`: a good bundle, and one
  failing case per rule: wrong repo id, fork, another workflow, a `pull_request` trigger, a
  tag or branch ref, another sha, another digest, self-hosted runner, wrong issuer or
  predicate, foreign chain, truncated, oversized, verifier exit 0 with unexpected output.
  Offline, with the verifier faked from recorded output; one optional test runs the real
  binary when present.
- **Arrival (U2):** tag resolves to an unattested digest; registry redirects; labels disagree
  with the sha; `built` together with `image`; an older request shape still valid.
- **Blockers (U3):** one test per reason, including every existing change line, a `.github/`
  diff, a truncated compare, a staged secret sent after planning, a blocked or switched-off target, each limit.
- **Crash (U5):** scenarios `AutoRelease`, `AutoWatchFails` (stateless, concurrent),
  `AutoWatchFailsOneWriter`, `AutoWatchUnknown`, `AutoRollbackFails`, `KillSwitchMidway`, killed after every step and during recovery, with the invariants: never
  live and unwatched past a tick, never unblocked after a failure, never two automatic rollbacks in a row,
  staged values never applied.
- **Golden:** targets with `approve = "always"` unchanged.

## Docs to update

Each in the milestone that changes behavior, marked *planned* until then:

- `docs/ARCHITECTURE.md`: roles (CI as a builder), release steps, contract, commands,
  transports (`built`), crash safety, one-tap approval (↩️), "Not yet".
- `site/public/docs/approval.md` (replace the "only `always`" paragraph; the automatic path,
  the kill switch), `contract.md`, `commands.md`, `transports.md`, `rules.md`, `faq.md`,
  `start.md`; a new page for the CI workflow; `host.md` (verifier, trusted root, kill switch).
- Their copies in `site/public/llms-full.txt` and `llms-full.md`, and the index in `llms.txt`
  and `llms.md` (`tests/test_site_docs.py` checks them).
- `README.md` status, `examples/` workflow, a `BUILD-` status table for U0–U6.

## Open questions for the owner

1. Verifier: a pinned external binary (which one), or vendored verification code? The first
   adds a host dependency to the trusted base; the second adds crypto code to maintain.
2. Private repositories use GitHub's own signing instance, not the public one. Support both
   from the start, or public first?
3. Should huta require a protection rule on `main` (read through the API) before it accepts
   `code-only`, or leave that to the operator?
4. May one-writer targets opt in at all in the first version, given no automatic rollback?
5. The trigger: a box's `submit --ci` only, or also a host timer that follows `main`?
6. Defaults: releases per day, minimum gap, how long ↩️ stays, whether a `.github/` change or
   any `app.toml` change always asks (planned: yes).
7. During a watch window, should a new CI release ask (planned) or wait in a queue?
8. The known limit of button presses (the relay can forge the owner) now also covers ↩️ and
   the absence of a tap. Wait for the receiver only huta controls before mode `on`?

## How to build this

1. Before any code: have this plan reviewed independently (codex, grok, agy), fold the findings
   in, and let the owner decide the questions above. Read the monitoring plan and settle the
   watch interface in both files.
2. Build in order U1 → U5, one milestone per install, each leaving every target working. U1–U4
   need nothing from monitoring; U5 starts only when the watch window is built.
3. Per commit: run `python3 -I -m unittest discover -s tests -t .` first; crash tests for every
   new step; docs (ARCHITECTURE, site pages and their `llms-full` copies) in the same commit;
   small, present-tense subject, why in the body.
4. The repo is public: fixtures come from a public example repo, examples use `example.org`
   and invented ids, and no operator detail appears in files or commit messages. Which
   targets opt in, and their limits, live in the operator's own records.
5. Bundles, registry answers and verifier output are untrusted input: argument arrays, strict
   schemas, size caps, no redirects, deadlines.
6. Mode `on` is set per host by the owner after U5's gate, never by an install.
