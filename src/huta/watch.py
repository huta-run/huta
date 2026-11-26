"""Watch windows (docs/PLAN-monitoring.md §6): after each release, huta looks at the target for
a while and records a verdict.

A watch is a row in huta.db, not a process: the monitoring tick opens, evaluates and closes
rows; nothing waits on one and no target lock is held. It belongs to a serving episode,
(release id, live.since): a release that goes live again later is a new episode.

Sources a target may have:
- local: the live slot's containers (restarts, OOM kills, a container that stopped), read with
  docker inspect and the cgroup; always there.
- probe: huta's own probe samples of this episode (monitor.uptime).
- errors: Bugsink, new issues and the event rate (monitor.errors, and the release got the DSN).
- checks: the target's healthchecks uptime check going down (only with healthchecks).

States: watching → degraded (a signal fired; at once) | ok (window over, no signal, every
source answered in each third of the window, nothing already down) | unknown (window over,
something not covered or already down) | closed (the episode stopped being live while
watching). unknown is never ok.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse

from . import monitor as M
from .cfg import Platform, Project, Target, redact

WATCHING, OK, DEGRADED, UNKNOWN, CLOSED = "watching", "ok", "degraded", "unknown", "closed"
VERDICTS = (OK, DEGRADED, UNKNOWN)
ISSUES_CAP, EVENTS_CAP = 20, 500
PROBE_GAP = 150            # seconds: three failed samples in a row, each this close
COVERAGE_KEEP = 200        # answer times kept per source


def window_of(platform: Platform, t: Target) -> int:
    return int(t.watch.get("window") or (platform.monitoring.get("watch") or {}).get("window", 900))


def sources(platform: Platform, project: Project, t: Target, rel) -> list[str]:
    """What this release is watched through. errors comes from what the release itself got,
    as its plan recorded it (effective.monitor.env), or from a DSN the registry sets with
    `crashes` naming its project; not from today's files (codex review of milestone 5, #2)."""
    out = ["local"]
    if "uptime" in t.monitor:
        out.append("probe")
    plan = json.loads(rel["plan"] or "{}")
    got = (((plan.get("effective") or {}).get("monitor") or {}).get("env")) or []
    own = "SENTRY_DSN" in (set(t.env) | set(t.secrets))
    if "errors" in t.monitor and ("SENTRY_DSN" in got or (own and t.crashes)):
        out.append("errors")
    if "uptime" in t.monitor and platform.monitoring.get("healthchecks"):
        out.append("checks")
    return out


def expected_components(app: dict) -> list[str]:
    from . import spec as SP
    return list(app["order"]) if SP.several(app) else [SP.public_name(app)]


def rows(st, project: str, target: str):
    return st.db.execute("SELECT * FROM watches WHERE project=? AND target=? ORDER BY since",
                         (project, target)).fetchall()


def row_of(st, rid: str):
    """The newest watch of a release (its latest episode), or None."""
    return st.db.execute("SELECT * FROM watches WHERE release_id=? ORDER BY since DESC LIMIT 1",
                         (rid,)).fetchone()


# ------------------------------------------------------------------ open / close

def open_watch(platform: Platform, st, project: Project, t: Target, rid: str, since: float,
               now: float | None = None, end: float | None = None) -> bool:
    """Open the watch of an episode. Twice is a no-op. An episode first seen after its window
    ended gets a row in state unknown: it was live and nobody looked."""
    if not t.watch:
        return False
    if st.db.execute("SELECT 1 FROM watches WHERE release_id=? AND since=?",
                     (rid, since)).fetchone():
        return False
    now = time.time() if now is None else now
    rel = st.release(rid)
    app = json.loads(rel["app_toml"])
    window = window_of(platform, t)
    until = since + window
    policy = {**t.watch, "window": window, "sources": sources(platform, project, t, rel),
              "components": expected_components(app)}
    late = now >= until
    state = UNKNOWN if late else WATCHING
    reasons = ["not watched: huta first saw this release after its window"] if late else []
    signals: dict = {}
    if not late:
        # The local baseline is taken with the row, in the same write: a restart between
        # opening and the first look is then still seen.
        try:
            live = st.live(project.name, t.name)
            if live and live["release_id"] == rid:
                base = _local_sample(t, live["slot"], rel["sha"], policy["components"],
                                     time.monotonic() + 15 if end is None else end)
                if base is not None:
                    signals["local_base"] = baseline(base, rel)
        except Exception:  # noqa: BLE001 — the first look takes it instead
            pass
    msg = reasons[0] if late else (f"watching {', '.join(policy['sources'])} until "
                                   f"{time.strftime('%H:%M', time.localtime(until))}")
    with st.db:                       # the row and its event: one write
        st.db.execute("BEGIN IMMEDIATE")
        cur = st.db.execute(
            "INSERT OR IGNORE INTO watches (release_id, since, project, target, opened, until,"
            " state, reasons, coverage, signals, last_tick, policy, notified)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, since, project.name, t.name, now, until, state, json.dumps(reasons), "{}",
             json.dumps(signals), None, json.dumps(policy, sort_keys=True), None))
        if cur.rowcount == 1:
            st.db.execute("INSERT INTO events (ts, release_id, project, target, kind, msg)"
                          " VALUES (?,?,?,?,?,?)", (now, rid, project.name, t.name,
                                                    "watch-unknown" if late else "watch-open",
                                                    redact(msg)))
    return cur.rowcount == 1


def open_live(platform: Platform, st, project: Project, t: Target,
              end: float | None = None) -> bool:
    """The live episode's watch, if the target has one and none exists yet."""
    live = st.live(project.name, t.name)
    if not (t.watch and live):
        return False
    return open_watch(platform, st, project, t, live["release_id"], live["since"], end=end)
    # (on_degraded's record is written with the notified mark in _tell, one transaction)


def close_stale(st, project: str, target: str, why: str = "no longer live") -> int:
    """Close every watching row whose episode isn't the live one. Never raises."""
    try:
        live = st.live(project, target)
        n = 0
        for r in st.db.execute("SELECT * FROM watches WHERE project=? AND target=? AND state=?",
                               (project, target, WATCHING)).fetchall():
            if live and (r["release_id"], r["since"]) == (live["release_id"], live["since"]) \
                    and why == "no longer live":
                continue
            cur = st.db.execute("UPDATE watches SET state=?, reasons=? WHERE release_id=? AND"
                                " since=? AND state=?", (CLOSED, json.dumps([why]), r["release_id"],
                                                         r["since"], WATCHING))
            if cur.rowcount:
                st.event("watch-closed", why, r["release_id"], project, target)
                n += 1
        return n
    except Exception:  # noqa: BLE001 — closing watches never fails the caller
        return 0


# ------------------------------------------------------------------ signals

def _local_sample(t: Target, slot: str, sha: str, expected: list[str], end: float) -> dict | None:
    """{component: {restarts, oomkilled, oom_kill, running}} of the live slot's containers of
    this release; an expected component with no container is {"running": False}. None when the
    answer is incomplete (Docker didn't answer in time, or an OOM counter couldn't be read):
    the tick's local evidence is then missing, never "all fine" (codex review of milestone 5)."""
    from .recipe import compose_name, sh
    from .runstate import cgroup_dir

    def left():
        x = min(10.0, end - time.monotonic())
        if x < 1:
            raise TimeoutError("no time left")
        return x
    try:
        ids = sh(["docker", "ps", "-aq", "--no-trunc",
                  "--filter", f"label=run.huta.project={t.project}",
                  "--filter", f"label=run.huta.target={t.name}",
                  "--filter", f"label=run.huta.slot={slot}"], timeout=left()).split()[:32]
        raw = sh(["docker", "inspect", *ids], timeout=left()) if ids else "[]"
    except Exception:  # noqa: BLE001
        return None
    if len(raw) > 4 << 20:
        return None
    out: dict = {c: {"restarts": 0, "oomkilled": False, "oom_kill": 0, "running": False,
                     "missing": True} for c in expected}
    for c in json.loads(raw):
        labels = (c.get("Config") or {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != compose_name(t) \
                or labels.get("run.huta.sha") != sha:
            continue
        comp = labels.get("run.huta.component", "") or (expected[0] if len(expected) == 1 else "")
        if comp not in out:
            continue
        s = c.get("State") or {}
        oom = 0
        if s.get("Running"):
            d = cgroup_dir(c.get("Id", ""), s.get("Pid"))
            try:
                ev = dict(x.split() for x in (d / "memory.events").read_text().splitlines())
                oom = int(ev["oom_kill"])
            except (OSError, ValueError, KeyError, TypeError):
                return None
        out[comp] = {"restarts": int(c.get("RestartCount") or 0),
                     "oomkilled": bool(s.get("OOMKilled")), "oom_kill": oom,
                     "running": bool(s.get("Running")), "missing": False,
                     "created": _ts(c.get("Created")) or 0.0}
    return out


ZERO = {"restarts": 0, "oomkilled": False, "oom_kill": 0}


def baseline(sample: dict, rel) -> dict:
    """What counts as 'before' for local signals. A container this release made (created after
    the release was planned) starts from zero: any restart or OOM kill of it counts, even one
    that happened while huta wasn't looking. A container from before (a rollback to a slot
    still running) starts from what it shows when the watch opens."""
    return {c: (dict(ZERO) if v.get("created", 0) >= rel["created"] or v.get("missing")
                else {k: v[k] for k in ZERO}) for c, v in sample.items()}


def _local_signals(base: dict, now_: dict) -> list[str]:
    out = []
    for comp, n in now_.items():
        b = base.get(comp) or {"restarts": 0, "oomkilled": False, "oom_kill": 0}
        if not n["running"]:
            out.append(f"{comp} is not running" + (" (no container)" if n.get("missing") else ""))
            continue
        if n["restarts"] > b["restarts"]:
            out.append(f"{comp} restarted {n['restarts'] - b['restarts']} time(s)")
        if n["oomkilled"] and not b["oomkilled"]:
            out.append(f"{comp} was OOM-killed")
        elif n["oom_kill"] > b["oom_kill"]:
            out.append(f"{comp}: the kernel OOM-killed a process in it")
    return out


def _probe_signal(samples: list[dict], rid: str, since: float) -> tuple[list[float], str | None]:
    """(times of this episode's samples, a reason if three failed in a row close together)."""
    mine = [s for s in samples if s.get("release") == rid and s.get("since") == since
            and isinstance(s.get("ts"), (int, float))]
    run: list[dict] = []
    fired = None
    for s in mine:
        if s.get("ok"):
            run = []
            continue
        if run and s["ts"] - run[-1]["ts"] > PROBE_GAP:
            run = []
        run.append(s)
        if len(run) >= 3:
            fired = f"public health failed 3 times in a row ({str(run[-1].get('why', ''))[:120]})"
    return [s["ts"] for s in mine], fired


def _ts(v) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _errors(platform: Platform, pid: int, since: float, window: float, now: float,
            deadline: float) -> tuple[int, int, int, bool]:
    """(new issues since the switch, events in the window before it, events since it,
    complete). Bounded: ISSUES_CAP issues, EVENTS_CAP events."""
    start = since - window
    new = 0
    seen = []
    cursor, done = None, False
    while not done and len(seen) < ISSUES_CAP:
        _s, page = M._bs(platform, "GET", "issues/", deadline,
                         {"project": pid, "sort": "last_seen", "order": "desc",
                          **({"cursor": cursor} if cursor else {})})
        if not isinstance(page, dict) or not isinstance(page.get("results"), list) \
                or "next" not in page:
            raise M.MonitorError("Bugsink answered with no issue list")
        for i in page["results"]:
            last, first = (_ts(i.get("last_seen")), _ts(i.get("first_seen"))) \
                if isinstance(i, dict) else (None, None)
            if last is None or first is None or not isinstance(i.get("id"), str) \
                    or not re.fullmatch(r"[0-9a-f-]{1,64}", i["id"]):
                raise M.MonitorError("Bugsink listed an issue huta can't read")
            if last < start:
                done = True
                break
            if first >= since:
                new += 1
            seen.append(i["id"])
            if len(seen) >= ISSUES_CAP:
                break
        if done or page["next"] is None:
            break
        cursor = M._cursor(page["next"])
        if not cursor:
            raise M.MonitorError("Bugsink's next page link has no cursor huta can read")
    complete = len(seen) < ISSUES_CAP or done
    before = after = total = 0
    for iid in seen:
        if total >= EVENTS_CAP:
            complete = False
            break
        _s, ev = M._bs(platform, "GET", "events/", deadline, {"issue": iid, "order": "desc"})
        if not isinstance(ev, dict) or not isinstance(ev.get("results"), list):
            raise M.MonitorError("Bugsink answered with no event list")
        older = False
        for e in ev["results"]:
            if total >= EVENTS_CAP:
                complete = False
                older = True             # stop here: a lower bound
                break
            ts = _ts(e.get("timestamp")) if isinstance(e, dict) else None
            if ts is None:
                raise M.MonitorError("Bugsink listed an event huta can't read")
            total += 1
            if ts < start:
                older = True
                break
            if ts >= since:
                after += 1
            else:
                before += 1
        if not older and ev.get("next") is not None:
            complete = False           # more of this issue's events than one page: a lower bound
    return new, before, after, complete


def _hc_down(platform: Platform, uuid: str, since: float, deadline: float) -> tuple[bool, bool]:
    """(went down after the switch, was already down at it) for a healthchecks check."""
    hc = platform.monitoring["healthchecks"]
    _s, d = M._call("GET", f"{hc['url']}/api/v3/checks/{uuid}/flips/?"
                    f"{urllib.parse.urlencode({'start': int(since)})}",
                    M._hc_headers(platform), None, deadline, ok=(200,))
    if not isinstance(d, dict) or not isinstance(d.get("flips"), list):
        raise M.MonitorError("healthchecks answered with no flips")
    for f in d["flips"]:
        if not (isinstance(f, dict) and f.get("up") in (0, 1) and not isinstance(f.get("up"), bool)
                and _ts(f.get("timestamp")) is not None):
            raise M.MonitorError("healthchecks listed a flip huta can't read")
    went_down = any(f["up"] == 0 for f in d["flips"])
    _s, c = M._call("GET", f"{hc['url']}/api/v3/checks/{uuid}", M._hc_headers(platform), None,
                    deadline, ok=(200,))
    status = c.get("status") if isinstance(c, dict) else None
    if status not in M.HC_STATES:
        raise M.MonitorError("healthchecks gave no check status huta can read")
    already = status == "down" and not d["flips"]
    return went_down, already


def _covered(times: list[float], since: float, until: float) -> bool:
    w = (until - since) / 3
    return all(any(since + k * w <= x < since + (k + 1) * w + (1 if k == 2 else 0) for x in times)
               for k in range(3))


# ------------------------------------------------------------------ evaluate

def evaluate(platform: Platform, st, project: Project, t: Target, row, end: float,
             now: float | None = None, log=lambda _m: None) -> str | None:
    """One look at a watching row. Returns the new state if it changed, else None."""
    now = time.time() if now is None else now
    rid, since, until = row["release_id"], row["since"], row["until"]
    policy = json.loads(row["policy"] or "{}")
    srcs = policy.get("sources", ["local"])
    signals = json.loads(row["signals"] or "{}")
    coverage = json.loads(row["coverage"] or "{}")
    fired: list[str] = list(signals.get("fired", []))
    new_fired: list[str] = []
    rel = st.release(rid)
    live = st.live(project.name, t.name)
    if not live or (live["release_id"], live["since"]) != (rid, since):
        close_stale(st, project.name, t.name)
        return CLOSED
    snap = _snapshot(platform, st, t)
    if snap is None:
        return None                   # a release or recovery is at work: look next tick

    def fire(kind: str, why: str):
        r = f"{kind}: {redact(why)[:200]}"
        if r not in fired:
            fired.append(r)
            new_fired.append(r)

    def answered(src: str):
        coverage.setdefault(src, []).append(now)
        coverage[src] = coverage[src][-COVERAGE_KEEP:]

    # local
    comps = policy.get("components") or expected_components(json.loads(rel["app_toml"]))
    cur = _local_sample(t, live["slot"], rel["sha"], comps, end)
    if cur is not None:
        if "local_base" not in signals:
            signals["local_base"] = baseline(cur, rel)
        for why in _local_signals(signals["local_base"], cur):
            fire("local", why)
        answered("local")
    # probe
    if "probe" in srcs:
        times, why = _probe_signal(M.samples(platform, t), rid, since)
        coverage["probe"] = times[-COVERAGE_KEEP:]
        if why:
            fire("probe", why)
    # errors
    if "errors" in srcs and time.monotonic() < end - 2:
        mon = st.monitor(project.name, t.name, "errors", M.name(platform, t))
        try:
            pid = int(mon["remote_id"]) if mon else t.crashes
            if not pid:
                raise M.MonitorError("no Bugsink project")
            new, before, after, complete = _errors(platform, pid, since,
                                                   policy["window"], now, end)
            if policy.get("new_issues") and new >= policy["new_issues"]:
                fire("new-issues", f"{new} new Bugsink issue(s) since the release")
            elapsed = max(60.0, now - since)
            rate_after = after / elapsed
            rate_before = max(before, 1) / policy["window"]
            if policy.get("events") and after >= policy["events"] \
                    and rate_after >= policy["factor"] * rate_before:
                fire("event-rate", f"{after} error events since the release "
                                   f"({rate_after * 60:.1f}/min, before {rate_before * 60:.1f}/min)")
            if complete:
                answered("errors")
        except Exception as e:  # noqa: BLE001
            log(f"{project.name}/{t.name} watch errors: {type(e).__name__}")
    # checks
    if "checks" in srcs and time.monotonic() < end - 2:
        mon = st.monitor(project.name, t.name, "uptime", M.name(platform, t))
        try:
            if mon is None:
                raise M.MonitorError("no healthchecks check")
            down, already = _hc_down(platform, mon["remote_id"], since, end)
            if down:
                fire("check", "the healthchecks check went down")
            if already:
                signals["already_down"] = True
            answered("checks")
        except Exception as e:  # noqa: BLE001
            log(f"{project.name}/{t.name} watch checks: {type(e).__name__}")
    signals["fired"] = fired
    if _snapshot(platform, st, t) != snap:
        return None                   # the episode changed while huta looked: drop this look
    state = WATCHING
    reasons: list[str] = []
    if fired:
        state, reasons = DEGRADED, fired
    elif now >= until:
        uncovered = [s for s in srcs if not _covered(coverage.get(s, []), since, until)]
        if uncovered or signals.get("already_down"):
            state = UNKNOWN
            reasons = [f"{s}: no answer in part of the window" for s in uncovered] + (
                ["a check was already down at the release"] if signals.get("already_down") else [])
        else:
            state = OK
    with st.db:
        st.db.execute("BEGIN IMMEDIATE")
        cur = st.db.execute(
            "UPDATE watches SET state=?, reasons=?, signals=?, coverage=?, last_tick=? WHERE"
            " release_id=? AND since=? AND state=? AND EXISTS (SELECT 1 FROM live WHERE"
            " project=? AND target=? AND release_id=? AND since=? AND slot=?) AND NOT EXISTS"
            " (SELECT 1 FROM releases WHERE project=? AND target=? AND state IN"
            " ('approved', 'running'))",
            # In the same write as the verdict: the live episode is the one looked at, and no
            # release of the target was approved since (approval marks it before it runs
            # anything), so a verdict never lands in the middle of a deployment.
            (state, json.dumps(reasons), json.dumps(signals), json.dumps(coverage), now, rid,
             since, WATCHING, project.name, t.name, rid, since, snap[2], project.name, t.name))
        if cur.rowcount != 1:
            return None
        for r in new_fired:
            st.db.execute("INSERT INTO events (ts, release_id, project, target, kind, msg)"
                          " VALUES (?,?,?,?,?,?)", (now, rid, project.name, t.name,
                                                    "watch-signal", redact(r)[:2000]))
        if state != WATCHING:
            st.db.execute("INSERT INTO events (ts, release_id, project, target, kind, msg)"
                          " VALUES (?,?,?,?,?,?)", (now, rid, project.name, t.name,
                                                    f"watch-{state}",
                                                    redact("; ".join(reasons) or
                                                           "quiet for the whole window")[:2000]))
    return state if state != WATCHING else None


def _snapshot(platform: Platform, st, t: Target):
    """What must not change while a watch looks: the live episode, and no release or recovery
    at work (a phase file). None when one is at work."""
    from .recipe import _phase_file
    live = st.live(t.project, t.name)
    if not live or _phase_file(platform, t).exists():
        return None
    return (live["release_id"], live["since"], live["slot"])


def on_degraded(platform: Platform, st, row) -> None:
    """The seam for automatic rollback (plan 3). Here it only records that the owner was told."""
    st.db.execute("UPDATE watches SET action=? WHERE release_id=? AND since=? AND action IS NULL",
                  ("notified", row["release_id"], row["since"]))


# ------------------------------------------------------------------ text

def line(row) -> str:
    """The watch's line for the release's history message and status."""
    if row is None:
        return ""
    st_ = row["state"]
    reasons = json.loads(row["reasons"] or "[]")
    if st_ == WATCHING:
        return f"👀 watching until {time.strftime('%H:%M', time.localtime(row['until']))}"
    if st_ == OK:
        return f"✓ quiet for {round((row['until'] - row['since']) / 60)} min"
    if st_ == DEGRADED:
        return "⚠️ " + "; ".join(reasons)[:400]
    if st_ == UNKNOWN:
        return "❔ watch unknown: " + "; ".join(reasons)[:400]
    return ""


def status_line(st, project: str, target: str) -> str:
    live = st.live(project, target)
    if not live:
        return ""
    r = st.db.execute("SELECT * FROM watches WHERE release_id=? AND since=?",
                      (live["release_id"], live["since"])).fetchone()
    return f"watch: {line(r)}" if r is not None else ""


def text(st, rid: str) -> str:
    out = []
    for r in st.db.execute("SELECT * FROM watches WHERE release_id=? ORDER BY since", (rid,)):
        pol = json.loads(r["policy"] or "{}")
        out.append(f"{r['release_id']} {r['project']}/{r['target']} episode from "
                   f"{time.strftime('%m-%d %H:%M', time.localtime(r['since']))} until "
                   f"{time.strftime('%H:%M', time.localtime(r['until']))}: {r['state']}")
        out.append(f"  sources: {', '.join(pol.get('sources', []))}")
        for x in json.loads(r["reasons"] or "[]"):
            out.append(f"  {x}")
        cov = json.loads(r["coverage"] or "{}")
        if cov:
            out.append("  answers: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(cov.items())))
    return "\n".join(out) or f"no watch for {rid}"


# ------------------------------------------------------------------ the tick's part

NOTIFY_FOR = 24 * 3600     # seconds a verdict's message is retried after the verdict
TELL_BATCH = 10            # verdict messages per tick
TELL_RESERVE = 8           # seconds of the tick kept for telling


def tick(platform: Platform, registry: dict, st, end: float, log=lambda _m: None) -> None:
    """Tell what is pending, then open, evaluate and close for every target with a watch, then
    tell what changed. Bounded by `end`; time is kept for telling."""
    # Leftovers from earlier ticks get a small share first, so failing deliveries can't eat the
    # time evaluation needs (codex, third look at milestone 5); the rest is told at the end.
    _tell(platform, st, min(end, time.monotonic() + TELL_RESERVE), log)
    work_end = end - TELL_RESERVE
    for project in registry.values():
        for t in project.targets.values():
            if not t.watch or t.kind == "gateway":
                continue
            if time.monotonic() > work_end - 3:
                break
            try:
                close_stale(st, project.name, t.name)
                if _snapshot(platform, st, t) is None:
                    continue                       # nothing live, or a release is at work
                open_live(platform, st, project, t, end=work_end)
                for row in st.db.execute("SELECT * FROM watches WHERE project=? AND target=? AND"
                                         " state=?", (project.name, t.name, WATCHING)).fetchall():
                    if time.monotonic() > work_end - 3:
                        break
                    new = evaluate(platform, st, project, t, row, work_end, log=log)
                    if new:
                        log(f"{project.name}/{t.name} watch {row['release_id']}: {new}")
            except Exception as e:  # noqa: BLE001 — one target never stops the others
                log(f"{project.name}/{t.name} watch: {type(e).__name__}")
    _tell(platform, st, end, log)


def _tell(platform: Platform, st, end: float, log) -> None:
    """Every state reaches the history message; degraded and unknown also send a reply, so the
    owner is notified. A row is marked told only when every send it needs worked (a missing
    history message is sent first). Retried for NOTIFY_FOR after the verdict, then recorded as
    given up. At most TELL_BATCH a call, within `end`."""
    from . import notify as N
    now = time.time()
    # In random order, so rows whose delivery keeps failing can't hold back newer ones.
    rows = st.db.execute("SELECT * FROM watches WHERE state IN (?,?,?,?) AND"
                         " (notified IS NULL OR notified != state) ORDER BY RANDOM() LIMIT ?",
                         (*VERDICTS, WATCHING, TELL_BATCH)).fetchall()
    for r in rows:
        left = end - time.monotonic()
        if left < 3:
            return
        per = min(10.0, (left - 1) / 2)         # two sends at most for this row
        try:
            told_at = r["last_tick"] or r["opened"]
            if r["state"] != WATCHING and now - told_at > NOTIFY_FOR:
                with st.db:
                    st.db.execute("BEGIN IMMEDIATE")
                    st.db.execute("UPDATE watches SET notified=? WHERE release_id=? AND since=?",
                                  (r["state"], r["release_id"], r["since"]))
                    st.db.execute("INSERT INTO events (ts, release_id, project, target, kind, msg)"
                                  " VALUES (?,?,?,?,?,?)",
                                  (now, r["release_id"], r["project"], r["target"],
                                   "notify-failed", f"the {r['state']} verdict was never delivered"
                                                    " to Telegram; gave up after 24 h"))
                continue
            if not N.history(platform, st, r["release_id"], create=True, timeout=per):
                continue                       # not delivered: next tick
            if r["state"] in (DEGRADED, UNKNOWN):
                if not N.reply(platform, st, r["release_id"], N.watch_reply(st, r), timeout=per):
                    continue
            with st.db:                        # told, and (degraded) the hook's record: one write
                st.db.execute("BEGIN IMMEDIATE")
                st.db.execute("UPDATE watches SET notified=? WHERE release_id=? AND since=?",
                              (r["state"], r["release_id"], r["since"]))
                if r["state"] == DEGRADED:
                    st.db.execute("UPDATE watches SET action=? WHERE release_id=? AND since=? AND"
                                  " action IS NULL", ("notified", r["release_id"], r["since"]))
        except Exception as e:  # noqa: BLE001
            log(f"watch notify: {type(e).__name__}")
