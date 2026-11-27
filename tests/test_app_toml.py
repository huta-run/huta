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

    def test_state_is_optional(self):
        app = parse_app_toml(HELLO.replace('[state]\nkind = "none"\n', ""), "hello")
        self.assertNotIn("state", app)

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

    def test_online_and_check(self):
        app = self.parse(MIGRATING.replace('"migrate"]', '"migrate"]\nonline = true\n'
                                           'check = ["python", "/app/app.py", "schema"]'))
        self.assertTrue(app["migrate"]["online"])
        self.assertEqual(app["migrate"]["check"], ["python", "/app/app.py", "schema"])
        self.refused(MIGRATING.replace('"migrate"]', '"migrate"]\ncheck = "schema"'), "argv")

    def test_migrate_image_must_exist(self):
        self.refused(MIGRATING.replace('[migrate]\nimage = "web"', '[migrate]\nimage = "api"'),
                     "must name one of")


MULTI = """schema = 1
project = "shop"
order = ["web", "api", "worker"]

[images.web]
dockerfile = "web/Dockerfile"
[images.api]
dockerfile = "api/Dockerfile"

[components.web]
image = "web"
public = true
port = 8080
[components.api]
image = "api"
port = 8081
health = "/ready"
data = "/data"
[components.worker]
image = "api"
kind = "worker"
cmd = ["python", "-m", "worker"]
data = "/data"

[state]
kind = "data"
"""


class SeveralComponents(unittest.TestCase):
    """Several containers per target (DESIGN-v2 §3)."""

    def parse(self, text):
        return parse_app_toml(text, "shop")

    def refused(self, text, pattern):
        with self.assertRaisesRegex(ConfigError, pattern):
            self.parse(text)

    def test_accepted(self):
        app = self.parse(MULTI)
        self.assertEqual(app["order"], ["web", "api", "worker"])
        self.assertEqual(len(app["images"]), 2)

    def test_one_public_needed(self):
        self.refused(MULTI.replace("public = true\n", ""), "exactly one HTTP component is public")
        self.refused(MULTI.replace('port = 8081', 'port = 8081\npublic = true'),
                     "exactly one HTTP component is public")

    def test_only_http_component_needs_no_mark(self):
        text = MULTI.replace("public = true\n", "").replace(
            'image = "api"\nport = 8081\nhealth = "/ready"', 'image = "api"\nkind = "worker"')
        self.assertEqual(self.parse(text)["components"]["api"]["kind"], "worker")

    def test_worker_serves_nothing(self):
        self.refused(MULTI.replace('kind = "worker"', 'kind = "worker"\nport = 9000'), "worker")

    def test_cmd_is_argv(self):
        self.refused(MULTI.replace('cmd = ["python", "-m", "worker"]', 'cmd = "python -m worker"'),
                     "argv")

    def test_reserved_name(self):
        self.refused(MULTI.replace('"worker"]', '"postgres"]').replace(
            "[components.worker]", "[components.postgres]"), "reserved")

    def test_data_needs_a_writer(self):
        self.refused(MULTI.replace('data = "/data"\n', ""), "needs components")


class Postgres(unittest.TestCase):
    """[state] kind = "postgres" (DESIGN-v2 §4)."""

    def test_accepted_with_schema_and_migrate(self):
        text = HELLO.replace('kind = "none"', 'kind = "postgres"\nschema = 1') + \
            '\n[migrate]\nimage = "web"\ncmd = ["alembic", "upgrade", "head"]\n'
        app = parse_app_toml(text, "hello")
        self.assertEqual(app["state"], {"kind": "postgres", "schema": 1, "runs_on": [1]})

    def test_no_data_volume_with_it(self):
        text = NOTES.replace('[state]\nkind = "data"', '[state]\nkind = "postgres"')
        with self.assertRaisesRegex(ConfigError, "not supported yet"):
            parse_app_toml(text, "notes")


class External(unittest.TestCase):
    """[external.<name>] (DESIGN-v2 §5)."""

    def parse(self, extra):
        return parse_app_toml(with_lines(HELLO, after=extra), "hello")

    def test_pages(self):
        app = self.parse('[external.site]\nkind = "cloudflare-pages"\nimage = "web"\ndir = "/out/"\n')
        self.assertEqual(app["external"]["site"], {"kind": "cloudflare-pages", "image": "web",
                                                   "dir": "/out"})

    def test_worker_needs_main(self):
        with self.assertRaisesRegex(ConfigError, "needs main"):
            self.parse('[external.edge]\nkind = "cloudflare-worker"\nimage = "web"\ndir = "/w"\n')
        app = self.parse('[external.edge]\nkind = "cloudflare-worker"\nimage = "web"\n'
                         'dir = "/w"\nmain = "index.js"\n')
        self.assertEqual(app["external"]["edge"]["main"], "index.js")

    def test_refused(self):
        for extra in ('[external.site]\nkind = "netlify"\nimage = "web"\ndir = "/out"\n',
                      '[external.site]\nkind = "cloudflare-pages"\nimage = "nope"\ndir = "/out"\n',
                      '[external.site]\nkind = "cloudflare-pages"\nimage = "web"\ndir = "out"\n',
                      '[external.site]\nkind = "cloudflare-pages"\nimage = "web"\ndir = "/etc"\n',
                      '[external.site]\nkind = "cloudflare-pages"\nimage = "web"\ndir = "/o"\nmain = "x.js"\n'):
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                self.parse(extra)


class Monitor(unittest.TestCase):
    """[monitor] env (docs/PLAN-monitoring.md, milestone 2)."""

    def test_accepted(self):
        app = parse_app_toml(with_lines(HELLO, after='[monitor]\nenv = ["sentry", "matomo"]\n'),
                             "hello")
        self.assertEqual(app["monitor"], {"env": ["matomo", "sentry"]})
        self.assertNotIn("monitor", parse_app_toml(HELLO, "hello"))

    def test_refused(self):
        for bad in ('[monitor]\nenv = ["sentry", "sentry"]\n', '[monitor]\nenv = ["datadog"]\n',
                    '[monitor]\nenv = "sentry"\n', '[monitor]\nchecks = true\n'):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                parse_app_toml(with_lines(HELLO, after=bad), "hello")


SNAP = '[[snapshot]]\nname = "db"\ncmd = ["python", "/app/app.py", "sanitize"]\n'
NOTES_BARE = NOTES.split("\n# A data snapshot")[0] + "\n"     # the example without its own


class Snapshot(unittest.TestCase):
    """[[snapshot]] (docs/PLAN-snapshot.md, milestone S1)."""

    def parse(self, extra, text=NOTES_BARE, project="notes"):
        return parse_app_toml(with_lines(text, after=extra), project)

    def test_the_example_declares_one(self):
        app = parse_app_toml(NOTES, "notes")
        self.assertEqual([x["name"] for x in app["snapshot"]], ["hits"])
        self.assertEqual(app["snapshot"][0]["check"], ["python", "/app/app.py", "verify"])

    def test_accepted_with_defaults(self):
        app = self.parse(SNAP)
        self.assertEqual(app["snapshot"], [{"name": "db", "cmd": ["python", "/app/app.py",
                                                                  "sanitize"],
                                            "timeout": 600, "component": "web"}])
        self.assertNotIn("snapshot", parse_app_toml(NOTES_BARE, "notes"))

    def test_check_timeout_component(self):
        app = self.parse(SNAP + 'check = ["python", "/app/app.py", "verify"]\ntimeout = "30m"\n'
                         'component = "web"\n')
        self.assertEqual(app["snapshot"][0]["check"], ["python", "/app/app.py", "verify"])
        self.assertEqual(app["snapshot"][0]["timeout"], 1800)

    def test_component_defaults_to_the_data_mount(self):
        app = parse_app_toml(MULTI + "\n" + SNAP, "shop")
        self.assertEqual(app["snapshot"][0]["component"], "api")

    def test_postgres_defaults_to_the_public_component(self):
        text = HELLO.replace('kind = "none"', 'kind = "postgres"')
        app = parse_app_toml(text + "\n" + SNAP, "hello")
        self.assertEqual(app["snapshot"][0]["component"], "web")

    def test_refused(self):
        bad = {
            "no state": (HELLO, "hello", SNAP, "needs \\[state\\]"),
            "shell": (NOTES_BARE, "notes", SNAP.replace('["python", "/app/app.py", "sanitize"]',
                                                   '"python sanitize"'), "argv"),
            "check shell": (NOTES_BARE, "notes", SNAP + 'check = "verify"\n', "argv"),
            "timeout": (NOTES_BARE, "notes", SNAP + 'timeout = "31m"\n', "out of range"),
            "unknown key": (NOTES_BARE, "notes", SNAP + 'network = true\n', "keys are"),
            "duplicate": (NOTES_BARE, "notes", SNAP + SNAP, "unique"),
            "bad name": (NOTES_BARE, "notes", SNAP.replace('"db"', '"DB"'), "unique lowercase"),
            "no component": (NOTES_BARE, "notes", SNAP + 'component = "api"\n', "no component"),
            "component type": (NOTES_BARE, "notes", SNAP + 'component = []\n', "no component"),
            "six": (NOTES_BARE, "notes", "".join(SNAP.replace('"db"', f'"s{i}"') for i in range(6)),
                    "1 to 5"),
            "overlap": (NOTES_BARE.replace('data = "/data"', 'data = "/snapshot/db"'), "notes", SNAP,
                        "overlaps"),
            "overlap above": (NOTES_BARE.replace('data = "/data"', 'data = "/snapshot"'), "notes",
                              SNAP, "overlaps"),
        }
        for why, (text, project, extra, pattern) in bad.items():
            with self.subTest(why=why), self.assertRaisesRegex(ConfigError, pattern):
                self.parse(extra, text, project)

    def test_worker_without_data_refused(self):
        text = MULTI + "\n" + SNAP + 'component = "web"\n'
        with self.assertRaisesRegex(ConfigError, "mounts no data"):
            parse_app_toml(text, "shop")


class PlannedFeaturesRefused(unittest.TestCase):
    """Today's refusals. A v2 step that builds a feature replaces its test here."""

    def refused(self, text: str, pattern: str):
        with self.assertRaisesRegex(ConfigError, pattern):
            parse_app_toml(text, "hello")

    def test_schema_2(self):
        self.refused(HELLO.replace("schema = 1", "schema = 2"), "schema must be 1")




if __name__ == "__main__":
    unittest.main()
