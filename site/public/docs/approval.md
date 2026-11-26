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
- **Watch window** (targets with `watch` in the registry): after a release goes live, the
  host watches it for a while (15 minutes by default) and adds a line to its history message:
  `👀 watching until 14:32`, then `✓ quiet for 15 min`, or `⚠️ <reasons>` as soon as something
  is wrong (new Bugsink issues, a jump in error events, the public health failing three times
  in a row, a container restarting, OOM-killed or stopped), or `❔ watch unknown: <why>` when
  a source didn't answer through the window. A `⚠️` or `❔` verdict also sends a reply to the
  message, which notifies. `sudo huta watch <release>` shows the details.
- **↩️ Roll back** (with buttons turned on): while the watch is open and for 2 hours after it,
  the live release's message also carries `↩️ Roll back` (unless the live release is itself a
  rollback, or there is nothing to go back to). One press by the owner plans a rollback to the
  release before it, fixed when the button was shown, and approves it at once when the target
  keeps no one-writer data or database and the plan is code-only with nothing else to warn
  about; otherwise the plan waits with its own ✅ / ❌ buttons. A press is single-use. When the
  old code can't run any more (the release is no longer live, or its schema moved on), the
  reply says why and points to `huta rollback` / `huta restore`.
- **Fallback** `platform.notify` (an argv that gets a plain-text message on stdin when a plan
  waits), used only when no Telegram history is configured.

A failure to notify never affects a release.
