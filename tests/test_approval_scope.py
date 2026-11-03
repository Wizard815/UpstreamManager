"""Approvals merge only what the request showed; skipped upstreams are reported."""
from upstream_upkeep import registry

from conftest import commit_file, git


def test_approval_merges_only_the_commits_the_request_showed(client, repos):
    up, mine = repos
    registry.add_repo(str(mine), None, "mine")
    commit_file(up, "b.txt", "seen\n", "commit the user saw")
    commit_file(mine, "m.txt", "mine\n", "my change")  # both sides moved: needs approval
    res = client.post("/api/run-daily", json={}).get_json()["results"]
    assert [r["status"] for r in res] == ["request_raised"]

    commit_file(up, "late.txt", "late\n", "pushed after the request")
    client.post("/api/repo/mine/fetch", json={})
    out = client.post("/api/request/mine:main/decide", json={"decision": "approve"}).get_json()
    assert out["ok"], out
    log = git(mine, "log", "--format=%s")
    assert "commit the user saw" in log and "pushed after the request" not in log
    assert "1 commit(s)" in out["request"]["result"][0]["detail"]


def test_daily_check_reports_an_upstream_without_the_focus_branch(client, repos):
    _, mine = repos
    git(mine, "checkout", "-qb", "feature")
    registry.add_repo(str(mine), None, "mine")
    res = client.post("/api/run-daily", json={}).get_json()["results"]
    assert len(res) == 1 and res[0]["status"] == "skipped"
    assert "has no branch feature" in res[0]["detail"] and "press Fetch" not in res[0]["detail"]
