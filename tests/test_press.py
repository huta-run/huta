"""↩️ Roll back (docs/PLAN-monitoring.md §6, milestone 6): the button on a watched release's
history message, the press, the unit it starts, and the unattended interface (§7).

The target is hello/prod on the fake host: r1 went live, then r2; r2's watch is open. Telegram
is faked at notify._call.
"""

import argparse
import dataclasses
import json
import os
import unittest
from unittest import mock

from tests._util import app_toml
from tests.fakehost import CRASH_EXIT, FakeHost

from huta import cli, notify as N, recipe, store as S, watch as W

SHA1, SHA2 = "1" * 40, "2" * 40
OWNER, CHAT = 1001, -100500


class Base(unittest.TestCase):
    project_name, target_name = "hello", "prod"

    def setUp(self):
        self.host = FakeHost()
        self.project = self.host.registry[self.project_name]
        t = self.project.targets[self.target_name]
        self.t = dataclasses.replace(t, watch={"window": 900, "new_issues": 1, "events": 10,
                                                "factor": 3.0, "on_degraded": "notify"})
        self.project.targets[self.t.name] = self.t
        self.host.platform = dataclasses.replace(
            self.host.platform,
            monitoring={"prefix": "huta", "healthchecks": None, "matomo": None, "bugsink": None,
                        "watch": {"window": 900, "button_ttl": 7200}},
            telegram={**self.host.platform.telegram, "owner_id": OWNER, "buttons": True,
                      "history_chat": CHAT, "history_thread": None})
        self.platform = self.host.platform
        app = app_toml(self.project_name)
        self.r1 = self.host.plan(self.project, self.t, app, SHA1)
        self.assertEqual(self.host.release(self.project, self.t, self.r1), S.LIVE)
        self.r2 = self.host.plan(self.project, self.t, app, SHA2)
        self.assertEqual(self.host.release(self.project, self.t, self.r2), S.LIVE)
        self.sent, self.next_id = [], 500
        self.units = []
        self.stack = [
            mock.patch.object(N, "_token", lambda _p: "tok"),
            mock.patch.object(N, "_call", self.fake_call),
            mock.patch.object(W, "_local_sample", lambda *a, **k: None),
            mock.patch.object(cli, "ctx", self.ctx),
            mock.patch.object(cli, "say", lambda _m: None),
            mock.patch.object(cli, "plan_release", self.fake_plan_release),
            mock.patch.object(cli, "GitHub", lambda platform: None),
            mock.patch.object(cli, "print_plan", lambda st, rid: None),
            mock.patch.object(cli, "_gates", lambda *a: None),
            mock.patch.object(cli, "current_cert", lambda platform, t: self.host.cert),
            mock.patch.object(cli.Cloudflare, "ip_ranges", lambda: []),
            mock.patch.object(recipe, "prune_images", lambda *a: []),
        ]
        for m in self.stack:
            m.start()
        self.patched = self.host.patched()
        self.patched.__enter__()
        self.st = self.host.store()
        live = self.st.live(self.project.name, self.t.name)
        W.open_watch(self.platform, self.st, self.project, self.t, self.r2, live["since"])
        self.since = live["since"]

    def tearDown(self):
        self.st.db.close()
        self.patched.__exit__(None, None, None)
        for m in self.stack:
            m.stop()
        self.host.close()

    # -------------------------------------------------------- fakes

    def ctx(self, name=None):
        return (self.platform, self.host.registry[name] if name else self.host.registry,
                self.host.store())

    def fake_call(self, _token, method, payload, timeout=15):
        self.sent.append((method, json.loads(json.dumps(payload))))
        if method == "sendMessage":
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": True}

    def fake_plan_release(self, platform, project, st, t, sha, image_id, tag, gh, note="",
                          images=None):
        """A plan as plan_release makes it for a rollback of the same code-only app."""
        src = next(r for r in st.releases(project.name, limit=50) if r["sha"] == sha)
        app = json.loads(src["app_toml"])
        live = st.live(project.name, t.name)
        plan = {"class": "code-only", "changes": [], "note": note,
                "planned_against_live": live["release_id"] if live else None,
                "effective": recipe.effective(platform, project, t, app),
                "history": {"status": "behind", "ahead": 0, "behind": 1}}
        return st.new_release(project=project.name, target=t.name, sha=sha, image_id=image_id,
                              image_tag=tag, app_toml=src["app_toml"], plan=plan,
                              plan_hash="0" * 64, built_by="test", images=st.images(src))

    # -------------------------------------------------------- helpers

    def tell(self, rid=None):
        N.history(self.platform, self.st, rid or self.r2)

    def keyboard(self):
        """The callback_data of the last ↩️ keyboard posted, or None."""
        for method, p in reversed(self.sent):
            if method in ("editMessageText", "sendMessage", "editMessageReplyMarkup"):
                rows = (p.get("reply_markup") or {}).get("inline_keyboard") or []
                return rows[0][0]["callback_data"] if rows else None
        return None

    def message(self, rid=None):
        return self.st.db.execute("SELECT chat, message_id FROM tg_messages WHERE release_id=?",
                                  (rid or self.r2,)).fetchone()

    def press(self, data, from_id=OWNER, chat=CHAT, message_id=None):
        mid = self.message()["message_id"] if message_id is None else message_id
        return N.take_press(self.platform, self.st, data, from_id, chat, mid)

    def row(self):
        return W.row_of(self.st, self.r2)

    def state(self, rid):
        return self.st.release(rid)["state"]


class Button(Base):
    def test_made_with_the_history_message(self):
        self.tell()
        data = self.keyboard()
        self.assertTrue(data and data.startswith("huta:b:"), self.sent)
        row = self.row()
        self.assertEqual(row["button_target"], self.r1)
        self.assertEqual(row["message_id"], self.message()["message_id"])
        self.assertEqual(row["button_expires"], row["until"] + 7200)
        self.assertNotIn(data.split(":")[2], json.dumps([tuple(row)]), "only the hash is kept")

    def test_every_edit_posts_a_fresh_token(self):
        self.tell()
        old = self.keyboard()
        self.st.set_state(self.r2, S.LIVE, msg="something new for the message")
        self.st.db.execute("UPDATE tg_messages SET text='' WHERE release_id=?", (self.r2,))
        self.tell()
        new = self.keyboard()
        self.assertNotEqual(old, new)
        with self.assertRaisesRegex(ValueError, "no longer valid"):
            self.press(old)
        self.assertEqual(self.press(new), ("b", self.r2))

    def test_no_button_without_telegram_buttons(self):
        self.platform.telegram["buttons"] = False
        self.tell()
        self.assertIsNone(self.keyboard())

    def test_no_button_after_ttl(self):
        self.st.db.execute("UPDATE watches SET until=? WHERE release_id=?",
                           (self.since - 7200 - 1, self.r2))
        self.tell()
        self.assertIsNone(self.keyboard())

    def test_no_button_on_a_rollback(self):
        self.st.db.execute("UPDATE releases SET plan=? WHERE id=?",
                           (json.dumps({"note": f"rollback to {self.r1}"}), self.r2))
        self.tell()
        self.assertIsNone(self.keyboard())

    def test_no_button_without_an_earlier_release(self):
        self.st.db.execute("UPDATE releases SET state=? WHERE id=?", (S.SUPERSEDED, self.r1))
        self.tell()
        self.assertIsNone(self.keyboard())

    def test_planned_release_keeps_its_deploy_keyboard(self):
        """A plan's history edits carry no ↩️ (its Deploy / Reject buttons are separate)."""
        r3 = self.fake_plan_release(self.platform, self.project, self.st, self.t, SHA1, "i", "t", None)
        self.tell(r3)
        self.assertIsNone(self.keyboard())

    def test_retired_when_no_longer_due(self):
        self.tell()
        self.st.db.execute("UPDATE watches SET until=? WHERE release_id=?",
                           (self.since - 7200 - 1, self.r2))
        import time
        W.retire_buttons(self.platform, self.st, time.monotonic() + 30)
        self.assertIsNone(self.row()["button_hash"])
        self.assertEqual(self.sent[-1][0], "editMessageReplyMarkup")
        self.assertEqual(self.sent[-1][1]["reply_markup"], {"inline_keyboard": []})


class Press(Base):
    def setUp(self):
        super().setUp()
        self.tell()
        self.data = self.keyboard()

    def test_owner_press_is_taken_once(self):
        self.assertEqual(self.press(self.data), ("b", self.r2))
        self.assertEqual(self.row()["action"], W.PRESSED)
        with self.assertRaisesRegex(ValueError, "no longer valid"):
            self.press(self.data)

    def test_forged_token(self):
        with self.assertRaisesRegex(ValueError, "no longer valid"):
            self.press("huta:b:" + "A" * 22)
        self.assertIsNotNone(self.row()["button_hash"])

    def test_stranger_and_foreign_chat_touch_nothing(self):
        with self.assertRaisesRegex(ValueError, "only the owner"):
            self.press(self.data, from_id=OWNER + 1)
        with self.assertRaisesRegex(ValueError, "doesn't belong"):
            self.press(self.data, chat=CHAT - 1)
        self.assertIsNotNone(self.row()["button_hash"], "not burned by a stranger")
        self.assertEqual(self.press(self.data), ("b", self.r2))

    def test_other_message_burns_it(self):
        with self.assertRaisesRegex(ValueError, "doesn't belong"):
            self.press(self.data, message_id=self.message()["message_id"] + 1)
        self.assertIsNone(self.row()["button_hash"])
        self.assertIsNone(self.row()["action"])
        with self.assertRaisesRegex(ValueError, "no longer valid"):
            self.press(self.data)

    def test_expired(self):
        self.st.db.execute("UPDATE watches SET button_expires=1 WHERE release_id=?", (self.r2,))
        with self.assertRaisesRegex(ValueError, "expired"):
            self.press(self.data)
        self.assertIsNone(self.row()["action"])

    def test_spent_episode_gets_no_new_button(self):
        self.press(self.data)
        self.st.db.execute("UPDATE tg_messages SET text='' WHERE release_id=?", (self.r2,))
        self.tell()
        self.assertIsNone(self.keyboard())

    def test_cmd_press_starts_the_rollback_unit(self):
        """A ↩️ press never falls through to Reject; it starts `huta rollback --press`."""
        import socket
        runs = []
        a, b = socket.socketpair()
        a.sendall(json.dumps({"v": 1, "data": self.data, "from_id": OWNER, "chat_id": CHAT,
                              "message_id": self.message()["message_id"]}).encode())
        a.shutdown(socket.SHUT_WR)
        self.platform.telegram["relay_user"] = __import__("pwd").getpwuid(os.getuid()).pw_name
        saved = os.dup(0)
        os.dup2(b.fileno(), 0)
        try:
            with mock.patch.object(cli.subprocess, "run",
                                   lambda argv, **k: runs.append(argv)):
                self.assertEqual(cli.cmd_press(argparse.Namespace()), 0)
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            b.close()
        answer = json.loads(a.recv(4096))
        a.close()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][-5:], ["rollback", "hello", "prod", "--press", self.r2])
        self.assertEqual(self.state(self.r2), S.LIVE, "not rejected")


class Unit(Base):
    """`huta rollback <p> <t> --press <release>`, the unit a press starts."""

    def setUp(self):
        super().setUp()
        self.tell()
        self.press(self.keyboard())

    def run_unit(self):
        return cli.cmd_rollback(argparse.Namespace(project=self.project.name,
                                                   target=self.t.name, press=self.r2))

    def newest(self):
        return self.st.releases(self.project.name, limit=1)[0]

    def test_stateless_target_rolls_back_in_one_press(self):
        self.assertEqual(self.run_unit(), 0)
        r3 = self.newest()
        self.assertEqual(r3["state"], S.LIVE)
        self.assertEqual(r3["sha"], SHA1)
        self.assertEqual(self.row()["action"], W.PLANNED_PREFIX + r3["id"])
        msgs = [e["msg"] for e in self.st.events(r3["id"], limit=20)]
        self.assertIn("approved with ↩️ in Telegram", msgs)
        self.assertEqual(self.host.running(self.t), {"a": SHA1})

    def test_rolled_away_release_is_never_picked_again(self):
        self.run_unit()
        live = self.st.live(self.project.name, self.t.name)
        cur = self.st.release(live["release_id"])
        to = W.rollback_target(self.st, self.project.name, self.t.name, cur)
        self.assertIsNone(to, "r2 was rolled away from by ↩️; r1 is live")

    def test_not_clean_waits_for_its_buttons(self):
        with mock.patch.object(W, "one_tap_clean", lambda t, rel: (False, "one-writer data")):
            self.assertEqual(self.run_unit(), 0)
        r3 = self.newest()
        self.assertEqual(r3["state"], S.PLANNED)
        self.assertEqual(self.state(self.r2), S.LIVE)

    def test_refused_when_no_longer_live(self):
        r3 = self.host.plan(self.project, self.t, app_toml("hello"), "3" * 40)
        self.assertEqual(self.host.release(self.project, self.t, r3), S.LIVE)
        n = len(self.st.releases(self.project.name, limit=50))
        self.assertEqual(self.run_unit(), 1)
        self.assertTrue(self.row()["action"].startswith(W.REFUSED_PREFIX))
        self.assertEqual(len(self.st.releases(self.project.name, limit=50)), n, "nothing planned")
        self.assertIn("sendMessage", [m for m, _ in self.sent])

    def test_refused_when_the_old_code_cant_run(self):
        def refuse(*a, **k):
            raise cli.ConfigError("the data's schema is newer than this release runs on")
        with mock.patch.object(cli, "plan_release", refuse):
            self.assertEqual(self.run_unit(), 1)
        self.assertIn("schema", self.row()["action"])
        reply = [p for m, p in self.sent if m == "sendMessage"][-1]
        self.assertIn("huta restore", reply["text"])

    def test_second_run_finds_no_press(self):
        self.run_unit()
        with self.assertRaisesRegex(cli.ConfigError, "no ↩️ press"):
            self.run_unit()


class OneTapClean(unittest.TestCase):
    def rel(self, **plan):
        base = {"class": "code-only", "changes": [], "history": {"status": "behind"}}
        return {"plan": json.dumps({**base, **plan})}

    def t(self, data=None, postgres=None):
        return mock.Mock(data=data, postgres=postgres)

    def test_rules(self):
        ok = W.one_tap_clean
        self.assertEqual(ok(self.t(), self.rel()), (True, ""))
        self.assertTrue(ok(self.t({"concurrent": True}), self.rel())[0])
        for t, rel in ((self.t({"concurrent": False}), self.rel()),
                       (self.t(postgres={"x": 1}), self.rel()),
                       (self.t(), self.rel(**{"class": "infra", "changes": ["changed: env"]})),
                       (self.t(), self.rel(migration={"run": True})),
                       (self.t(), self.rel(staged_secrets={"K": "f"})),
                       (self.t(), self.rel(external={"site": {}})),
                       (self.t(), self.rel(history={"status": "unknown"})),
                       (self.t(), self.rel(history=None))):
            with self.subTest(plan=rel["plan"]):
                self.assertFalse(ok(t, rel)[0])


class Unattended(Base):
    """watch.blocks, --ack and monitor.ready (§7)."""

    def blocks(self):
        return W.blocks(self.st, self.project.name, self.t.name)

    def test_open_watch_blocks(self):
        self.assertIn("still open", self.blocks())

    def test_failure_blocks_until_acked_and_a_newer_ok_does_not_clear_it(self):
        self.st.db.execute("UPDATE watches SET state=? WHERE release_id=?", (W.DEGRADED, self.r2))
        self.assertIn("not acknowledged", self.blocks())
        r3 = self.host.plan(self.project, self.t, app_toml("hello"), "3" * 40)
        self.host.release(self.project, self.t, r3)
        live = self.st.live(self.project.name, self.t.name)
        W.open_watch(self.platform, self.st, self.project, self.t, r3, live["since"])
        self.st.db.execute("UPDATE watches SET state=? WHERE release_id=?", (W.OK, r3))
        self.assertIn(self.r2, self.blocks())
        self.assertEqual(W.ack(self.st, self.r2, "owner"), 1)
        self.assertIsNone(self.blocks())
        self.assertEqual(W.ack(self.st, self.r2, "owner"), 0)
        row = self.row()
        self.assertEqual(row["acked_by"], "owner")

    def test_a_press_needs_an_ack_too(self):
        self.st.db.execute("UPDATE watches SET state=?, action=? WHERE release_id=?",
                           (W.OK, W.PRESSED, self.r2))
        self.assertIn("↩️", self.blocks())

    def test_live_episode_without_a_row_blocks(self):
        self.st.db.execute("DELETE FROM watches")
        self.assertIn("no watch yet", self.blocks())

    def test_ack_is_root_only(self):
        with mock.patch.object(cli.os, "geteuid", lambda: 1000), \
                mock.patch.object(cli, "load_platform", lambda: self.platform), \
                self.assertRaisesRegex(cli.ConfigError, "root"):
            cli.cmd_watch(argparse.Namespace(release=self.r2, ack=True))

    def test_ready(self):
        from huta import monitor as M
        self.assertEqual(M.ready(self.platform, self.st, self.project, self.t)[0], True)
        t = dataclasses.replace(self.t, monitor={"errors": True})
        ok, why = M.ready(self.platform, self.st, self.project, t)
        self.assertFalse(ok)
        self.assertIn("errors", why)


class WatchRollback(Base):
    """Crash: the ↩️ unit dies before planning, after planning, and during the approval."""

    def setUp(self):
        super().setUp()
        self.tell()
        self.press(self.keyboard())

    def child(self, fn):
        pid = os.fork()
        if pid == 0:
            try:
                fn()
            finally:
                os._exit(CRASH_EXIT)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), CRASH_EXIT)

    def unit(self):
        cli.cmd_rollback(argparse.Namespace(project=self.project.name, target=self.t.name,
                                            press=self.r2))

    def check(self):
        self.host.reconcile(self.project, self.t)
        st = self.host.store()
        try:
            live = st.live(self.project.name, self.t.name)
            stuck = [r["id"] for r in st.releases(self.project.name, limit=50)
                     if r["state"] in (S.RUNNING, S.APPROVED)]
            self.assertEqual(stuck, [])
            self.assertEqual(self.host.running(self.t),
                             {live["slot"]: st.release(live["release_id"])["sha"]})
        finally:
            st.db.close()

    def test_dies_before_planning(self):
        def die(*a, **k):
            os._exit(CRASH_EXIT)
        with mock.patch.object(cli, "_plan_rollback", die):
            self.child(self.unit)
        self.check()
        self.assertEqual(self.row()["action"], W.PRESSED, "the press is spent")
        self.assertEqual(self.state(self.r2), S.LIVE)
        # The CLI still works.
        self.assertEqual(cli.cmd_rollback(argparse.Namespace(
            project=self.project.name, target=self.t.name, press=None)), 0)

    def test_dies_after_planning(self):
        real = cli._plan_rollback

        def plan_then_die(*a, **k):
            real(*a, **k)
            os._exit(CRASH_EXIT)
        with mock.patch.object(cli, "_plan_rollback", plan_then_die):
            self.child(self.unit)
        self.check()
        r3 = self.st.releases(self.project.name, limit=1)[0]
        self.assertEqual(r3["state"], S.PLANNED, "a plan the owner can approve")
        self.assertEqual(self.state(self.r2), S.LIVE)

    def test_dies_during_the_approval(self):
        base = len(self.host.ops())
        code, err = self.host.in_child(self.unit, None)
        self.assertEqual(code, 0, err)
        steps = len(self.host.ops()) - base
        self.assertTrue(steps)
        for k in range(1, steps + 1):
            with self.subTest(step=k):
                self.tearDown()
                self.setUp()
                base = len(self.host.ops())
                code, err = self.host.in_child(self.unit, base + k)
                self.assertEqual(code, CRASH_EXIT, err)
                self.check()
                live = self.st.live(self.project.name, self.t.name)
                self.assertIn(self.st.release(live["release_id"])["sha"], (SHA1, SHA2))


if __name__ == "__main__":
    unittest.main()
