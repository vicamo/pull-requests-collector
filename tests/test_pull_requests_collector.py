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
