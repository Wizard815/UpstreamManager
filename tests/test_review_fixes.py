"""Regression tests for problems found in a logic review."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from upstream_upkeep import components, credentials, memory, registry, store

from conftest import commit_file, git


def _listener():
    """A local server that records the Authorization header it receives."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            body = b'{"data": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_saved_api_key_only_goes_to_the_saved_server(client):
    saved, saved_seen = _listener()
    other, other_seen = _listener()
    try:
        saved_url = f"http://127.0.0.1:{saved.server_port}/v1"
        client.post("/api/settings", json={"llm": {"base_url": saved_url, "model": "m", "api_key": "sk-SECRET-123456"}})
        masked = client.get("/api/settings").get_json()["llm"]["api_key"]
        client.get(f"/api/models?base_url=http://127.0.0.1:{other.server_port}/v1&api_key={masked}",
                   headers={"Origin": "https://evil.example"})
        assert not any("SECRET" in (h or "") for h in other_seen)
        client.get(f"/api/models?base_url={saved_url}&api_key={masked}")
        assert saved_seen and "sk-SECRET-123456" in saved_seen[-1]
    finally:
        saved.shutdown(); other.shutdown()


def test_approved_push_sends_only_the_reviewed_commits(client, repos, tmp_path):
    _, mine = repos
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(mine, "remote", "add", "pub", str(bare))
    git(mine, "checkout", "-q", "-b", "feature")
    commit_file(mine, "f.txt", "x\n", "reviewed")
    registry.add_repo(str(mine), None, "mine")
    req = client.post("/api/repo/mine/push/request", json={"remote": "pub"}).get_json()["request"]
    commit_file(mine, "g.txt", "y\n", "NOT reviewed")
    assert client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()["ok"]
    assert git(bare, "log", "--format=%s", "feature").splitlines() == ["reviewed", "base"]


def test_push_refuses_when_the_reviewed_commit_was_rewritten(client, repos, tmp_path):
    _, mine = repos
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(mine, "remote", "add", "pub", str(bare))
    git(mine, "checkout", "-q", "-b", "feature")
    commit_file(mine, "f.txt", "x\n", "reviewed")
    registry.add_repo(str(mine), None, "mine")
    req = client.post("/api/repo/mine/push/request", json={"remote": "pub"}).get_json()["request"]
    git(mine, "commit", "-q", "--amend", "-m", "rewritten")
    out = client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()
    assert not out["ok"] and "no longer contains" in out["request"]["result"][0]["detail"]
    assert git(bare, "branch", "--list") == ""


@pytest.mark.parametrize("branch", ["--output=/tmp/pwned.txt", "-x", "nope", "a..b"])
def test_focus_branch_must_be_a_real_branch(client, repos, branch):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = client.post("/api/repo/mine/focus", json={"branch": branch})
    assert r.status_code == 400 and registry.get_repo("mine")["focus_branch"] == "main"


def test_option_looking_revs_are_not_read_as_options(repos, tmp_path):
    from upstream_upkeep import gitops
    _, mine = repos
    target = tmp_path / "pwned.txt"
    gitops.list_commits(str(mine), f"--output={target}")
    assert not target.exists()


def test_remote_names_keep_their_case(client, repos, tmp_path):
    up, mine = repos
    git(mine, "remote", "add", "Upstream", str(up))
    registry.add_repo(str(mine), None, "mine")
    names = [u["name"] for u in registry.get_repo("mine")["upstreams"]]
    assert "Upstream" in names
    plan = client.post("/api/repo/mine/fetch", json={}).get_json()["plan"]
    assert not next(u for u in plan["upstreams"] if u["name"] == "Upstream")["missing_ref"]


def test_submodule_names_with_spaces(repos):
    _, mine = repos
    Path(mine, ".gitmodules").write_text('[submodule "my lib"]\n\tpath = lib\n\turl = https://example.com/x.git\n')
    assert components.detect(str(mine)) == [{"name": "my lib", "path": "lib", "url": "https://example.com/x.git"}]


def test_missing_focus_branch_is_not_up_to_date(client, repos):
    up, mine = repos
    registry.add_repo(str(mine), None, "mine")
    commit_file(up, "b.txt", "x", "new")
    with store.lock:  # a branch deleted after it was chosen
        registry.get_repo("mine")["focus_branch"] = "gone"
    plan = client.post("/api/repo/mine/fetch", json={}).get_json()["plan"]
    u = plan["upstreams"][0]
    assert not u["up_to_date"] and u["missing_ref"] and "gone does not exist" in u["conflict_detail"]
    res = client.post("/api/run-daily", json={}).get_json()["results"]
    assert res and res[0]["status"] == "skipped"


def test_add_upstream_checks_git(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = client.post("/api/repo/mine/upstream", json={"name": "origin", "url": "https://github.com/someone/else.git"})
    assert r.status_code == 400 and "already points to" in r.get_json()["error"]
    r = client.post("/api/repo/mine/upstream", json={"name": "fork", "url": "--bogus"})
    assert r.status_code == 400 and "fork" not in git(mine, "remote").split()
    assert "fork" not in [u["name"] for u in registry.get_repo("mine")["upstreams"]]


def test_huge_days_ago_does_not_crash():
    assert memory.days_mentioned("what happened 100000000 days ago") == []


def test_null_credential_is_not_stored_as_none(client, monkeypatch):
    monkeypatch.setenv("UPSTREAM_SECRETS_BACKEND", "file")
    client.post("/api/credentials", json={"dockerhub_username": None})
    assert credentials.get("dockerhub_username") == ""


def test_git_dir_is_protected_whatever_its_case(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = client.post("/api/files/contents", json={"repo": "mine", "path": ".GIT/hooks/post-merge", "content": "x"})
    assert r.status_code >= 400 and not Path(mine, ".GIT", "hooks", "post-merge").exists()
