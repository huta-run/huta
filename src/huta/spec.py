"""One description of a release, from any app.toml huta accepts (DESIGN-v2, compatibility).

A v1 manifest is the case with one component: it is public, it is the only image's user, and
nothing else exists. Code that needs "the component the vhost routes to" asks here instead of
reading the first entry of `order`, so manifests with several components need no second path.

Works on the parsed manifest as stored in a release row (`releases.app_toml`), including rows
written by older versions of huta.
"""

from __future__ import annotations


def public_name(app: dict) -> str:
    """The component the vhost routes to: the one marked `public = true`, else the only HTTP
    component, else the first in `order` (every v1 manifest: one component)."""
    comps = app["components"]
    marked = [n for n in app["order"] if comps[n].get("public") is True]
    if marked:
        return marked[0]
    http = [n for n in app["order"] if comps[n].get("kind", "http") == "http"]
    if len(http) == 1:
        return http[0]
    return app["order"][0]


def public(app: dict) -> dict:
    """The public component's table from the manifest."""
    return app["components"][public_name(app)]


def health(app: dict) -> str:
    return public(app).get("health", "/healthz")


def port(app: dict) -> int:
    return int(public(app).get("port", 8080))
