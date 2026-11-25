"""The repo side of the contract (cfg.parse_app_toml).

Two kinds of tests. The examples must parse, now and in every later version (v1 manifests stay
valid). Each planned feature is refused by name today; when a step of v2 builds one, its
refusal test here changes in the same commit, and the rest stay as they are.
"""

import unittest

from tests._util import ROOT

from huta.cfg import ConfigError, parse_app_toml

HELLO = (ROOT / "examples/hello/deploy/app.toml").read_text()


def with_lines(text: str, before_tables: str = "", after: str = "") -> str:
    """Insert top-level lines before the first [table] (TOML puts later keys into that table),
    and tables at the end."""
    head, sep, tail = text.partition("\n[")
    return head + "\n" + before_tables + sep + tail + "\n" + after


class Examples(unittest.TestCase):
    def test_examples_parse(self):
        for p in ("hello", "notes"):
            with self.subTest(project=p):
                app = parse_app_toml((ROOT / f"examples/{p}/deploy/app.toml").read_text(), p)
                self.assertEqual(app["order"], ["web"])
                self.assertEqual(list(app["components"]), ["web"])

    def test_project_must_match(self):
        with self.assertRaises(ConfigError):
            parse_app_toml(HELLO, "other")

    def test_unknown_key_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, "unknown"):
            parse_app_toml(with_lines(HELLO, before_tables="colour = \"blue\"\n"), "hello")


NOTES = (ROOT / "examples/notes/deploy/app.toml").read_text()
MIGRATING = NOTES.replace('[state]\nkind = "data"', '[state]\nkind = "data"\nschema = 4\nruns_on = [3, 4]') \
    + '\n[migrate]\nimage = "web"\ncmd = ["python", "/app/app.py", "migrate"]\ntimeout = "5m"\n'


class Migrations(unittest.TestCase):
    """[migrate] and [state] schema (DESIGN-v2 §2)."""

    def parse(self, text):
        return parse_app_toml(text, "notes")

    def test_accepted(self):
        app = self.parse(MIGRATING)
        self.assertEqual(app["state"]["schema"], 4)
        self.assertEqual(app["state"]["runs_on"], [3, 4])
        self.assertEqual(app["migrate"]["cmd"], ["python", "/app/app.py", "migrate"])
        self.assertEqual(app["migrate"]["timeout_s"], 300)

    def test_runs_on_defaults_to_schema(self):
        app = self.parse(NOTES.replace('[state]\nkind = "data"', '[state]\nkind = "data"\nschema = 2'))
        self.assertEqual(app["state"]["runs_on"], [2])

    def refused(self, text, pattern):
        with self.assertRaisesRegex(ConfigError, pattern):
            self.parse(text)

    def test_runs_on_must_include_schema(self):
        self.refused(MIGRATING.replace("runs_on = [3, 4]", "runs_on = [3]"), "includes schema")

    def test_schema_needs_data(self):
        with self.assertRaisesRegex(ConfigError, "needs kind"):
            parse_app_toml(HELLO.replace('kind = "none"', 'kind = "none"\nschema = 3'), "hello")

    def test_migrate_needs_schema(self):
        self.refused(NOTES + '\n[migrate]\nimage = "web"\ncmd = ["x"]\n', "needs \\[state\\] schema")

    def test_migrate_is_argv(self):
        self.refused(MIGRATING.replace('cmd = ["python", "/app/app.py", "migrate"]',
                                       'cmd = "python /app/app.py migrate"'), "argv")

    def test_migrate_image_must_exist(self):
        self.refused(MIGRATING.replace('[migrate]\nimage = "web"', '[migrate]\nimage = "api"'),
                     "must name one of")


class PlannedFeaturesRefused(unittest.TestCase):
    """Today's refusals. A v2 step that builds a feature replaces its test here."""

    def refused(self, text: str, pattern: str):
        with self.assertRaisesRegex(ConfigError, pattern):
            parse_app_toml(text, "hello")

    def test_schema_2(self):
        self.refused(HELLO.replace("schema = 1", "schema = 2"), "schema must be 1")

    def test_second_component(self):
        text = HELLO.replace('order = ["web"]', 'order = ["web", "worker"]')
        self.refused(with_lines(text, after='[components.worker]\nimage = "web"\nkind = "worker"\n'),
                     "exactly one component")

    def test_worker_kind(self):
        self.refused(HELLO.replace('kind = "http"', 'kind = "worker"'), 'kind = "http" only')

    def test_second_image(self):
        self.refused(with_lines(HELLO, after='[images.api]\ndockerfile = "Dockerfile.api"\n'),
                     "exactly one")

    def test_external(self):
        self.refused(with_lines(HELLO, after='[external.site]\nkind = "cloudflare-pages"\n'),
                     "not supported by huta yet")

    def test_online_migration(self):
        with self.assertRaisesRegex(ConfigError, "not supported by huta yet"):
            parse_app_toml(MIGRATING.replace('"migrate"]', '"migrate"]\nonline = true'), "notes")

    def test_postgres_state(self):
        self.refused(HELLO.replace('kind = "none"', 'kind = "postgres"'), "state.kind")


if __name__ == "__main__":
    unittest.main()
