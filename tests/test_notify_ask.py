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
        self.assertIsNotNone(self.row(), "kept: the last try may have reached Telegram")

    def test_call_reads_the_error_body_in_its_thread(self):
        body = b'{"description":"Bad Request: message is not modified"}'

        def urlopen(_req, timeout):
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(body))
        with mock.patch.object(N.urllib.request, "urlopen", urlopen), \
                self.assertRaises(urllib.error.HTTPError) as cm:
            N._call("t", "editMessageReplyMarkup", {}, timeout=2)
        self.assertTrue(N._not_modified(cm.exception))

    def test_other_http_errors_are_retried_too(self):
        e = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b"message not found"))
        self.ask([e, {"ok": True}])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.failures(), [])


if __name__ == "__main__":
    unittest.main()
