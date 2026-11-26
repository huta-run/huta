# Plan: `huta snapshot`

Order among the post-v2 plans: 2 of 4 (monitoring → snapshot → unattended → plain TLS);
independent of monitoring.

Status: **planned**. Nothing here is built. Every key, op and command below is *planned* until
its milestone flips it. Reviewed by codex, grok and agy (2026-11-26); their findings are folded
in below.

## Goal

A dev box can ask for a copy of a target's production data to debug with. The copy is made by
the app's own **sanitize** command (repo-declared, run in the image of the release that was live
when the box asked), from a backup copy, never from the live volume or database. The owner
approves each copy by its hash, with one tap. The copy goes only to the box that asked, and the
host deletes its copies on a schedule.

## Non-goals

- huta does not know what personal data is. It never claims a snapshot is anonymous.
- No shell, no ad-hoc queries, no live data, no streaming of changes, no scheduled snapshots.
- No delivery anywhere but the requesting box. No restore of a snapshot into any target.
- No fresh backup on request: the snapshot is as old as the last backup copy (its age is shown,
  and the registry may cap it).
- No encryption of the delivered file (it would not separate agents that share the box's user).
- The operator's `huta data export` stays as it is.

## Contract (all *planned*)

**`deploy/app.toml`** (at most 5; absent: nothing changes):

```toml
[[snapshot]]
name = "db"
cmd = ["node", "tools/sanitize.mjs"]     # argv, no shell
check = ["node", "tools/sanitize.mjs", "--verify"]   # optional, argv; non-zero refuses
timeout = "10m"                          # at most 30m, covers cmd; check has 5m
component = "web"                        # whose image; default: the component that mounts the data
```

- Only for `[state] kind = "data"` or `"postgres"`; refused for `none` and for gateways.
- Data volume targets: the pinned backup copy is mounted **read-only** at the component's data
  path (as `huta diag`); the command writes its result to `/snapshot` (env
  `HUTA_SNAPSHOT_OUT`). `/snapshot` must not overlap the data path (no prefix of either).
- Postgres targets: `DATABASE_URL` points at a **scratch server** restored from the last dump
  (`127.0.0.1`, see Postgres below); the command changes that database in place; huta dumps it
  afterwards as plain SQL (`snapshot.sql`).
- Env: the registry's non-secret `env` of that component, `RELEASE_SHA`, `HUTA_PROJECT`,
  `HUTA_TARGET`, `HUTA_SNAPSHOT`. No secrets, no network. The box's `reason` never reaches it.
  The component's `limits` apply.
- `[[snapshot]]` is part of `recipe.effective` only when non-empty (an absent key leaves every
  golden byte-identical; `plan_release`'s defaults gain `"snapshot": []`). Adding, changing or
  removing one shows in the release plan on its own `📦` line with `cmd`, `check`, `component`
  and `timeout` (the code a later tap trusts), and `notify.render` shows that line in
  Telegram. Such a release is `infra`, so the unattended plan never approves it on its own (a
  test pins that).

**Host registry** (`projects.d/<p>.toml`, per target; absent: op refused):

```toml
snapshot = { max = "200m", keep = "24h", per_day = 4, scratch = "2g", max_age = "7d",
             canaries = ["CANARY_EMAIL"] }
```

`max`: packed size cap (ceiling 2 GiB). `keep`: how long a delivered copy stays on the host
(ceiling 7 d). `per_day`: accepted requests per target and day, failures included. `scratch`:
work space cap (ceiling 16 GiB, at least `max`; Postgres needs several times its dump size).
`max_age`: optional, refuse when the backup copy is older (default: no cap; the approval
message warns past 24 h). `canaries`: names of secret files under the project, used by nothing
else, whose values must never appear in a snapshot. `huta check` refuses a canary name any
target uses (`secrets`, `job_secrets`, `migrate_secrets`, `env`), and the box op `secret` can
never stage one.

**`[transport.pull]`** gains `owner_gid` (the dev user's group as seen on the host), needed for
the outbox permissions.

**Request ops** (`proto.py`; both transports unless said):

```
{"op": "snapshot", "project", "target", "snapshot": "<name>"[, "reason": "<≤200 printable>"]}
{"op": "snapshot-list", "project", "target"}                              (the box's own rows)
{"op": "snapshot-status", "project", "target", "snapshot_id": "<8 hex>"}   (read-only)
{"op": "snapshot-get", ...same}      (push only: JSON header line, then the bytes)
{"op": "snapshot-rm", ...same}       (delete the host's copy now)
```

A repeated `snapshot` for the same box, target and name while one is open answers the open id
(a lost answer doesn't lock the box out). `reason`: one line, no control or bidi characters,
redacted, at most 200.

**CLI**, box: `huta snapshot request <project> <target> <name> [--reason …] [--wait]`,
`huta snapshot list|status|get|rm <project> <target> [<id>] [--out file]` (`get` works on both
transports: pull copies from the outbox, both check length and hash and write 0600 through a
temp file). Host: `huta snapshot list|show|prepare|export|rm|sweep`, and `huta approve|reject
<id>`. Host `export` is the operator's root-only bypass (like `huta data export`), recorded as
its own event.

## Design

A new module `snapshot.py`, a `snapshots` table (one migration), a plan kind `snapshot`.

**One id, one source of truth.** The snapshot id is the plan id: `Store.new_id` checks
`snapshots` too, and `new_plan` accepts the id. The `snapshots` row carries the lifecycle; the
`plans` row is only the approval handle (`planned → approved / rejected / superseded`), and one
`Store` method changes both and writes the event in one `BEGIN IMMEDIATE` transaction.

States: `preparing → ready → approved → delivered → deleted`, or `failed`, `rejected`,
`expired`, `needs-human` (each but the last then `deleted`). `delivered` means *available to
the box* (pull: published in the outbox; push: fetchable), with a fixed deletion deadline
recorded then; for pull, exposure starts at publication even if huta dies before recording it.
Open = `preparing`, `ready`, `approved`; one per target (a partial unique index), checked with
`per_day` in the same transaction as the insert.

**Locks.** A snapshot never takes the target lock for its long steps and never runs `reconcile`.
It holds `Store.acquire(<p>, "_snapshot-<t>")` for every step and decision (prepare, approve,
reject, expiry, deliver, fetch, rm, sweep), and one host-wide `_snapshot` lock while preparing
(order: per-target, then host-wide; never mixed with the target or secrets locks). Backup
publication (`data._publish`, `pg.copy`, `recover_publish`) and pinning share a short lock,
`<backup dir>/<p>/<t>/.publish.lock` (built in S0); the input is pinned under it. `store.busy` stays as it is: every active step holds a lock in
`locks/`, and no snapshot plan stays `approved` (a delivery that can't finish ends `failed` or
`needs-human`). The approval, rejection and expiry paths in `cli` (`_approve_plan`,
`cmd_reject`, `cmd_press`) branch on `kind == "snapshot"` **before** their target lock and
`reconcile`; `recipe._reconcile` skips snapshot plans.

1. **Request.** Checks: box in `boxes` (and `accounts.box_ok` for push); target has `snapshot`;
   the live release declares the name; a backup copy exists and is within `max_age`; no other
   open snapshot of the target; under `per_day`. The row records box, transport, the live
   release id and image ids, the definition hash and the reason. Event, then a transient unit
   with a fixed name `huta-snapshot-<id>` runs `huta snapshot prepare <id>` (timeout: restore
   budget + `timeout` + 5 min + scan budget; `ExecStopPost` removes its fixed containers). A
   failed `systemd-run` fails the row at once. The answer is the id; the box polls.
2. **Input.** Under the publish lock: recover a half-done rotation, then pin the generation:
   `cp -al --no-dereference` of `<backup>/<p>/<t>/current` to `<backup>/<p>/<t>/snaps/<id>`
   (data; never `pins/`, which restore points prune by id), or one hard link of the dump
   (Postgres). The copy's time is the generation directory's mtime, stamped when it is
   published (`data.copy_time`, built in S0), not `last.json`. For Postgres, a file beside the
   dump records the server's image and major version (S5); inside a data generation no file is
   added, since it would be restored onto the volume. The unit re-reads its
   row under `_snapshot-<t>` and exits unless it is still `preparing`.
3. **Scratch.** A sparse ext4 image of `scratch` bytes under `<state>/snapshots/<p>/<t>/<id>/`,
   loop-mounted `nodev,nosuid,noexec` at `<id>/mnt`; `mnt/out` (project uid, 0700) is bind
   mounted as `/snapshot` (so `lost+found` stays out), `mnt/pgdata` for Postgres. A snapshot
   room check counts volume images, scratch images, artifacts and the reserve (peak: scratch
   plus packed file), and the outbox's filesystem before a pull delivery.
4. **Sanitize.** The release recorded in the row: its image (`recipe.check_image`) and argv.
   `docker run` built from `diag.run` (project uid, read-only root, `cap-drop ALL`,
   `--network none`; Postgres: `--network container:<scratch server>`, loopback only),
   `--log-driver none`, fixed container name. Output (256 KiB, log gate) is kept **root-only**
   for `huta snapshot show` and deleted with the snapshot. The box gets only exit status and
   fixed refusal codes (`sanitize-failed`, `check-failed`, `shape`, `too-big`, `secret-found`,
   `timeout`, …): output and file names would be an unapproved data channel.
5. **Validate.** Every container is removed first (`docker rm -f`, verified), then, on the
   frozen tree, all must pass, else `failed` and everything is removed:
   - shape: walk with directory fds (`O_DIRECTORY|O_NOFOLLOW`), every file opened
     `O_NOFOLLOW|O_NONBLOCK` and `fstat`ed; regular files and directories only, `st_nlink == 1`,
     same `st_dev`; names `[A-Za-z0-9._-]` per segment, no leading `-`, depth ≤ 16, ≤ 100 000
     entries, path ≤ 1024; a **logical** byte cap (sum of `st_size` ≤ `scratch`), so a sparse
     file can't make root read terabytes of zeros;
   - the repo's `check`, same isolation, `/snapshot` read-only (Postgres: below);
   - **needle scan** with a dedicated, fresh, project-scoped needle set (not the redaction
     cache): every live and retired secret file of the project (never `.staged` or `.claim`
     files), the canaries (also their raw value, case-insensitive), the scratch server's
     passwords; all forms `cfg.needles` derives. Read in fixed chunks with `max_len - 1`
     overlap, one pass. An unreadable secret or canary fails closed. A hit refuses; nothing is
     redacted in place. The box learns only `secret-found`; the name goes to a host event
     whose release id is none (the box op `log` must not show it);
   - size: the packed artifact ≤ `max`.
6. **Pack.** huta writes the tar itself from the same fds (fixed owner, mode and mtime, no
   extended metadata), gzip (mtime 0), to `.artifact.tmp`, fsync, rename, fsync the directory,
   sha256. Scratch, server and input links are removed now. In one transaction: row `ready`
   (size, sha256, file count, the needle-source fingerprint) and its `snapshot` plan, created
   now (so a long prepare doesn't eat the 30 minutes).
7. **Approve.** History message and buttons like other plans, title "📦 DATA SNAPSHOT", button
   "✅ Send" (the box name goes in the body). Lines are huta's own facts: box and transport,
   release and sha, copy age (⚠️ past 24 h), size, file count, sha256, `keep`, "sanitized by
   the repo's command; huta did not inspect the content". The box's `reason` is html-escaped
   in `render_plan`, prefixed "box says:" so it can't pose as a huta line. 30-minute expiry.
   The plan binds box, transport, target, definition hash, source generation, artifact
   sha256 and size, `keep` and the needle-source fingerprint. Approval re-checks all of them
   (registry still allows it, box still in `boxes` and enrolled, artifact re-hashed from an
   `O_NOFOLLOW` fd of a root-owned 0600 file, needle sources unchanged; otherwise
   superseded).
8. **Deliver.** Pull: copied to `<outbox>/snapshots/<id>/<p>-<t>-<name>.tar.gz` (directories
   `root:<owner_gid>` 0750, file 0640, temp name, fsync, rename, hash re-checked on the
   published fd), then `delivered`, and the host-side artifact is removed (one copy during
   `keep`). Push: `delivered` at once; the box fetches with `snapshot-get`, which re-checks
   access each time: the host answers one JSON line `{…, "stream_bytes": n, "sha256": …}` then
   exactly `n` bytes from the verified fd; after the header a failure ends the stream, never
   JSON. A failed delivery that can't finish ends `failed` (files removed) or `needs-human`.
9. **Delete.** At the recorded deadline, on `snapshot-rm`, reject, expiry or failure: files
   first, then `deleted`. `huta snapshot sweep` runs hourly (`huta-snapshot.timer`), best
   effort for that target only at the end of `reconcile` (never its lock, never a reason for
   needs-human), and before every snapshot op. "Deleted" means host-managed paths removed, not
   erasure: an open descriptor, the box's copy or a filesystem snapshot outlives it. The
   snapshot directories are left out of the host's own backups.

### Push stream (S5)

`systemd/huta-ssh`, `cmd_push_request` and `client._ssh` all assume one buffered JSON answer
(≤ 2 MiB, text mode). Streaming is a separate binary path, only for a successful
`snapshot-get`: `huta-ssh` reads the header line (≤ 64 KiB), checks `ok`, an integer
`0 < stream_bytes ≤ 2 GiB` and the hash, then pipes exactly that many bytes in 64 KiB chunks
and exits non-zero on a short read or extra bytes; every other answer keeps today's 2 MiB JSON
path. `cmd_push_request` lifts its 60 s timeout for the stream, treats a broken pipe as a clean
end, and reserves `stream_bytes` for this op. One stream per box at a time (a flock), and
`MaxConnections` goes up so one box can't hold every slot. The client reads with `Popen`,
writes `.<file>.partial` 0600, hashes as it goes and renames only when length and hash match.
`huta revoke` removes the box's user, which ends its fetches.

### Postgres (S6)

- Never `pg.ensure`, `pg.compose_doc` or `pg._restore_file` (they touch the live server's
  networks and database). A container from the image recorded with the dump (same major
  version; otherwise refuse), `--network none`, no published ports, `--user` the project uid,
  PGDATA on `mnt/pgdata`, tmpfs for the socket, `--shm-size 256m`, the component's limits,
  `listen_addresses=127.0.0.1`, `fsync=off`, `synchronous_commit=off`,
  `full_page_writes=off`, passwords from a 0600 env file.
- Roles as live: `huta_owner`, `migrate`, `app`, with new random passwords (also needles) and
  the schema grants; the `-Fc` dump is piped into `pg_restore` inside the scratch container
  (fail fast, no `--no-owner/--no-acl`, so policies and default privileges restore). An
  extension the image lacks fails the restore.
- Sanitize runs as `migrate` (owns the schema, not a superuser). `check` runs as a read-only
  session (`default_transaction_read_only=on`) against the database. Then connections are
  terminated and huta dumps `pg_dump --format=plain --no-owner --no-acl` as `migrate` to
  `mnt/out/snapshot.sql`, stops and removes the server, and validates and packs that file.
- The promise says: plain SQL is an encoding; bytea and large objects come out escaped (hex),
  which the scan does not decode.

## Security analysis

This is the one feature that moves production data off the host on a box's request.

| Threat | Answer |
|---|---|
| Sanitize touches live data | It only ever sees a pinned backup generation, read-only, or a scratch server restored from a pinned dump. No target lock, no live mount, no live network. |
| Sanitize output attacks root (links, devices, huge or sparse trees, names) | `nodev,nosuid,noexec` scratch, containers gone before validation, fd walk with `O_NOFOLLOW`, `st_nlink`, `st_dev`, name allowlist, logical and entry caps, huta's own tar writer. |
| Disk or CPU exhaustion | Scratch image and the room check (scratch, artifacts, outbox), logical cap, target limits, one prepare per host, `per_day`, deadlines for every stage. |
| Sanitize prints data instead of writing it | Its output stays root-only; the box sees fixed codes. |
| A box asks for another project's data | Same `boxes` check and same answer as a missing project. List, fetch and rm only by the requesting box, re-checked each time. |
| The owner approves A, gets B | The plan binds the artifact's sha256 and everything it was made from; delivery hashes the fd it sends. |
| A forged press (the relay knows the button secrets) | The relay is trusted with the presser's id, as for releases (decided, question 10). A forged press can send only an artifact that is ready, already validated and bound by hash, and only to the box that asked for it. |
| Misleading text in the approval | Only huta's facts; the reason is escaped, prefixed and stripped of control and bidi characters. |
| Needle scan as a guessing oracle | A box can still learn yes/no per request (a release can print a guess). Refusals don't name the secret, count toward `per_day`, and repeated `secret-found` raises an alert. Staged values never enter the needle set, so a box can't choose needles. |
| Other agents on the dev box read the outbox | File is group-readable by the dev user only. Every agent sharing that user (also ones granted other projects) can read it: accepted, said in the docs. |
| A malicious release encodes secrets or raw data into the output | **Not preventable.** The cap is `max` bytes per owner tap. The tap is trust in the live code and in the box. |
| Copies outlive their purpose | Host copies are deleted at a fixed deadline and recorded. Copies on the box are beyond huta. |

**What huta promises:** the command ran without network or secrets, on a copy; the exact
bytes approved are the bytes delivered; no known secret value (in the forms `cfg.needles`
derives) or canary appears in the uncompressed output; one box got it; the host's copies are
removed after `keep`; every step is an event (who, what, size, hash; never content).

**What it can't promise:** that personal data was removed (the app's job); secrets that are
short (under 8 characters), hex, UTF-16, SQL- or bytea-escaped, split or transformed, or inside
compressed, encrypted or re-encoded content; secrets whose retired files were already deleted
(an old copy can hold them); bytes left in SQLite free pages (the docs must say: `VACUUM INTO` a
new file, never delete rows in a copied file); what the box does afterwards.

## Failure matrix

`sweep` is the recovery. It acts on a snapshot only when `_snapshot-<t>` is free; a
`preparing` row is dead only when, in addition, its unit `huta-snapshot-<id>` is neither active
nor activating and the row is older than the unit-start budget. It works when the target or
project is gone from the registry.

| Step | Recorded first | Crash right after → sweep | Outcome |
|---|---|---|---|
| request accepted | row `preparing` | unit never started: as below | failed |
| unit started, lock taken | — | row re-read; not `preparing`: exit | (as recorded) |
| input pinned | (fixed names `snaps/<id>`) | remove the links (strict remover: parent, name, no symlink, `--one-file-system`) | failed |
| scratch image made / attached / mounted | (fixed path) | `docker rm -f` (verified gone), unmount if mounted (retry on EBUSY, never lazy), `losetup -d`, delete the image | failed |
| scratch server / sanitize / check | (fixed container names) | `docker rm -f` all, then as above | failed |
| `.artifact.tmp` / artifact renamed | — | row still `preparing`: delete it all | failed |
| row `ready` + plan | one transaction | nothing to do; notification is best effort, `huta snapshot list` shows it | waits, then expired |
| expiry | — | sweep moves `ready` → `expired` (plan superseded, buttons closed, message told), deletes | deleted |
| approved | plan `approved`, row `approved` (one transaction) | re-check authorization (registry, box, enrollment, hash), then redo the delivery; refused → `rejected`, files removed | delivered |
| outbox `.tmp` / renamed | — | hash of the published file matches: mark delivered; else remove and redo | delivered |
| delivery can't finish (outbox full, copy fails) | event | `failed`, files removed (never left `approved`: `store.busy` would block installs) | failed |
| push fetch | event per fetch | nothing; the client sees a short read and fetches again | delivered |
| delete files | — | files of a terminal or overdue row, and directories with no row, are deleted | deleted |
| cleanup can't finish (unmount or unlink fails) | event `snapshot-cleanup-failed` | image and record kept; retried every sweep; alert | needs-human |
| sweep's own steps | — | each is idempotent; sweep is crashed after each step in the tests | as above |

A prepare is never retried by itself: the box asks again. `install.sh` already refuses while a
lock is held or a plan is `approved`.

## Milestones

Rules as in `BUILD-v2.md`: the whole test suite passes before every commit, golden files stay
byte-identical, every new durable step is a step in `tests/fakehost.py`, docs change in the
same commit.

| Milestone | What | Status |
|---|---|---|
| S0 | Records: `snapshots` table, one id with plans, states, `sweep`, host `list/show/rm/sweep`; the publish lock and the copy time in backups | built |
| S1 | Contract: `[[snapshot]]`, registry `snapshot`, `owner_gid`, the `📦` plan line | planned |
| S2 | Prepare for data volumes, validation, host-only `prepare`/`export` | planned |
| S3 | Approval and pull delivery: plan kind, history message, buttons, expiry, outbox, retention, box ops and client commands | planned |
| S4 | Push delivery: the answer stream | planned |
| S5 | Postgres targets: scratch server | planned |

- **S0.** Migration; `store.busy` unchanged. Tests: `test_store.py` (migration, one id across
  releases, plans and snapshots, transitions refuse wrong order, the partial unique index),
  sweep on an empty and on a littered directory, the publish lock against a pin (a backup
  rotating mid-pin can't tear it). Gate: v1 goldens unchanged; install on a host with nothing
  using it changes nothing; the next backup's `current/` has its publish time.
- **S1.** Tests: `test_app_toml.py` (valid, each bad key, state kind, `/snapshot` overlapping
  the data path, the component default, absent → `effective` identical), `test_v1_golden.py`,
  a release adding or changing `[[snapshot]]` is `infra` with the `📦` line, `huta check` on
  canary names. Gate: a release that adds `[[snapshot]]` shows the line in Telegram.
- **S2.** Steps 2–6 for data targets. Tests: `tests/test_snapshot.py` (shape refusals: symlink,
  symlinked directory, hard link, fifo, device, names with `..`, newline, leading `-`, depth,
  count, sparse file over the logical cap; size cap; needle hit in each derived form and across
  a chunk boundary; canary, also short and missing; staged values not needles; `check`
  failure; output never reaches the box; deterministic tar); crash scenarios
  (`SnapshotPrepare`, `SnapshotSanitizeFails`, `SnapshotNeedleHit`, request vs sweep before the
  unit starts): die after every step, then sweep, sweep again, reboot + sweep; invariants: no
  container, no mount, no loop device, no `snaps/<id>`, no scratch, state terminal or `ready`
  with a matching hash, the backup generation's content digest unchanged, the target's release
  state untouched. Sweep itself is crashed after each of its steps. Gate: the operator reads an
  exported artifact from a test target.
- **S3.** Tests: `test_plans.py` (bound to hash and sources, expiry by sweep, reject deletes,
  registry change, revoked box and new needle sources supersede, a tampered artifact refuses,
  approve/reject never take the target lock or run reconcile, reconcile leaves snapshot plans
  alone), `test_proto.py` (new ops, v1 requests unchanged), handler tests (wrong box answers
  like a missing project, `per_day`, one open under concurrent requests, a repeat returns the
  open id), crash scenarios `SnapshotDeliverPull` and `SnapshotDelete`, outbox modes. Gate:
  one tap delivers a file to a box, one reject deletes; the file is gone after `keep`.
- **S4.** `huta-ssh`, `push-request`, client. Tests: a socketpair stream (exact length, short
  read, extra bytes, hash mismatch, wrong box, no stream without the header, ceiling, slow
  reader, broken pipe, one stream per box). Gate: fetch twice, then `rm`.
- **S5.** Tests: `test_pg.py` additions, crash scenarios `SnapshotPg` and
  `SnapshotPgRestoreFails` (fake scratch server); invariants: the live server, its networks and
  its dumps untouched; roles, read-only check, image from the dump's provenance. Gate: restore
  the delivered dump on a box.

## Docs to update (same commit as the behavior)

- `docs/ARCHITECTURE.md`: contract blocks, "Other reads for boxes", request ops, commands, the
  outbox layout, push stream, crash safety, the "Not yet" list.
- `site/public/docs/`: `contract.md` (`[[snapshot]]`), `commands.md`, `rules.md` (the
  *Planned* paragraph and the promises above), `transports.md` (stream, outbox directory),
  `approval.md` (the new plan), `faq.md` (SQLite `VACUUM INTO`, what "sanitized" means, what
  the scan misses), `start.md`; `host.md` (timer, registry key, `owner_gid`).
- Their copies in `site/public/llms-full.txt` and `llms-full.md`, `llms.txt`/`llms.md`, the
  checklist in `index.html`/`index.md` (`tests/test_site_docs.py` checks the copies).
- `README.md`, `systemd/` (timer unit, `huta-ssh`, `huta-push.socket`), `install.sh`,
  `examples/notes` (a sample sanitize).
- This file's status table.

## Open questions for the owner

The reviewers' answers are in brackets.

1. Prepare before approval (you approve a hash, size and validation result, at the cost of CPU
   per request) or approve first, then prepare? [all three: prepare first]
2. Show the box's `reason` in the approval message, or drop the field? [all three: keep it,
   escaped, prefixed, no control or bidi characters]
3. Defaults: `max` 200m, `keep` 24h, `per_day` 4, sanitize timeout ceiling 30m. Right? [yes;
   plus a `scratch` ceiling, Postgres needs a larger `scratch`]
4. Canaries: wanted, and are secret files the right place for them? [yes, fail closed, never
   used or stageable by any target]
5. Postgres: a throwaway server per snapshot (no load or roles on the live server) or a
   scratch database on the live one? [all three: throwaway server]
6. An old backup copy: use it and say its age, refuse beyond some age, or allow a `--fresh`
   that runs a backup first? [use it, show its age, optional registry `max_age`; no `--fresh`]
7. Non-production targets: a tap every time too, or a registry `approve = "auto"`? [all
   three: a tap every time]
8. Push delivery: a stream through `huta-ssh` (changes the trusted forced command) or another
   channel? [all three: through `huta-ssh`, as a separate binary path]
9. Should the snapshot be encrypted to a key of the box while it sits in the outbox? [all
   three: no]
10. (From the reviews.) The Telegram relay can forge a press, which for a snapshot means
    exporting data. Accept that as for releases, or require `huta approve` on the host for
    snapshots (no button)? [codex: prefer a receiver only huta controls; agy: call it out, and
    consider an opt-in]

## Decided

The owner decided the open questions on 2026-11-26:

1. **Prepare first**; the owner approves the exact artifact.
2. **Keep `reason`**, escaped, prefixed "box says:", one line, no control or bidi characters.
3. **Defaults** `max` 200m, `keep` 24h, `per_day` 4, sanitize ceiling 30m; `scratch` ceiling
   16 GiB and at least `max`.
4. **Canaries** as secret files, never used or stageable by a target, failing closed.
5. **A throwaway Postgres server** per snapshot.
6. **The last backup copy**, its age shown (⚠️ past 24 h), an optional registry `max_age`; no
   `--fresh`.
7. **A tap every time**, on every target.
8. **Push fetch streams through `huta-ssh`**, as a separate binary path.
9. **No encryption** of the delivered file.
10. **A button, as for releases**: snapshot plans get "✅ Send" on their history message. The
    relay's trust limit (it can forge a press) is accepted as for releases and said in the
    docs next to the approval buttons: here a forged press sends one approved artifact to the
    box that asked, nothing else.

## How to build this (next session)

1. Done: reviews by codex, grok and agy (2026-11-26), findings folded in above, the owner's
   answers under "Decided".
2. Then S0 → S5 in order. S4 and S5 don't depend on each other.
3. Rules:
   - run the test suite (`python3 -I tests/run.py`) before every commit;
   - write each step's crash scenario before the step;
   - docs in the same commit as the behavior, marked *planned* until it works;
   - small commits, present-tense subject, why in the body;
   - no operator details anywhere (hosts, ids, names, quotes): examples use `example.org`;
   - huta is installed by hand after the owner has seen the diff, one milestone per install;
   - host-specific gate checks go to the operator's records, not here.
