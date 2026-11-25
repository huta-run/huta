"""The request a dev box sends (proto.parse). An old client's requests must stay valid."""

import json
import unittest

import tests._util  # noqa: F401  (puts src/ on the path)

from huta.proto import RequestError, parse

SUBMIT = {"v": 1, "id": "0123456789abcdef", "op": "submit", "project": "hello",
          "target": "prod", "sha": "0123456789abcdef0123456789abcdef01234567",
          "image": "huta-build/hello:0123456789ab"}


def raw(d: dict) -> bytes:
    return json.dumps(d).encode()


class Requests(unittest.TestCase):
    def test_v1_submit(self):
        self.assertEqual(parse(raw(SUBMIT))["op"], "submit")
        self.assertEqual(parse(raw({**SUBMIT, "digest": "sha256:" + "0" * 64}))["op"], "submit")

    def test_status(self):
        parse(raw({"v": 1, "id": "0123456789abcdef", "op": "status", "project": "hello",
                   "target": "prod"}))

    def test_version_2_refused(self):
        with self.assertRaisesRegex(RequestError, "v = 1"):
            parse(raw({**SUBMIT, "v": 2}))

    def test_unexpected_field_refused(self):
        with self.assertRaisesRegex(RequestError, "unexpected"):
            parse(raw({**SUBMIT, "colour": "blue"}))

    def test_images_map(self):
        """Several images (DESIGN-v2 §3): optional, inside v = 1."""
        imgs = {"web": {"image": "huta-build/hello:0123456789ab"},
                "api": {"image": "huta-build/hello/api:0123456789ab",
                        "digest": "sha256:" + "1" * 64}}
        self.assertEqual(parse(raw({**SUBMIT, "images": imgs}))["images"], imgs)
        for bad in ({"api": {"image": "huta-build/other/api:0123456789ab"}},
                    {"api": {"image": "huta-build/hello/web:0123456789ab"}},   # name mismatch
                    {"API": {"image": "huta-build/hello/API:0123456789ab"}},
                    {"api": "huta-build/hello/api:0123456789ab"},
                    {"api": {"image": "huta-build/hello/api:0123456789ab", "x": 1}},
                    {}):
            with self.subTest(images=bad), self.assertRaises(RequestError):
                parse(raw({**SUBMIT, "images": bad}))

    def test_image_must_belong_to_project(self):
        with self.assertRaisesRegex(RequestError, "bad image"):
            parse(raw({**SUBMIT, "image": "huta-build/other:0123456789ab"}))

    def test_duplicate_keys_refused(self):
        with self.assertRaisesRegex(RequestError, "duplicate"):
            parse(b'{"v": 1, "v": 1}')


if __name__ == "__main__":
    unittest.main()
