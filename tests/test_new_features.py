"""Merge helpers in workspaces, auto-approval rules, repo groups, GitHub issues as tasks, MCP over HTTP."""
import os
from pathlib import Path

import pytest

from upstream_upkeep import registry, store, workspaces

from conftest import commit_file, git
from test_agents import conflicted  # noqa: F401  (fixture)


# ------------------------------------------------------------ 1. rerere + Mergiraf
def test_a_conflict_resolved_once_is_resolved_again_by_itself(client, conflicted, monkeypatch):  # noqa: F811
    monkeypatch.setenv("UPSTREAM_MERGIRAF", "off")
    client.post("/api/repo/mine/fetch", json={})
    ws = client.post("/api/repo/mine/workspaces", json={"upstream": "origin"}).get_json()["workspace"]
    assert ws["merging"] and ws["unresolved"] == ["config.py"]
    path = Path(ws["path"])
    (path / "config.py").write_text("PORT = int(env)\nHOST = '0.0.0.0'\n")
    git(path, "add", "config.py")                       # resolved by hand, like an agent with its own shell
    git(path, "-c", "user.email=a@a", "-c", "user.name=A", "commit", "-q", "--no-edit")
    st = client.get(f"/api/repo/mine/workspaces/{ws['name']}").get_json()
    st = st.get("workspace", st)
    assert st["merge_helpers"]["rerere"] and not st["merging"]
    assert client.delete(f"/api/repo/mine/workspaces/{ws['name']}", json={}).get_json()["ok"]
    again = client.post("/api/repo/mine/workspaces", json={"upstream": "origin"}).get_json()["workspace"]
    assert not again["merging"] and not again["unresolved"], again
    assert (Path(again["path"]) / "config.py").read_text() == "PORT = int(env)\nHOST = '0.0.0.0'\n"
    assert any("config.py" in r for r in again["reused_resolutions"]) and again["ready"]
    # the user's own repo config is left alone
    mine = conflicted[1]
    import subprocess
    assert subprocess.run(["git", "config", "--get", "rerere.enabled"], cwd=mine, capture_output=True, text=True).stdout == ""


def test_mergiraf_is_used_as_the_merge_driver_when_installed(client, conflicted, monkeypatch, tmp_path):  # noqa: F811
    """A stand-in for Mergiraf proves the wiring: it is called for config.py and its result is kept."""
    fake = tmp_path / "mergiraf"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = languages ]; then echo '*.py merge=mergiraf'; exit 0; fi\n"
                    "# merge --git %O %A %B ...: write the merged result into %A\n"
                    f"echo \"$@\" >> {tmp_path}/calls\n"
                    "printf \"PORT = int(env)\\nHOST = '0.0.0.0'\\n\" > \"$4\"\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("UPSTREAM_MERGIRAF", str(fake))
    workspaces._mergiraf_attributes.cache_clear()
    client.post("/api/repo/mine/fetch", json={})
    ws = client.post("/api/repo/mine/workspaces", json={"upstream": "origin"}).get_json()["workspace"]
    assert not ws["merging"] and ws["ready"] and ws["merge_helpers"]["mergiraf"], ws
    assert (tmp_path / "calls").read_text().startswith("merge --git")
    assert (Path(ws["path"]) / "config.py").read_text() == "PORT = int(env)\nHOST = '0.0.0.0'\n"


# ------------------------------------------------------------ 2. auto-approval rules
from upstream_upkeep import autoapprove  # noqa: E402


def _clean_upstream(repos):
    up, mine = repos
    commit_file(up, "new.txt", "new\n", "upstream: new file")
    commit_file(mine, "mine.txt", "mine\n", "mine")       # so it needs a merge commit (not a fast-forward)
    registry.add_repo(str(mine), None, "mine")
    return up, mine


def test_rules_are_the_users_only(client, repos):
    _clean_upstream(repos)
    agent = {"X-Upstream-Client": "agent"}
    assert client.post("/api/repo/mine/auto-approve", json={"clean": True}, headers=agent).status_code == 403
    r = client.post("/api/repo/mine/auto-approve", json={"workspaces": True}).get_json()
    assert not r["ok"] and "check command" in r["error"]


def test_a_clean_pull_that_passes_the_check_goes_in_by_itself(client, repos):
    up, mine = _clean_upstream(repos)
    assert client.post("/api/repo/mine/auto-approve", json={"clean": True, "check": "test -f new.txt && test -f mine.txt"}).get_json()["ok"]
    client.post("/api/repo/mine/fetch", json={})
    req = client.post("/api/repo/mine/upstream/origin/request-pull", json={"target": "origin/main"}).get_json()["request"]
    autoapprove.wait()
    done = store.get_request(req["id"])
    assert done["status"] == "approved" and done["auto"]["state"] == "passed", done
    assert "auto-approval" in done["comment"] and (Path(mine) / "new.txt").is_file()
    assert not list((Path(store.config.CONFIG_PATH.parent) / "checks" / "mine").glob("*"))   # the checkout is gone


def test_a_failing_check_leaves_it_for_you_with_the_output(client, repos):
    up, mine = _clean_upstream(repos)
    client.post("/api/repo/mine/auto-approve", json={"clean": True, "check": "echo boom >&2; exit 3"})
    client.post("/api/repo/mine/fetch", json={})
    req = client.post("/api/repo/mine/upstream/origin/request-pull", json={"target": "origin/main"}).get_json()["request"]
    autoapprove.wait()
    left = store.get_request(req["id"])
    assert left["status"] == "pending" and left["auto"]["state"] == "failed" and "boom" in left["auto"]["detail"]
    assert not (Path(mine) / "new.txt").exists()
    assert "auto-approval check: failed" in client.get("/api/repo/mine/briefing").get_json()["text"]


def test_an_agents_workspace_needs_the_rule_and_a_passing_check(client, conflicted):  # noqa: F811
    client.post("/api/repo/mine/fetch", json={})
    ws = client.post("/api/repo/mine/workspaces", json={"upstream": "origin"}).get_json()["workspace"]
    p = Path(ws["path"])
    (p / "config.py").write_text("PORT = int(env)\nHOST = '0.0.0.0'\n")
    client.post(f"/api/repo/mine/workspaces/{ws['name']}/continue", json={})
    # no rule: it waits
    req = client.post(f"/api/repo/mine/workspaces/{ws['name']}/submit", json={"summary": "x"}).get_json()["request"]
    autoapprove.wait()
    assert store.get_request(req["id"])["status"] == "pending"
    # turning the rule on checks the waiting request too
    client.post("/api/repo/mine/auto-approve", json={"workspaces": True, "check": "grep -q 'int(env)' config.py"})
    autoapprove.wait()
    assert store.get_request(req["id"])["status"] == "approved"
    assert "int(env)" in (Path(conflicted[1]) / "config.py").read_text()


def test_pushes_never_approve_themselves():
    assert autoapprove.kind_of({"type": "push"}) is None and autoapprove.kind_of({"type": "actions"}) is None
    assert autoapprove.kind_of({"safe": False, "conflicts": ["x"], "upstreams": []}) is None


# ------------------------------------------------------------ 3. groups
def test_groups_filter_fetch_and_check(client, repos):
    up, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = client.post("/api/repo/mine/groups", json={"groups": "Glance, web , glance, bad name!"}).get_json()
    assert r["groups"] == ["glance", "web"] and r["all"] == {"glance": ["mine"], "web": ["mine"]}
    assert [x["id"] for x in client.get("/api/repos?group=glance").get_json()] == ["mine"]
    assert client.get("/api/repos?group=other").get_json() == []
    commit_file(up, "b.txt", "b\n", "upstream b")
    f = client.post("/api/groups/glance/fetch", json={}).get_json()
    assert f["ok"] and f["results"][0]["repo"] == "mine"
    assert client.post("/api/groups/nope/fetch", json={}).status_code == 404
    # the daily check for one group leaves the others alone
    assert client.post("/api/run-daily", json={"group": "other"}).get_json()["results"] == []
    assert client.post("/api/run-daily", json={"group": "glance"}).get_json()["results"]


# ------------------------------------------------------------ 4. GitHub issues
def test_import_issues_once_each(client, repos, monkeypatch):
    from upstream_upkeep import actions
    up, mine = repos
    git(mine, "remote", "set-url", "origin", "https://github.com/acme/widget.git")
    registry.add_repo(str(mine), None, "mine")
    issue = {"number": 7, "title": "Crash on save", "html_url": "https://github.com/acme/widget/issues/7", "state": "open",
             "labels": [{"name": "bug"}], "body": "Steps: press save", "user": {"login": "sam"}, "comments": 2}
    pr = {"number": 8, "title": "a PR", "html_url": "https://github.com/acme/widget/pull/8", "pull_request": {}}
    calls = []

    def fake_get(path, params=None):
        calls.append(path)
        if path == "/repos/acme/widget/issues":
            return [issue, pr]
        if path.endswith("/7"):
            return issue
        if path.endswith("/8"):
            return pr
        raise AssertionError(path)
    monkeypatch.setattr(actions, "_get", fake_get)
    lst = client.get("/api/repo/mine/issues").get_json()
    assert lst["source"]["slug"] == "acme/widget" and [i["number"] for i in lst["items"]] == [7]
    r = client.post("/api/repo/mine/issues/import", json={"numbers": [7, 8]}).get_json()
    assert len(r["created"]) == 1 and r["skipped"][0]["number"] == 8
    t = r["created"][0]
    assert t["kind"] == "bug" and t["title"] == "#7 Crash on save" and "press save" in t["body"]
    assert t["about"][0] == {"type": "issue", "id": issue["html_url"], "label": "acme/widget#7"}
    again = client.post("/api/repo/mine/issues/import", json={"numbers": [7]}).get_json()
    assert not again["created"] and again["skipped"][0]["why"] == "already a task"
    assert client.get("/api/repo/mine/issues").get_json()["items"][0]["task"] == t["id"]


def test_issues_need_a_github_remote(client, repos):
    registry.add_repo(str(repos[1]), None, "mine")
    r = client.get("/api/repo/mine/issues")
    assert r.status_code == 400 and "github.com" in r.get_json()["error"]


# ------------------------------------------------------------ 5. MCP over HTTP
def _free_port():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_mcp_over_http_needs_the_token(client, repos, monkeypatch):
    import asyncio
    import threading
    import time

    import requests as rq
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from upstream_upkeep import mcp_server
    from test_agents import _route_to
    registry.add_repo(str(repos[1]), None, "mine")
    port = _free_port()
    with pytest.raises(SystemExit):
        mcp_server.http_app("0.0.0.0", token="")          # beyond this machine only with a token
    srv = uvicorn.Server(uvicorn.Config(mcp_server.http_app("0.0.0.0", token="s3cret"), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        assert rq.post(url, json={}, timeout=5).status_code == 401
        assert rq.post(url, json={}, headers={"Authorization": "Bearer nope"}, timeout=5).status_code == 401
        _route_to(client, monkeypatch)                      # the tools' calls to the app go to the test app

        async def go():
            import httpx2
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer s3cret"}, trust_env=False) as hc:
                async with streamable_http_client(url, http_client=hc) as (r, w, *_):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        names = [t.name for t in (await s.list_tools()).tools]
                        res = await s.call_tool("list_repos", {})
                        return names, res
        names, res = asyncio.run(go())
        assert "list_repos" in names and "import_issues" in names and "set_repo_groups" in names
        assert not res.is_error and "mine" in res.content[0].text
    finally:
        srv.should_exit = True
        th.join(5)


def test_a_failed_check_is_tried_again_after_another_request_goes_in(client, repos):
    up, mine = _clean_upstream(repos)
    store.record_request({"id": "mine:other", "repo_id": "mine", "focus_branch": "main", "status": "pending",
                          "safe": True, "conflicts": [], "created_at": "x", "auto": {"state": "failed"},
                          "upstreams": [{"name": "origin", "target": "origin/main", "conflict": False}]})
    calls = []
    real = autoapprove._on_request
    autoapprove._on_request = lambda rec: calls.append(rec["id"]) if rec["id"] == "mine:other" else real(rec)
    try:
        client.post("/api/repo/mine/auto-approve", json={"clean": True})
        client.post("/api/repo/mine/fetch", json={})
        autoapprove.consider(client.post("/api/repo/mine/upstream/origin/request-pull", json={"target": "origin/main"}).get_json()["request"]["id"])
    finally:
        autoapprove._on_request = real
    assert "mine:other" in calls


# ------------------------------------------------------------ release wait
def _old_commit(repo, name, days_ago, msg):
    import time as _t
    when = str(int(_t.time() - days_ago * 86400))
    env = {**os.environ, "GIT_AUTHOR_DATE": f"@{when}", "GIT_COMMITTER_DATE": f"@{when}"}
    Path(repo, name).write_text(msg + "\n")
    import subprocess
    subprocess.run(["git", "add", name], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True, env=env)


def test_release_wait_takes_only_old_enough_commits(client, repos):
    up, mine = repos
    registry.add_repo(str(mine), None, "mine")
    _old_commit(up, "old.txt", 5, "five days old")
    _old_commit(up, "new.txt", 0.1, "brand new")
    agent = {"X-Upstream-Client": "agent"}
    assert client.post("/api/repo/mine/release-wait", json={"days": 0}, headers=agent).status_code == 403
    assert client.post("/api/repo/mine/release-wait", json={"days": 2}).get_json()["upstreams"] == {"origin": 2.0}
    plan = client.post("/api/repo/mine/fetch", json={}).get_json()["plan"]
    u = plan["upstreams"][0]
    assert u["behind"] == 1 and u["waiting"] == 1 and u["ready_at"] and u["target"], u
    res = client.post("/api/run-daily", json={}).get_json()["results"]
    assert any(r.get("status") == "merged" for r in res), res
    assert (Path(mine) / "old.txt").is_file() and not (Path(mine) / "new.txt").exists()
    # nothing old enough left: reads as up to date, with one waiting
    u = client.get("/api/repo/mine/compare").get_json()["plan"]["upstreams"][0]
    assert u["behind"] == 0 and u["waiting"] == 1
    client.post("/api/repo/mine/release-wait", json={"days": 0})
    u = client.get("/api/repo/mine/compare").get_json()["plan"]["upstreams"][0]
    assert u["behind"] == 1 and "waiting" not in u


# ------------------------------------------------------------ an agent changes code inside a submodule
def test_a_submodule_commit_made_in_a_workspace_reaches_the_users_checkout(client, tmp_path, monkeypatch):
    import subprocess
    for k, v in {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}.items():
        monkeypatch.setenv(k, v)   # the test's submodule upstream is a local path
    g = lambda cwd, *a: subprocess.run(["git", "-c", "user.email=t@t.t", "-c", "user.name=T", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    api = tmp_path / "up-api"; api.mkdir(); g(api, "init", "-q", "-b", "main")
    (api / "server.js").write_text("ok for every path\n"); g(api, "add", "-A"); g(api, "commit", "-qm", "api")
    hub_up = tmp_path / "up-hub"; hub_up.mkdir(); g(hub_up, "init", "-q", "-b", "main")
    g(hub_up, "submodule", "add", "-q", str(api), "api"); g(hub_up, "commit", "-qm", "hub")
    hub = tmp_path / "hub"; g(tmp_path, "clone", "-q", "--recurse-submodules", str(hub_up), str(hub))
    registry.add_repo(str(hub), None, "hub")
    # the agent, in a change workspace: commits inside the submodule, then the new pin
    ws = client.post("/api/repo/hub/workspaces", json={"change": "404 for unknown paths"}).get_json()["workspace"]
    p = Path(ws["path"])
    g(p, "submodule", "update", "--init")
    (p / "api" / "server.js").write_text("404 for unknown paths\n")
    g(p / "api", "commit", "-qam", "api: 404 for unknown paths")
    new_sha = g(p / "api", "rev-parse", "HEAD")
    g(p, "commit", "-qam", "bump api")
    st = client.get(f"/api/repo/hub/workspaces/{ws['name']}").get_json()
    st = st.get("workspace", st)
    assert st["submodule_changes"] == [{"path": "api", "name": "api", "sha": new_sha, "local": True}]
    # the auto-approval check fetches that commit from the workspace, and approval brings it over
    client.post("/api/repo/hub/auto-approve", json={"workspaces": True, "check": "grep -q 404 api/server.js"})
    req = client.post(f"/api/repo/hub/workspaces/{ws['name']}/submit", json={"summary": "404"}).get_json()["request"]
    assert "carries a new commit" in req["upstreams"][0]["reason"]
    autoapprove.wait()
    done = store.get_request(req["id"])
    assert done["status"] == "approved", (done.get("auto"), done.get("result"))
    assert (hub / "api" / "server.js").read_text() == "404 for unknown paths\n"
    assert g(hub / "api", "rev-parse", "HEAD") == new_sha
    assert g(hub / "api", "rev-parse", f"refs/upkeep/local/{new_sha}") == new_sha   # kept until you push it
    # the submodule's upstream was not touched
    assert subprocess.run(["git", "cat-file", "-e", new_sha], cwd=api).returncode != 0
    assert any("on this machine only" in o["detail"] for o in done["result"])
