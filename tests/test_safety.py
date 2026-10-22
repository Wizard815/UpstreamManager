"""Regression tests for the audit fixes: request forgery, file access, merge logic,
focus branches, approval requests and concurrent state writes."""
import threading
from pathlib import Path

from conftest import commit_file, git

from upstream_upkeep import daily_check, gitops, registry, store


def post(client, url, body=None, **headers):
    return client.post(url, json=body if body is not None else {}, headers=headers)


# --------------------------------------------------------------- request forgery
def test_text_plain_post_is_refused(client):
    r = client.post("/api/add", data='{"path": "x"}', content_type="text/plain")
    assert r.status_code == 415


def test_foreign_origin_is_refused(client):
    assert post(client, "/api/add", {"path": "x"}, Origin="https://evil.example").status_code == 403


def test_same_origin_json_is_allowed(client):
    r = post(client, "/api/add", {"path": "Z:/no/such/repo"}, Origin="http://localhost")
    assert r.status_code not in (403, 415)


# ------------------------------------------------------------------ file access
def test_legacy_listing_needs_a_tracked_repo(client):
    assert client.get("/api/files?path=C:/Windows").status_code == 404
    assert client.get("/api/branches?repo=C:/Windows").status_code == 404


def test_repo_file_cannot_escape(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    assert client.get("/api/repo/mine/file?path=a.txt").get_json()["ok"]
    assert client.get("/api/repo/mine/file?path=../up/a.txt").status_code == 404
    assert client.get("/api/files?repo=mine&path=..").status_code == 400


def test_bad_remote_name_rejected(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = post(client, "/api/repo/mine/upstream", {"name": "--mirror", "url": "x"})
    assert r.status_code == 400


# ------------------------------------------------------------------ merge logic
def test_counts_and_read_only_conflict_check(repos):
    up, mine = repos
    commit_file(up, "a.txt", "one\nUP\nthree\n", "up edits a")
    commit_file(up, "c.txt", "c\n", "up adds c")
    commit_file(mine, "a.txt", "one\nMINE\nthree\n", "my edit")
    git(mine, "fetch", "-q", "origin")
    assert gitops.behind_ahead(str(mine), "main", "refs/remotes/origin/main") == (2, 1)
    check = gitops.merge_check(str(mine), "main", "refs/remotes/origin/main")
    assert check["conflict"] and check["files"] == ["a.txt"]
    assert git(mine, "status", "--porcelain") == ""  # nothing touched


def test_behind_only_fast_forwards(repos):
    up, mine = repos
    commit_file(up, "b.txt", "b\n", "up adds b")
    git(mine, "fetch", "-q", "origin")
    r = daily_check.merge_upstream(str(mine), {"name": "origin", "branch": "main"}, "main")
    assert r["ok"] and r["action"] == "fast-forward"
    assert gitops.behind_ahead(str(mine), "main", "refs/remotes/origin/main") == (0, 0)


def test_dirty_tree_or_other_branch_is_left_alone(repos):
    up, mine = repos
    commit_file(up, "b.txt", "b\n", "up adds b")
    git(mine, "fetch", "-q", "origin")
    Path(mine, "a.txt").write_text("local edit\n")
    r = daily_check.merge_upstream(str(mine), {"name": "origin", "branch": "main"}, "main")
    assert not r["ok"] and "uncommitted" in r["detail"]
    git(mine, "checkout", "-q", "--", "a.txt")
    git(mine, "checkout", "-q", "-b", "other")
    r = daily_check.merge_upstream(str(mine), {"name": "origin", "branch": "main"}, "main")
    assert not r["ok"] and "checked-out branch" in r["detail"]


def test_diverged_needs_approval_and_conflicts_abort(repos):
    up, mine = repos
    commit_file(up, "a.txt", "one\nUP\nthree\n", "up edits a")
    commit_file(mine, "a.txt", "one\nMINE\nthree\n", "my edit")
    git(mine, "fetch", "-q", "origin")
    up_ref = {"name": "origin", "branch": "main"}
    assert "needs approval" in daily_check.merge_upstream(str(mine), up_ref, "main")["detail"]
    r = daily_check.merge_upstream(str(mine), up_ref, "main", allow_merge_commit=True)
    assert not r["ok"] and r["action"] == "aborted"
    assert git(mine, "status", "--porcelain") == ""
    assert not Path(mine, ".git", "MERGE_HEAD").exists()


# ------------------------------------------------------- focus branch / requests
def test_focus_change_keeps_upstream_branch(repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    git(mine, "checkout", "-q", "-b", "feature")
    registry.set_focus_branch("mine", "feature")
    assert all(u["branch"] == "main" for u in store.get_repo("mine")["upstreams"])


def test_daily_check_raises_one_request_and_approval_merges(client, repos):
    up, mine = repos
    commit_file(up, "c.txt", "c\n", "up adds c")
    commit_file(mine, "d.txt", "d\n", "my file")
    registry.add_repo(str(mine), None, "mine")
    for _ in range(2):  # running twice must not duplicate the request
        res = post(client, "/api/run-daily").get_json()
        assert res["results"][0]["status"] == "request_raised"
    reqs = client.get("/api/requests").get_json()
    assert len(reqs) == 1 and reqs[0]["status"] == "pending"
    r = post(client, f"/api/request/{reqs[0]['id']}/decide", {"decision": "approve"})
    body = r.get_json()
    assert body["ok"] and body["request"]["status"] == "approved"
    assert body["request"]["result"][0]["action"] == "merge"
    assert gitops.behind_ahead(str(mine), "main", "refs/remotes/origin/main")[0] == 0
    assert post(client, f"/api/request/{reqs[0]['id']}/decide", {"decision": "approve"}).status_code == 409


def test_daily_check_fast_forwards_without_request(client, repos):
    up, mine = repos
    commit_file(up, "c.txt", "c\n", "up adds c")
    registry.add_repo(str(mine), None, "mine")
    res = post(client, "/api/run-daily").get_json()
    assert res["results"][0]["status"] == "merged"
    assert client.get("/api/requests").get_json() == []


# ----------------------------------------------------------------- state lock
def test_concurrent_chat_writes_and_saves(repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    sid = store.new_session("mine")["id"]
    errors = []

    def writer():
        try:
            for i in range(40):
                store.add_chat_message("mine", sid, "user", f"m{i}")
        except Exception as e:  # pragma: no cover - the failure we guard against
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.get_session("mine", sid)["messages"]) == 240
