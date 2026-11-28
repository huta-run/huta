"""notify.ask_owner: the Deploy / Reject buttons on a plan's history message survive one slow
answer from Telegram, and a press always finds its secrets."""

import io
import unittest
import urllib.error
from unittest import mock

from tests._util import app_toml
from tests.fakehost import FakeHost

from huta import notify as N


class AskOwner(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.project = self.host.registry["hello"]
        self.t = self.project.targets["prod"]
        self.rid = self.host.plan(self.project, self.t, app_toml("hello"), "1" * 40)
        self.platform = self.host.platform
        self.platform.telegram.update({"owner_id": 1, "buttons": True})
        self.st = self.host.store()
        self.st.db.execute("INSERT INTO tg_messages (release_id, chat, message_id, text)"
                           " VALUES (?, -100, 42, '')", (self.rid,))
        self.calls = []

    def tearDown(self):
        self.st.db.close()
        self.host.close()

    def ask(self, answers):
        """answers: what each editMessageReplyMarkup call does (an exception or a reply)."""
        def call(_token, method, payload, timeout=15):
            self.calls.append((method, payload))
            # The press arrives while the call is under way: its secrets must be recorded.
            self.rows_during.append(self.row())
            a = answers.pop(0)
            if isinstance(a, BaseException):
                raise a
            return a
        self.rows_during = []
        with mock.patch.object(N, "_token", lambda _p: "t"), mock.patch.object(N, "_call", call), \
                mock.patch.object(N.time, "sleep", lambda _s: None):
            N.ask_owner(self.platform, self.st, self.rid)

    def row(self):
        return self.st.db.execute("SELECT * FROM approvals WHERE release_id=?",
                                  (self.rid,)).fetchone()

    def failures(self):
        return [r["msg"] for r in self.st.db.execute(
            "SELECT msg FROM events WHERE release_id=? AND kind='notify-failed'", (self.rid,))]

    def test_one_timeout_is_retried(self):
        self.ask([TimeoutError("telegram too slow"), {"ok": True}])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][1], self.calls[1][1], "the same keyboard both times")
        self.assertIsNotNone(self.row())
        self.assertTrue(all(self.rows_during), "secrets recorded before the call")
        self.assertEqual(self.failures(), [])

    def test_not_modified_means_done(self):
        e = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b""))
        e.tg_body = '{"ok":false,"description":"Bad Request: message is not modified"}'
        self.ask([TimeoutError("lost answer"), e])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.failures(), [])

    def test_gives_up_after_tries(self):
        self.ask([TimeoutError("x")] * N.ASK_TRIES)
        self.assertEqual(len(self.calls), N.ASK_TRIES)
        self.assertEqual(self.failures(), ["telegram buttons: TimeoutError"])
        self.assertIsNone(self.row(), "forgotten, so the next try posts a fresh keyboard")

    def test_call_reads_the_error_body_in_its_thread(self):
        body = b'{"description":"Bad Request: message is not modified"}'

        def urlopen(_req, timeout):
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(body))
        with mock.patch.object(N._OPENER, "open", urlopen), \
                self.assertRaises(urllib.error.HTTPError) as cm:
            N._call("t", "editMessageReplyMarkup", {}, timeout=2)
        self.assertTrue(N._not_modified(cm.exception))

    def test_other_http_errors_are_retried_too(self):
        e = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b"message not found"))
        self.ask([e, {"ok": True}])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.failures(), [])


class Connect(unittest.TestCase):
    """One address whose connects hang can't use up the call: each gets CONNECT_TRY seconds,
    then the next is tried."""

    def test_falls_back_to_the_next_address(self):
        import socket
        tried = []

        class Sock:
            def __init__(self, family, kind, proto):
                self.family = family

            def settimeout(self, t):
                tried.append(("timeout", t))

            def connect(self, addr):
                tried.append(addr)
                if self.family == socket.AF_INET6:
                    raise TimeoutError("timed out")

            def close(self):
                pass
        infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::9", 443, 0, 0)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.9", 443))]
        conn = N._Conn("api.telegram.org", timeout=15)
        conn._context = mock.Mock(wrap_socket=lambda sock, server_hostname: ("tls", sock))
        with mock.patch.object(socket, "getaddrinfo", lambda *a: infos), \
                mock.patch.object(socket, "socket", Sock):
            conn.connect()
        self.assertEqual([x for x in tried if x[0] != "timeout"],
                         [("2001:db8::9", 443, 0, 0), ("192.0.2.9", 443)])
        self.assertEqual(tried[0], ("timeout", N.CONNECT_TRY))
        self.assertEqual(conn.sock[0], "tls")

    def test_nothing_reachable_is_not_sent(self):
        import socket

        class Sock:
            def __init__(self, *a):
                pass

            def settimeout(self, t):
                pass

            def connect(self, addr):
                raise ConnectionRefusedError()

            def close(self):
                pass
        conn = N._Conn("api.telegram.org", timeout=15)
        with mock.patch.object(socket, "getaddrinfo", lambda *a: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.9", 443))]), \
                mock.patch.object(socket, "socket", Sock), self.assertRaises(N.NotSent):
            conn.connect()


class FirstMessage(unittest.TestCase):
    """The first send is tried again only when it never left; a plan whose message or buttons
    didn't get there is retried by the tick, and the box is told."""

    def setUp(self):
        self.host = FakeHost()
        self.project = self.host.registry["hello"]
        self.t = self.project.targets["prod"]
        self.platform = self.host.platform
        self.platform.telegram.update({"owner_id": 1, "buttons": True, "history_chat": -100})
        self.st = self.host.store()
        self.rid = self.host.plan(self.project, self.t, app_toml("hello"), "1" * 40)
        self.st.db.execute("UPDATE releases SET state='planned', plan=? WHERE id=?",
                           ('{"project": "hello", "target": "prod", "sha": "' + "1" * 40 +
                            '", "class": "code-only", "changes": [], "checks": {},'
                            ' "effective": {}, "from": null}', self.rid))
        self.sent = []

    def tearDown(self):
        self.st.db.close()
        self.host.close()

    def call(self, answers):
        def call(_token, method, payload, timeout=15):
            self.sent.append(method)
            a = answers.pop(0) if answers else {"ok": True, "result": {"message_id": 7}}
            if isinstance(a, BaseException):
                raise a
            return a
        return call

    def test_unsent_is_retried_sent_is_not(self):
        with mock.patch.object(N, "_token", lambda _p: "t"), \
                mock.patch.object(N, "_call", self.call([N.NotSent("no route")])), \
                mock.patch.object(N.time, "sleep", lambda _s: None):
            self.assertTrue(N.history(self.platform, self.st, self.rid, timeout=25))
        self.assertEqual(self.sent, ["sendMessage", "sendMessage"])
        self.sent.clear()
        self.st.db.execute("DELETE FROM tg_messages")
        with mock.patch.object(N, "_token", lambda _p: "t"), \
                mock.patch.object(N, "_call", self.call([TimeoutError("telegram too slow")])):
            self.assertFalse(N.history(self.platform, self.st, self.rid, timeout=25))
        self.assertEqual(self.sent, ["sendMessage"], "may have landed: never sent twice")

    def test_tick_retries_a_plan_that_never_got_there(self):
        self.st.db.execute("UPDATE releases SET created=? WHERE id=?",
                           (__import__("time").time() - 300, self.rid))
        with mock.patch.object(N, "_token", lambda _p: "t"), \
                mock.patch.object(N, "_call", self.call([])):
            n = N.retry_pending(self.platform, self.st, __import__("time").monotonic() + 30)
        self.assertEqual(n, 1)
        self.assertEqual(self.sent, ["sendMessage", "editMessageReplyMarkup"])
        self.sent.clear()
        with mock.patch.object(N, "_token", lambda _p: "t"), \
                mock.patch.object(N, "_call", self.call([])):
            self.assertEqual(N.retry_pending(self.platform, self.st,
                                             __import__("time").monotonic() + 30), 0)
        self.assertEqual(self.sent, [], "delivered: left alone")

    def test_fresh_and_expired_plans_are_left_to_others(self):
        import time as _t
        for age in (10, 31 * 60):
            with self.subTest(age=age):
                self.st.db.execute("UPDATE releases SET created=? WHERE id=?",
                                   (_t.time() - age, self.rid))
                self.assertEqual(N.pending(self.st), [])


class Edit(unittest.TestCase):
    """History edits get a second try when Telegram is slow, within the caller's time."""

    def run_edit(self, answers, timeout):
        calls = []

        def call(_token, method, payload, timeout=15):
            calls.append(timeout)
            a = answers.pop(0)
            if isinstance(a, BaseException):
                raise a
            return a
        with mock.patch.object(N, "_call", call), mock.patch.object(N.time, "sleep", lambda s: None):
            N._edit("t", {"text": "x"}, timeout)
        return calls

    def test_slow_edit_is_tried_again(self):
        calls = self.run_edit([TimeoutError("slow"), {"ok": True}], 25)
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(calls[0], N.EDIT_TRY)

    def test_no_second_try_without_time(self):
        with self.assertRaises(TimeoutError):
            self.run_edit([TimeoutError("slow"), {"ok": True}], 2)

    def test_a_refusal_is_not_tried_again(self):
        e = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b""))
        e.tg_body = "message to edit not found"
        with self.assertRaises(urllib.error.HTTPError):
            self.run_edit([e, {"ok": True}], 25)

    def test_not_modified_is_done(self):
        e = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b""))
        e.tg_body = "Bad Request: message is not modified"
        self.assertEqual(len(self.run_edit([e], 25)), 1)


if __name__ == "__main__":
    unittest.main()
