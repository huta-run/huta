"""Clients for GitHub and Cloudflare. Tokens are read from files and never logged."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .cfg import Platform, zone_for

UA = "huta/0.1 (+https://huta.run)"


class ExtError(Exception):
    pass


def _req(method: str, url: str, headers: dict, body=None, raw=False, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    h = {"User-Agent": UA, **headers}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            raise ExtError(f"{method} {urllib.parse.urlsplit(url).path}: HTTP {e.code}")
        try:
            return json.loads(payload)
        except ValueError:
            raise ExtError(f"{method} {urllib.parse.urlsplit(url).path}: HTTP {e.code}")
    return payload.decode() if raw else json.loads(payload)


# ---------------------------------------------------------------- GitHub (read-only token)

class GitHub:
    API = "https://api.github.com"

    def __init__(self, platform: Platform):
        self._h = {"Authorization": "Bearer " + platform.secret("GITHUB_TOKEN"),
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}

    def full_sha(self, repo: str, ref: str) -> str:
        r = _req("GET", f"{self.API}/repos/{repo}/commits/{urllib.parse.quote(ref)}", self._h)
        if "sha" not in r:
            raise ExtError(f"{repo}: no commit {ref!r}")
        return r["sha"]

    def on_main(self, repo: str, sha: str) -> bool:
        """True if sha is origin/main or an ancestor of it (fetched now, never cached)."""
        r = _req("GET", f"{self.API}/repos/{repo}/compare/{sha}...main", self._h)
        if "status" not in r:
            raise ExtError(f"{repo}: compare {sha[:12]}...main failed")
        return r["status"] in ("ahead", "identical")

    def compare(self, repo: str, base: str, head: str) -> dict:
        """How head relates to base: {"status": ahead|behind|diverged|identical, "ahead": n,
        "behind": n} (ahead: commits head has that base lacks; behind: the reverse)."""
        r = _req("GET", f"{self.API}/repos/{repo}/compare/{base}...{head}", self._h)
        bad = ExtError(f"{repo}: compare {base[:12]}...{head[:12]} failed")
        if not isinstance(r, dict):
            raise bad
        if r.get("status") not in ("ahead", "behind", "diverged", "identical"):
            # GitHub answers 404 both when base isn't in the repo (it moved, or its history was
            # replaced) and when the two commits share no history. Say which, in words.
            if not self.has_commit(repo, base):
                return {"status": "unrelated", "live_in_repo": False}
            if "no common ancestor" in str(r.get("message", "")).lower():
                return {"status": "unrelated", "live_in_repo": True}
            raise bad
        st, a, b = r.get("status"), r.get("ahead_by"), r.get("behind_by")
        if st not in ("ahead", "behind", "diverged", "identical") or not all(
                type(x) is int and 0 <= x < 10**7 for x in (a, b)):
            raise bad
        # The counts must agree with the status, or the answer reassures by mistake (codex #3).
        if (a > 0, b > 0) != {"ahead": (True, False), "behind": (False, True),
                              "diverged": (True, True), "identical": (False, False)}[st]:
            raise bad
        return {"status": st, "ahead": a, "behind": b}

    def has_commit(self, repo: str, sha: str) -> bool:
        """True if the repo has this commit; False only on a clear 404 or 422 from GitHub."""
        try:
            _req("GET", f"{self.API}/repos/{repo}/commits/{urllib.parse.quote(sha)}", self._h,
                 raw=True)
        except ExtError as e:
            if str(e).endswith(("HTTP 404", "HTTP 422")):
                return False
            raise
        return True

    def read_file(self, repo: str, path: str, sha: str) -> str:
        h = dict(self._h, Accept="application/vnd.github.raw")
        q = urllib.parse.quote(path)
        return _req("GET", f"{self.API}/repos/{repo}/contents/{q}?ref={sha}", h, raw=True)

    def ci_green(self, repo: str, sha: str, workflow: str = "ci") -> bool:
        r = _req("GET", f"{self.API}/repos/{repo}/actions/runs?head_sha={sha}&event=push"
                        f"&branch=main&per_page=100", self._h)
        runs = [x for x in r.get("workflow_runs", []) if x.get("name") == workflow
                and x.get("head_sha") == sha]
        if not runs:
            return False
        newest = max(runs, key=lambda x: x.get("created_at", ""))
        return newest.get("status") == "completed" and newest.get("conclusion") == "success"


# ---------------------------------------------------------------- Cloudflare (platform token)

class Cloudflare:
    API = "https://api.cloudflare.com/client/v4"

    def __init__(self, platform: Platform):
        self.platform = platform
        self._h = {"Authorization": "Bearer " + platform.secret("CLOUDFLARE_API_TOKEN")}
        self._zone_ids: dict[str, str] = {}

    def _call(self, method, path, body=None):
        r = _req(method, self.API + path, self._h, body)
        if not r.get("success"):
            errs = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in r.get("errors", []))
            raise ExtError(f"cloudflare {method} {path.split('?')[0]}: {errs or 'failed'}")
        return r["result"]

    def zone_id(self, host: str) -> tuple[str, str]:
        zone = zone_for(host, self.platform)  # refuses anything outside the allowlist
        if zone not in self._zone_ids:
            res = self._call("GET", f"/zones?name={zone}")
            if len(res) != 1:
                raise ExtError(f"zone {zone} not visible to the token")
            self._zone_ids[zone] = res[0]["id"]
        return zone, self._zone_ids[zone]

    @staticmethod
    def ip_ranges() -> list[str]:
        r = _req("GET", "https://api.cloudflare.com/client/v4/ips", {})
        if not r.get("success"):
            raise ExtError("could not fetch Cloudflare IP ranges")
        return r["result"]["ipv4_cidrs"] + r["result"]["ipv6_cidrs"]

    def dns_covers(self, host: str) -> str:
        """'record', 'wildcard' or '' — whether DNS for host already points somewhere."""
        zone, zid = self.zone_id(host)
        if self._address_records(zid, host):
            return "record"
        # A zone apex has no covering wildcard inside its zone (and its parent is outside it).
        if host != zone and self._address_records(zid, "*." + host.split(".", 1)[1]):
            return "wildcard"
        return ""

    def _address_records(self, zid: str, name: str) -> list:
        """Records that make a name resolve to a server. MX, TXT and the like at the same name
        (a zone apex with mail routing) do not count."""
        return [r for r in self._call("GET", f"/zones/{zid}/dns_records?name={name}")
                if r.get("type") in ("A", "AAAA", "CNAME")]

    def create_proxied_a(self, host: str, ipv4: str) -> str:
        """Create a proxied A record host → ipv4, only when the name has no address record
        (A, AAAA or CNAME). Never edits or deletes an existing record."""
        _, zid = self.zone_id(host)
        if self._address_records(zid, host):
            return "record already exists; left alone"
        res = self._call("POST", f"/zones/{zid}/dns_records", {
            "type": "A", "name": host, "content": ipv4, "proxied": True, "ttl": 1,
            "comment": "huta: created by huta edge"})
        return f"created proxied A {host} → {ipv4} ({res.get('id', '?')})"

    def zone_ssl(self, host: str) -> str:
        _, zid = self.zone_id(host)
        return self._call("GET", f"/zones/{zid}/settings/ssl")["value"]

    @staticmethod
    def _rule_body(r: dict) -> dict:
        """What a rule *does*: compared before/after so foreign rules are proven untouched."""
        return {k: r.get(k) for k in ("expression", "action", "action_parameters", "enabled",
                                      "description")}

    def ensure_host_strict(self, host: str) -> str:
        """Per-host Configuration Rule ssl=strict. Never PUTs an entrypoint (a PUT replaces every
        rule, NOTES footgun; codex #8). Adds one rule, or creates the entrypoint with a
        create-only call that fails if someone else created it first, then re-reads and checks
        every foreign rule's content is unchanged."""
        _, zid = self.zone_id(host)
        desc = f"huta: {host}"
        expr = f'(http.host eq "{host}")'
        phase = "http_config_settings"
        path = f"/zones/{zid}/rulesets/phases/{phase}/entrypoint"
        rule = {"expression": expr, "action": "set_config", "description": desc,
                "action_parameters": {"ssl": "strict"}, "enabled": True}
        try:
            rs = self._call("GET", path)
        except ExtError as e:
            if "10003" not in str(e):
                raise
            created = self._call("POST", f"/zones/{zid}/rulesets", {
                "name": "default", "kind": "zone", "phase": phase, "rules": [rule]})
            got = [r for r in created.get("rules", []) if r.get("description") == desc]
            if len(created.get("rules", [])) != 1 or not got:
                raise ExtError(f"{host}: new entrypoint does not hold exactly our rule")
            return f"created entrypoint with rule {got[0]['id']}"
        before = {r["id"]: self._rule_body(r) for r in rs.get("rules", [])}
        # Rules created before the rename carry "slipway: <host>"; they are ours too.
        ours = (desc, f"slipway: {host}")
        mine = [i for i, b in before.items() if b["description"] in ours]
        if mine:
            b = before[mine[0]]
            if b["expression"] == expr and (b["action_parameters"] or {}).get("ssl") == "strict" \
                    and b["enabled"] is not False:
                return f"rule already present ({mine[0]})"
            raise ExtError(f"{host}: a rule named {b['description']!r} exists but differs; not touching it")
        added = self._call("POST", f"/zones/{zid}/rulesets/{rs['id']}/rules", rule)
        after = {r["id"]: self._rule_body(r) for r in self._call("GET", path).get("rules", [])}
        changed = [before[i]["description"] or i for i in before if after.get(i) != before[i]]
        if changed:
            raise ExtError(f"{host}: foreign rules changed after adding ours: {changed}")
        new_ids = [i for i in after if i not in before]
        return f"rule {new_ids[0] if new_ids else '?'} added ({len(before)} foreign rules unchanged)" \
            if added is not None else "rule added"

    def origin_cert(self, hosts: list[str], csr_pem: str) -> str:
        for h in hosts:
            zone_for(h, self.platform)
        res = self._call("POST", "/certificates", {
            "hostnames": hosts, "requested_validity": 5475,
            "request_type": "origin-ecc", "csr": csr_pem})
        return res["certificate"]
