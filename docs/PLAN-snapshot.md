# Plan: `huta snapshot`

Order among the post-v2 plans: 2 of 4 (monitoring → snapshot → unattended → plain TLS);
independent of monitoring, can run in parallel with it.

Status: **planned**. Nothing here is built. Every key, op and command below is *planned* until
its milestone flips it.

## Goal

A dev box can ask for a copy of a target's production data to debug with. The copy is made by
the app's own **sanitize** command (repo-declared, run in the live image), from a backup copy,
never from the live volume or database. The owner approves each copy by its hash, with one
tap. The copy goes only to the box that asked, and the host deletes its copies on a schedule.

## Non-goals

- huta does not know what personal data is. It never claims a snapshot is anonymous.
- No shell, no ad-hoc queries, no live data, no streaming of changes, no scheduled snapshots.
- No delivery anywhere but the requesting box. No restore of a snapshot into any target.
- No fresh backup on request (first version): the snapshot is as old as the last backup copy.
- The operator's `huta data export` stays as it is.

## Contract (all *planned*)

**`deploy/app.toml`** (at most 5; absent: nothing changes):

```toml
[[snapshot]]
name = "db"
cmd = ["node", "tools/sanitize.mjs"]     # argv, no shell
check = ["node", "tools/sanitize.mjs", "--verify"]   # optional, argv; non-zero refuses
timeout = "10m"                          # at most 30m, covers cmd; check has 5m
component = "web"                        # whose image; default: the public component
```

- Data volume targets: the backup copy is mounted **read-only** at the manifest's data path
  (as `huta diag`); the command writes its result to `/snapshot` (env `HUTA_SNAPSHOT_OUT`).
- Postgres targets: `DATABASE_URL` points at a **scratch server** holding the last dump; the
  command changes that database in place; huta dumps it afterwards.
- Env: the registry's non-secret `env`, `RELEASE_SHA`, `HUTA_PROJECT`, `HUTA_TARGET`,
  `HUTA_SNAPSHOT`. No secrets, no network.
- `[[snapshot]]` is part of `recipe.effective`: adding or changing one shows in the release plan
  on its own line. (The unattended plan must never auto-approve such a change.)

**Host registry** (`projects.d/<p>.toml`, per target; absent: op refused):

```toml
snapshot = { max = "200m", keep = "24h", per_day = 4, scratch = "2g", canaries = ["CANARY_EMAIL"] }
```

`max`: packed size cap (ceiling 2 GiB). `keep`: how long a delivered copy stays on the host
(ceiling 7 d). `scratch`: work space cap. `canaries`: names of secret files, used by nothing
else, whose values must never appear in a snapshot (an operator's own test strings).

**Request ops** (`proto.py`; both transports unless said):

```
{"op": "snapshot", "project", "target", "snapshot": "<name>"[, "reason": "<≤200 printable>"]}
{"op": "snapshot-status", "project", "target", "snapshot_id": "<8 hex>"}   (read-only)
{"op": "snapshot-get", ...same}      (push only: JSON header line, then the bytes)
{"op": "snapshot-rm", ...same}       (delete the host's copy now)
```

**CLI**, box: `huta snapshot <project> <target> <name> [--reason …] [--out file]`,
`huta snapshot status|get|rm <project> <target> <id>`. Host:
`huta snapshot list|show|prepare|export|rm|sweep`, and `huta approve|reject <id>`.

## Design

A new module `snapshot.py`, a `snapshots` table (one migration), a plan kind `snapshot`.
A snapshot never takes the target lock or the phase file: releases and snapshots don't block
each other. It holds `Store.acquire(<p>, "_snapshot-<t>")` and one host-wide lock (one
prepare at a time).

States: `preparing → ready → approved → delivered → deleted`, or `failed`, `rejected`,
`expired` (each then `deleted`). Only `delivered` means data left root's hands.

1. **Request.** Checks: box in `boxes`; target has `snapshot`; the live release declares the
   name; a backup copy exists; no other open snapshot of the target; under `per_day`. Row
   `preparing` (box, transport, release, definition hash, copy timestamp), event, then a
   transient systemd unit runs `huta snapshot prepare <id>`. The answer is the id; the box
   polls `snapshot-status`.
2. **Input.** Data: `cp -al` of `<backup>/<p>/<t>/current` to `.snap-<id>` beside it (hard
   links, as diag). Postgres: a hard link of the last dump.
3. **Scratch.** A sparse ext4 image of `scratch` bytes under `<state>/snapshots/<p>/<t>/<id>/`,
   loop-mounted `nodev,nosuid,noexec`, owned by the project uid. The filesystem is the hard
   cap; `data._check_room` rules apply. Postgres: a throwaway server from the target's pinned
   `[postgres.images]` entry, `--network none`, PGDATA on the scratch image, a random password,
   the dump restored with `--no-owner --no-acl`.
4. **Sanitize.** `docker run` with the slot hardening, `--network none` (Postgres:
   `--network container:<scratch server>`, so loopback only), `--log-driver none`. Output is
   capped at 256 KiB and passes the log gate; the box reads it in `snapshot-status`.
5. **Validate.** All must pass, else `failed` and everything is removed:
   - shape: regular files and directories only (a symlink, hard link, special or odd name
     refuses), entry and depth caps, every file opened `O_NOFOLLOW` and `fstat`ed;
   - the repo's `check`, same isolation, `/snapshot` read-only;
   - **needle scan**: every needle of every live and retired secret of the project, plus the
     canaries, searched in the raw bytes (Postgres: in the plain SQL dump). A hit refuses the
     snapshot; nothing is redacted in place (binary data can't be). The box learns only "a
     secret value was found"; the name goes to the host's events.
   - size: the packed artifact ≤ `max`.
6. **Pack.** huta writes the tar itself (fixed owner, mode and mtime), gzip, to `.artifact.tmp`,
   fsync, rename, sha256. Scratch, server and input links are removed now. In one transaction:
   row `ready` (size, sha256, file count) and a `snapshot` plan bound to that sha256.
7. **Approve.** History message and buttons like other plans, title "📦 DATA SNAPSHOT", button
   "✅ Send to <box>". Lines are huta's own facts: box and transport, release and sha, copy
   age, size, file count, sha256, `keep`, "sanitized by the repo's command; huta did not
   inspect the content". The box's `reason` is shown escaped and labelled as the box's words.
   30-minute expiry. Approval re-checks: registry still allows it, box still in `boxes`,
   artifact hash unchanged.
8. **Deliver.** Pull: copied to `<outbox>/snapshots/<id>/<p>-<t>-<name>.tar.gz` (temp name,
   rename; root-owned, group = the dev user, 0640), then `delivered`. Push: `delivered` at
   once; the box fetches with `snapshot-get`: the host answers one JSON line
   `{…, "stream_bytes": n, "sha256": …}` then exactly `n` bytes. `huta-ssh` relays a stream
   only when the header announces it, up to the 2 GiB ceiling. The client writes 0600, checks
   length and hash, never unpacks.
9. **Delete.** At `keep` after delivery, on `snapshot-rm`, on reject or expiry: files first,
   then `deleted`. `huta snapshot sweep` runs hourly (`huta-snapshot.timer`), in `reconcile`
   and before every snapshot op.

## Security analysis

This is the one feature that moves production data off the host on a box's request.

| Threat | Answer |
|---|---|
| Sanitize touches live data | It only ever sees hard links of a backup copy, read-only, or a scratch server. No target lock, no live mount, no live network. |
| Sanitize output attacks root (links, devices, huge trees, names) | `nodev,nosuid,noexec` scratch, shape check, `O_NOFOLLOW`, huta's own tar writer, caps on size, entries and depth. |
| Disk or CPU exhaustion | Scratch image is the cap; room check; target limits; one prepare per host; `per_day`; timeouts. |
| A box asks for another project's data | Same `boxes` check and same answer as a missing project. Fetch and rm only by the requesting box. |
| The owner approves A, gets B | The plan is bound to the artifact's sha256; nothing runs between approval and delivery. |
| Misleading text in the approval | Only huta's facts; the reason is labelled; sanitize output never reaches the message. |
| Needle scan as a guessing oracle | Refusals don't name the secret; failures count toward `per_day`. |
| Other users on the dev box read the outbox | File is group-readable by the dev user only. All agents sharing that user see it: accepted, said in the docs. |
| A malicious release encodes secrets or raw data into the output | **Not preventable.** The cap is `max` bytes per owner tap. The tap is trust in the live code and in the box. |
| Copies outlive their purpose | Host copies are deleted on schedule and recorded. Copies on the box are beyond huta. |

**What huta promises:** the command ran without network or secrets, on a copy; the exact
bytes approved are the bytes delivered; no known secret value (in the forms `cfg.needles`
derives) or canary appears in the uncompressed output; one box got it; the host's copies are
gone after `keep`; every step is an event (who, what, size, hash; never content).

**What it can't promise:** that personal data was removed (the app's job); secrets inside
compressed, encrypted or re-encoded content; bytes left in SQLite free pages (the docs must say:
`VACUUM INTO` a new file, never delete rows in a copied file); what the box does afterwards.

## Failure matrix

`sweep` is the recovery. It acts on a snapshot only when its lock is free (no live process).

| Step | Recorded first | Crash right after → sweep | Outcome |
|---|---|---|---|
| request accepted | row `preparing` | no lock holder: remove leftovers | failed |
| input links | (fixed names `.snap-<id>`) | remove the links | failed |
| scratch image mounted | (fixed path) | unmount, delete the image | failed |
| scratch server / sanitize / check | (fixed container names) | `docker rm -f` both, then as above | failed |
| artifact renamed into place | — | row still `preparing`: delete it all | failed |
| row `ready` + plan | one transaction | nothing to do; notification is best effort, `huta snapshot list` shows it | waits, then expired |
| approved | plan `approved`, row `approved` | pull: redo the copy to the outbox (same bytes, hash checked); push: mark delivered | delivered |
| outbox copy renamed | — | hash matches: mark delivered | delivered |
| push fetch | event per fetch | nothing; the client sees a short read and fetches again | delivered |
| delete files | — | files of a terminal or overdue row, and directories with no row, are deleted | deleted |
| cleanup can't finish (unmount or unlink fails) | event `snapshot-cleanup-failed` | retried every sweep; alert | needs-human |

A prepare is never retried by itself: the box asks again. `install.sh` already refuses while a
lock is held or a plan is `approved`.

## Milestones

Rules as in `BUILD-v2.md`: the whole test suite passes before every commit, golden files stay
byte-identical, every new durable step is a step in `tests/fakehost.py`, docs change in the
same commit.

| Milestone | What | Status |
|---|---|---|
| S0 | Records: `snapshots` table, states, `sweep`, host `list/show/rm/sweep` | planned |
| S1 | Contract: `[[snapshot]]`, registry `snapshot`, plan line | planned |
| S2 | Prepare for data volumes, validation, host-only `prepare`/`export` | planned |
| S3 | Approval: plan kind, history message, buttons, expiry | planned |
| S4 | Requests and pull delivery, retention, client commands | planned |
| S5 | Push delivery: the answer stream | planned |
| S6 | Postgres targets: scratch server | planned |

- **S0.** Migration; `store.busy` unchanged. Tests: `test_store.py` (migration, state
  transitions refuse wrong order), sweep on an empty and on a littered directory. Gate: v1
  goldens unchanged; install on a host with nothing using it changes nothing.
- **S1.** Lift the by-name refusal in `parse_app_toml` and the registry loader. Tests:
  `test_app_toml.py` (valid, each bad key, `/snapshot` colliding with the data path, absent →
  `effective` identical), `test_v1_golden.py`. Gate: a release that adds `[[snapshot]]` shows
  the line.
- **S2.** Steps 2–6 for data targets. Tests: `tests/test_snapshot.py` (shape refusals: symlink,
  hard link, fifo, name, depth, count; size cap; needle hit in each derived form; canary;
  `check` failure; deterministic tar); crash scenarios in `test_crash.py` style
  (`SnapshotPrepare`, `SnapshotSanitizeFails`, `SnapshotNeedleHit`): die after every step, then
  sweep, sweep again, reboot + sweep; invariants: no container, no mount, no `.snap-*`, no
  scratch, state terminal or `ready` with a matching hash, the backup copy's fingerprint
  unchanged, the target's release state untouched. Sweep itself is crashed after each of its
  steps. Gate: the operator reads an exported artifact from a test target.
- **S3.** Tests: `test_plans.py` (bound to hash, expiry, reject deletes, registry change
  supersedes, a tampered artifact refuses). Gate: one tap and one reject on a test target.
- **S4.** Tests: `test_proto.py` (new ops, v1 requests unchanged), handler tests (wrong box
  answers like a missing project, `per_day`, one open), crash scenarios `SnapshotDeliverPull`
  and `SnapshotDelete`, outbox modes. Gate: a box gets a file; it is gone after `keep`.
- **S5.** `huta-ssh`, `push-request`, client. Tests: a socketpair stream (exact length, short
  read, hash mismatch, wrong box, no stream without the header, ceiling). Gate: fetch twice,
  then `rm`.
- **S6.** Tests: `test_pg.py` additions, crash scenarios `SnapshotPg` and `SnapshotPgRestoreFails`
  (fake scratch server); invariant: the live server and its dumps untouched. Gate: restore the
  delivered dump on a box.

## Docs to update (same commit as the behavior)

- `docs/ARCHITECTURE.md`: contract blocks, "Other reads for boxes", request ops, commands, the
  outbox layout, push stream, crash safety, the "Not yet" list.
- `site/public/docs/`: `contract.md` (`[[snapshot]]`), `commands.md`, `rules.md` (the
  *Planned* paragraph and the promises above), `transports.md` (stream, outbox directory),
  `approval.md` (the new plan), `faq.md` (SQLite `VACUUM INTO`, what "sanitized" means),
  `start.md`; `host.md` (timer, registry key).
- Their copies in `site/public/llms-full.txt` and `llms-full.md`, `llms.txt`/`llms.md`, the
  checklist in `index.html`/`index.md` (`tests/test_site_docs.py` checks the copies).
- `README.md`, `systemd/` (timer unit, `huta-ssh`), `examples/hello` (a sample sanitize).
- This file's status table.

## Open questions for the owner

1. Prepare before approval (planned here: you approve a hash, size and validation result, at
   the cost of CPU per request) or approve first, then prepare?
2. Show the box's `reason` in the approval message, or drop the field?
3. Defaults: `max` 200m, `keep` 24h, `per_day` 4, sanitize timeout ceiling 30m. Right?
4. Canaries: wanted, and are secret files the right place for them?
5. Postgres: a throwaway server per snapshot (planned: no load or roles on the live server) or
   a scratch database on the live one (faster, riskier)?
6. An old backup copy: use it and say its age (planned), refuse beyond some age, or allow a
   `--fresh` that runs a backup first?
7. Non-production targets: a tap every time too, or a registry `approve = "auto"`?
8. Push delivery: a stream through `huta-ssh` (planned; changes the trusted forced command) or
   another channel?
9. Should the snapshot be encrypted to a key of the box while it sits in the outbox?

## How to build this (next session)

1. First: send this plan to codex, grok and agy for review; record their findings in the
   operator's private records, fix the plan, get the owner's answers to the questions above.
2. Then S0 → S6 in order. S5 and S6 don't depend on each other.
3. Rules:
   - run `python3 -I -m unittest discover -s tests -t .` before every commit;
   - write each step's crash scenario before the step;
   - docs in the same commit as the behavior, marked *planned* until it works;
   - small commits, present-tense subject, why in the body;
   - no operator details anywhere (hosts, ids, names, quotes): examples use `example.org`;
   - huta is installed by hand after the owner has seen the diff, one milestone per install;
   - host-specific gate checks go to the operator's records, not here.
