# Plan: TLS without Cloudflare (plain mode)

Order among the post-v2 plans: 4 of 4 (monitoring → snapshot → unattended → plain TLS); independent of the others.

Status: **planned**. Nothing here is built. To be reviewed as a plan (codex, grok, agy) before
any code; the owner answers the open questions at the end first.

## Goal

A target can be served without the Cloudflare proxy: on a host with no Cloudflare account at
all, or for a site that must not be proxied (uploads over Cloudflare's body limit, long
streams, a zone hosted elsewhere). Such a target gets a publicly trusted certificate by ACME,
and everything else works as today: generated vhost, edge plan and approval, journaled change,
release, public checks, rollback, `reconcile`.

A target with `cloudflare = "orange"` does not change: same vhost bytes, same plan text, same
calls. `tests/golden/vhost/*` stay as they are.

## Non-goals

- huta as an ACME client, wildcard certificates, on-demand TLS.
- Managing DNS at providers other than Cloudflare. The operator creates those records.
- Replacing Cloudflare's DDoS protection, WAF or cache. Plain targets don't have them.
- A switch between modes on a live target with no interruption (procedure below).
- Certificate expiry alerts (they belong to the monitoring plan; this plan gives it the check).

## Contract (*planned*)

```toml
# projects.d/<p>.toml
[targets.prod]
cloudflare = "off"     # "orange" (default, unchanged) | "grey" | "off"
```

- **`"off"`** *(planned)*: huta makes no Cloudflare call for this target. Its hosts need not be
  in `[cloudflare] zones`. The operator creates the DNS records; huta checks them.
- **`"grey"`** *(planned)*: the host is in an allowlisted Cloudflare zone, with a DNS-only
  (unproxied) record. As `"off"`, except that huta reads the record through the API, refuses a
  proxied one, and creates a missing one as DNS-only under the same create-only rule as today.
- Gateways accept the same three values *(planned)*.

```toml
# huta.toml
[platform]
public_ipv4 = "203.0.113.10"
public_ipv6 = "2001:db8::10"   # planned, optional: needed when hosts have AAAA records
[cloudflare]
zones = []                      # planned: may be empty or absent on a host without Cloudflare
```

`CLOUDFLARE_API_TOKEN` is needed only when some target is `orange` or `grey` or publishes to
Cloudflare *(planned)*. Nothing changes in `app.toml`, in the request or in `huta.db`. The repo
cannot choose the mode: it is a registry key.

For apps, the visitor's address stays `CF-Connecting-IP` in every mode (see "Client address").

## Design

### Certificates: Caddy's automatic HTTPS, checked by huta

The plain vhost has **no `tls` line**. Caddy obtains and renews a certificate for each name on
its own, with whatever the host's Caddy global options say (account email, CA, challenge).
huta never talks to a CA and never reads or writes Caddy's storage. It proves the result: a
TLS handshake to the host's own Caddy with the name as SNI, verified against the system trust
store.

Why not certificates managed by huta (an ACME client, pem files in `tls_dir`, as with Origin
CA):

- huta is root code on the standard library only. An ACME client is a new network parser and
  a new account key in root's hands, or a new dependency.
- Renewal would be a scheduled root job with an edge transaction per target every 60 days,
  and CAs are moving to 45-day and 6-day certificates. Each one is a new durable step with
  its own crash recovery. A 15-year Origin CA certificate never needed that.
- Caddy owns ports 80 and 443, so HTTP-01 and TLS-ALPN-01 need its cooperation anyway.
- Caddy already does the whole job (renewal, OCSP, backoff, a retry against the CA's staging
  endpoint before production) and the vhost never changes at renewal, so the journal and the
  release recipe stay untouched.

What we give up: a certificate in hand before the vhost changes, pem files under huta's own
backup, one certificate per target (Caddy issues one per name). The first is covered by the
order of work: `huta edge` installs the 503 "not released yet" vhost first, so issuance
happens there, long before a release. A release only changes the upstream port and never
triggers issuance.

Challenges: huta emits no issuer settings, so Caddy uses HTTP-01 and TLS-ALPN-01 (either
suffices; port 443 alone is enough). DNS-01 is needed only for wildcards or hosts the world
can't reach; both are out of scope. An operator who configures DNS-01 globally in their own
Caddyfile gets it for huta's sites too, with no change here.

Rate limits (Let's Encrypt today: 50 certificates per registered domain a week, 5 for the
same name a week, 5 failed validations per name an hour): huta protects them by checking DNS
*before* the vhost is installed, by never removing and re-adding names without reason, and by
leaving retries to Caddy. Caddy's storage survives vhost changes, so a rollback or a
re-install reuses the certificate.

### The plain vhost

```
# managed-by: huta — project notes, target prod.
# Generated from /etc/huta/projects.d/notes.toml. Do not edit by hand.
notes.example.org, www.notes.example.org {
	encode zstd gzip
	reverse_proxy 127.0.0.1:19110 {
		header_up CF-Connecting-IP {remote_host}
		header_up -CF-IPCountry
		…
	}
	log { … }
}
```

No certificate paths, no `@notcf` filter. `render_vhost` branches on `target.cloudflare`; the
orange branch is the code that exists now, untouched. The `edge` knobs, the access log, the
503 form and the gateway form work the same. The exact header lines are fixed in milestone 1
and checked with `caddy adapt`.

### Client address

Orange apps read `CF-Connecting-IP`, which only Cloudflare can set because other peers are
dropped. On a plain site anyone can send that header, so the vhost **overwrites** it with the
TCP peer and deletes the other Cloudflare request headers. One rule for app authors in every
mode; no forwarded header from the client is trusted. `X-Forwarded-For` is set by Caddy to
the peer (huta sets no `trusted_proxies`). `huta traffic` reads the access log's peer address,
which is now the visitor.

### What replaces the Cloudflare-only filter

Nothing equivalent, and the docs say so. What a plain target has:

- `edge.body` and `edge.timeout` as today. The plan warns when a plain target has no body cap.
- Rate limiting: stock Caddy has no rate-limit handler. `host.md` gets a generic firewall
  example (per-source connection limit on 443). A vhost knob that requires the extra Caddy
  module is an open question, not part of the first milestones.
- The app's own limits. `rules.md` tells agents a plain target faces the internet directly.

### DNS

For `off`, the survey resolves every host (A and AAAA) and requires that every address is
`public_ipv4` or `public_ipv6`. An AAAA record pointing elsewhere is refused by name: CAs
prefer IPv6. If the check fails, the dry run prints the record the operator must create, and
nothing is installed, so no validation attempt is spent. The resolved addresses are plan
inputs: a change before approval makes the plan stale. For `grey`, the same check plus the
record's `proxied` flag from the API.

### Edge plan and apply

Survey (read-only): DNS as above; whether Caddy's automatic HTTPS is usable (`caddy adapt` of
the host config: not disabled, no local CA); certificate state per host (present and trusted,
or missing); vhost; knobs.

Apply: (grey only) create the missing DNS-only record → journaled vhost transaction, as today
→ **wait for certificates**: probe each host for up to about two minutes. On success the plan
is `applied`. On timeout the plan is `failed` with the reason (last lines Caddy logged for the
name, capped); the vhost stays, Caddy keeps retrying, and `huta edge` again only re-checks.
No `ensure_cert`, no strict SSL rule, no `prune_certs`.

`Cloudflare(platform)` is built only when the target needs it; `huta check` prints "no
Cloudflare" for plain hosts; a release of a plain target does not fetch Cloudflare's ranges.

### Releases and public checks

- `current_cert` for a plain target is the handshake probe for every host. A missing or
  untrusted certificate refuses the release at submit and at approval, naming `huta edge`.
- The probe runs again right before the switch; a failure there takes the existing
  abort-before-switch path (`nothing-activated`).
- `public_check` and the public smoke are the same code: `https://<host>` by public DNS,
  verified. They now reach the host's own address directly, which also proves the certificate.
  A host behind NAT needs hairpin routing for this; `host.md` says so.
- A revert replays the stored vhost text as today and needs no certificate.

### Mixed hosts and mode changes

Orange and plain sites live side by side in one Caddy; each vhost is self-contained. A plain
site publishes the host's address, so orange sites on that host lose their hidden origin
(they still drop non-Cloudflare peers).

Changing the mode of a target with a live release is a normal edge plan that says so in
plain words. The operator first changes DNS (the survey refuses until it matches), then
approves; visitors may see certificate errors for the minutes in between. The reverse
(`off` → `orange`) is the same in the other direction.

### Gateways

Same vhost rules. The plan text adds one line: the upstream becomes reachable from the whole
internet through this hostname; the secret header and the upstream's own checks are what
protect it.

## Security considerations

- **Ports 80 and 443 open to the world** was already the firewall rule; until now every huta
  vhost dropped non-Cloudflare peers. Plain sites answer everyone. Port 80 serves only ACME
  and redirects.
- **Exposed origin**: address in DNS, hostnames in Certificate Transparency logs (staging
  names too).
- **Who can cause issuance**: only the registry (hosts) plus an approved edge plan. Never the
  repo, an image or a visitor; on-demand TLS is never emitted.
- **Keys**: private keys and the ACME account key stay in Caddy's storage, owned by the caddy
  user. huta does not copy them. Losing the storage means re-issuance, within the weekly limit.
- **Issuance failures** never take down a serving site: an existing certificate keeps being
  served while renewal fails, and a release is refused before it starts when the certificate
  is bad.
- **Spoofed headers**: see "Client address".

## Failure matrix

No new durable step: huta writes nothing new that the edge journal doesn't already cover.

| Step | Recorded first | Crash right after → recovery | Outcome |
|---|---|---|---|
| DNS and Caddy survey | nothing (read-only) | nothing to undo | none |
| create DNS-only record (grey) | plan `approved` | record exists; `reconcile` fails the plan; the next survey finds the record | failed, plan again |
| vhost transaction | edge journal (`.pending`, `.prev`) | `recover()` restores the previous file and reloads; a certificate Caddy already got stays in its storage and is reused | failed, plan again |
| wait for certificates | plan `approved`, journal clean | vhost installed; `reconcile` fails the plan; the next `huta edge` only re-checks | failed, then applied |
| issuance times out | plan `failed` with the reason | vhost stays (503 form or the old route); Caddy retries on its own schedule | failed; releases refused |
| certificate missing at submit or approval | nothing | refused before any step | nothing changes |
| certificate bad right before the switch | phase (as today) | abort-before-switch, old slot serves | nothing-activated |
| certificate breaks after the switch | phase `checks` | public checks fail → today's path; the message says it is a certificate error, not the release | rolled-back / needs-human |
| renewal keeps failing | none (Caddy's job) | old certificate serves until expiry; `huta edge` dry run and the release check warn once it is overdue | warning |
| Caddy storage lost | none | Caddy re-issues at start; TLS errors until then | operator restores or waits |

## Milestones

Rules as in `BUILD-v2.md`: all tests pass at every commit, orange goldens byte-identical, the
loader keeps refusing each new value until its milestone lifts it.

| # | What | Tests | Gate |
|---|---|---|---|
| P1 | `render_vhost` plain forms (routed, unreleased, knobs) | goldens `vhost/plain-prod*.caddy`; orange goldens unchanged; header lines pass `caddy adapt` | goldens reviewed |
| P2 | Certificate probe, DNS check, automatic-HTTPS check as pure functions | unit tests with a fake resolver and a fake handshake: trusted, untrusted, wrong name, expired, AAAA elsewhere | — |
| P3 | Survey, apply and wait for `off`; lazy Cloudflare client; no token needed; release checks; lift the refusal for app targets | `fakehost.py` gains ACME state (certificate appears after a reload; faults `acme_never`, `dns_wrong`, `acme_lost`); `test_plans` for inputs and staleness; `test_crash` kills edge apply after each step and requires: clean journal, loaded config equals disk, plan has an outcome, a second edge converges; release refused without a certificate | on a test host: throwaway name, edge → release → rollback, public checks pass; an orange target released before and after, vhost unchanged |
| P4 | Gateways with `off` | golden `vhost/gw-plain.caddy`; loader matrix; plan text | a gateway reached without Cloudflare |
| P5 | `grey`: API survey, proxied record refused, DNS-only create | fake Cloudflare records; crash after the create | one grey target on a test zone |
| P6 | Mode change on a live target | plan text; survey refuses until DNS matches; crash tests reuse P3 | one switch each way on a test target |

## Docs to update (same commit as the behavior)

- `docs/ARCHITECTURE.md`: registry `cloudflare` line, `huta.toml` (`public_ipv6`, empty
  zones), "What huta generates" (vhost, Cloudflare), "Crash safety" (edge plan inputs),
  Commands (`huta edge`), "Not yet". `README.md`: the edge sentence.
- `site/public/host.md`: the domains question (Cloudflare optional), firewall comment and the
  rate-limit example, token step marked conditional, `huta.toml`, the edge step, new
  troubleshooting rows (DNS mismatch, issuance timeout, NAT hairpin).
- `site/public/docs/contract.md` (key table, vhost paragraph, edge paragraph, gateways),
  `runtime.md` (visitor's address, checks through Cloudflare), `commands.md` (`huta edge`),
  `moving.md` (adopting an old site), `rules.md` and `faq.md` (a plain target faces the
  internet), `start.md`, `approval.md`, `llms.txt`/`llms.md`.
- Their copies in `site/public/llms-full.txt` and `llms-full.md`; `tests/test_site_docs.py`
  enforces the twins and gets a stale-claim check for "orange only".
- Until a milestone is built, every mention says *planned*.

## Open questions for the owner

1. Two values (`off`, `grey`) or one? Should `grey` create the DNS-only record, or only check?
2. Client address: overwrite `CF-Connecting-IP` (one rule for apps), or strip it and document
   `X-Forwarded-For` for plain targets?
3. Rate limiting: firewall example only, or also an `edge.rate` knob that requires the Caddy
   rate-limit module and is refused without it?
4. A default body cap for plain targets, or only the warning?
5. DNS check through the system resolver (simple; `/etc/hosts` and caches can mislead), or a
   configured public resolver?
6. Issuance timeout on a first edge: keep the 503 vhost so Caddy retries (proposed), or
   remove it?
7. Is a short interruption on a mode change acceptable, or is P6 dropped and a mode change
   done by a new hostname?
8. CA, account email and challenge stay in the host's own Caddy options (proposed), or does
   `huta.toml` set them?

## How to build this (next session)

1. Send this plan to codex, grok and agy for review; fold the findings in; the owner answers
   the open questions. No code before that.
2. Read `CLAUDE.md`, `docs/ARCHITECTURE.md`, `src/huta/edge.py`, `_edge_survey`/`_edge_apply`
   in `cli.py`, `tests/fakehost.py`.
3. Build P1 → P6 in order, one milestone per install. Add a status table for them to this
   file and keep it current.
4. Rules:
   - `python3 -I -m unittest discover -s tests -t .` before every commit.
   - Orange goldens never change. A diff there is a bug in the change, not in the golden.
   - Docs in the same commit as the behavior; *planned* until built.
   - Small commits, present-tense subject, the why in the body.
   - No operator details anywhere: `example.org`, documentation address ranges, made-up
     names. Check the staged diff and the message before each commit.
   - A new step that writes anything durable gets a step in `fakehost.py` and a crash test,
     and a row in the failure matrix above.
   - Code review by codex, grok and agy after P3 and at the end.
