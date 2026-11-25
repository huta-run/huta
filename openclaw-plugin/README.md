# huta press relay (OpenClaw plugin)

Relays Telegram inline-button presses for huta from an OpenClaw gateway to
huta's local press socket. It is for hosts where OpenClaw already polls the bot
that huta uses to post its approval messages. huta sends those messages straight
through the Bot API, so OpenClaw is the only process that receives the button
presses.

Written against OpenClaw 2026.9.4. Plain ESM, no build step, no dependencies.

## What it does

- It registers an interactive handler for channel `telegram`, namespace `huta`.
  OpenClaw takes the namespace from the text before the first `:` of
  `callback_data`, so every `huta:<anything>` press goes to this plugin. A
  claimed press is never sent to the LLM agent as `callback_data: ...` text.
- On a press it connects to the socket (default `/run/huta/press.sock`) and
  writes one line:
  `{"v":1,"data":"<callback_data>","from_id":<presser id>,"chat_id":<chat id>,"message_id":<message id>}`.
  Then it half-closes and reads one JSON line `{"ok":bool,"toast":"..."}`, up to
  4 KB, with a 10 s timeout.
- It shows the toast text, cut to 200 characters. On any error it shows
  `huta is unavailable; use sudo huta approve on the host`.
- It logs only `huta button pressed` and error reasons, never the
  `callback_data`.

### Limitation: no real toast

OpenClaw answers every callback query itself, with no text, before any plugin
handler runs. With durable ingress it answers as soon as it stores the update.
Telegram accepts only one answer per query, and the plugin API has no way to
set the answer text. So the plugin cannot show a real
`answerCallbackQuery` toast. Instead it posts the toast text as a short message
in the same chat (and topic): `feedback: "reply"` for every press, or `"errors"` (the default) only when huta refused it. Set
`feedback: "none"` to show nothing, for example if huta edits its own message
to show the outcome.

## Requirements on the host

- The gateway runs as its own user. That user must be able to read the plugin
  directory, and connect to the socket: write permission on the socket and
  search permission on its directory. Set this with a group on huta's side.
- Telegram access policy still applies before the plugin is called:
  - `channels.telegram.capabilities.inlineButtons` (per account:
    `channels.telegram.accounts.<id>.capabilities.inlineButtons`) must not be
    `off`. It must also allow the surface where huta posts: `dm`, `group`,
    `all` or `allowlist`. The default is `allowlist`, which covers both DMs
    and groups.
  - In a DM, the presser must pass the DM policy (`dmPolicy` and
    `allowFrom`, or the pairing store). If not, OpenClaw drops the press
    silently. In a group, the group must pass `groupPolicy` and the group
    allowlist.
  - huta must still check `from_id` itself. These checks only decide whether
    the press reaches huta at all.
- OpenClaw does not check who sent the message that holds the buttons, so
  presses on messages that huta sent directly are routed like any other.

## Install

Run everything as the gateway user. The plugin id is `huta-press`.

Option A, the same way as other hand-placed local plugins (explicit load path):

1. Copy this directory to a place the gateway user owns, for example
   `~/.openclaw/extensions-src/huta-press/` in that user's home.
2. In `openclaw.json`:
   ```json5
   {
     plugins: {
       load: { paths: ["<abs path>/huta-press"] },   // append to the existing list
       entries: {
         "huta-press": {
           enabled: true,
           // optional; these are the defaults:
           config: { socketPath: "/run/huta/press.sock", timeoutMs: 10000, feedback: "errors" },
         },
       },
     },
   }
   ```
   If `plugins.allow` is set, add `huta-press` to it.
3. Restart the gateway: `openclaw gateway restart`, or restart its systemd
   user unit. A restart is needed because the gateway discovers plugins only
   at startup.

Option B, a managed install record (CLI):

```bash
openclaw plugins install /abs/path/to/openclaw-plugin --force   # copy; or: -l (link) instead of copying
openclaw plugins enable huta-press                              # if the install left it disabled
openclaw gateway restart                                        # if the gateway did not restart itself
```

`--force` confirms a local, non-ClawHub source when run noninteractively. The
CLI may also ask for capability consent; pass `--accept-capabilities` once you
have reviewed it.

Check:

```bash
openclaw plugins inspect huta-press --runtime --json   # expect one telegram interactive handler, namespace "huta"
```

Then press a test button whose `callback_data` is `huta:test`. The gateway log
shows `huta button pressed`, and the agent does not answer.

## Uninstall

- Option A: remove `huta-press` from `plugins.entries`, `plugins.load.paths`
  (and `plugins.allow`), delete the copied directory, and restart the gateway.
- Option B: `openclaw plugins uninstall huta-press`. Use `--dry-run` first to
  preview, and restart the gateway if it does not restart itself.

Once the plugin is gone, `huta:` presses go back to the agent as
`callback_data: huta:...` text. So turn off huta's Telegram buttons first.

## Fail-open risk and optional hardening

If the plugin is missing, disabled or failed to load, a plain
`huta:<secret>` press is handed to the agent as text. That puts the one-time
secret into the model's context.

OpenClaw 2026.9.4 has an internal "opaque" callback wrapper that fails closed
instead: `tgcb1:<checksum>:<value>`. Its checksum is 32-bit FNV-1a over the
UTF-16 code units of `<value>`, written in base 36, cut to the first 5
characters and left-padded with `0`. OpenClaw unwraps the value before it
matches a plugin handler, so this plugin still receives `huta:<secret>` as
`callback.data`. If no handler claims it, OpenClaw clears the buttons and
replies "This action is no longer available.", and the agent never sees it.
The wrapper adds 12 bytes to the 64-byte `callback_data` limit. It is
undocumented and could change between OpenClaw versions, so test it on each
upgrade before relying on it.
