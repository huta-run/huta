"""Telegram history: one message per release, edited as the release moves (planned → approved
→ live / not activated / needs a human / rejected / expired). Sent with the bot token named in
huta.toml; never fails the caller, and the token is never logged.

  [telegram]
  history_chat = -100…           # where history goes
  history_thread = 135           # forum topic (optional)
  token_json = "/path/to/a.json" # a JSON file that holds the bot token …
  token_keys = ["a", "b", "c"]   # … under these keys
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.client
import json
import secrets
import ssl
import time
import urllib.error
import urllib.request

from . import store as S

PREFIX = "huta:"

STATE_LINE = {
    S.PLANNED: "⏳ waiting for approval",
    S.APPROVED: "🔄 approved, deploying…",
    S.RUNNING: "🔄 deploying…",
    S.LIVE: "✅ live",
    S.RETIRED: "↩️ replaced",
    S.NOTHING_ACTIVATED: "⚠️ not activated",
    S.NEEDS_HUMAN: "🆘 needs a human",
    S.ROLLED_BACK: "↩️ reverted automatically",
    S.REJECTED: "✖️ rejected",
    S.SUPERSEDED: "⌛ expired or superseded",
    "applied": "✅ applied",
    "failed": "🆘 failed, needs a human",
}
PLAN_TITLE = {"edge": "🌐 edge change", "restore": "♻️ DATA RESTORE",
              "postgres": "🐘 postgres upgrade", "external": "☁️ Cloudflare rollback"}


def created(st: S.Store, rid: str) -> float:
    """When a release or a plan was made (its approval window starts there)."""
    p = st.plan_row(rid)
    return p["created"] if p else st.release(rid)["created"]


def render_plan(st: S.Store, pid: str) -> str:
    """A plan that is not a release (edge, restore): what it changes, warnings first."""
    r = st.plan_row(pid)
    p = json.loads(r["plan"])
    esc = html.escape
    lines = [f"{PLAN_TITLE.get(r['kind'], r['kind'])} <b>{esc(r['project'])} → "
             f"{esc(r['target'])}</b>  ·  <code>{pid}</code>"]
    body = p.get("lines", [])
    lines += [esc(x[:400]) for x in body if x.startswith("⚠️")]
    lines += [esc(x[:300]) for x in body if not x.startswith("⚠️")][:12]
    state = r["state"]
    head = STATE_LINE.get(state, state)
    if state == S.PLANNED:
        expires = time.strftime("%H:%M", time.localtime(r["created"] + 30 * 60))
        flag = " --confirm-data-loss" if r["kind"] == "restore" else ""
        lines += [f"{head} · until {expires}", f"<code>sudo huta approve {pid}{flag}</code>"]
    else:
        why = next((ev["msg"] for ev in st.events(pid, limit=5) if ev["kind"] == state), "")
        lines += [head + (f" — {esc(why[:300])}" if why and why != state else "")]
    return "\n".join(lines)


def _token(platform) -> str | None:
    t = platform.telegram
    if not t.get("token_json") or not t.get("history_chat"):
        return None
    d = json.loads(open(t["token_json"]).read())
    for k in t.get("token_keys", []):
        d = d[k]
    return d if isinstance(d, str) and d else None


CONNECT_TRY = 3       # seconds for one address's TCP connect before the next one is tried


class NotSent(OSError):
    """No connection to any of Telegram's addresses: the request never left, so even a
    sendMessage may be tried again without posting twice."""


class _Conn(http.client.HTTPSConnection):
    """Connects to each of the host's addresses in turn with a short timeout, so one address
    whose connects hang (seen with Telegram's IPv6 now and then) can't use up the whole call
    before the next family gets a turn. The request's own timeout still bounds each read."""

    def connect(self):
        import socket
        last: Exception | None = None
        for family, kind, proto, _n, addr in socket.getaddrinfo(self.host, self.port, 0,
                                                                 socket.SOCK_STREAM):
            sock = socket.socket(family, kind, proto)
            sock.settimeout(min(CONNECT_TRY, self.timeout or CONNECT_TRY))
            try:
                sock.connect(addr)
            except OSError as e:
                sock.close()
                last = e
                continue
            sock.settimeout(self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            return
        raise NotSent(f"no connection to {self.host}: {type(last).__name__ if last else 'no address'}")


class _Handler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_Conn, req, context=ssl.create_default_context())


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Handler)


def _not_sent(e: BaseException) -> bool:
    return isinstance(e, NotSent) or (isinstance(e, urllib.error.URLError)
                                      and isinstance(getattr(e, "reason", None), NotSent))


def _call(token: str, method: str, payload: dict, timeout: float = 15) -> dict:
    """One Bot API call, the whole exchange within `timeout` (the request runs in a thread
    abandoned at the deadline: socket timeouts only bound each wait)."""
    import threading
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    box: list = []

    def run():
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                box.append(("ok", json.loads(r.read(65536))))
        except BaseException as e:  # noqa: BLE001 — handed to the caller
            if isinstance(e, urllib.error.HTTPError):
                try:             # the error's body is read here, within the deadline
                    e.tg_body = e.read(4096).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    e.tg_body = ""
            box.append(("err", e))
    th = threading.Thread(target=run, daemon=True, name="huta-telegram")
    th.start()
    th.join(timeout)
    if not box:
        raise TimeoutError("telegram too slow")
    if box[0][0] == "err":
        raise box[0][1]
    return box[0][1]


SNAPSHOT_TEXT = 1800      # characters of 📦 lines in one message (Telegram allows 4096)


def history_note(p: dict) -> tuple[str, bool]:
    """How the plan's commit relates to the live one, in words, and whether it is a warning.
    ("", False) for a first release or a plan made before huta compared commits."""
    h, frm = p.get("history"), p.get("from")
    if not h or not frm:
        return "", False
    live = frm["sha"][:12]
    n = lambda k: f"{h[k]} commit{'' if h[k] == 1 else 's'}"   # noqa: E731
    rel = {
        "ahead": f"{n('ahead') if 'ahead' in h else '?'} ahead of the live {live}",
        "identical": f"the same commit as the live {live} (a redeploy)",
        "behind": f"goes back {n('behind') if 'behind' in h else '?'} from the live {live}",
        "diverged": f"drops {n('behind') if 'behind' in h else '?'} of the live {live} "
                    f"and adds {n('ahead') if 'ahead' in h else '?'}",
        "unrelated": (f"can't compare: the live {live} is not in the repo (it moved, or its "
                      "history was replaced)" if h.get("live_in_repo") is False else
                      f"can't compare: shares no history with the live {live}"),
    }.get(h.get("status"), f"couldn't compare with the live {live}: {h.get('error', '?')}")
    # A rollback is the owner's explicit choice of an older image: say what it is, whatever the
    # comparison shows (rolling back twice can be "ahead"), with no warning (codex #5).
    if (p.get("note") or "").startswith("rollback"):
        return f"rollback: {rel}", False
    if h.get("status") in ("ahead", "identical"):
        return rel, False
    if h.get("status") == "behind":
        return f"⚠️ DOWNGRADE: {rel}", True
    if h.get("status") == "diverged":
        return f"⚠️ NOT BASED ON LIVE: {rel}", True   # e.g. a commit from an older branch
    return f"⚠️ {rel}", True


def render(st: S.Store, rid: str) -> str:
    if st.plan_row(rid):
        return render_plan(st, rid)
    r = st.release(rid)
    p = json.loads(r["plan"])
    e = p.get("effective", {})
    frm = p.get("from")
    esc = html.escape
    src = next((ev["msg"] for ev in st.events(rid, limit=50) if ev["kind"] == "submitted-by"), "")
    lines = [
        f"🚀 <b>{esc(r['project'])} → {esc(r['target'])}</b>  ·  <code>{rid}</code>",
        (f"<code>{frm['sha'][:7]}</code> → " if frm else "first release · ")
        + f"<code>{r['sha'][:7]}</code>  ·  {esc(p.get('class', '?'))}",
        " · ".join(x for x in [
            esc(src) if src else "",
            "main ✓",
            "CI ✓" if p.get("checks", {}).get("ci") == "green" else "CI not required",
        ] if x),
    ]
    # Data first: a migration (or anything else marked ⚠️) leads, before secrets (DESIGN-v2 §0).
    for c in p.get("changes", []):
        if c.startswith("⚠️"):
            lines.append(esc(c[:400]))
    note, warn = history_note(p)
    if warn or note.startswith("rollback"):
        lines.append(esc(note))
    budget = SNAPSHOT_TEXT       # all 📦 lines together, so the message stays under 4096
    for c in p.get("changes", []):
        # Secret-bearing jobs, Cloudflare publishing, snapshot commands: always shown.
        if c.startswith("📦"):         # commands in full when they fit, else said where
            head = c.split("): ", 1)[0] + "): "      # name, new/changed, digest: always whole
            room = max(len(head) + 20, min(900, budget))
            budget -= min(len(c), room)
            lines.append(esc(c) if len(c) <= room else
                         esc(c[:room - 20]) + f"… (whole: <code>sudo huta show {esc(rid)}</code>)")
        elif c.startswith(("🔑", "☁️")):
            lines.append(esc(c[:300]))
    state = r["state"]
    head = STATE_LINE.get(state, state)
    # Who approved, so the owner sees at once that a plan was approved elsewhere (the CLI) and
    # needs no tap.
    how = next((ev["msg"] for ev in st.events(rid, limit=50) if ev["kind"] == S.APPROVED), "")
    if how and state != S.PLANNED:
        lines.append("👤 " + esc(how.replace("approved on the CLI by uid 0", "approved on the host")))
    if state == S.PLANNED:
        expires = time.strftime("%H:%M", time.localtime(r["created"] + 30 * 60))
        lines += [f"{head} · until {expires}", f"<code>sudo huta approve {rid}</code>"]
    elif state == S.LIVE:
        hosts = ", ".join(f'<a href="https://{esc(h)}/">{esc(h)}</a>' for h in e.get("hosts", []))
        lines += [f"{head} on {hosts}  ·  slot {esc(r['slot'] or '-')}  ·  "
                  f"{time.strftime('%H:%M', time.localtime(r['updated']))}"]
    elif state in (S.APPROVED, S.RUNNING):
        lines += [head]
    else:
        why = next((ev["msg"] for ev in st.events(rid, limit=5) if ev["kind"] == state), "")
        lines += [head + (f" — {esc(why[:300])}" if why and why != state else "")]
    w = _watch_line(st, rid)
    if w:
        lines.append(esc(w))
    return "\n".join(lines)


def _watch_line(st: S.Store, rid: str) -> str:
    """The release's watch window (docs/PLAN-monitoring.md §6), if it has one."""
    try:
        from . import watch as W
        return W.line(W.row_of(st, rid))
    except Exception:  # noqa: BLE001 — a database from before watches, or none yet
        return ""


def watch_reply(st: S.Store, row) -> str:
    """The reply a degraded or unknown verdict sends (an edit doesn't notify)."""
    from . import watch as W
    esc = html.escape
    head = {"degraded": "⚠️ degraded after release", "unknown": "❔ couldn't tell after release"}
    return (f"{head.get(row['state'], row['state'])} <b>{esc(row['project'])} → "
            f"{esc(row['target'])}</b> · <code>{esc(row['release_id'])}</code>\n"
            f"{esc(W.line(row))}\n<code>sudo huta watch {esc(row['release_id'])}</code>")


def reply(platform, st: S.Store, rid: str, text: str, timeout: float = 15) -> bool:
    """A reply to the release's history message, which notifies. True when sent, or when there
    is nothing to send it with (no Telegram configured, no history message); never raises."""
    try:
        token = _token(platform)
        if not token:
            return True                  # no Telegram: nothing to send it with
        row = st.db.execute("SELECT chat, message_id FROM tg_messages WHERE release_id=?",
                            (rid,)).fetchone()
        if not row:
            return False                 # the history message must exist first (history())
        payload = {"chat_id": row["chat"], "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True,
                   "reply_parameters": {"message_id": row["message_id"],
                                        "allow_sending_without_reply": True}}
        if row["chat"] == platform.telegram.get("history_chat") \
                and platform.telegram.get("history_thread"):
            payload["message_thread_id"] = platform.telegram["history_thread"]
        _call(token, "sendMessage", payload, timeout)
        return True
    except Exception as e:  # noqa: BLE001
        try:
            st.event("notify-failed", f"telegram reply: {type(e).__name__}", rid)
        except Exception:  # noqa: BLE001
            pass
        return False


def history(platform, st: S.Store, rid: str, create: bool = True, timeout: float = 15) -> bool:
    """Send or edit this release's history message (create=False: only edit an existing one).
    Never raises. True when the message now says what render() says, or there is nothing to
    send it with (no Telegram configured); False when sending or editing failed."""
    try:
        token = _token(platform)
        if not token:
            return True
        t = platform.telegram
        text = render(st, rid)
        row = st.db.execute("SELECT chat, message_id, text FROM tg_messages WHERE release_id=?",
                            (rid,)).fetchone()
        if row and row["text"] == text:
            return True
        # An edit without reply_markup takes the keyboard off: a live release whose ↩️ is due
        # gets a fresh one with every edit (docs/PLAN-monitoring.md §6).
        due = _rollback_due(platform, st, rid)
        if row:
            payload = {"chat_id": row["chat"], "message_id": row["message_id"], "text": text,
                       "parse_mode": "HTML", "disable_web_page_preview": True}
            if due:
                payload["reply_markup"] = _rollback_markup(platform, st, due, row["chat"],
                                                           row["message_id"])
            _edit(token, payload, timeout)
            st.db.execute("UPDATE tg_messages SET text=? WHERE release_id=?", (text, rid))
            return True
        if not create:
            return True
        payload = {"chat_id": t["history_chat"], "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if t.get("history_thread"):
            payload["message_thread_id"] = t["history_thread"]
        if due:             # the message id is known only after sending: recorded right after
            payload["reply_markup"] = _rollback_markup(platform, st, due, t["history_chat"], None)
        # A send is tried again only when it never left (no connection): one that timed out
        # after sending may have landed, and a second would post the message twice.
        end = time.monotonic() + timeout
        while True:
            try:
                res = _call(token, "sendMessage", payload, max(1.0, end - time.monotonic()))
                break
            except Exception as e:  # noqa: BLE001
                if not _not_sent(e) or end - time.monotonic() < 3:
                    raise
                time.sleep(1)
        mid = res["result"]["message_id"]
        st.db.execute("INSERT OR REPLACE INTO tg_messages (release_id, chat, message_id, text)"
                      " VALUES (?,?,?,?)", (rid, t["history_chat"], mid, text))
        if due:
            st.db.execute("UPDATE watches SET message_id=? WHERE release_id=? AND since=? AND"
                          " message_id IS NULL", (mid, rid, due[0]["since"]))
        return True
    except Exception as e:  # noqa: BLE001 — telling the owner must never break a release
        try:
            st.event("notify-failed", f"telegram: {type(e).__name__}", rid)
        except Exception:  # noqa: BLE001
            pass
        return False


EDIT_TRY = 10          # seconds for one try of an edit; a retry needs at least 3 s left


def _transient(e: Exception) -> bool:
    """Worth trying again: no answer in time, a network error, Telegram busy (429, 5xx)."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429 or e.code >= 500
    return isinstance(e, (TimeoutError, OSError))


def _edit(token: str, payload: dict, timeout: float) -> None:
    """editMessageText within `timeout`, tried again after a slow or failed try while time is
    left (an edit is idempotent: a repeat that changes nothing is "not modified", done). A
    sendMessage is never repeated: that would post the message twice."""
    end = time.monotonic() + timeout
    while True:
        try:
            _call(token, "editMessageText", payload, min(EDIT_TRY, end - time.monotonic()))
            return
        except Exception as e:  # noqa: BLE001
            if _not_modified(e):
                return
            if not _transient(e) or end - time.monotonic() < 3:
                raise
            time.sleep(min(1.0, end - time.monotonic() - 2))


def _rollback_due(platform, st: S.Store, rid: str):
    try:
        from . import watch as W
        return W.button_due(platform, st, rid)
    except Exception:  # noqa: BLE001 — a database from before watches: no ↩️
        return None


def _rollback_markup(platform, st: S.Store, due, chat: int, message_id: int | None) -> dict:
    from . import watch as W
    tok = W.make_button(platform, st, due, chat, message_id)
    return {"inline_keyboard": [[{"text": "↩️ Roll back", "callback_data": f"{PREFIX}b:{tok}"}]]}


def clear_keyboard(platform, chat: int, message_id: int, timeout: float = 10) -> bool:
    """Take the keyboard off a message. Never raises."""
    try:
        token = _token(platform)
        if not token or timeout < 1:
            return False
        _call(token, "editMessageReplyMarkup", {"chat_id": chat, "message_id": message_id,
                                                "reply_markup": {"inline_keyboard": []}}, timeout)
        return True
    except Exception as e:  # noqa: BLE001
        return _not_modified(e)


# ------------------------------------------------------------------ one-tap approval
#
# The buttons sit on the release's history message. They carry the secret: callback_data =
# "huta:a:<22 chars>" (Deploy) or "huta:r:<…>" (Reject), 128 random bits each, made here and
# stored only as SHA-256 hashes. The press reaches the chat gateway's poller, whose small huta
# plugin (no LLM) relays it to /run/huta/press.sock with the presser's id.
# Known limit: huta trusts that id. The strings are in the message's keyboard and the gateway
# may store updates, so anything running as relay_user can forge an owner press (see
# docs/ARCHITECTURE.md). A database reader only sees hashes.

def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


ASK_TRIES, ASK_TIMEOUT, ASK_PAUSE = 3, 10, 2


def _not_modified(e: Exception) -> bool:
    """Telegram's 400 for an edit that changes nothing: the keyboard is already there. The body
    was read by _call, inside its deadline."""
    return isinstance(e, urllib.error.HTTPError) and e.code == 400 \
        and "message is not modified" in getattr(e, "tg_body", "")


def buttons_on(platform) -> bool:
    # Only once the relay plugin is installed: an unclaimed press would reach the chat agent as
    # text, secret included.
    return bool(_token(platform) and platform.telegram.get("owner_id")
                and platform.telegram.get("buttons") is True)


def ask_owner(platform, st: S.Store, rid: str, ttl: int = 30 * 60, tries: int | None = None,
              timeout: float | None = None) -> bool:
    """Put Deploy / Reject buttons on the release's history message (the configured topic), so
    there is one message per release, in one place (owner, 2026-11-09). Never raises. True when
    the buttons are on the message, or there are none to put (buttons off); False when they
    could not be posted: the secrets are forgotten then, and `retry_pending` tries again."""
    tries = ASK_TRIES if tries is None else tries
    timeout = ASK_TIMEOUT if timeout is None else timeout
    try:
        token = _token(platform)
        if not buttons_on(platform):
            return True
        msg = st.db.execute("SELECT chat, message_id FROM tg_messages WHERE release_id=?",
                            (rid,)).fetchone()
        if not msg:
            return False
        a = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
        r = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
        plan = st.plan_row(rid)
        yes = {"edge": "✅ Apply", "restore": "♻️ Restore (loses data)",
               "postgres": "✅ Upgrade (site down meanwhile)"}.get(
            plan["kind"], "✅ Apply") if plan else "✅ Deploy"
        expires = created(st, rid) + ttl
        # Recorded before the call: an answer lost on the way back (the keyboard is on the
        # message) or a press faster than our write must still find its secrets.
        st.db.execute("INSERT OR REPLACE INTO approvals (release_id, approve_hash, reject_hash, expires,"
                      " chat, message_id) VALUES (?,?,?,?,?,?)",
                      (rid, _h(a), _h(r), expires, msg["chat"], msg["message_id"]))
        markup = {"chat_id": msg["chat"], "message_id": msg["message_id"],
                  "reply_markup": {"inline_keyboard": [[
                      {"text": yes, "callback_data": f"{PREFIX}a:{a}"},
                      {"text": "❌ Reject", "callback_data": f"{PREFIX}r:{r}"}]]}}
        del a, r
        # One slow answer from Telegram left a plan with no buttons: try a few times, the same
        # keyboard each time (a repeat that finds it already there is "not modified": done).
        for attempt in range(tries):
            try:
                _call(token, "editMessageReplyMarkup", markup, timeout=timeout)
                break
            except Exception as e:  # noqa: BLE001
                if _not_modified(e):
                    break
                if attempt == tries - 1:
                    raise
                time.sleep(ASK_PAUSE)
        return True
    except Exception as e:  # noqa: BLE001
        try:
            # A keyboard that may not be there: its secrets go, so the next try posts a fresh
            # one (a keyboard that did land answers "no longer valid" until then).
            st.db.execute("DELETE FROM approvals WHERE release_id=?", (rid,))
            st.event("notify-failed", f"telegram buttons: {type(e).__name__}", rid)
        except Exception:  # noqa: BLE001
            pass
        return False


PENDING_AFTER = 90       # seconds a new plan is left to its own submit before the tick helps


def pending(st: S.Store, now: float | None = None) -> list[str]:
    """Plans still waiting, not expired, older than PENDING_AFTER (their submit is done)."""
    now = time.time() if now is None else now
    lo, hi = now - 30 * 60, now - PENDING_AFTER
    out = [r["id"] for r in st.db.execute(
        "SELECT id FROM releases WHERE state=? AND created BETWEEN ? AND ?", (S.PLANNED, lo, hi))]
    out += [r["id"] for r in st.db.execute(
        "SELECT id FROM plans WHERE state=? AND created BETWEEN ? AND ?", (S.PLANNED, lo, hi))]
    return out


def retry_pending(platform, st: S.Store, end: float, log=lambda _m: None) -> int:
    """A waiting plan whose message or buttons never reached Telegram gets them now (run every
    minute by the monitoring tick). Bounded by `end`. Returns how many it delivered."""
    if not _token(platform):
        return 0
    n = 0
    for rid in pending(st):
        left = end - time.monotonic()
        if left < 4:
            break
        has_msg = st.db.execute("SELECT 1 FROM tg_messages WHERE release_id=?",
                                (rid,)).fetchone()
        asked = st.db.execute("SELECT 1 FROM approvals WHERE release_id=?", (rid,)).fetchone()
        if has_msg and (asked or not buttons_on(platform)):
            continue
        per = min(10.0, (left - 1) / 2)
        if not has_msg and not history(platform, st, rid, create=True, timeout=per):
            continue
        if ask_owner(platform, st, rid, tries=1, timeout=min(10.0, end - time.monotonic() - 1)):
            st.event("notified", "approval message delivered by the retry", rid)
            log(f"plan {rid}: message delivered on a retry")
            n += 1
    return n


def close_ask(platform, st: S.Store, rid: str) -> None:
    """Retire the buttons (pressed, decided on the CLI, or expired): forget the secrets and take
    the keyboard off the message. Idempotent; never raises."""
    try:
        row = st.db.execute("SELECT chat, message_id FROM approvals WHERE release_id=?", (rid,)).fetchone()
        st.db.execute("DELETE FROM approvals WHERE release_id=?", (rid,))
        # A press already consumed the approvals row: the keyboard is still on the history
        # message (grok review of 674b48c, #5).
        row = row or st.db.execute("SELECT chat, message_id FROM tg_messages WHERE release_id=?",
                                   (rid,)).fetchone()
        token = _token(platform)
        if row and token and row["message_id"]:
            _call(token, "editMessageReplyMarkup", {"chat_id": row["chat"],
                                                    "message_id": row["message_id"],
                                                    "reply_markup": {"inline_keyboard": []}})
    except Exception:  # noqa: BLE001
        pass


def refresh_ask(platform, st: S.Store, rid: str) -> None:
    """Older releases had a separate DM message; keep it in step until it's gone. Never raises."""
    try:
        row = st.db.execute("SELECT chat, message_id FROM tg_dm WHERE release_id=?", (rid,)).fetchone()
        token = _token(platform)
        if row and token:
            _call(token, "editMessageText", {"chat_id": row["chat"], "message_id": row["message_id"],
                                             "text": render(st, rid), "parse_mode": "HTML",
                                             "disable_web_page_preview": True})
    except Exception:  # noqa: BLE001
        pass


def take_press(platform, st: S.Store, data: str, from_id: int, chat_id: int,
               message_id: int | None = None) -> tuple[str, str]:
    """Check and consume a button press. Returns (action, release id), action "a" (Deploy),
    "r" (Reject) or "b" (↩️ Roll back: the watch row is marked pressed). Raises ValueError with
    a short, safe reason otherwise."""
    owner = platform.telegram.get("owner_id")
    if not owner or from_id != owner:
        raise ValueError("only the owner can approve")
    if chat_id not in (platform.telegram.get("history_chat"), owner):
        raise ValueError("this button doesn't belong to this chat")
    if not (isinstance(data, str) and data.startswith(PREFIX) and len(data) <= 64):
        raise ValueError("not a huta button")
    kind, _, tok = data[len(PREFIX):].partition(":")
    if kind not in ("a", "r", "b") or not (16 <= len(tok) <= 32):
        raise ValueError("not a huta button")
    h = _h(tok)
    if kind == "b":                 # its own table: never the approvals' Reject
        from . import watch as W
        return "b", W.take_button(st, h, chat_id, message_id)
    col = "approve_hash" if kind == "a" else "reject_hash"
    with st.db:
        st.db.execute("BEGIN IMMEDIATE")
        rows = st.db.execute(f"SELECT release_id, {col} AS h, expires, chat, message_id FROM approvals").fetchall()
        match = next((x for x in rows if hmac.compare_digest(x["h"], h)), None)
        if not match:
            raise ValueError("this button is no longer valid")
        st.db.execute("DELETE FROM approvals WHERE release_id=?", (match["release_id"],))
        if match["chat"] != chat_id or match["message_id"] != message_id:
            raise ValueError("this button doesn't belong to this chat")
    if time.time() > match["expires"]:
        raise PlanExpired(match["release_id"])
    return kind, match["release_id"]


class PlanExpired(ValueError):
    """A button pressed after the plan's 30 minutes: the caller marks the release superseded."""
    def __init__(self, rid: str):
        super().__init__("this plan expired; submit again")
        self.rid = rid
