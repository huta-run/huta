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
import json
import secrets
import time
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
}


def _token(platform) -> str | None:
    t = platform.telegram
    if not t.get("token_json") or not t.get("history_chat"):
        return None
    d = json.loads(open(t["token_json"]).read())
    for k in t.get("token_keys", []):
        d = d[k]
    return d if isinstance(d, str) and d else None


def _call(token: str, method: str, payload: dict) -> dict:
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read(65536))


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
    note, warn = history_note(p)
    if warn or note.startswith("rollback"):
        lines.append(esc(note))
    for c in p.get("changes", []):
        if c.startswith("🔑"):          # a secret-bearing job: the owner must see its command
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
    return "\n".join(lines)


def history(platform, st: S.Store, rid: str, create: bool = True) -> None:
    """Send or edit this release's history message (create=False: only edit an existing one).
    Never raises."""
    try:
        token = _token(platform)
        if not token:
            return
        t = platform.telegram
        text = render(st, rid)
        row = st.db.execute("SELECT chat, message_id, text FROM tg_messages WHERE release_id=?",
                            (rid,)).fetchone()
        if row and row["text"] == text:
            return
        if row:
            _call(token, "editMessageText", {"chat_id": row["chat"], "message_id": row["message_id"],
                                             "text": text, "parse_mode": "HTML",
                                             "disable_web_page_preview": True})
            st.db.execute("UPDATE tg_messages SET text=? WHERE release_id=?", (text, rid))
            return
        if not create:
            return
        payload = {"chat_id": t["history_chat"], "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if t.get("history_thread"):
            payload["message_thread_id"] = t["history_thread"]
        res = _call(token, "sendMessage", payload)
        st.db.execute("INSERT OR REPLACE INTO tg_messages (release_id, chat, message_id, text)"
                      " VALUES (?,?,?,?)", (rid, t["history_chat"], res["result"]["message_id"], text))
    except Exception as e:  # noqa: BLE001 — telling the owner must never break a release
        try:
            st.event("notify-failed", f"telegram: {type(e).__name__}", rid)
        except Exception:  # noqa: BLE001
            pass


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


def ask_owner(platform, st: S.Store, rid: str, ttl: int = 30 * 60) -> None:
    """Put Deploy / Reject buttons on the release's history message (the configured topic), so
    there is one message per release, in one place (owner, 2026-11-09). Never raises."""
    try:
        token = _token(platform)
        owner = platform.telegram.get("owner_id")
        # Only once the relay plugin is installed: an unclaimed press would reach the chat agent
        # as text, secret included.
        if not token or not owner or platform.telegram.get("buttons") is not True:
            return
        msg = st.db.execute("SELECT chat, message_id FROM tg_messages WHERE release_id=?",
                            (rid,)).fetchone()
        if not msg:
            return
        a = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
        r = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
        expires = st.release(rid)["created"] + ttl
        _call(token, "editMessageReplyMarkup", {
            "chat_id": msg["chat"], "message_id": msg["message_id"],
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Deploy", "callback_data": f"{PREFIX}a:{a}"},
                {"text": "❌ Reject", "callback_data": f"{PREFIX}r:{r}"}]]}})
        st.db.execute("INSERT OR REPLACE INTO approvals (release_id, approve_hash, reject_hash, expires,"
                      " chat, message_id) VALUES (?,?,?,?,?,?)",
                      (rid, _h(a), _h(r), expires, msg["chat"], msg["message_id"]))
        del a, r
    except Exception as e:  # noqa: BLE001
        try:
            st.event("notify-failed", f"telegram buttons: {type(e).__name__}", rid)
        except Exception:  # noqa: BLE001
            pass


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
    """Check and consume a button press. Returns (action, release id), action "a" or "r".
    Raises ValueError with a short, safe reason otherwise."""
    owner = platform.telegram.get("owner_id")
    if not owner or from_id != owner:
        raise ValueError("only the owner can approve")
    if chat_id not in (platform.telegram.get("history_chat"), owner):
        raise ValueError("this button doesn't belong to this chat")
    if not (isinstance(data, str) and data.startswith(PREFIX) and len(data) <= 64):
        raise ValueError("not a huta button")
    kind, _, tok = data[len(PREFIX):].partition(":")
    if kind not in ("a", "r") or not (16 <= len(tok) <= 32):
        raise ValueError("not a huta button")
    h = _h(tok)
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
