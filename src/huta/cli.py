"""huta command line (root on the host). Approve with `huta approve`, or with one tap in the chat when [telegram] buttons are on.

  huta check                                   validate the whole host registry
  huta edge <project> <target> [--apply|--now] certificate, vhost, Cloudflare per-host rule
                                               (--apply plans it for approval; --now applies)
  huta import-dev <project> <dev-image-ref>    v0 transport: image built on dev → local docker
  huta submit <project> <target> <sha> <image-id-or-tag>   verify and plan
  huta show <id>                               print a plan (release or edge)
  huta approve <id> | reject <id>
  huta rollback <project> <target>             plan a release of the previous image
  huta reconcile <project> <target>            repair state after an interruption
  huta status [project [target]] | log [release]   with a target: its containers now

  huta secret list|set|import|rm|discard <project> [NAME…] [--from .env]   secrets (values never shown)
  huta data status|ensure|import|export <project> <target> [dir | path…]
  huta backup <project> <target>               consistent copy + ship (timer runs it too)
  huta job list|run <project> <target> [name]  scheduled jobs of the live release
  huta logs <project> <target> [--since 1h --grep … --slot live --job …]   redacted logs
  huta logs-collect                            store new slot output (huta-logs.timer runs it)
  huta egress-log|traffic|crashes <project> <target> [--since …]   refusals, edge summary, Bugsink
  huta diag <project> <target> <name>          run a [[diag]] of the live release (no network, no secrets)
  huta units <project> <target>                rewrite the job and backup timers
  huta egress sync                             refresh the egress daemon's policy
  huta stop|start|restart <project> <target>   the live slot (restart: picks up new secrets)
  huta snapshot list|show|rm|sweep [id | project [target]]   data snapshots for dev boxes (records)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import edge, images, recipe
from . import runtime as R
from .data import DataError
from . import store as S
from .cfg import (ConfigError, load_platform, load_registry, parse_app_toml, read_secret, redact,
                  redact_obj,
                  zone_for, NAME_RE)
from .ext import Cloudflare, ExtError, GitHub
from . import spec as SP

SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# Errors huta raises on purpose to turn something down: "refused", not "huta broke".
REFUSALS = (ConfigError, ExtError, recipe.RecipeError, edge.EdgeError, RuntimeError, KeyError,
            OSError, DataError, S.StoreError)
ETC_REGISTRY_HTPASSWD = Path("/etc/huta/registry/htpasswd")
PLAN_TTL = 30 * 60


def say(msg: str) -> None:
    print(redact(msg), flush=True)


def ctx(project_name: str | None = None):
    platform = load_platform()
    registry = load_registry(platform=platform)
    st = S.Store(platform.state_dir)
    if project_name is None:
        return platform, registry, st
    if project_name not in registry:
        raise ConfigError(f"no project {project_name!r} in /etc/huta/projects.d")
    return platform, registry[project_name], st


def target_of(project, name, gateway_ok: bool = False):
    if name not in project.targets:
        raise ConfigError(f"{project.name} has no target {name!r} (targets: {', '.join(project.targets)})")
    t = project.targets[name]
    if t.kind == "gateway" and not gateway_ok:
        raise ConfigError(f"{project.name}/{name} is a gateway: nothing runs there to release, "
                          "start or read (huta edge and huta traffic work)")
    return t


def current_cert(platform, t):
    have = edge.cert_ok(platform, t)
    if not have:
        raise ConfigError(f"no valid certificate for {t.hosts[0]}; run `huta edge {t.project} {t.name} --apply`")
    return have


# ---------------------------------------------------------------- commands

def cmd_check(a):
    platform, registry, _ = ctx()
    for project in registry.values():
        say(f"project {project.name}: repo {project.repo}, app.toml {project.app_toml}, uid {project.uid},"
            f" boxes {', '.join(project.boxes) or 'none (host CLI only)'}")
        for t in project.targets.values():
            zones = ", ".join(f"{h} → {zone_for(h, platform)}" for h in t.hosts)
            if t.kind == "gateway":
                say(f"  gateway {t.name}: {zones} → {t.gateway['to']}")
                continue
            say(f"  target {t.name}: {zones}; slots {t.slots[0]}/{t.slots[1]}; egress {t.egress};"
                f" approve {t.approve}; require_ci {t.require_ci}")
    say("registry ok (no shared ports or hosts)")
    return 0


def _edge_survey(platform, project, t, cf) -> dict:
    """What `huta edge` would change for a target, read-only. Its "inputs" bind an edge plan:
    if they differ at approval, the plan is stale."""
    missing, lines = [], []
    for h in t.hosts:
        dns = cf.dns_covers(h)
        lines.append(f"{h}: zone {zone_for(h, platform)} (ssl {cf.zone_ssl(h)}), dns {dns or 'missing'}")
        if not dns:
            missing.append(h)
    if missing and not platform.public_ipv4:
        raise ExtError(f"{', '.join(missing)}: no DNS record, and no platform.public_ipv4 to create one")
    if missing:
        lines.append(f"would create proxied A record(s) → {platform.public_ipv4}: {', '.join(missing)}")
    have = edge.cert_ok(platform, t)
    lines.append(f"certificate: {'ok ' + have[0].name if have else 'missing or not covering all hosts (a new one is made)'}")
    inputs = {"hosts": list(t.hosts), "cloudflare": t.cloudflare, "access_log": t.access_log,
              "missing_dns": missing, "cert": have[0].name if have else None,
              "vhost": edge.installed(platform, t) or "", "edge": t.edge}
    if t.kind == "gateway":
        g = dict(t.gateway)
        lines.append(f"gateway → {g['to']}" + (f", header {g['header']} from secret {g['secret']}"
                                               if g.get("secret") else ""))
        if g.get("secret"):
            # The value never appears; its fingerprint binds the plan to it.
            g["secret_sha256"] = hashlib.sha256(
                R.secret_value(platform, project.name, g["secret"]).encode()).hexdigest()
        inputs["gateway"] = g
    if t.edge:
        lines.append("edge: " + ", ".join(f"{k} {v}" for k, v in sorted(t.edge.items())))
    return {"lines": lines, "missing": missing, "inputs": inputs}


def _edge_apply(platform, project, st, t, cf, missing, how: str) -> None:
    """Make the edge changes. Hold the target lock."""
    for h in missing:
        say(f"{h}: {cf.create_proxied_a(h, platform.public_ipv4)}")
    msg, cert = edge.ensure_cert(platform, cf, t)
    say(msg)
    if t.kind == "gateway" and t.gateway.get("secret"):
        if edge.write_gateway_secret(platform, t, R.secret_value(platform, project.name,
                                                                 t.gateway["secret"])):
            say(f"gateway header file written ({edge.gateway_secret_path(platform, t)})")
    live = st.live(project.name, t.name)
    # Keep whatever the vhost routes to now; never re-point traffic from the database
    # (grok #9). A first edge has no route yet: the 503 form.
    port = edge.routed_port(platform, t) if edge.installed(platform, t) else None
    if port is None and live:
        port = dict(zip(recipe.SLOTS, t.slots))[live["slot"]]
    with edge.edge_lock(platform):
        for m in edge.recover(platform):
            say(m)
        foreign = edge.adopt_pending(platform, t)
        if foreign is not None and not edge.installed(platform, t):
            say(f"adopt: {foreign} keeps serving; the first release replaces it")
        else:
            say(edge.change_vhost(platform, t, edge.render_vhost(
                platform, t, Cloudflare.ip_ranges(), cert, port))[0])
    edge.prune_certs(platform, t, cert)
    for h in t.hosts:
        if edge.adopt_pending(platform, t) is not None:
            say(f"{h}: SSL mode left as it is while the old site serves; the first release "
                "sets the per-host strict rule")
        elif cf.zone_ssl(h) != "strict":
            say(f"{h}: {cf.ensure_host_strict(h)}")
    st.event("edge", f"edge applied for {', '.join(t.hosts)} ({how})", None, project.name, t.name)


def cmd_edge(a):
    """Dry run by default. --apply makes an edge plan the owner approves (huta approve <id>);
    --now applies at once, for an operator at the console, and is recorded (DESIGN-v2 §0)."""
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target, gateway_ok=True)
    cf = Cloudflare(platform)
    survey = _edge_survey(platform, project, t, cf)
    for line in survey["lines"]:
        say(line)
    if a.now:
        with st.lock(project.name, t.name):
            _edge_apply(platform, project, st, t, cf, survey["missing"],
                        "--now, at the console without a plan")
        return 0
    if not a.apply:
        say("dry run; --apply plans these changes for approval (--now applies them at once)")
        return 0
    pid = st.new_plan(project.name, t.name, "edge", {
        "lines": survey["lines"], "inputs": survey["inputs"]})
    tell(platform, st, pid)
    ask(platform, st, pid)
    say(f"\nedge plan {pid}; approve with: huta approve {pid}")
    return 0


def _approve_plan(platform, registry, st, pid: str, via: str, confirmed: bool = False) -> int:
    """Approve and apply a plan that is not a release (DESIGN-v2 §0: infra and restore)."""
    r = st.plan_row(pid)
    project = registry[r["project"]]
    t = target_of(project, r["target"], gateway_ok=True)
    with st.lock(project.name, t.name, wait=300):
        r = st.plan_row(pid)
        if r["state"] != S.PLANNED:
            raise ConfigError(f"plan {pid} is {r['state']}, not planned")
        plan = json.loads(r["plan"])
        if time.time() - r["created"] > PLAN_TTL:
            st.plan_transition(pid, S.PLANNED, S.SUPERSEDED, "approval expired (30 min)")
            retire_buttons(platform, st, pid)
            tell(platform, st, pid)
            raise ConfigError("plan expired; make it again")
        # An interrupted release is finished first: a plan must never overwrite its phase.
        recipe.reconcile(platform, st, project, t, say)
        if r["kind"] == "restore":
            return _approve_restore(platform, st, project, t, pid, plan, via, confirmed)
        if r["kind"] == "postgres":
            return _approve_pg_upgrade(platform, st, project, t, pid, plan, via)
        if r["kind"] == "external":
            return _approve_external_rollback(platform, st, project, t, pid, plan, via)
        if r["kind"] != "edge":
            raise ConfigError(f"plan {pid}: kind {r['kind']!r} is not handled by this huta")
        cf = Cloudflare(platform)
        survey = _edge_survey(platform, project, t, cf)
        if survey["inputs"] != plan["inputs"]:
            st.plan_transition(pid, S.PLANNED, S.SUPERSEDED, "the edge changed since planning")
            retire_buttons(platform, st, pid)
            tell(platform, st, pid)
            raise ConfigError("the edge or its config changed since this plan was made; plan again")
        st.plan_transition(pid, S.PLANNED, S.APPROVED, f"approved {via}")
        retire_buttons(platform, st, pid)
        tell(platform, st, pid)
        try:
            _edge_apply(platform, project, st, t, cf, survey["missing"], f"plan {pid}")
        except Exception as e:  # noqa: BLE001 — every failure ends in a recorded outcome
            st.plan_transition(pid, S.APPROVED, "failed", f"{type(e).__name__}: {e}")
            tell(platform, st, pid)
            raise
        st.plan_transition(pid, S.APPROVED, "applied", "edge applied")
        tell(platform, st, pid)
    say("outcome: applied")
    return 0


def cmd_import_dev(a):
    platform, project, st = ctx(a.project)
    container = (platform.pull or {}).get("container", "dev")
    image_id, tag, sha = images.import_from_dev(project.name, a.ref, platform.state_dir, container)
    st.event("imported", f"{tag} ({image_id[:19]}) from dev {a.ref}", None, project.name, None)
    say(f"imported {tag}\n  id  {image_id}\n  sha {sha}  (a claim until submit checks it)")
    return 0


def _resolve_image(ref: str) -> tuple[str, str]:
    """(image id, huta tag) from either."""
    f = recipe.image_facts(ref)
    image_id = f["id"]
    tag = ref if recipe.TAG_RE.match(ref) else ""
    if not tag:
        labels = f["labels"]
        sha = labels.get("org.opencontainers.image.revision", "")
        tag = f"huta/{labels.get('run.huta.project', '?')}:{sha[:12]}-{image_id.split(':')[1][:12]}"
    return image_id, tag


def _secs(n: int) -> str:
    return f"{n // 60} min" if n % 60 == 0 else f"{n} s"


def snapshot_lines(old: list, new: list) -> list[str]:
    """📦 lines for [[snapshot]] added, changed or removed (docs/PLAN-snapshot.md): the
    commands in full, since a box's snapshot runs them on production data and the owner's tap
    trusts them. Any such line makes the release infra (never auto-approved)."""
    before = {x["name"]: x for x in old}
    after = {x["name"]: x for x in new}
    out = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a == b:
            continue
        if b is None:
            out.append(f"📦 snapshot {name} removed")
            continue
        what = "new" if a is None else "changed"
        out.append(f"📦 snapshot {name} ({what}): runs {' '.join(b['cmd'])[:300]}"
                   + (f"; check {' '.join(b['check'])[:200]}" if b.get("check") else "")
                   + f"; component {b['component']}, at most {_secs(b['timeout'])}, on a "
                   "backup copy, no network, no secrets")
    return out


def plan_release(platform, project, st, t, sha, image_id, tag, gh, note="", images=None) -> str:
    """images: {name: image id} for a project with several images (DESIGN-v2 §3); image_id and
    tag are then the public component's."""
    # P-003: the manifest comes from git at this sha, not from the submitter or image labels.
    app = parse_app_toml(gh.read_file(project.repo, project.app_toml, sha), project.name)
    if len(app["images"]) > 1:
        if not images:
            raise ConfigError(f"app.toml at {sha[:12]} names {len(app['images'])} images and the "
                              "request brought one: update the client (huta self-update), "
                              "pack and submit again")
        if set(images) != set(app["images"]):
            raise ConfigError(f"the request brings images {sorted(images)}, app.toml names "
                              f"{sorted(app['images'])}")
        image_id = images[SP.public(app)["image"]]
        tag = _resolve_image(image_id)[1]
    else:
        images = {next(iter(app["images"])): image_id}
    recipe.check_image_map(images, sha, app, project.name)
    from . import pg as PG
    if R.data_mount(app) or any(c.get("data") for c in app["components"].values()):
        if not t.data:
            raise ConfigError(f"app.toml mounts data, but the host registry gives "
                              f"{project.name}/{t.name} no data (operator: add data = {{ size = … }})")
    if PG.wants(app) and not t.postgres:
        raise ConfigError(f"app.toml asks for postgres, but the host registry gives "
                          f"{project.name}/{t.name} none (operator: add postgres = {{ … }})")
    if SP.several(app) or PG.wants(app):
        from . import egress as E
        if not E.intra_ready():
            raise ConfigError("this release's containers talk over a bridge (several components, "
                              "or the target's database); the host firewall's guard has no "
                              "huta_intra set yet (operator: load "
                              "systemd/nftables-huta-egress.nft.example again)")
    if t.monitor and platform.monitoring:
        # The resources that change the container are made now, before the plan is written, so
        # the plan shows exactly what the release gets (docs/PLAN-monitoring.md §2). A failure
        # is an event; the release goes on without them.
        from . import monitor as M
        M.ensure(platform, st, project, t, kinds=M.PLAN_KINDS, log=say)
    live = st.live(project.name, t.name)
    live_rel = st.release(live["release_id"]) if live else None
    if R.data_mount(app) and not t.data:
        raise ConfigError(f"app.toml mounts data at {R.data_mount(app)}, but the host registry "
                          f"gives {project.name}/{t.name} no data (operator: add data = {{ size = … }})")
    # Values a box staged for this target's secrets go live with this plan if it is approved.
    staged = R.staged(platform, project.name, secret_names(t))
    for name in secret_names(t):
        if name not in staged:
            R.secret_value(platform, project.name, name)     # refuses a missing secret now
    unknown = sorted(set(t.job_secrets) - {j["name"] for j in app.get("jobs", [])})
    eff = recipe.effective(platform, project, t, app)
    changes = []
    mig = recipe.migration(st, t, app)            # refuses what this data can't take
    externals = {}
    for name, e in (app.get("external") or {}).items():
        reg = t.external.get(name)
        if not reg:
            raise ConfigError(f"app.toml publishes external.{name}, but the host registry has no "
                              f"external.{name} for {project.name}/{t.name} (operator: add it)")
        from . import external as E
        import secrets as _secrets
        key = f"{sha[:12]}-{_secrets.token_hex(4)}"      # this plan's own copy
        got = E.extract(platform, t, key, name, images[e["image"]], e)
        externals[name] = {**got, "kind": e["kind"], "key": key}
        where = "Pages project" if e["kind"] == "cloudflare-pages" else "Worker"
        changes.append(f"☁️ publishes {name} after the switch: {got['files']} files "
                       f"({got['bytes'] >> 10} KiB, {got['hash'][:12]}) to the Cloudflare {where} "
                       f"{reg['name']}" + (f" on {', '.join(reg['routes'])}" if reg["routes"] else "")
                       + f"; token {reg['token']} is account-wide")
    if mig and mig["run"]:
        m = app["migrate"]
        frm = mig['from'] if mig['from'] is not None else 'unversioned'
        if recipe.online_ok(st, t, app, mig):
            changes.append(f"⚠️ migrates ONLINE from schema {frm} to {mig['to']} with "
                           f"{' '.join(m['cmd'])} (image {m['image']}, at most {m['timeout']}) "
                           "while the live release keeps serving; a reference copy is pinned, but "
                           "nothing is restored automatically: a failed migration is needs-human")
        else:
            changes.append(f"⚠️ migrates the data from schema {frm} to {mig['to']} with "
                           f"{' '.join(m['cmd'])} (image {m['image']}, at most {m['timeout']}). "
                           "Every writer stops first and the site answers 502 until the switch; "
                           "a copy is taken and pinned, and any failure before the switch puts it "
                           "back" + (" (online was asked for, but the host, the live release's "
                                     "runs_on or overlap don't allow it)" if m.get("online") else ""))
    if not live_rel:
        changes.append("first release of this target")
        if edge.adopt_pending(platform, t):
            changes.append(f"adopts the live site {t.adopt} at the switch")
    else:
        old = json.loads(live_rel["plan"]).get("effective") or {}
        # A key a plan made by an older huta doesn't have counts as its default, so an upgrade
        # alone doesn't list every new key as changed.
        dflt = {"env": {}, "secrets": [], "upstreams": {}, "data": None, "jobs": [], "smoke": [],
                "adopt": "", "access_log": True, "job_secrets": {}, "diag": [], "edge": {},
                "health_timeout": 30, "migrate": None, "migrate_secrets": [], "schema": None,
                "runs_on": None, "snapshot": []}
        # [[snapshot]] gets its own 📦 lines below, with the commands.
        diff = sorted(k for k in set(old) | set(eff) if k not in ("compose", "snapshot")
                      and old.get(k, dflt.get(k)) != eff.get(k, dflt.get(k)))
        if diff or old.get("compose") != eff.get("compose"):
            changes.append("changed: " + (", ".join(diff) if diff else "runtime config"))
    changes += snapshot_lines((json.loads(live_rel["plan"]).get("effective") or {})
                              .get("snapshot", []) if live_rel else [], eff.get("snapshot", []))
    mon = eff.get("monitor")
    old_mon = (json.loads(live_rel["plan"]).get("effective") or {}).get("monitor") if live_rel else None
    if mon and mon != old_mon:
        # What monitoring changes in the container, said in words (docs/PLAN-monitoring.md §3).
        parts = []
        if mon["env"]:
            parts.append("sets " + ", ".join(mon["env"]))
        if mon["egress"] and (not old_mon or old_mon.get("egress") != mon["egress"]):
            parts.append("egress adds " + ", ".join(mon["egress"]))
        parts += [f"not set: {m}" for m in mon["missing"]]
        changes.append("📈 monitoring: " + ("; ".join(parts) or "no variables (the repo asks "
                                                             "for none)"))
    for name, when in R.staged_list(platform, project.name):
        if name in staged:
            # Secrets are per project: say what else the new value reaches (codex secrets #2).
            others = sorted(o.name for o in project.targets.values()
                            if o.name != t.name and name in secret_names(o))
            changes.append(f"🔑 new value for secret {name}, sent by a dev box "
                           f"{time.strftime('%m-%d %H:%M', time.localtime(when))}; "
                           "it goes live with this release"
                           + (f" and also reaches {', '.join(others)} (their jobs at the next run, "
                              "their slots at the next release or restart)" if others else ""))
    if unknown:   # harmless (nothing receives them), but worth seeing
        changes.append(f"job_secrets for jobs this release doesn't have: {', '.join(unknown)}")
    # The repo names its jobs, so it decides which code a job's secrets reach: a secret-bearing
    # job that is new, or whose command changed, is called out with its command (job secrets
    # review, codex #1).
    old_jobs = {j["name"]: j for j in (json.loads(live_rel["app_toml"]).get("jobs", [])
                                       if live_rel else [])}
    old_js = (json.loads(live_rel["plan"]).get("effective") or {}).get("job_secrets", {}) \
        if live_rel else {}
    for j in app.get("jobs", []):
        if j["name"] not in t.job_secrets:
            continue
        newly = sorted(set(t.job_secrets[j["name"]]) - set(old_js.get(j["name"], [])))
        if old_jobs.get(j["name"], {}).get("cmd") != j["cmd"] or newly:
            what = ("new" if j["name"] not in old_jobs else "command changed"
                    if old_jobs[j["name"]].get("cmd") != j["cmd"] else f"newly gets {', '.join(newly)}")
            changes.append(f"🔑 job {j['name']} gets {', '.join(t.job_secrets[j['name']])} — "
                           f"{what}: {' '.join(j['cmd'])[:200]}")
    # Which way the commit moves: a plan that goes back (or isn't based on live) says so, since
    # an older sha from a second session looks like any other plan. It warns, never refuses:
    # going back on purpose is the owner's call.
    history = None
    if live_rel:
        if live_rel["sha"] == sha:
            history = {"status": "identical", "ahead": 0, "behind": 0}
        else:
            try:
                history = gh.compare(project.repo, live_rel["sha"], sha)
            except Exception as e:  # noqa: BLE001 — informational: it warns, never refuses
                history = {"status": "unknown", "error": f"{type(e).__name__}: {e}"[:200]}
    plan = {
        "project": project.name, "target": t.name, "sha": sha, "image": tag, "image_id": image_id,
        "from": {"release": live_rel["id"], "sha": live_rel["sha"], "image_id": live_rel["image_id"]}
        if live_rel else None,
        "class": "code-only" if not changes else "infra", "changes": changes,
        "checks": {"on_main": True, "ci": "green" if t.require_ci else "not required"},
        "planned_against_live": live_rel["id"] if live_rel else None,
        **({"migration": mig} if mig else {}),   # {"from": S, "to": M, "run": bool}
        **({"external": externals} if externals else {}),   # name -> {hash, files, bytes, kind, key}
        "effective": eff, "built_by": "dev", "note": note, "history": history,
        "staged_secrets": staged,     # {name: fingerprint}: approval applies exactly these
        "secret_versions": R.live_versions(platform, project.name, secret_names(t)
                                           + _monitor_secrets(platform, project, t, app)),
    }
    if t.data:
        from .data import last_backup_text
        plan["backup_state"] = last_backup_text(platform, t)
    blob = json.dumps(plan, sort_keys=True)
    return st.new_release(project=project.name, target=t.name, sha=sha, image_id=image_id,
                          image_tag=tag, app_toml=json.dumps(app), plan=plan,
                          plan_hash=hashlib.sha256(blob.encode()).hexdigest(), built_by="dev",
                          images=images,
                          schema=(app.get("state") or {}).get("schema"))


def _monitor_secrets(platform, project, t, app) -> list[str]:
    if not t.monitor:
        return []
    from . import monitor as M
    return M.secret_names(platform, project, t, app)


def secret_names(t) -> list[str]:
    """Every secret name a target uses: its own, its jobs' and a gateway's header."""
    gw = [t.gateway["secret"]] if t.gateway and t.gateway.get("secret") else []
    mig = [n for n in t.migrate_secrets if n not in t.secrets]
    comp = [n for c in t.components.values() for n in c["secrets"]]
    names = t.secrets + [n for names in t.job_secrets.values() for n in names] + gw + mig + comp
    return list(dict.fromkeys(names))


def _gates(gh, project, t, sha):
    if not gh.on_main(project.repo, sha):
        raise ExtError(f"{sha[:12]} is not on {project.repo} main")
    if t.require_ci and not gh.ci_green(project.repo, sha):
        raise ExtError(f"CI for {sha[:12]} (push to main) is not green")


def cmd_submit(a):
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    if not SHA_RE.match(a.sha):
        raise ConfigError("sha must be hex")
    sha = GitHub(platform).full_sha(project.repo, a.sha)
    image_id, tag = _resolve_image(a.image)
    images = None
    if a.images:
        images = {}
        for item in a.images:
            name, _, ref = item.partition("=")
            if not NAME_RE.match(name) or not ref:
                raise ConfigError("--image takes <name>=<image id or tag>")
            images[name] = _resolve_image(ref)[0]
    rid = submit_release(platform, project, st, t, sha, image_id, tag, images)
    st.event("submitted-by", f"CLI on {platform.host}", rid, project.name, t.name)
    tell(platform, st, rid)
    ask(platform, st, rid)
    print_plan(st, rid)
    say(f"\napprove with: huta approve {rid}")
    return 0


def plan_text(st, rid) -> str:
    r = st.release(rid)
    p = json.loads(r["plan"])
    frm = p["from"]
    e = p["effective"]
    lines = [
        f"plan {rid} · {p['project']} → {p['target']} · state {r['state']}"
        + (f" · {p['note']}" if p.get("note") else ""),
        f"  {frm['sha'][:12] if frm else '(nothing live)'} → {p['sha'][:12]}   image {p['image']}",
    ]
    from .notify import history_note
    note, _ = history_note(p)
    if note:
        lines.append(f"  {note}")
    lines += [
        f"  image id {p['image_id']}",
        f"  hosts: {', '.join(e['hosts'])} · uid {e['uid']} · slots {e['slots']} · limits {e['limits']}",
    ]
    if e.get("env") or e.get("secrets") or e.get("job_secrets"):
        lines.append(f"  env: {', '.join(e.get('env') or {}) or '-'} · secrets (names): "
                     f"{', '.join(e.get('secrets') or []) or '-'}"
                     + "".join(f" · job {j} only: {', '.join(n)}"
                               for j, n in (e.get("job_secrets") or {}).items()))
    lines.append(f"  egress: {e['egress'] if e['egress'] == 'none' else ', '.join(e['egress'])}"
                 + (f" · upstreams: {', '.join(e['upstreams'])}" if e.get("upstreams") else ""))
    d = e.get("data")
    if d:
        lines.append(f"  data: {d['size']} at {d['mount']} · "
                     + ("concurrent-safe" if d["concurrent_safe"]
                        else "NOT concurrent-safe: the old slot stops first (a few seconds of 502)")
                     + f" · backup {d['backup']}" + (f" · {p['backup_state']}" if p.get("backup_state") else ""))
    extra = []
    if e.get("jobs"):
        extra.append("jobs: " + ", ".join(
            f"{j['name']} ({j['schedule']}{', and after this release' if j.get('after_release') else ''})"
            for j in e["jobs"]))
    if e.get("smoke"):
        extra.append(f"smoke: {len(e['smoke'])} checks")
    if e.get("adopt"):
        extra.append(f"adopt: {e['adopt']}")
    if e.get("access_log") is False:
        extra.append("access log off")
    if extra:
        lines.append("  " + " · ".join(extra))
    lines += [
        f"  class: {p['class']}" + (f" — {'; '.join(p['changes'])}" if p["changes"] else ""),
        f"  checks: on origin/main ✓ · CI {p['checks']['ci']} · built by {p['built_by']}",
        f"  plan hash {r['plan_hash'][:16]} · expires "
        f"{time.strftime('%H:%M', time.localtime(r['created'] + PLAN_TTL))}",
    ]
    return "\n".join(lines)


def print_plan(st, rid):
    say(plan_text(st, rid))


def tell(platform, st, rid: str, create: bool = True) -> None:
    """Update this release's history message (Telegram, edited in place) and, if the owner was
    DMed buttons for it, that DM too. Never fails."""
    from . import notify as N
    # 25 s: room for a second try of an edit when Telegram is slow (it was, now and then).
    N.history(platform, st, rid, create, timeout=25)
    N.refresh_ask(platform, st, rid)


def ask(platform, st, rid: str) -> None:
    """DM the owner Deploy / Reject buttons for a new plan. Never fails."""
    from . import notify as N
    N.ask_owner(platform, st, rid, PLAN_TTL)


def retire_buttons(platform, st, rid: str) -> None:
    """The plan was decided some other way (CLI approve/reject, expiry): the DM buttons die."""
    from . import notify as N
    N.close_ask(platform, st, rid)


def notify(platform, text: str) -> None:
    """Plain-text fallback when no Telegram history is configured. Never fails the caller."""
    if not platform.notify or platform.telegram.get("token_json"):
        return
    try:
        subprocess.run(platform.notify, input=redact(text), text=True, capture_output=True,
                       timeout=20, cwd="/")
    except Exception:  # noqa: BLE001
        pass


def submit_release(platform, project, st, t, sha, image_id, tag, images=None) -> str:
    """Shared by `huta submit` and the pull/push intake: gates, reconcile, plan."""
    gh = GitHub(platform)
    _gates(gh, project, t, sha)
    with st.lock(project.name, t.name), st.lock(project.name, R.SECRETS_LOCK, wait=60):
        recipe.reconcile(platform, st, project, t, say)
        settle_journal(platform, st, project)
        return plan_release(platform, project, st, t, sha, image_id, tag, gh, images=images)


# Release states after which an interrupted secret apply can be settled: keep the new values
# (the release went live, or may have: needs-human) or put the old ones back.
_KEEP = {S.LIVE, S.RETIRED, S.NEEDS_HUMAN}
_REVERT = {S.NOTHING_ACTIVATED, S.ROLLED_BACK, S.REJECTED, S.SUPERSEDED, S.PLANNED}


def _settle(platform, st, project, rid: str, outcome: str) -> None:
    keep = outcome in _KEEP
    back = R.settle_secrets(platform, project.name, rid, keep)
    r = st.release(rid)
    if back:
        st.event("secret", f"the release ended {outcome}: the old values of {', '.join(back)} are "
                 "live again; the new ones are staged again", rid, project.name, r["target"])
        say(f"secrets: {', '.join(back)} back to their old values (the new ones are staged again)")


def settle_journal(platform, st, project) -> None:
    """Finish secret applies a crashed process left behind, once their release has an outcome.
    The caller holds the project's secrets lock, so no apply is in progress; a release still
    approved or running is left until its own target's reconcile decides it."""
    for rid in sorted(R.pending_journal(platform, project.name)):
        try:
            state = st.release(rid)["state"]
        except KeyError:
            state = S.NOTHING_ACTIVATED          # no such release: never went live
        if state in _KEEP or state in _REVERT:
            _settle(platform, st, project, rid, state)


def cmd_show(a):
    st = S.Store(load_platform().state_dir)
    p = st.plan_row(a.release)
    if p:
        plan = json.loads(p["plan"])
        say(f"{p['kind']} plan {p['id']} for {p['project']}/{p['target']}: {p['state']}")
        for line in plan.get("lines", []):
            say(f"  {line}")
        return 0
    print_plan(st, a.release)
    return 0


def cmd_restore(a):
    """Plan putting the data back to a restore point, for a release whose code can't run on the
    current schema (DESIGN-v2 §2). Every write since the point is lost: the plan says so first,
    and needs --confirm-data-loss on the CLI (or the Telegram button)."""
    from . import data as D
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    to = st.release(a.to)
    if (to["project"], to["target"]) != (project.name, t.name):
        raise ConfigError(f"release {a.to} is not {project.name}/{t.name}'s")
    to_app = json.loads(to["app_toml"])
    runs_on = (to_app.get("state") or {}).get("runs_on")
    kind = recipe._res(to_app).kind
    s = recipe.schema_now(st, t, kind)
    if s is None:
        raise ConfigError("this target's data has no recorded schema: there is no restore point "
                          "a schema points to (huta rollback plans an older image)")
    if runs_on and s in runs_on:
        raise ConfigError(f"release {a.to} runs on the current schema {s}: no restore is needed "
                          "(huta rollback, or submit that commit)")
    points = st.db.execute("SELECT * FROM restore_points WHERE project=? AND target=? AND"
                           " kind=? AND state='kept' ORDER BY created DESC",
                           (project.name, t.name, kind)).fetchall()
    pt = next((x for x in points if (x["schema"] in runs_on if runs_on else x["schema"] is None)),
              None)
    if pt is None:
        raise ConfigError(f"no kept restore point holds a schema release {a.to} runs on "
                          f"({runs_on or 'unversioned'})")
    if D.fingerprint(Path(pt["path"])) != pt["fingerprint"]:
        raise ConfigError(f"restore point {pt['id']} changed since it was pinned; refusing it")
    live = st.live(project.name, t.name)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(pt["created"]))
    lines = [f"⚠️ RESTORES the data to the copy pinned {when} before release {pt['release_id']} "
             f"migrated it (schema {pt['schema'] if pt['schema'] is not None else 'unversioned'}). "
             f"EVERY WRITE SINCE {when} IS LOST.",
             f"the data is at schema {s} now; release {a.to} ({to['sha'][:12]}) runs on "
             f"{runs_on or 'unversioned data'}",
             f"the target stops for the restore and stays stopped; then a release of {a.to} is "
             "planned for approval"]
    pid = st.new_plan(project.name, t.name, "restore", {
        "lines": lines, "point": pt["id"], "path": pt["path"], "fingerprint": pt["fingerprint"],
        "schema": pt["schema"], "release": a.to, "schema_now": s, "res": kind,
        "planned_against_live": live["release_id"] if live else None})
    tell(platform, st, pid)
    ask(platform, st, pid)
    for line in lines:
        say(line)
    say(f"\nrestore plan {pid}; approve with: huta approve {pid} --confirm-data-loss")
    return 0


def _approve_restore(platform, st, project, t, pid, plan, via, confirmed) -> int:
    """Hold the target lock. Check the plan still holds, restore, plan the release."""
    from . import data as D
    if not confirmed:
        raise ConfigError("a restore loses data: approve it with --confirm-data-loss (or the "
                          "Telegram button)")
    live = st.live(project.name, t.name)
    stale = None
    if (live["release_id"] if live else None) != plan["planned_against_live"]:
        stale = "a release went live since planning"
    elif recipe.schema_now(st, t, plan.get("res", "data")) != plan["schema_now"]:
        stale = "the data's schema changed since planning"
    elif D.fingerprint(Path(plan["path"])) != plan["fingerprint"]:
        stale = "the restore point changed since planning"
    if stale:
        st.plan_transition(pid, S.PLANNED, S.SUPERSEDED, stale)
        tell(platform, st, pid)
        raise ConfigError(f"{stale}; plan the restore again")
    st.plan_transition(pid, S.PLANNED, S.APPROVED, f"approved {via}")
    retire_buttons(platform, st, pid)
    tell(platform, st, pid)
    try:
        recipe.restore_point(platform, st, project, t, pid, plan, say)
    except Exception as e:  # noqa: BLE001 — every failure ends in a recorded outcome
        st.plan_transition(pid, S.APPROVED, "failed", f"{type(e).__name__}: {e}")
        tell(platform, st, pid)
        raise
    st.plan_transition(pid, S.APPROVED, "applied", "data restored; the target is stopped")
    tell(platform, st, pid)
    to = st.release(plan["release"])
    try:
        imgs = st.images(to)
        with st.lock(project.name, R.SECRETS_LOCK, wait=60):
            rid = plan_release(platform, project, st, t, to["sha"], to["image_id"], to["image_tag"],
                               GitHub(platform), note=f"after restore {pid}",
                               images=imgs if len(imgs) > 1 else None)
    except Exception as e:  # noqa: BLE001 — the data is back; the code can be planned again
        say(f"data restored; planning release {plan['release']} failed: {type(e).__name__}: {e}")
        say(f"plan it with: huta submit {project.name} {t.name} {to['sha']} {to['image_id']}")
        return 1
    st.event("submitted-by", f"restore {pid} on {platform.host}", rid, project.name, t.name)
    tell(platform, st, rid)
    ask(platform, st, rid)
    print_plan(st, rid)
    say(f"\ndata restored. Approve the release of that code with: huta approve {rid}")
    return 0


def cmd_postgres(a):
    """huta postgres <p> <t> [status|upgrade]: the target's database (DESIGN-v2 §4)."""
    from . import pg as PG
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    if not t.postgres:
        raise ConfigError(f"{project.name}/{t.name} has no postgres in the host registry")
    change = PG.pending(platform, project, t)
    if a.cmd == "status":
        say(f"postgres {t.postgres['version']} ({platform.postgres_images[t.postgres['version']]})")
        say(f"server: {'running' if PG.running(t) else 'not running' if PG.container(t) else 'not created yet'}")
        say(f"schema: {recipe.schema_now(st, t, 'postgres')}")
        say("upgrade pending: " + (f"{change['from_image']} → {change['to_image']}"
                                   + (" (new cluster, restored from a dump)" if change["new_cluster"] else "")
                                   if change else "no"))
        return 0
    if not change:
        raise ConfigError("nothing to upgrade: the running definition is the registry's")
    live = st.live(project.name, t.name)
    lines = [f"⚠️ the site is down while postgres is upgraded: {change['from_image']} → "
             f"{change['to_image']}",
             "the app stops, the database is dumped and the dump pinned, the new server starts"
             + (", as a new cluster restored from that dump (the old data directory stays)"
                if change["new_cluster"] else ""),
             "any failure puts the old server back and starts the app on it"]
    pid = st.new_plan(project.name, t.name, "postgres", {
        "lines": lines, "change": change,
        "planned_against_live": live["release_id"] if live else None})
    tell(platform, st, pid)
    ask(platform, st, pid)
    for line in lines:
        say(line)
    say(f"\npostgres upgrade plan {pid}; approve with: huta approve {pid}")
    return 0


def _approve_pg_upgrade(platform, st, project, t, pid, plan, via) -> int:
    from . import pg as PG
    if PG.pending(platform, project, t) != plan["change"]:
        st.plan_transition(pid, S.PLANNED, S.SUPERSEDED, "the definition changed since planning")
        tell(platform, st, pid)
        raise ConfigError("the postgres definition changed since planning; plan again")
    st.plan_transition(pid, S.PLANNED, S.APPROVED, f"approved {via}")
    retire_buttons(platform, st, pid)
    tell(platform, st, pid)
    try:
        PG.upgrade(platform, st, project, t, pid, say)
    except Exception as e:  # noqa: BLE001 — every failure ends in a recorded outcome
        st.plan_transition(pid, S.APPROVED, "failed", f"{type(e).__name__}: {e}")
        tell(platform, st, pid)
        recipe.reconcile(platform, st, project, t, say)     # the app comes back
        raise
    st.plan_transition(pid, S.APPROVED, "applied", "postgres upgraded")
    tell(platform, st, pid)
    recipe.reconcile(platform, st, project, t, say)         # the app starts again
    say("outcome: applied")
    return 0


def cmd_external(a):
    """huta external <p> <t> [status | rollback <name>] (DESIGN-v2 §5)."""
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    rows = st.db.execute("SELECT * FROM external_deploys WHERE project=? AND target=?"
                         " ORDER BY id DESC", (project.name, t.name)).fetchall()
    if a.cmd == "status":
        if not rows:
            say("nothing published from this target yet")
        for r in rows[:20]:
            say(f"{time.strftime('%m-%d %H:%M', time.localtime(r['created']))}  {r['name']}  "
                f"{r['kind']}  {r['deployment']}  (was {r['previous'] or '-'})  release {r['release_id']}")
        return 0
    name = a.name
    if not name or name not in t.external:
        raise ConfigError(f"rollback which external? ({', '.join(t.external) or 'none'})")
    last = next((r for r in rows if r["name"] == name), None)
    if not last or not last["previous"]:
        raise ConfigError(f"{name}: no earlier deployment recorded to go back to")
    reg = t.external[name]
    lines = [f"⚠️ {name}: Cloudflare goes back to deployment {last['previous']} (from "
             f"{last['deployment']}, published by release {last['release_id']})",
             f"the host target is not changed; token {reg['token']} is account-wide"]
    pid = st.new_plan(project.name, t.name, "external", {
        "lines": lines, "name": name, "kind": last["kind"], "to": last["previous"],
        "from": last["deployment"], "release": last["release_id"]})
    tell(platform, st, pid)
    ask(platform, st, pid)
    for line in lines:
        say(line)
    say(f"\nrollback plan {pid}; approve with: huta approve {pid}")
    return 0


def _approve_external_rollback(platform, st, project, t, pid, plan, via) -> int:
    from . import external as E
    reg = t.external.get(plan["name"])
    if not reg:
        raise ConfigError(f"the registry no longer has external.{plan['name']}")
    if E.current(platform, plan["kind"], reg) not in (plan["from"], None):
        st.plan_transition(pid, S.PLANNED, S.SUPERSEDED, "Cloudflare changed since planning")
        raise ConfigError("Cloudflare serves another deployment since planning; plan again")
    st.plan_transition(pid, S.PLANNED, S.APPROVED, f"approved {via}")
    retire_buttons(platform, st, pid)
    tell(platform, st, pid)
    try:
        E.rollback(platform, plan["kind"], reg, plan["to"])
    except Exception as e:  # noqa: BLE001
        st.plan_transition(pid, S.APPROVED, "failed", f"{type(e).__name__}: {e}")
        tell(platform, st, pid)
        raise
    recipe._record_external(st, t, plan["release"], plan["name"], plan["kind"], plan["to"],
                            plan["from"], None)
    st.plan_transition(pid, S.APPROVED, "applied", f"{plan['name']} back on {plan['to']}")
    tell(platform, st, pid)
    say("outcome: applied")
    return 0


def cmd_approve(a):
    platform, registry, st = ctx()
    if st.plan_row(a.release):
        return _approve_plan(platform, registry, st, a.release,
                             "with the Telegram button" if a.via == "telegram" else "on the host",
                             confirmed=a.via == "telegram" or getattr(a, "confirm_data_loss", False))
    r = st.release(a.release)
    project = registry[r["project"]]
    t = target_of(project, r["target"])
    # The target lock (a running job or backup finishes first), then the project's secrets lock:
    # no value is staged, set, applied or reverted between this plan's checks and the moment
    # its containers read their environment (grok secrets #4).
    with st.lock(project.name, t.name, wait=300), \
            st.lock(project.name, R.SECRETS_LOCK, wait=300):
        r = st.release(a.release)   # re-read under the lock
        plan = json.loads(r["plan"])
        if r["state"] != S.PLANNED:
            raise ConfigError(f"release {a.release} is {r['state']}, not planned")

        def supersede(why: str, msg: str):
            st.transition(a.release, S.PLANNED, S.SUPERSEDED, why)
            retire_buttons(platform, st, a.release)
            tell(platform, st, a.release)
            raise ConfigError(msg)

        if time.time() - r["created"] > PLAN_TTL:
            supersede("approval expired (30 min)", "plan expired; submit again")
        recipe.reconcile(platform, st, project, t, say)
        settle_journal(platform, st, project)
        live = st.live(project.name, t.name)
        if (live["release_id"] if live else None) != plan["planned_against_live"]:
            supersede("prod changed since planning", "prod changed since this plan was made; submit again")
        app = json.loads(r["app_toml"])
        if recipe.effective(platform, project, t, app) != plan["effective"]:
            supersede("host config changed since planning",
                      "host config changed since this plan was made; submit again")
        try:
            mig_now = recipe.migration(st, t, app)
        except recipe.RecipeError as e:
            supersede(f"the data changed since planning: {e}", f"plan is stale: {e}")
        if mig_now != plan.get("migration"):
            supersede("the data's schema changed since planning",
                      "the data's schema changed since this plan was made; submit again")
        _gates(GitHub(platform), project, t, r["sha"])   # still on main, CI still green
        staged = plan.get("staged_secrets") or {}
        if "secret_versions" in plan and R.live_versions(
                platform, project.name, plan["secret_versions"]) != plan["secret_versions"]:
            supersede("a secret changed since planning",
                      "a secret's live value changed since this plan was made; submit again")
        cert = current_cert(platform, t)
        if staged:
            # Claim the planned files before the approval is recorded: a value sent after
            # planning supersedes the plan instead of using up the approval (grok secrets #7).
            try:
                R.claim_staged(platform, project.name, a.release, staged)
            except ConfigError:
                supersede("a staged secret changed since planning",
                          "a secret value staged by a box changed since this plan was made; "
                          "submit again")
        who = os.environ.get("SUDO_USER", "") if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}",
                                                              os.environ.get("SUDO_USER", "")) else ""
        via = ("with the Telegram button" if a.via == "telegram"
               else "with ↩️ in Telegram" if a.via == "rollback-button"
               else f"on the host by {who}" if who else "on the host (as root)")
        st.transition(a.release, S.PLANNED, S.APPROVED, f"approved {via}")
        retire_buttons(platform, st, a.release)
        tell(platform, st, a.release)
        applied: list[str] = []
        try:
            # The owner approved the staged values with the plan: they go live first (the old
            # values kept aside, journaled by release id), so the new slot starts with them.
            if staged:
                st.event("secret", f"applying staged values: {', '.join(sorted(staged))}",
                         a.release, project.name, t.name)
                R.promote_claimed(platform, project.name, a.release, applied)
                st.event("secret", f"now live: {', '.join(applied)}", a.release, project.name, t.name)
            outcome = recipe.run(platform, st, project, t, a.release, cert, Cloudflare.ip_ranges(), say)
        except Exception as e:  # noqa: BLE001 — every failure must end in a recorded outcome
            state = st.release(a.release)["state"]
            outcome = S.NEEDS_HUMAN if state in (S.RUNNING, S.LIVE) else S.NOTHING_ACTIVATED
            st.set_state(a.release, outcome, msg=f"{type(e).__name__}: {e}")
            say(f"error: {type(e).__name__}: {e}")
            if outcome == S.NEEDS_HUMAN:
                say("run `huta reconcile` to repair state from what the vhost routes to")
        if staged:
            # Values go live with the release or not at all (grok secrets #1, #2).
            _settle(platform, st, project, a.release, outcome)
        if t.watch:
            # The watch window of what is live now opens (docs/PLAN-monitoring.md §6); an older
            # episode's closes. If huta dies before this, the tick opens it.
            from . import watch as W
            W.close_stale(st, project.name, t.name)
            try:
                if outcome == S.LIVE:
                    W.open_live(platform, st, project, t)
            except Exception as e:  # noqa: BLE001 — the tick opens it
                say(f"watch not opened ({type(e).__name__}); the monitoring tick will")
        if t.monitor_egress:
            # Whatever the outcome, the Bugsink grant now follows the releases that are live:
            # one dropped by this release, or by a failed one, goes (docs/PLAN-monitoring.md §3).
            recipe._egress_sync(platform, say)
        tell(platform, st, a.release)
        if outcome == S.LIVE and plan.get("planned_against_live"):
            tell(platform, st, plan["planned_against_live"], create=False)   # now "replaced"
        removed = recipe.prune_images(st, project, t)
        if removed:
            say(f"untagged {len(removed)} old image(s)")
    say(f"outcome: {outcome}")
    return 0 if outcome == S.LIVE else 1


def cmd_reject(a):
    platform, registry, st = ctx()
    p = st.plan_row(a.release)
    if p:
        with st.lock(p["project"], p["target"]):
            st.plan_transition(a.release, S.PLANNED, S.REJECTED, "rejected on the CLI")
        retire_buttons(platform, st, a.release)
        tell(platform, st, a.release)
        say("rejected")
        return 0
    r = st.release(a.release)
    with st.lock(r["project"], r["target"]):
        st.transition(a.release, S.PLANNED, S.REJECTED, "rejected on the CLI")
    retire_buttons(platform, st, a.release)
    tell(platform, st, a.release)
    say("rejected")
    return 0


def cmd_rollback(a):
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    if getattr(a, "press", None):
        return _rollback_press(platform, project, st, t, a.press)
    from . import watch as W
    with st.lock(project.name, t.name), st.lock(project.name, R.SECRETS_LOCK, wait=60):
        recipe.reconcile(platform, st, project, t, say)
        settle_journal(platform, st, project)
        live = st.live(project.name, t.name)
        if not live:
            raise ConfigError("nothing is live")
        cur = st.release(live["release_id"])
        # A release differs by any of its images (a rollback that only changes a worker counts);
        # one a ↩️ press rolled away from is never picked (docs/PLAN-monitoring.md §6).
        p = W.rollback_target(st, project.name, t.name, cur)
        if p is None:
            raise ConfigError("no earlier release to roll back to")
        rid = _plan_rollback(platform, project, st, t, p, note=f"rollback to {p['id']}")
    st.event("submitted-by", f"rollback on {platform.host}", rid, project.name, t.name)
    tell(platform, st, rid)
    ask(platform, st, rid)
    print_plan(st, rid)
    say(f"\nrollback planned; approve with: huta approve {rid}")
    return 0


def _plan_rollback(platform, project, st, t, p, note: str) -> str:
    """Plan a release of exactly `p`'s images. The historical identity must still be on this
    host, exactly (codex #9). Caller holds the target and secrets locks."""
    p_images = st.images(p)
    for iid in p_images.values():
        recipe.check_image(iid, p["sha"], project.name)
    return plan_release(platform, project, st, t, p["sha"], p["image_id"], p["image_tag"],
                        GitHub(platform), note=note,
                        images=p_images if len(p_images) > 1 else None)


def _rollback_press(platform, project, st, t, rid: str) -> int:
    """`huta rollback <p> <t> --press <release>`: the unit a ↩️ press starts
    (docs/PLAN-monitoring.md §6). Under the target lock: refuse unless the pressed episode is
    still live and the release fixed when the button was made is still retired with its images
    here; else plan the rollback of exactly that release and record it on the watch row. After
    the lock: post the plan with its buttons, and approve it at once only if it is one-tap
    clean. A unit that dies before planning leaves the press spent and nothing changed; after
    planning, a plan that waits for its buttons."""
    from . import notify as N
    from . import watch as W
    refused = ""
    with st.lock(project.name, t.name, wait=300), \
            st.lock(project.name, R.SECRETS_LOCK, wait=300):
        recipe.reconcile(platform, st, project, t, say)
        settle_journal(platform, st, project)
        row = W.pressed_row(st, rid)
        if row is None or (row["project"], row["target"]) != (project.name, t.name):
            raise ConfigError(f"no ↩️ press of {rid} waits on {project.name}/{t.name}")
        live = st.live(project.name, t.name)
        to = None
        try:
            to = st.release(row["button_target"]) if row["button_target"] else None
        except KeyError:
            pass
        if not live or (live["release_id"], live["since"]) != (rid, row["since"]):
            refused = "the release is no longer the live one"
        elif to is None or to["state"] != S.RETIRED or to["project"] != project.name \
                or to["target"] != t.name:
            refused = "the release to go back to is no longer a retired release of this target"
        else:
            try:
                new = _plan_rollback(platform, project, st, t, to,
                                     note=f"rollback to {to['id']} (↩️ from {rid})")
            except (ConfigError, recipe.RecipeError, ExtError, DataError) as e:
                refused = redact(str(e))
        if refused:
            W.set_action(st, row, W.REFUSED_PREFIX + refused[:300])
            st.event("rollback-refused", f"↩️ refused: {refused}", rid, project.name, t.name)
        else:
            W.set_action(st, row, W.PLANNED_PREFIX + new)
            st.event("submitted-by", "↩️ in Telegram", new, project.name, t.name)
    W.drop_keyboard(platform, st, row)                  # spent: the ↩️ keyboard goes
    if refused:
        N.reply(platform, st, rid, f"↩️ not rolled back: {html.escape(refused)}\n"
                                   f"<code>sudo huta rollback {project.name} {t.name}</code> plans "
                                   "the previous image; <code>sudo huta restore</code> brings data "
                                   "back from a copy")
        tell(platform, st, rid, create=False)
        say(f"↩️ refused: {refused}")
        return 1
    tell(platform, st, new)
    clean, why = W.one_tap_clean(t, st.release(new))
    if not clean:
        ask(platform, st, new)
        say(f"rollback {new} planned; it waits for approval ({why})")
        return 0
    # One tap: no Deploy / Reject buttons for the seconds until it is approved (they'd only
    # answer "no longer valid"). If approval refuses without deciding the plan, it gets them.
    try:
        return cmd_approve(argparse.Namespace(release=new, via="rollback-button",
                                              confirm_data_loss=False))
    except Exception:
        if st.release(new)["state"] == S.PLANNED:
            ask(platform, st, new)
        raise


def handle_request(platform, registry, st, req: dict, transport: str, box: str) -> dict:
    """One validated request from a dev box (proto.parse). Returns the answer body.
    transport: "pull" (box = the Incus container) or "push" (box = name from the ssh key)."""
    op = req["op"]
    if op == "client":
        import base64
        import hashlib
        blob = client_bundle()
        return {"ok": True, "rev": server_rev(), "sha256": hashlib.sha256(blob).hexdigest(),
                "bundle": base64.b64encode(blob).decode()}
    if op == "credentials":
        if transport != "push":
            raise ConfigError("credentials are only handed out over the push transport")
        pw = read_secret(platform.secrets_dir / "registry" / box)
        st.event("credentials", f"registry login handed to box {box}")
        return {"ok": True, "registry": platform.push["registry_public"], "username": box,
                "password": pw}
    if op == "log":
        try:
            r = st.release(req["release"])
        except KeyError:
            r = None
        # The same answer for a release that doesn't exist and one of another project, so a box
        # can't probe for other projects' releases.
        if r is None or r["project"] not in registry or box not in registry[r["project"]].boxes:
            raise ConfigError(f"no release {req['release']} for box {box}")
        lines = [f"{time.strftime('%m-%d %H:%M:%S', time.localtime(e['ts']))}  {e['kind']:<18} {e['msg']}"
                 for e in reversed(st.events(req["release"], limit=50))]
        return {"ok": True, "release": r["id"], "state": r["state"], "text": "\n".join(lines)}
    # A box sees and submits only the projects whose host registry names it (boxes = [...]);
    # an ungranted project answers like a missing one.
    if req["project"] not in registry or box not in registry[req["project"]].boxes:
        raise ConfigError(f"no project {req['project']!r} for box {box}")
    project = registry[req["project"]]
    if op == "secret":
        # Only names the operator already put in the registry; the value is write-only (never
        # answered, logged or shown) and waits for an approved release (runtime.STAGED).
        name = req["name"]
        if name not in {n for t in project.targets.values() for n in secret_names(t)}:
            raise ConfigError(f"{project.name} has no secret {name} in the host registry; "
                              "ask the operator to add the name")
        try:
            fd = st.acquire(project.name, R.SECRETS_LOCK, wait=20)
        except RuntimeError:
            raise ConfigError(f"a release of {project.name} is applying secrets right now; "
                              "send the value again in a few minutes") from None
        try:
            R.secret_stage(platform, project.name, name, req["value"])
        finally:
            os.close(fd)
        st.event("secret-staged", f"new value for {name} sent by box {box} ({transport})",
                 None, project.name, None)
        users = sorted(t.name for t in project.targets.values() if name in secret_names(t))
        return {"ok": True, "text": (
            f"{name} staged for {project.name} (used by {', '.join(users)}). It goes live with the "
            f"next release of {' or '.join(users)} the owner approves; its plan lists it. With "
            "no code change, submit the live commit again.")}
    if req["target"] in project.legacy and op in ("logs", "crashes"):
        # A service of the project that huta doesn't run: its journal and its Bugsink project,
        # read-only (observe.py).
        from . import observe as O
        lg = req["target"]
        if op == "logs":
            if any(k in req for k in ("slot", "release", "job")):
                raise ConfigError(f"{lg} is not run by huta: no slots, releases or jobs")
            res = O.legacy_logs(platform, project, lg, req.get("since"), req.get("until"),
                                req.get("grep"), req.get("tail"))
        else:
            res = O.crashes(platform, project, project.legacy[lg]["crashes"],
                            f"{project.name}/{lg}", req.get("since"))
        st.event(f"{op}-read", f"box {box} ({transport}): {lg} → {res['lines']} lines",
                 None, project.name, None)
        return {"ok": True, "text": res["text"], "truncated": res["truncated"]}
    t = target_of(project, req["target"], gateway_ok=op == "traffic")
    if op in ("egress-log", "traffic", "crashes"):
        from . import observe as O
        if op == "egress-log":
            res = O.egress_log(platform, project, t, req.get("since"), req.get("until"))
        elif op == "traffic":
            res = O.traffic(platform, project, t, req.get("since"), req.get("until"))
        else:
            res = O.crashes(platform, project, t.crashes, f"{project.name}/{t.name}",
                            req.get("since"))
        st.event(f"{op}-read", f"box {box} ({transport}) → {res['lines']} lines", None,
                 project.name, t.name)
        return {"ok": True, "text": res["text"], "truncated": res["truncated"]}
    if op == "diag":
        from . import diag as DG
        res = DG.run(platform, st, project, t, req["diag"], f"box {box} ({transport})")
        return {"ok": True, "text": res["text"], "exit": res["exit"]}
    if op == "status":
        live = st.live(project.name, t.name)
        rows = st.db.execute("SELECT * FROM releases WHERE project=? AND target=?"
                             " ORDER BY created DESC LIMIT 10", (project.name, t.name)).fetchall()
        text = "\n".join(f"{r['id']}  {time.strftime('%m-%d %H:%M', time.localtime(r['created']))}  "
                         f"{r['sha'][:12]}  {r['state']}" for r in rows)
        w = ""
        if t.watch:
            from . import watch as W
            w = W.status_line(st, project.name, t.name)
        return {"ok": True, "live": dict(live) if live else None,
                "text": text + "\n\n" + _runtime(platform, st, project, t) + (f"\n{w}" if w else "")}
    if op == "jobs":
        # Read-only: schedules and the outcome of recent runs, never their output (a job's
        # output can carry data or a secret the redactor doesn't know).
        from . import jobs as J
        return {"ok": True, "text": J.jobs_text(st, t, 20)}
    if op == "logs":
        # Redacted at ingest and again here, then filtered and cut (logs.py). Every read is an
        # event, with what was asked but none of the lines.
        from . import logs as L
        opts = {k: req[k] for k in ("since", "until", "grep", "tail", "slot", "release", "job",
                                    "component")
                if k in req}
        res = L.query(platform, st, project, t, box=box, **opts)
        st.event("logs-read", f"box {box} ({transport}): {json.dumps(opts)[:300]} → "
                 f"{res['lines']} lines{' (truncated)' if res['truncated'] else ''}",
                 None, project.name, t.name)
        return {"ok": True, "text": res["text"], "truncated": res["truncated"]}
    # submit
    def fetch(ref: str, digest: str | None):
        if transport == "push":
            if not digest:
                raise ConfigError("a push submit must name the pushed digest of every image")
            return images.import_from_registry(project.name, ref, digest,
                                               platform.push["registry_local"])
        if digest:
            raise ConfigError("digest is only used by the push transport")
        return images.import_from_dev(project.name, ref, platform.state_dir, box)

    fetched: list[tuple[str, str]] = []
    try:
        if "images" in req:
            # Several images (DESIGN-v2 §3): each comes in like the one image of a v1 request.
            imap = {}
            for name, v in sorted(req["images"].items()):
                iid, itag, claimed = fetch(v["image"], v.get("digest"))
                fetched.append((iid, itag))
                if claimed != req["sha"]:
                    raise ConfigError(f"image {name} says {claimed[:12]}, request says {req['sha'][:12]}")
                imap[name] = iid
            image_id, tag = fetched[0]
            rid = submit_release(platform, project, st, t, req["sha"], image_id, tag, imap)
        else:
            image_id, tag, claimed = fetch(req["image"], req.get("digest"))
            fetched.append((image_id, tag))
            if claimed != req["sha"]:
                raise ConfigError(f"image says {claimed[:12]}, request says {req['sha'][:12]}")
            rid = submit_release(platform, project, st, t, req["sha"], image_id, tag)
    except Exception:
        # A refused submit must not leave its images behind (transport review #2). Untag only
        # when no release refers to one; docker frees an image once nothing names or uses it.
        for iid, itag in fetched:
            if not st.db.execute("SELECT 1 FROM releases WHERE image_id=? OR images LIKE ?",
                                 (iid, f'%"{iid}"%')).fetchone():
                subprocess.run(["docker", "image", "rm", itag], capture_output=True, timeout=120)
        raise
    st.event("submitted-by", f"from {box} ({transport})", rid, project.name, t.name)
    text = plan_text(st, rid)
    tell(platform, st, rid)
    ask(platform, st, rid)
    notify(platform, f"huta: plan {rid} for {project.name}/{t.name} (from {box}, {transport}) "
                     f"waits for approval\n\napprove: sudo huta approve {rid}")
    from .notify import history_note
    note, warn = history_note(json.loads(st.release(rid)["plan"]))
    ans = {"ok": True, "release": rid, "state": S.PLANNED, "text": text,
           "next": "waiting for the owner's approval"}
    if warn:
        ans["warning"] = note       # the client repeats it last, where it can't be missed
    return ans


def cmd_intake(a):
    """Pull transport: process the shared inbox (run by huta-intake.service)."""
    from . import intake, proto
    platform, registry, st = ctx()
    pull = platform.pull
    if not pull:
        raise ConfigError("no [transport.pull] in huta.toml")
    # Scan again until the inbox is empty: a request renamed into place while this run was
    # answering the previous one would otherwise wait for the next trigger (found 2026-11-09:
    # a client's follow-up request sat for 10 minutes).
    for _round in range(20):
        seen = 0
        for item in intake.claim_requests(pull["inbox"], pull["work"], pull["owner_uid"]):
            seen += 1
            _intake_one(platform, registry, st, pull, *item)
        if not seen:
            break
    intake.prune_outbox(pull["outbox"])
    return 0


def _intake_one(platform, registry, st, pull, rid, raw, problem):
    """Answer one claimed pull request."""
    from . import intake, proto
    if rid is None:                       # an unexpected inbox entry, already removed
        st.event("inbox", problem[:300])
        return
    try:
        if problem:
            raise proto.RequestError(problem)
        req = proto.parse(raw)
        if req["id"] != rid:
            raise proto.RequestError("the request id must match its file name")
        if req["op"] in proto.PUSH_ONLY:
            raise proto.RequestError(f"{req['op']} is not available over pull")
        if not st.first_time(rid, "pull", pull["container"]):
            raise proto.RequestError("this request id was already used")
        st.event("request", f"{req['op']} {req.get('project', '')}/{req.get('target', '')} via pull",
                 None, req.get("project"), req.get("target"))
        body = handle_request(platform, registry, st, req, "pull", pull["container"])
    except Exception as e:  # noqa: BLE001 — every request gets an answer
        body = {"ok": False, "error": f"{type(e).__name__}: {e}"[:2000]}
        st.event("request-refused", f"{rid}: {body['error']}")
    # The client bundle is huta's own code (no secrets); redaction could only corrupt it.
    intake.answer(pull["outbox"], rid, {**redact_obj(body, frozenset({"bundle"})),
                                        "huta": server_rev()})
    say(f"{rid}: {'ok' if body.get('ok') else 'refused'}")


def _peer_box(sock) -> str:
    """Who is on the other end, from the kernel, not from the request (transport review #1).
    Each push box has its own system user `huta-<box>`; huta-ssh runs as that user."""
    import pwd
    import socket
    import struct
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    name = pwd.getpwuid(uid).pw_name
    if not name.startswith("huta-"):
        raise ConfigError(f"socket peer uid {uid} is not a huta push user")
    return name.removeprefix("huta-")


def cmd_push_request(a):
    """Push transport: one request per connection on huta-push.socket (stdin is the connection).
    The box is the connecting user's name; the answer is one JSON line on the socket. Anything
    else this process prints goes to the journal (StandardOutput=journal)."""
    import socket
    from . import proto
    sock = socket.socket(fileno=os.dup(0))
    sock.settimeout(60)
    body, rid, keep, crashed = {}, None, frozenset(), False
    try:
        try:
            platform, registry, st = ctx()
        except Exception as e:  # noqa: BLE001
            # The details name other projects' files, targets and hosts: they go to the journal,
            # never to a box, and the unit fails so host alerting sees it (codex boxes #1).
            say(redact(f"push request: the host config doesn't load: {type(e).__name__}: {e}"))
            crashed = True
            raise ConfigError("the host's configuration doesn't load; the operator has to fix it "
                              "(details in the host's journal)") from None
        if not platform.push:
            raise ConfigError("no [transport.push] in huta.toml")
        box = _peer_box(sock)
        from . import accounts
        if not (accounts.box_ok(box) or box in platform.push["boxes"]):
            raise ConfigError(f"box {box!r} is not allowed to push")
        raw = b""
        while len(raw) <= proto.MAX_REQUEST and (chunk := sock.recv(65536)):
            raw += chunk
        req = proto.parse(raw)
        rid = req["id"]
        if not st.first_time(rid, "push", box):
            raise proto.RequestError("this request id was already used")
        st.event("request", f"{req['op']} {req.get('project', '')}/{req.get('target', '')} via push from {box}",
                 None, req.get("project"), req.get("target"))
        body = handle_request(platform, registry, st, req, "push", box)
        if req["op"] == "credentials":
            keep = frozenset({"password"})   # the box's own login, on purpose; never logged
        elif req["op"] == "client":
            keep = frozenset({"bundle"})     # huta's own code, no secrets
    except (*REFUSALS, proto.RequestError, ValueError) as e:
        body = {"ok": False, "error": f"{type(e).__name__}: {e}"[:2000]}
    except Exception as e:  # noqa: BLE001 — every request gets an answer, even after a huta bug
        body = {"ok": False, "error": f"{type(e).__name__}: {e}"[:2000]}
        crashed = True
    out = {"v": 1, "id": rid, "ts": int(time.time()), **redact_obj(body, keep), "huta": server_rev()}
    try:
        sock.sendall((json.dumps(out) + "\n").encode())
    finally:
        sock.close()
    say(redact(f"push request {rid}: {'ok' if out.get('ok') else 'refused: ' + out.get('error', '')[:300]}"))
    # A refusal is an answered request, not a failure: exit 0 so the unit doesn't show up as
    # failed (host alerting watches failed units). Only a huta bug fails the unit.
    return 1 if crashed else 0


def approve_minutes(r) -> int:
    """How long the approval unit may run: the release's own worst case (migration, health
    wait, drain, copy) plus margin, never less than 20 minutes (DESIGN-v2 §0)."""
    if "kind" in r.keys():                     # a plan, not a release
        return 6 * 60 if r["kind"] in ("restore", "postgres") else 20
    try:
        plan = json.loads(r["plan"])
        eff = plan.get("effective") or {}
        secs = (int((eff.get("migrate") or {}).get("timeout_s") or 0)
                + int(eff.get("health_timeout") or 30)
                + int(eff.get("drain") or 0) + 15 * 60)
    except (ValueError, TypeError, KeyError):
        return 20
    return max(20, -(-secs // 60))


def cmd_press(a):
    """A Telegram button press relayed by the OpenClaw plugin (huta-press.socket; stdin is the
    connection). Only the configured relay user may connect, and the press itself must come from
    the owner, on the release's own message, with a live single-use secret (notify.take_press). Deploy runs
    as its own transient systemd unit, so the button gets its answer at once."""
    import pwd
    import socket
    import struct
    from . import notify as N
    sock = socket.socket(fileno=os.dup(0))
    sock.settimeout(10)
    toast, ok, crashed = "huta could not handle this button", False, False
    try:
        platform, registry, st = ctx()
        relay = platform.telegram.get("relay_user", "")
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        if not relay or pwd.getpwuid(struct.unpack("3i", creds)[1]).pw_name != relay:
            raise ConfigError("socket peer is not the configured relay user")
        raw = b""
        while len(raw) <= 4096 and (chunk := sock.recv(4096)):
            raw += chunk
        d = json.loads(raw)
        if not isinstance(d, dict) or d.get("v") != 1:
            raise ValueError("bad relay message")
        kind, rid = N.take_press(platform, st, d.get("data"), d.get("from_id"), d.get("chat_id"),
                                  d.get("message_id"))
        pr = st.plan_row(rid)
        r = pr or st.release(rid)
        name = f"{r['project']} → {r['target']}"
        if kind == "b":
            # ↩️: the rollback is planned (and, if one-tap clean, approved) by its own unit, so
            # the button gets its answer at once (docs/PLAN-monitoring.md §6).
            st.event("button", "↩️ pressed in Telegram", rid, r["project"], r["target"])
            subprocess.run(["systemd-run", f"--unit=huta-rollback-{rid}-{int(time.time())}",
                            "--collect", "--quiet",
                            f"--property=TimeoutStartSec={approve_minutes(r) + 10}min",
                            "/usr/local/sbin/huta", "rollback", r["project"], r["target"],
                            "--press", rid],
                           check=True, capture_output=True, timeout=20)
            toast, ok = f"Rolling {name} back…", True
        elif kind == "a":
            st.event("button", "Deploy pressed in Telegram", rid, r["project"], r["target"])
            subprocess.run(["systemd-run", f"--unit=huta-approve-{rid}", "--collect", "--quiet",
                            f"--property=TimeoutStartSec={approve_minutes(r)}min",
                            "/usr/local/sbin/huta", "approve", rid, "--via", "telegram"],
                           check=True, capture_output=True, timeout=20)
            toast, ok = f"Deploying {name}…", True
        else:
            with st.lock(r["project"], r["target"]):
                if pr:
                    st.plan_transition(rid, S.PLANNED, S.REJECTED, "rejected with the Telegram button")
                else:
                    st.transition(rid, S.PLANNED, S.REJECTED, "rejected with the Telegram button")
            retire_buttons(platform, st, rid)
            tell(platform, st, rid)
            toast, ok = f"Rejected {name}", True
    except N.PlanExpired as e:
        # Mark it, so status doesn't keep showing "planned" (site agent's review).
        try:
            pr = st.plan_row(e.rid)
            r = pr or st.release(e.rid)
            with st.lock(r["project"], r["target"]):
                if pr:
                    st.plan_transition(e.rid, S.PLANNED, S.SUPERSEDED, "approval expired (30 min)")
                else:
                    st.transition(e.rid, S.PLANNED, S.SUPERSEDED, "approval expired (30 min)")
            retire_buttons(platform, st, e.rid)
            tell(platform, st, e.rid)
        except Exception:  # noqa: BLE001
            pass
        toast = str(e)[:180]
    except (ValueError, RuntimeError) as e:   # a stale, reused or foreign button: answered
        toast = str(e)[:180]
    except Exception as e:  # noqa: BLE001 — wrong relay peer, bad config or a huta bug
        toast = f"huta: {type(e).__name__}"[:180]
        say(redact(f"press failed: {type(e).__name__}: {e}"))
        crashed = True
    try:
        sock.sendall((json.dumps({"ok": ok, "toast": toast}) + "\n").encode())
    finally:
        sock.close()
    say(redact(f"button press: {'ok' if ok else 'refused: ' + toast}"))
    # Refused presses exit 0 (an old button tapped again is normal); the unit fails, and host
    # alerting sees it, only when huta itself could not handle the press.
    return 1 if crashed else 0


def cmd_enroll(a):
    """Enroll a push box: its user, its key (pinned to --from), its registry login. Prints the
    client settings as JSON for install-client.sh --settings. Idempotent (re-run to replace the
    key)."""
    from . import accounts
    platform, registry, st = ctx()
    if not platform.push:
        raise ConfigError("no [transport.push] in huta.toml")
    if not platform.push["ssh_host"]:
        raise ConfigError("[transport.push] ssh_host is not set")
    accounts.enroll(a.box, a.pubkey, a.from_ip, platform.secrets_dir)
    users = accounts.write_htpasswd(platform.secrets_dir, ETC_REGISTRY_HTPASSWD)
    accounts.restart_registry(platform.push["registry_project"])
    st.event("enroll", f"push box {a.box} enrolled from {a.from_ip} (registry users: {', '.join(users)})")
    print(json.dumps({"box": a.box, "ssh": f"huta-{a.box}@{platform.push['ssh_host']}",
                      "port": platform.push["ssh_port"],
                      "registry": platform.push["registry_public"],
                      "host_key": accounts.host_key()}))
    return 0


def cmd_revoke(a):
    from . import accounts
    platform, registry, st = ctx()
    for line in accounts.revoke(a.box, platform.secrets_dir):
        say(line)
    accounts.write_htpasswd(platform.secrets_dir, ETC_REGISTRY_HTPASSWD)
    if platform.push:
        accounts.restart_registry(platform.push["registry_project"])
    st.event("revoke", f"push box {a.box} revoked")
    return 0


def cmd_client_bundle(a):
    """The client, for a dev box: a tar on stdout (install-client.sh + src/huta) to unpack and
    run there. Lets an agent install over the ssh access it already has, no repo needed."""
    sys.stdout.buffer.write(client_bundle())
    return 0


def server_rev() -> str:
    """The installed huta revision (install.sh writes it), told to boxes in every answer so an
    outdated client notices."""
    try:
        return Path("/opt/huta/REVISION").read_text().strip()[:60]
    except OSError:
        return "unknown"


def client_bundle() -> bytes:
    """install-client.sh + src/huta + REVISION, as tar.gz: the client this host would install."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        lib = Path("/opt/huta/lib/huta")
        for f in sorted(lib.glob("*.py")):
            tar.add(f, arcname=f"huta-client/src/huta/{f.name}")
        tar.add("/opt/huta/install-client.sh", arcname="huta-client/install-client.sh")
        rev = (server_rev() + "\n").encode()
        info = tarfile.TarInfo("huta-client/REVISION")
        info.size, info.mode, info.mtime = len(rev), 0o644, int(time.time())
        tar.addfile(info, io.BytesIO(rev))
    return buf.getvalue()


def cmd_reconcile(a):
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    with st.lock(project.name, t.name):
        recipe.reconcile(platform, st, project, t, say)
        with st.lock(project.name, R.SECRETS_LOCK, wait=300):
            settle_journal(platform, st, project)
    live = st.live(project.name, t.name)
    say(f"reconciled; live: {live['release_id'] + ' on slot ' + live['slot'] if live else 'nothing'}")
    return 0


def _runtime(platform, st, project, t) -> str:
    """The containers now (runstate.py); never fails the status it is part of."""
    from . import runstate
    try:
        return runstate.runtime_text(platform, st, project, t)
    except Exception as e:  # noqa: BLE001
        return f"now: not available ({type(e).__name__})"


def cmd_status(a):
    if a.target:
        platform, project, st = ctx(a.project)
        t = target_of(project, a.target)
        for r in st.db.execute("SELECT * FROM releases WHERE project=? AND target=?"
                               " ORDER BY created DESC LIMIT ?", (project.name, t.name, a.n)):
            say(f"{r['id']}  {time.strftime('%m-%d %H:%M', time.localtime(r['created']))}  "
                f"{r['sha'][:12]}  {r['state']:<18} slot {r['slot'] or '-'}")
        say("\n" + _runtime(platform, st, project, t))
        if t.watch:
            from . import watch as W
            w = W.status_line(st, project.name, t.name)
            if w:
                say(w)
        return 0
    st = S.Store(load_platform().state_dir)
    for r in st.releases(a.project, limit=a.n):
        when = time.strftime("%m-%d %H:%M", time.localtime(r["created"]))
        say(f"{r['id']}  {when}  {r['project']}/{r['target']}  {r['sha'][:12]}  "
            f"{r['state']:<18} slot {r['slot'] or '-'}")
    return 0


def cmd_log(a):
    st = S.Store(load_platform().state_dir)
    for e in reversed(st.events(a.release, limit=a.n)):
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"]))
        say(f"{when}  {e['release_id'] or '-':<8}  {e['kind']:<18} {e['msg']}")
    return 0


# ---------------------------------------------------------------- v1: secrets, data, jobs, egress

def cmd_secret(a):
    if a.project == "_platform":
        return _platform_secret(a)
    platform, project, st = ctx(a.project)
    if a.action == "list":
        return _cmd_secret(a, platform, project, st)
    # Never in the middle of a release applying or reverting values (grok secrets #4).
    with st.lock(project.name, R.SECRETS_LOCK, wait=300):
        return _cmd_secret(a, platform, project, st)


PLATFORM_SECRETS = ("BUGSINK_TOKEN", "HEALTHCHECKS_API_KEY", "MATOMO_TOKEN", "GITHUB_TOKEN",
                    "CLOUDFLARE_API_TOKEN")


def _platform_secret(a):
    """huta secret set|list|rm _platform NAME: the host's own tokens (secrets/_platform/), which
    no container ever gets. Names are not checked against a list: [external] tokens have the
    operator's names."""
    platform = load_platform()
    st = S.Store(platform.state_dir)
    d = platform.secrets_dir / "_platform"
    if a.action == "list":
        have = sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.startswith(".")) \
            if d.is_dir() else []
        for n in have:
            say(n)
        for n in PLATFORM_SECRETS:
            if n not in have:
                say(f"{n:<32} (not set)")
        return 0
    if a.action == "set":
        if len(a.names) != 1:
            raise ConfigError("secret set _platform <NAME>; the value comes from stdin")
        if sys.stdin.isatty():
            import getpass
            value = getpass.getpass(f"{a.names[0]} (not echoed): ")
        else:
            value = sys.stdin.read()
        R._write_secret(d, a.names[0], value.rstrip("\n"))
        st.event("secret", f"platform secret {a.names[0]} set", None, None, None)
        say(f"{a.names[0]} set")
        return 0
    if a.action == "rm":
        for n in a.names:
            p = d / n
            if R.ENV_NAME_RE.match(n) and p.is_file() and not p.is_symlink():
                R.retire(p, d, n)
                p.unlink()
                st.event("secret", f"platform secret {n} removed", None, None, None)
                say(f"{n} removed")
        return 0
    raise ConfigError("secret list|set|rm _platform")


def cmd_watch(a):
    from . import watch as W
    platform = load_platform()
    st = S.Store(platform.state_dir)
    if getattr(a, "ack", False):
        # Root on the host only: never a Telegram button or a box request (§7).
        if os.geteuid() != 0:
            raise ConfigError("huta watch --ack runs as root on the host")
        if W.row_of(st, a.release) is None:
            raise ConfigError(f"no watch for {a.release}")
        sudo = os.environ.get("SUDO_USER", "")
        who = sudo if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", sudo) else "root"
        n = W.ack(st, a.release, who)
        say(f"acknowledged {n} verdict(s) of {a.release}" if n else "nothing to acknowledge")
    say(W.text(st, a.release))
    row = W.row_of(st, a.release)
    if row is not None:
        why = W.blocks(st, row["project"], row["target"])
        say(f"unattended releases of {row['project']}/{row['target']}: "
            + (f"blocked: {why}" if why else "not blocked"))
    return 0


def cmd_snapshot(a):
    """Host side of data snapshots (docs/PLAN-snapshot.md); S0: the records and their sweep."""
    from . import snapshot as SN
    platform = load_platform()
    st = S.Store(platform.state_dir)
    if a.action == "list":
        say(SN.list_text(st, a.args[0] if a.args else None, a.args[1] if len(a.args) > 1 else None,
                         all_=a.all))
        return 0
    if a.action == "sweep":
        SN.sweep(platform, st, a.args[0] if a.args else None,
                 a.args[1] if len(a.args) > 1 else None, log=say)
        return 0
    if len(a.args) != 1:
        raise ConfigError(f"huta snapshot {a.action} <id>")
    if a.action == "show":
        say(SN.show_text(st, a.args[0]))
        return 0
    sudo = os.environ.get("SUDO_USER", "")
    who = sudo if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", sudo) else "root"
    say(f"{a.args[0]}: {SN.remove(platform, st, a.args[0], who, log=say)}")
    return 0


def cmd_monitor(a):
    from . import monitor as M
    if a.action == "tick":
        platform, registry, st = ctx()
        res = M.tick(platform, registry, st, log=say)
        if res.get("left"):
            say(f"tick out of time: {res['left']} target(s) wait for the next one")
        return 0
    if not (a.project and a.target):
        raise ConfigError(f"huta monitor {a.action} <project> <target>")
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    res: dict = {}
    if a.action == "ensure":
        # The DSN is a project secret: written under the same lock as every other secret.
        with st.lock(project.name, R.SECRETS_LOCK, wait=300):
            res = M.ensure(platform, st, project, t, log=say)
        if not res:
            say("nothing to make (no monitor on this target, or monitoring is off)")
    for line in M.status_lines(platform, st, project, t):
        say(line)
    return 0 if all(v == "ok" for v in res.values()) else 1


def _cmd_secret(a, platform, project, st):
    if a.action == "list":
        have = dict(R.secret_list(platform, project.name))
        staged = dict(R.staged_list(platform, project.name))
        wanted = sorted({n for t in project.targets.values() for n in secret_names(t)})
        for n in sorted(set(have) | set(wanted) | set(staged)):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(have[n])) if n in have else "NOT SET"
            used = "" if n in wanted else "  (not used by any target)"
            new = (f"  · new value from a box {time.strftime('%m-%d %H:%M', time.localtime(staged[n]))},"
                   " waits for an approved release" if n in staged else "")
            say(f"{n:<32} {when}{used}{new}")
        return 0
    if a.action == "set":
        if len(a.names) != 1:
            raise ConfigError("secret set <project> <NAME>; the value comes from stdin")
        if sys.stdin.isatty():
            import getpass
            value = getpass.getpass(f"{a.names[0]} (not echoed): ")
        else:
            value = sys.stdin.read()
        value = value.rstrip("\n")
        R.secret_set(platform, project.name, a.names[0], value)
        st.event("secret", f"secret {a.names[0]} set", None, project.name, None)
        say(f"{a.names[0]} set; it applies to the next release (or huta restart)")
        return 0
    if a.action == "import":
        if not a.file:
            raise ConfigError("secret import <project> NAME… --from <env file>")
        vals = R.parse_env_file(Path(a.file).read_text())
        names = a.names or sorted({n for t in project.targets.values() for n in secret_names(t)})
        missing = [n for n in names if n not in vals]
        if missing:
            raise ConfigError(f"not in {a.file}: {', '.join(missing)}")
        for n in names:
            R.secret_set(platform, project.name, n, vals[n])
        st.event("secret", f"secrets imported: {', '.join(names)}", None, project.name, None)
        say(f"imported {len(names)}: {', '.join(names)}")
        return 0
    if a.action == "rm":
        for n in a.names:
            p = R.secret_dir(platform, project.name) / n
            if R.ENV_NAME_RE.match(n) and p.is_file() and not p.is_symlink():
                R.retire(p, p.parent, n)
                p.unlink()
                say(f"{n} removed")
            if R.staged_drop(platform, project.name, n):
                say(f"{n}: staged value removed")
        return 0
    if a.action == "discard":
        # Drop a value a box sent, keep the live one. A plan that listed it can't be approved.
        for n in a.names:
            if R.staged_drop(platform, project.name, n):
                st.event("secret", f"staged value of {n} discarded", None, project.name, None)
                say(f"{n}: staged value discarded")
            else:
                say(f"{n}: nothing staged")
        return 0
    raise ConfigError("secret list|set|import|rm|discard")


def _nothing_runs(t) -> None:
    members = [c for m in recipe.slot_members(t).values() for c in m.values()]
    if any(c["running"] for c in members) or recipe.jobs_running(t):
        raise ConfigError(f"{t.project}/{t.name} is running; stop it first "
                          f"(huta stop {t.project} {t.name})")


def cmd_data(a):
    from . import data as D
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    if not t.data:
        raise ConfigError(f"{project.name}/{t.name} has no data in the host registry")
    if a.action == "status":
        u = D.usage(platform, t)
        img, where = R.data_paths(platform, t)
        say(f"{project.name}/{t.name}: {t.data['size']} at {where} ({'mounted' if u else 'NOT mounted'})"
            + (f", used {u[0] / 2**20:.0f} MiB of {u[1] / 2**20:.0f} MiB" if u else ""))
        say(f"  backup {t.data['backup']} · {D.last_backup_text(platform, t)}")
        return 0
    if a.action == "export":
        if sys.stdout.isatty():
            raise ConfigError("export writes a tar to stdout: redirect it to a file")
        # Under the lock: a backup can't replace the copy while tar reads it (code review #19).
        with st.lock(project.name, t.name, wait=300):
            D.export_tar(platform, t, a.paths, sys.stdout.buffer)
        st.event("export", f"data exported ({', '.join(a.paths) or 'all'})", None, project.name, t.name)
        return 0
    with st.lock(project.name, t.name, wait=60):
        if a.action == "ensure":
            say(D.ensure(platform, project, t))
        elif a.action == "import":
            if not a.paths or len(a.paths) != 1:
                raise ConfigError("data import <project> <target> <directory>")
            if recipe._phase_get(platform, t):
                raise ConfigError("a release was interrupted: run huta reconcile first")
            _nothing_runs(t)
            say(D.ensure(platform, project, t))
            D.import_dir(platform, project, t, Path(a.paths[0]), recipe.import_marker(platform, t), say)
            st.event("import", f"data imported from {a.paths[0]}", None, project.name, t.name)
            if t.data["backup"] != "off":
                say(f"backup: {D.backup(platform, t, st, say)}")
        else:
            raise ConfigError("data status|ensure|import|export")
    return 0


def cmd_backup(a):
    from . import data as D
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    with st.lock(project.name, t.name, wait=1800):
        recipe.reconcile(platform, st, project, t, say)   # an interrupted release first
        from . import pg as PG
        try:
            if t.data:
                say(f"backup: {D.backup(platform, t, st, say)}")
            if t.postgres and PG.container(t):
                say(f"backup: {PG.backup(platform, t, st, say)}")
        except (D.DataError, PG.PgError) as e:
            st.event("backup-fail", str(e), None, project.name, t.name)
            raise ConfigError(str(e))
    return 0


def cmd_job(a):
    from . import jobs as J
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    if a.action == "run":
        if not a.name:
            raise ConfigError("job run <project> <target> <name>")
        return J.run_job(platform, project, t, a.name, st, say)
    say(J.jobs_text(st, t, a.n))
    return 0


def cmd_logs(a):
    """The operator's view of a target's logs: the same store and gate as a box's."""
    from . import logs as L
    platform, project, st = ctx(a.project)
    if a.target in project.legacy:
        from . import observe as O
        print(O.legacy_logs(platform, project, a.target, a.since, a.until, a.grep, a.tail)["text"])
        return 0
    t = target_of(project, a.target)
    try:
        L.collect(platform, project, t, st)     # up to the moment, not the last timer run
    except RuntimeError:
        pass                                    # the collector is busy; read what is there
    res = L.query(platform, st, project, t, since=a.since, until=a.until, grep=a.grep,
                  tail=a.tail, slot=a.slot, release=a.release, job=a.job,
                  component=a.component)
    print(res["text"])
    return 0


def cmd_observe(a):
    """huta egress-log | traffic | crashes <project> <target> (the operator's view)."""
    from . import observe as O
    platform, project, st = ctx(a.project)
    if a.cmd == "crashes" and a.target in project.legacy:
        res = O.crashes(platform, project, project.legacy[a.target]["crashes"],
                        f"{project.name}/{a.target}", a.since)
    else:
        t = target_of(project, a.target, gateway_ok=a.cmd == "traffic")
        if a.cmd == "egress-log":
            res = O.egress_log(platform, project, t, a.since, a.until)
        elif a.cmd == "traffic":
            res = O.traffic(platform, project, t, a.since, a.until)
        else:
            res = O.crashes(platform, project, t.crashes, f"{project.name}/{t.name}", a.since)
    print(res["text"])
    return 0


def cmd_diag(a):
    from . import diag as DG
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    res = DG.run(platform, st, project, t, a.name, f"operator {os.environ.get('SUDO_USER') or 'root'}")
    print(res["text"])
    return 0 if res["exit"] == 0 else 1


def cmd_logs_collect(a):
    """huta-logs.timer: every target's slot logs into the store, then the size caps."""
    from . import logs as L
    platform, registry, st = ctx()
    failed = 0
    for project in registry.values():
        for t in project.targets.values():
            if t.kind == "gateway":
                continue          # no containers, no logs (its access log is Caddy's)
            try:
                L.collect(platform, project, t, st)
            except Exception as e:  # noqa: BLE001 — one target never stops the others
                failed += 1
                say(f"WARNING {project.name}/{t.name}: logs not collected: {type(e).__name__}: {e}")
    L.trim_total(platform)
    return 1 if failed else 0


def cmd_units(a):
    from . import jobs as J
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    with st.lock(project.name, t.name, wait=300):     # never against a release in progress
        live = st.live(project.name, t.name)
        app = json.loads(st.release(live["release_id"])["app_toml"]) if live else {}
        for m in J.sync_units(t, app.get("jobs", [])) or ["units unchanged"]:
            say(m)
    return 0


def cmd_egress(a):
    from . import egress as E
    platform, registry, st = ctx()
    changed = E.sync(platform, registry)
    pol = json.loads(E.POLICY.read_text()) if E.POLICY.exists() else {"targets": []}
    for t in pol["targets"]:
        say(f"{t['target']}: subnets {', '.join(t['subnets']) or '-'}; allow "
            f"{', '.join(t['allow']) or '-'}; upstreams {', '.join(t['upstreams']) or '-'}")
    say("policy updated" if changed else "policy unchanged")
    return 0


def cmd_stop(a):
    """Stop a target's slots (for a data import or maintenance). The vhost keeps routing, so the
    site answers 502 until `huta start`."""
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    with st.lock(project.name, t.name, wait=60):
        recipe.stopped_marker(platform, t).write_text("stopped by the operator\n")
        for slot, members in recipe.slot_members(t).items():
            if any(c["running"] for c in members.values()):
                recipe._stop_members(t, slot)          # every container of the slot
                say(f"stopped slot {slot}")
        st.event("stopped", "stopped by the operator", None, project.name, t.name)
        if t.watch:
            from . import watch as W
            W.close_stale(st, project.name, t.name, "stopped by the operator")
    return 0


def cmd_start(a):
    """Start the routed slot again (after huta stop), or recreate it with current env/secrets
    (huta restart): a few seconds of 502."""
    platform, project, st = ctx(a.project)
    t = target_of(project, a.target)
    with st.lock(project.name, t.name, wait=60):
        if recipe.import_marker(platform, t).exists():
            raise ConfigError("a data import was interrupted: run huta data import again first")
        recipe.stopped_marker(platform, t).unlink(missing_ok=True)
        recipe.reconcile(platform, st, project, t, say)   # an interrupted release first
        live = st.live(project.name, t.name)
        if not live:
            raise ConfigError("nothing is live")
        rel = st.release(live["release_id"])
        app = json.loads(rel["app_toml"])
        if R.data_mount(app):
            from . import data as D
            say(D.ensure(platform, project, t))
        cons = recipe.slot_containers(t)
        imgs = {s: recipe.slot_images(st, rel, app) for s in recipe.SLOTS}
        other = "b" if live["slot"] == "a" else "a"
        if other in cons and not SP.several(app):
            imgs[other] = (cons[other]["image"], cons[other]["sha"])
        recipe.write_compose(platform, t, recipe.compose_doc(platform, project, t, app, imgs))
        args = ["up", "-d", "--no-deps", "--pull", "never"]
        if a.cmd == "restart":
            args.append("--force-recreate")
        recipe.compose(platform, t, *args, *recipe.services(app, live["slot"]))
        recipe._egress_sync(platform, say)
        recipe.wait_health(t.slots[recipe.SLOTS.index(live["slot"])], SP.health(app),
                           rel["sha"], timeout=max(60, t.health_timeout))
        if SP.several(app):
            recipe._check_internal(t, app, live["slot"], rel["sha"], say)
            recipe._complete_slot(platform, st, project, t, rel, live["slot"], say)   # workers: unless-stopped
        st.event("started", f"slot {live['slot']} {a.cmd}ed by the operator", live["release_id"],
                 project.name, t.name)
        say(f"slot {live['slot']} serving {rel['sha'][:12]}")
    return 0


def _logs_args(s) -> None:
    """The options of `huta logs`, the same on the host and on a box (client.py)."""
    s.add_argument("--since", help="90s, 15m, 1h (default), 2d, or a UTC time 2026-11-25T10:00")
    s.add_argument("--until", help="same forms as --since")
    s.add_argument("--grep", help="plain substring, case-insensitive (no regex)")
    s.add_argument("--tail", type=int, help="the last N matching lines (default 500, max 5000)")
    s.add_argument("--slot", choices=["a", "b", "live", "previous"])
    s.add_argument("--release", help="a release id (8 hex)")
    s.add_argument("--job", help="a job's output instead of the slots'")
    s.add_argument("--component", help="one component's lines (targets with several)")


def main(argv=None) -> int:
    if os.geteuid() != 0:
        # One command name everywhere (D-018): as a normal user, huta is the dev-side client.
        from .client import main as client_main
        return client_main(argv)
    ap = argparse.ArgumentParser(prog="huta", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")
    s = sub.add_parser("check"); s.set_defaults(f=cmd_check)
    s = sub.add_parser("edge"); s.add_argument("project"); s.add_argument("target")
    s.add_argument("--apply", action="store_true", help="plan the changes for approval")
    s.add_argument("--now", action="store_true", help="apply at once (console; recorded)")
    s.set_defaults(f=cmd_edge)
    s = sub.add_parser("import-dev"); s.add_argument("project"); s.add_argument("ref")
    s.set_defaults(f=cmd_import_dev)
    s = sub.add_parser("submit"); s.add_argument("project"); s.add_argument("target")
    s.add_argument("sha"); s.add_argument("image")
    s.add_argument("--image", dest="images", action="append", default=[],
                   help="<name>=<image>: every image of a project with several (repeat)")
    s.set_defaults(f=cmd_submit)
    s = sub.add_parser("show"); s.add_argument("release"); s.set_defaults(f=cmd_show)
    s = sub.add_parser("approve"); s.add_argument("release")
    s.add_argument("--via", choices=["cli", "telegram", "rollback-button"], default="cli")
    s.add_argument("--confirm-data-loss", action="store_true",
                   help="required to approve a restore plan on the CLI")
    s.set_defaults(f=cmd_approve)
    s = sub.add_parser("external"); s.add_argument("project"); s.add_argument("target")
    s.add_argument("cmd", choices=["status", "rollback"], nargs="?", default="status")
    s.add_argument("name", nargs="?")
    s.set_defaults(f=cmd_external)
    s = sub.add_parser("postgres"); s.add_argument("project"); s.add_argument("target")
    s.add_argument("cmd", choices=["status", "upgrade"], nargs="?", default="status")
    s.set_defaults(f=cmd_postgres)
    s = sub.add_parser("restore"); s.add_argument("project"); s.add_argument("target")
    s.add_argument("--to", required=True, help="the release whose code the data goes back for")
    s.set_defaults(f=cmd_restore)
    s = sub.add_parser("reject"); s.add_argument("release"); s.set_defaults(f=cmd_reject)
    s = sub.add_parser("rollback"); s.add_argument("project"); s.add_argument("target")
    s.add_argument("--press", metavar="RELEASE", help=argparse.SUPPRESS)   # the ↩️ unit
    s.set_defaults(f=cmd_rollback)
    s = sub.add_parser("reconcile"); s.add_argument("project"); s.add_argument("target")
    s.set_defaults(f=cmd_reconcile)
    s = sub.add_parser("intake"); s.set_defaults(f=cmd_intake)
    s = sub.add_parser("push-request"); s.set_defaults(f=cmd_push_request)
    s = sub.add_parser("press"); s.set_defaults(f=cmd_press)
    s = sub.add_parser("enroll"); s.add_argument("box"); s.add_argument("--from", dest="from_ip", required=True)
    s.add_argument("--pubkey", required=True); s.set_defaults(f=cmd_enroll)
    s = sub.add_parser("revoke"); s.add_argument("box"); s.set_defaults(f=cmd_revoke)
    s = sub.add_parser("client-bundle"); s.set_defaults(f=cmd_client_bundle)
    s = sub.add_parser("status"); s.add_argument("project", nargs="?"); s.add_argument("target", nargs="?")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(f=cmd_status)
    s = sub.add_parser("log"); s.add_argument("release", nargs="?"); s.add_argument("-n", type=int, default=50)
    s.set_defaults(f=cmd_log)
    s = sub.add_parser("secret"); s.add_argument("action", choices=["list", "set", "import", "rm", "discard"])
    s.add_argument("project"); s.add_argument("names", nargs="*")
    s.add_argument("--from", dest="file"); s.set_defaults(f=cmd_secret)
    s = sub.add_parser("watch"); s.add_argument("release"); s.add_argument("--ack", action="store_true")
    s.set_defaults(f=cmd_watch)
    s = sub.add_parser("snapshot"); s.add_argument("action", choices=["list", "show", "rm", "sweep"])
    s.add_argument("args", nargs="*"); s.add_argument("--all", action="store_true")
    s.set_defaults(f=cmd_snapshot)
    s = sub.add_parser("monitor"); s.add_argument("action", choices=["status", "ensure", "tick"])
    s.add_argument("project", nargs="?"); s.add_argument("target", nargs="?")
    s.set_defaults(f=cmd_monitor)
    s = sub.add_parser("data"); s.add_argument("action", choices=["status", "ensure", "import", "export"])
    s.add_argument("project"); s.add_argument("target"); s.add_argument("paths", nargs="*")
    s.set_defaults(f=cmd_data)
    s = sub.add_parser("backup"); s.add_argument("project"); s.add_argument("target")
    s.set_defaults(f=cmd_backup)
    s = sub.add_parser("job"); s.add_argument("action", choices=["run", "list"])
    s.add_argument("project"); s.add_argument("target"); s.add_argument("name", nargs="?")
    s.add_argument("-n", type=int, default=20); s.set_defaults(f=cmd_job)
    s = sub.add_parser("logs"); s.add_argument("project"); s.add_argument("target")
    _logs_args(s); s.set_defaults(f=cmd_logs)
    s = sub.add_parser("logs-collect"); s.set_defaults(f=cmd_logs_collect)
    for name in ("egress-log", "traffic", "crashes"):
        s = sub.add_parser(name); s.add_argument("project"); s.add_argument("target")
        s.add_argument("--since"); s.add_argument("--until") if name != "crashes" else None
        s.set_defaults(f=cmd_observe, until=None)
    s = sub.add_parser("diag"); s.add_argument("project"); s.add_argument("target")
    s.add_argument("name"); s.set_defaults(f=cmd_diag)
    s = sub.add_parser("units"); s.add_argument("project"); s.add_argument("target")
    s.set_defaults(f=cmd_units)
    s = sub.add_parser("egress"); s.add_argument("action", choices=["sync"]); s.set_defaults(f=cmd_egress)
    for name, fn in (("stop", cmd_stop), ("start", cmd_start), ("restart", cmd_start)):
        s = sub.add_parser(name); s.add_argument("project"); s.add_argument("target")
        s.set_defaults(f=fn)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    if os.geteuid() != 0:
        print("huta must run as root", file=sys.stderr)
        return 2
    try:
        return a.f(a)
    except REFUSALS as e:
        print(redact(f"refused: {e}"), file=sys.stderr)
        return 1
