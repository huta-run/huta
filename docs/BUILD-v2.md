# Building v2

The order and the gates for `docs/DESIGN-v2.md`. Each milestone is one install on a host; each
leaves every target working. A milestone is done when its gate passes; the next starts after.

Every milestone card answers the same questions: what it lets the repo or registry ask for,
what changes in the contract, which new durable steps it adds (and what `reconcile` does after
a crash at each, with the crash test that proves it), which docs change, and what to run on a
test target after installing. Host-specific checks (which targets, which commands) live in the
operator's own records, not here.

Rules for every milestone:

- `python3 -I -m unittest discover -s tests -t .` passes, including `tests/golden/` (v1 output
  unchanged) and `tests/test_crash.py` (every new step is crashed after).
- A new durable step is recorded as a step in `tests/fakehost.py`, so the crash tests cover it.
- Docs change in the same commit as the behavior: `ARCHITECTURE.md`, the site docs, and this
  file's status line.
- Small commits; each passes the tests on its own.

| Milestone | What | Status |
|---|---|---|
| T | Test harness: v1 snapshots, fake host, crash tests | built |
| T+ | Recovery fixes the crash tests found (phase left, retire race, revert needs cert) | built |
| 0a | Database version, refuse a newer one; install guard | built |
| 0b | One release description for every manifest; `order[0]` sites read it | built |
| 0c | Records v2, resources, restore points, plans table, phase v2, edge plans | built |
| 1 | Gateways, `edge` knobs on every vhost | built |
| 2 | Migrations, exclusive mode; `health_timeout`; restore plans | in progress: migrations built; rollback guard, restore plans, plan buttons next |
| 3 | Several containers per target; images map | planned |
| 4 | Postgres per target | planned |
| 5 | External publishing (Pages, Workers) | planned |
| 6 | Migrations, online mode | planned |

## T: test harness (built)

- `tests/golden/`: compose, vhost, effective and names for made-up v1 targets.
- `tests/test_app_toml.py`, `tests/test_proto.py`: v1 manifests and requests stay valid; each
  planned key is refused by name until its milestone.
- `tests/fakehost.py`, `tests/test_crash.py`: release and recovery on a fake host, killed after
  every step (and recovery killed after each of its own); invariants after recovery, after a
  second recovery and after a reboot.

## 0a: database version and install guard (built)

- `store.py`: `PRAGMA user_version`; migration 1 is today's schema. A newer version: refuse.
- `install.sh`: refuse while a phase file, a running/approved release or a held lock exists.
- Gate: v1 snapshots unchanged; one normal release and one rollback on a test target.

## 0b: one release description (built)

- `spec.py`: the public component of any manifest (marked `public`, else the only HTTP one,
  else the first in `order`), its health path and port. Every place that read
  `app["components"][app["order"][0]]` asks it. `concurrent_safe` checks every component that
  mounts data.
- Per-slot container sets (`slot_containers` by component) come with milestone 3, which is
  the first to have more than one container per slot.
- Gate: every golden file byte-identical.

## 0c: records, plans, phase v2 (built)

- Migration 2: release `images` and `schema`; `resources`, `restore_points`, `plans` tables.
  New releases record their image map.
- Phase records: unreadable, newer, or an unknown stage → reconcile stops with needs-human
  and keeps the record. Each later milestone adds its stages with their recovery.
- `huta edge --apply` makes an edge plan bound to what it surveyed; `huta approve <id>` applies
  it or supersedes it if the edge changed; `huta reject`/`show` take plan ids; `--now` applies
  at once and is recorded.
- The Telegram approval unit's limit comes from the release (health wait, migration, drain).
- Telegram buttons for non-release plans: with milestone 2 (restore plans need them).

## 1–6

Each milestone lifts exactly its own refusal in `parse_app_toml` / the registry loader, adds
its crash scenarios to `tests/test_crash.py`, and documents itself (planned → built).
