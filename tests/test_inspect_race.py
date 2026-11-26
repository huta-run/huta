"""A container of the target removed between `docker ps` and `docker inspect` (a job of the
same target finishing) must not fail the listing."""

import json
import unittest
from unittest import mock

import tests._util  # noqa: F401  (puts src on sys.path)
from huta import recipe
from huta.cfg import Target


class InspectRace(unittest.TestCase):
    def setUp(self):
        self.t = mock.Mock(spec=Target)
        self.t.project, self.t.name = "demo", "prod"
        self.live = {"Id": "a" * 64, "Image": "img", "State": {"Running": True},
                     "Config": {"Labels": {"com.docker.compose.project": recipe.compose_name(self.t),
                                           "run.huta.slot": recipe.SLOTS[0]}}}

    def _fake(self, inspect_errors):
        calls = {"ps": 0}

        def sh(cmd, check=True, input_=None, timeout=600):
            if cmd[1] == "ps":
                calls["ps"] += 1
                return "a" * 64 + ("\n" + "b" * 64 if calls["ps"] == 1 else "")
            if cmd[1] == "inspect":
                if inspect_errors:
                    raise recipe.RecipeError(inspect_errors.pop(0))
                return json.dumps([self.live])
            raise AssertionError(cmd)
        return sh, calls

    def test_vanished_container_lists_again(self):
        sh, calls = self._fake(["docker inspect aaaa…: error: no such object: " + "b" * 64])
        with mock.patch.object(recipe, "sh", sh):
            got = recipe.target_containers(self.t)
        self.assertEqual([c["Id"] for c in got], ["a" * 64])
        self.assertEqual(calls["ps"], 2)

    def test_other_errors_still_raise(self):
        sh, _ = self._fake(["docker inspect aaaa…: Cannot connect to the Docker daemon"])
        with mock.patch.object(recipe, "sh", sh), self.assertRaises(recipe.RecipeError):
            recipe.target_containers(self.t)

    def test_gives_up_after_tries(self):
        sh, calls = self._fake(["no such object: x"] * 5)
        with mock.patch.object(recipe, "sh", sh), self.assertRaises(recipe.RecipeError):
            recipe.target_containers(self.t)
        self.assertEqual(calls["ps"], 3)


if __name__ == "__main__":
    unittest.main()
