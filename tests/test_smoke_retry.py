"""Public [[smoke]] checks after the switch: no answer at all (a resolver still caching NXDOMAIN
for a name just created, a dropped connection) is tried again within a short deadline; a wrong
answer is never retried, and checks against the new slot (loopback) are tried once."""

import io
import socket
import types
import unittest
import urllib.error
from unittest import mock

from huta import recipe

CHECK = {"path": "/", "status": 200}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


class Resp:
    def __init__(self, code=200):
        self.code, self.headers, self.fp = code, mock.Mock(get_all=lambda n: None), None

    def close(self):
        pass


class SmokeRetry(unittest.TestCase):
    def run_smoke(self, answers, retry_for):
        clock = Clock()
        calls = []

        def open_(req, timeout):
            calls.append(clock.now)
            a = answers.pop(0) if answers else answers_tail[0]
            if isinstance(a, BaseException):
                raise a
            return a
        answers_tail = [answers[-1]]
        fake_time = types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep,
                                          time=lambda: clock.now)
        with mock.patch.object(recipe, "_OPENER", mock.Mock(open=open_)), \
                mock.patch.object(recipe, "time", fake_time):
            out = recipe.smoke("https://www.example.org", [CHECK], None, retry_for=retry_for)
        return out, calls

    def nxdomain(self):
        return urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))

    def test_resolver_error_is_tried_again(self):
        out, calls = self.run_smoke([self.nxdomain(), self.nxdomain(), Resp(200)], 30)
        self.assertEqual(out, [])
        self.assertEqual(len(calls), 3)

    def test_gives_up_at_the_deadline(self):
        out, calls = self.run_smoke([self.nxdomain()], 30)
        self.assertEqual(len(out), 1)
        self.assertIn("Name or service not known", out[0])
        self.assertLessEqual(calls[-1] - calls[0], 30)
        self.assertGreater(len(calls), 5)

    def test_wrong_status_is_not_retried(self):
        err = urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b""))
        err.headers = mock.Mock(get_all=lambda n: None)
        out, calls = self.run_smoke([err, Resp(200)], 30)
        self.assertEqual(out, ["/: status 500, expected 200"])
        self.assertEqual(len(calls), 1)

    def test_connection_refused_is_retried(self):
        out, calls = self.run_smoke([urllib.error.URLError(ConnectionRefusedError()), Resp(200)],
                                    30)
        self.assertEqual((out, len(calls)), ([], 2))

    def test_no_retry_by_default(self):
        out, calls = self.run_smoke([self.nxdomain(), Resp(200)], 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
