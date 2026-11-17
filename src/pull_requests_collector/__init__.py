"""Collect pull requests from GitHub, Gitea or Forgejo, resolve their
dependencies and cherry-pick them, in topological order, onto a git tree.

Stdlib only.  Progress goes to stderr; a JSON result goes to stdout.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import NoReturn

PAGE_SIZE = 50
BUGLINK_RE = re.compile(r"^BugLink:\s*(https://bugs\.launchpad\.net/bugs/\d+)", re.MULTILINE)

# DSL key -> (gitea/forgejo param, github param)
FILTER_MAP = {
    "state": ("state", "state"),
    "label": ("labels", "labels"),
    "labels": ("labels", "labels"),
    "milestone": ("milestones", "milestone"),
    "milestones": ("milestones", "milestone"),
    "author": ("created_by", "creator"),
    "assignee": ("assigned_by", "assignee"),
    "q": ("q", None),  # free-text search: gitea/forgejo only
}
CLIENT_FILTER_KEYS = {"base", "ready"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str) -> NoReturn:
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


# --- Platform / API client ---


class Api:
    def __init__(self, server_url: str, token: str, platform: str):
        self.server_url = server_url.rstrip("/")
        self.token = token
        host = urllib.parse.urlparse(self.server_url).hostname or ""
        if platform == "auto":
            platform = self._detect(host)
        self.platform = platform
        self.github = platform == "github"
        if not self.github:
            self.base = f"{self.server_url}/api/v1"
        elif host == "github.com":
            self.base = "https://api.github.com"
        else:
            self.base = f"{self.server_url}/api/v3"  # GitHub Enterprise Server

    def _detect(self, host: str) -> str:
        if host == "github.com":
            return "github"
        try:
            urllib.request.urlopen(f"{self.server_url}/api/v1/version", timeout=15)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):  # sign-in required still proves the API exists
                return "github"
        except OSError:
            return "github"
        return "gitea"  # gitea and forgejo share the API

    def get(self, endpoint: str, params: dict | None = None, soft: bool = False):
        url = self.base + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if soft and e.code in (404, 410):
                return []
            die(f"API request failed: {e.code} {url}\n{e.read().decode(errors='replace')}")

    def get_all(self, endpoint: str, params: dict | None = None, soft: bool = False) -> list:
        params = dict(params or {})
        params.update(limit=PAGE_SIZE, per_page=PAGE_SIZE)
        results: list = []
        page = 1
        while True:
            params["page"] = page
            data = self.get(endpoint, params, soft)
            results.extend(data)
            if len(data) < PAGE_SIZE:
                return results
            page += 1

    def dependencies(self, ref: PRRef) -> list[PRRef]:
        """PRs that must be applied before ref."""
        if self.github:
            ep = f"/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/dependencies/blocked_by"
        else:
            ep = f"/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/dependencies"
        deps = []
        for issue in self.get_all(ep, soft=True):
            if issue.get("pull_request") is None:
                continue
            if "repository_url" in issue:  # github
                owner, repo = issue["repository_url"].rstrip("/").split("/")[-2:]
            else:  # gitea / forgejo
                owner, repo = issue["repository"]["owner"], issue["repository"]["name"]
            deps.append(PRRef(owner, repo, issue["number"]))
        return deps

    def milestone_numbers(self, owner: str, repo: str, names: str) -> str:
        """GitHub filters by milestone number; translate names."""
        by_title = {
            m["title"]: str(m["number"])
            for m in self.get_all(f"/repos/{owner}/{repo}/milestones", {"state": "all"})
        }
        out = []
        for name in (n.strip() for n in names.split(",") if n.strip()):
            if name not in by_title:
                die(f"Milestone '{name}' not found in {owner}/{repo}")
            out.append(by_title[name])
        if len(out) != 1:
            die("GitHub supports exactly one milestone per query")
        return out[0]


# --- Query parsing ---


@dataclass
class PRQuery:
    owner: str
    repo: str
    filters: dict = field(default_factory=dict)  # DSL key -> value (server side)
    client: dict = field(default_factory=dict)  # base, ready, labels


def parse_query_line(line: str) -> PRQuery:
    parts = line.split()
    if "/" not in parts[0]:
        raise ValueError(f"Invalid repo (expected owner/repo): {parts[0]}")
    owner, repo = parts[0].split("/", 1)
    query = PRQuery(owner, repo)
    for token in parts[1:]:
        if ":" not in token:
            raise ValueError(f"Invalid filter (expected key:value): {token}")
        key, value = token.split(":", 1)
        key = key.lower()
        if key in CLIENT_FILTER_KEYS:
            query.client[key] = value
            continue
        if key not in FILTER_MAP:
            raise ValueError(
                f"Unknown filter '{key}'. "
                f"Supported: {', '.join(sorted(FILTER_MAP.keys() | CLIENT_FILTER_KEYS))}"
            )
        if key in ("label", "labels"):
            # some servers silently ignore unknown labels; re-check client-side
            query.client["labels"] = value
            key = "label"
        elif key == "milestones":
            key = "milestone"
        query.filters[key] = value
    query.filters.setdefault("state", "open")
    return query


def api_params(api: Api, query: PRQuery) -> dict:
    params = {} if api.github else {"type": "pulls"}
    for key, value in query.filters.items():
        gitea_name, github_name = FILTER_MAP[key]
        name = github_name if api.github else gitea_name
        if name is None:
            die(f"Filter '{key}' is not supported on {api.platform}")
        if api.github and key == "milestone":
            value = api.milestone_numbers(query.owner, query.repo, value)
        params[name] = value
    return params


# --- PR model ---


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass
class PRNode:
    ref: PRRef
    title: str
    base_ref: str
    merge_base: str  # empty if the server does not report it
    mergeable: bool
    deps: list[PRRef] = field(default_factory=list)


def matches_client_filters(pr: dict, filters: dict) -> bool:
    if "labels" in filters:
        required = {x.strip() for x in filters["labels"].split(",") if x.strip()}
        actual = {x["name"] for x in pr.get("labels") or []}
        if not required <= actual:
            return False
    if "base" in filters and pr["base"]["ref"] != filters["base"]:
        return False
    if "ready" in filters:
        want_ready = filters["ready"].lower() in ("true", "yes", "1")
        if want_ready == bool(pr.get("draft")):
            return False
    return True


def collect(api: Api, queries: list[PRQuery]) -> set[PRRef]:
    found: set[PRRef] = set()
    for q in queries:
        log(f"Query: {q.owner}/{q.repo} {q.filters} {q.client}")
        for issue in api.get_all(f"/repos/{q.owner}/{q.repo}/issues", api_params(api, q)):
            if issue.get("pull_request") is None:
                continue
            if q.client:
                pr = api.get(f"/repos/{q.owner}/{q.repo}/pulls/{issue['number']}")
                if not matches_client_filters(pr, q.client):
                    continue
            ref = PRRef(q.owner, q.repo, issue["number"])
            found.add(ref)
            log(f"  Found: {ref.full} - {issue['title']}")
    return found


def resolve(api: Api, seeds: set[PRRef]) -> dict[PRRef, PRNode]:
    nodes: dict[PRRef, PRNode] = {}
    queue = sorted(seeds, key=lambda r: r.full)
    while queue:
        ref = queue.pop(0)
        if ref in nodes:
            continue
        log(f"Resolving: {ref.full}")
        pr = api.get(f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}")
        # github reports null while computing; gitea/forgejo report false for every
        # draft regardless of conflicts, so only trust the flag for ready PRs
        mergeable = pr.get("mergeable") is not False or bool(pr.get("draft"))
        nodes[ref] = node = PRNode(
            ref=ref,
            title=pr["title"],
            base_ref=pr["base"]["ref"],
            merge_base=pr.get("merge_base") or "",
            mergeable=mergeable,
        )
        node.deps = api.dependencies(ref)
        queue.extend(d for d in node.deps if d not in nodes)
    return nodes


def topological_sort(nodes: dict[PRRef, PRNode]) -> list[PRRef]:
    """Kahn's algorithm; dependencies first.  Raises on cycle."""
    in_degree = {r: 0 for r in nodes}
    dependents: dict[PRRef, list[PRRef]] = {r: [] for r in nodes}
    for ref, node in nodes.items():
        for dep in node.deps:
            in_degree[ref] += 1
            dependents[dep].append(ref)
    queue = [r for r, d in in_degree.items() if d == 0]
    order: list[PRRef] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for nxt in dependents[cur]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(nodes):
        raise RuntimeError(
            "Dependency cycle among: " + ", ".join(r.full for r in set(nodes) - set(order))
        )
    return order


# --- Git ---


def git(*args: str, cwd: str, check: bool = True, config: tuple[str, ...] = ()):
    cmd = ["git"]
    for c in config:
        cmd += ["-c", c]
    r = subprocess.run(cmd + list(args), capture_output=True, text=True, cwd=cwd, check=False)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed:\n{r.stderr}")
    return r


def auth_config(server_url: str, token: str) -> tuple[str, ...]:
    if not token:
        return ()
    cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    p = urllib.parse.urlparse(server_url)
    return (f"http.{p.scheme}://{p.hostname}/.extraheader=AUTHORIZATION: basic {cred}",)


def commit_buglinks(sha: str, cwd: str) -> set[str]:
    return set(BUGLINK_RE.findall(git("log", "-1", "--format=%B", sha, cwd=cwd).stdout))


def select_proposed_commits(commits: list[str], cwd: str) -> tuple[list[str], set[str]] | None:
    """Pick the topmost run of commits sharing the newest commit's BugLink set.

    Older commits are base-branch artifacts from rebases.  Returns None if the
    newest commit has no BugLink.
    """
    fingerprint = commit_buglinks(commits[-1], cwd)
    if not fingerprint:
        return None
    selected: list[str] = []
    for sha in reversed(commits):
        if commit_buglinks(sha, cwd) != fingerprint:
            break
        selected.append(sha)
    selected.reverse()
    return selected, fingerprint


def simulate_cherry_pick(
    selected: list[str], tip: str, cwd: str, committer: tuple[str, ...]
) -> str | None:
    """Replay commits in memory on top of tip; return the new tip or None on conflict.

    Uses merge-tree and commit-tree only, so the index and worktree stay untouched.
    """
    for sha in selected:
        mt = git(
            "merge-tree", "--write-tree", f"--merge-base={sha}^", tip, sha, cwd=cwd, check=False
        )
        if mt.returncode != 0:
            log(f"::error::Cherry-pick of {sha} would conflict:\n{mt.stdout}{mt.stderr}")
            return None
        tree = mt.stdout.splitlines()[0]
        tip = git(
            "commit-tree", tree, "-p", tip, "-m", f"dry-run {sha}", cwd=cwd, config=committer
        ).stdout.strip()
    return tip


def cherry_pick(
    api: Api,
    node: PRNode,
    cwd: str,
    auth: tuple[str, ...],
    committer: tuple[str, ...],
    dry_run: bool,
    tip: str,
) -> tuple[bool, list[str], str]:
    """Returns (success, buglinks, tip).  tip is only advanced in dry-run mode."""
    ref = node.ref
    log(f"{'Simulating' if dry_run else 'Cherry-picking'} {ref.full}: {node.title}")
    url = f"{api.server_url}/{ref.owner}/{ref.repo}.git"
    head_ref = f"refs/pull-requests-collector/{ref.owner}/{ref.repo}/{ref.number}"
    refspecs = [f"+refs/pull/{ref.number}/head:{head_ref}"]
    base_local = ""
    if not node.merge_base:
        base_local = head_ref + "-base"
        refspecs.append(f"+refs/heads/{node.base_ref}:{base_local}")
    git("fetch", "--quiet", url, *refspecs, cwd=cwd, config=auth)

    merge_base = node.merge_base or git("merge-base", base_local, head_ref, cwd=cwd).stdout.strip()
    commits = git("rev-list", "--reverse", f"{merge_base}..{head_ref}", cwd=cwd).stdout.split()
    if not commits:
        log(f"::warning::No commits in {ref.full}")
        return True, [], tip

    selection = select_proposed_commits(commits, cwd)
    if selection is None:
        log(f"::error::Topmost commit of {ref.full} has no BugLink")
        return False, [], tip
    selected, buglinks = selection
    skipped = len(commits) - len(selected)
    note = f", {skipped} older commit(s) skipped" if skipped else ""
    log(f"  {len(selected)} commit(s) to apply{note}, BugLink: {', '.join(sorted(buglinks))}")

    if dry_run:
        new_tip = simulate_cherry_pick(selected, tip, cwd, committer)
        return new_tip is not None, sorted(buglinks), new_tip or tip

    save_point = git("rev-parse", "HEAD", cwd=cwd).stdout.strip()
    for sha in selected:
        cp = git("cherry-pick", "-x", "-s", sha, cwd=cwd, check=False, config=committer)
        if cp.returncode != 0:
            log(f"::error::Cherry-pick of {sha} from {ref.full} failed:\n{cp.stderr}")
            git("cherry-pick", "--abort", cwd=cwd, check=False)
            git("reset", "--hard", save_point, cwd=cwd)
            return False, [], tip
    return True, sorted(buglinks), tip


def resolve_committer(cwd: str, name: str, email: str) -> tuple[str, ...]:
    """git -c settings: explicit args win, then the repo's git config, then HEAD's committer."""
    config = []
    for key, value, fmt in (("user.name", name, "%cn"), ("user.email", email, "%ce")):
        if not value:
            value = git("config", "--get", key, cwd=cwd, check=False).stdout.strip()
        if not value:
            value = git("log", "-1", f"--format={fmt}", cwd=cwd).stdout.strip()
        config.append(f"{key}={value}")
    return tuple(config)


# --- Main ---


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--server-url", required=True, help="e.g. https://github.com")
    p.add_argument(
        "--query",
        action="append",
        required=True,
        help="'owner/repo [key:value ...]'; repeatable, results are unioned",
    )
    p.add_argument("--token", default="", help="API / git token")
    p.add_argument("--platform", default="auto", choices=["auto", "github", "gitea", "forgejo"])
    p.add_argument("--working-directory", default=".", help="existing git tree")
    p.add_argument("--committer-name", default="")
    p.add_argument("--committer-email", default="")
    p.add_argument(
        "--skip-on-error", action="store_true", help="continue after a failed cherry-pick"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="simulate the cherry-picks without touching index or worktree",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    api = Api(args.server_url, args.token, args.platform)
    log(f"Platform: {api.platform}, API: {api.base}")

    queries = [parse_query_line(q) for q in args.query]
    seeds = collect(api, queries)
    nodes = resolve(api, seeds)
    order = topological_sort(nodes)
    log(f"Order: {', '.join(r.full for r in order)}")

    cwd = args.working_directory
    applied: list[PRRef] = []
    failed: list[PRRef] = []
    skipped: list[PRRef] = []
    pr_buglinks: dict[str, list[str]] = {}
    grouped: dict[str, list[str]] = {}

    git("rev-parse", "--git-dir", cwd=cwd)
    committer = resolve_committer(cwd, args.committer_name, args.committer_email)
    auth = auth_config(api.server_url, args.token)
    head = git("rev-parse", "HEAD", cwd=cwd).stdout.strip()
    tip = head  # simulated tip, advanced only in dry-run mode
    for ref in order:
        node = nodes[ref]
        if not node.mergeable:
            log(f"::warning::Skipping {ref.full}: unmergeable")
            skipped.append(ref)
            continue
        ok, buglinks, tip = cherry_pick(api, node, cwd, auth, committer, args.dry_run, tip)
        if not ok:
            failed.append(ref)
            if not args.skip_on_error:
                break
            continue
        applied.append(ref)
        pr_buglinks[ref.full] = buglinks
        for url in buglinks:
            grouped.setdefault(url, []).append(ref.full)

    def full(refs: list[PRRef]) -> list[str]:
        return [r.full for r in refs]

    result = {
        "dry_run": args.dry_run,
        "platform": api.platform,
        "enumerated": full(order),
        "applied": full(applied),
        "skipped": full(skipped),
        "failed": full(failed),
        "titles": {r.full: nodes[r].title for r in order},
        "buglinks": pr_buglinks,
        "buglinks_grouped": grouped,
        "ref": git("rev-parse", "HEAD", cwd=cwd).stdout.strip(),
    }
    print(json.dumps(result, indent=2))
    sys.exit(1 if failed else 0)
