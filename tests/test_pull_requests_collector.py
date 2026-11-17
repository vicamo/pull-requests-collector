import os
import subprocess

import pytest

import pull_requests_collector as prc


def R(n):
    return prc.PRRef("o", "r", n)


def node(n, deps=()):
    return prc.PRNode(R(n), "t", "main", "", True, [R(d) for d in deps])


def test_parse_query():
    q = prc.parse_query_line("o/r label:a,b milestone:m base:main ready:true")
    assert q.filters == {"label": "a,b", "milestone": "m", "state": "open"}
    assert q.client == {"labels": "a,b", "base": "main", "ready": "true"}


def test_parse_query_errors():
    with pytest.raises(ValueError):
        prc.parse_query_line("norepo")
    with pytest.raises(ValueError):
        prc.parse_query_line("o/r bogus:1")


def test_toposort():
    nodes = {r.ref: r for r in (node(1, [2]), node(2, [3]), node(3))}
    assert prc.topological_sort(nodes) == [R(3), R(2), R(1)]


def test_toposort_cycle():
    nodes = {r.ref: r for r in (node(1, [2]), node(2, [1]))}
    with pytest.raises(RuntimeError):
        prc.topological_sort(nodes)


def test_client_filters():
    pr = {"labels": [{"name": "sru"}], "base": {"ref": "main"}, "draft": False}
    assert prc.matches_client_filters(pr, {"labels": "sru", "base": "main", "ready": "true"})
    assert not prc.matches_client_filters(pr, {"labels": "sru,x"})
    assert not prc.matches_client_filters(pr, {"ready": "false"})


def test_platform_urls():
    assert prc.Api("https://github.com", "", "auto").base == "https://api.github.com"
    assert prc.Api("https://ghe.example", "", "github").base == "https://ghe.example/api/v3"
    assert prc.Api("https://x.example/forgejo", "", "forgejo").base.endswith("/forgejo/api/v1")


# --- git-backed tests, run in throwaway repos ---


def run_git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    run_git(tmp_path, "init", "-q", "-b", "main")
    return str(tmp_path)


def commit(repo, name, content, buglink=None):
    with open(os.path.join(repo, name), "w") as f:
        f.write(content)
    msg = "change " + name
    if buglink:
        msg += f"\n\nBugLink: https://bugs.launchpad.net/bugs/{buglink}"
    run_git(repo, "add", name)
    run_git(repo, "commit", "-q", "-m", msg)
    return run_git(repo, "rev-parse", "HEAD")


def test_select_proposed_commits(repo):
    old = commit(repo, "a", "1", "1000001")
    mid = commit(repo, "b", "1", "1000002")
    top1 = commit(repo, "c", "1", "1000002")
    top2 = commit(repo, "d", "1", "1000002")
    selected, links = prc.select_proposed_commits([old, mid, top1, top2], repo)
    assert selected == [mid, top1, top2]
    assert links == {"https://bugs.launchpad.net/bugs/1000002"}


def test_select_proposed_commits_no_buglink(repo):
    c1 = commit(repo, "a", "1", "1000001")
    c2 = commit(repo, "b", "1")
    assert prc.select_proposed_commits([c1, c2], repo) is None


def test_simulate_cherry_pick_clean(repo):
    base = commit(repo, "a", "1")
    run_git(repo, "checkout", "-q", "-b", "pr")
    c1 = commit(repo, "b", "1", "1000001")
    run_git(repo, "checkout", "-q", "main")
    tip = commit(repo, "c", "1")
    committer = ("user.name=T", "user.email=t@example.com")
    new_tip = prc.simulate_cherry_pick([c1], tip, repo, committer)
    assert new_tip and new_tip != tip
    assert run_git(repo, "rev-parse", "HEAD") == tip
    assert run_git(repo, "status", "--porcelain") == ""
    assert base != tip


def test_simulate_cherry_pick_conflict(repo):
    commit(repo, "a", "base")
    run_git(repo, "checkout", "-q", "-b", "pr")
    c1 = commit(repo, "a", "from pr", "1000001")
    run_git(repo, "checkout", "-q", "main")
    tip = commit(repo, "a", "from main")
    committer = ("user.name=T", "user.email=t@example.com")
    assert prc.simulate_cherry_pick([c1], tip, repo, committer) is None


def test_resolve_committer_fallback_to_head(repo):
    commit(repo, "a", "1")
    assert prc.resolve_committer(repo, "", "") == ("user.name=T", "user.email=t@example.com")
    assert prc.resolve_committer(repo, "X", "x@example.com") == (
        "user.name=X",
        "user.email=x@example.com",
    )


class FakeApi:
    def __init__(self, prs):
        self.prs = prs

    def get(self, endpoint):
        return self.prs[int(endpoint.rsplit("/", 1)[1])]

    def dependencies(self, ref):
        return []


@pytest.mark.parametrize(
    ("mergeable", "draft", "expected"),
    [(True, False, True), (None, False, True), (False, False, False), (False, True, True)],
)
def test_resolve_mergeable(mergeable, draft, expected):
    pr = {"title": "t", "base": {"ref": "main"}, "mergeable": mergeable, "draft": draft}
    nodes = prc.resolve(FakeApi({1: pr}), {R(1)})
    assert nodes[R(1)].mergeable is expected
