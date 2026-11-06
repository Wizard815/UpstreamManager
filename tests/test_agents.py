"""Coding agents: briefing, conflict details, merge workspaces and the MCP server."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from upstream_upkeep import mcp_server, registry, store

from conftest import commit_file, git


@pytest.fixture
def conflicted(repos):
    """'mine' and its upstream both changed config.py; upstream also added docs.md."""
    up, mine = repos
    commit_file(up, "config.py", "PORT = 5000\nHOST = 'localhost'\n", "base config")
    git(mine, "pull", "-q", "--ff-only")
    commit_file(up, "config.py", "PORT = int(env)\nHOST = 'localhost'\n", "upstream: PORT from env")
    commit_file(up, "docs.md", "docs\n", "upstream: docs")
    commit_file(mine, "config.py", "PORT = 5000\nHOST = '0.0.0.0'\n", "mine: LAN")
    registry.add_repo(str(mine), None, "mine")
    return up, mine


def test_briefing_has_state_requests_and_rules(client, conflicted):
    client.post("/api/repo/mine/fetch", json={})
    client.post("/api/run-daily", json={})
    text = client.get("/api/repo/mine/briefing").get_json()["text"]
    assert "2 commit(s) BEHIND" in text and "conflict in 1 file(s): config.py" in text
    assert "Waiting for the user's approval" in text and "mine:main" in text
    assert "Never push" in text and "merge workspace" in text


def test_conflict_details_show_both_sides(client, conflicted):
    client.post("/api/repo/mine/fetch", json={})
    d = client.get("/api/repo/mine/conflicts?upstream=origin").get_json()
    assert d["ok"] and d["conflict_files"] == ["config.py"]
    f = d["files"][0]
    assert "+HOST = '0.0.0.0'" in f["yours"]["diff"] and "+PORT = int(env)" in f["upstream"]["diff"]
    assert f["yours"]["commits"][0].endswith("mine: LAN") and f["upstream"]["commits"][0].endswith("PORT from env")
    assert client.get("/api/repo/mine/conflicts?upstream=nope").status_code == 400


def test_merge_workspace_flow(client, conflicted):
    _, mine = conflicted
    client.post("/api/repo/mine/fetch", json={})
    before = git(mine, "rev-parse", "HEAD")
    ws = client.post("/api/repo/mine/workspaces", json={"upstream": "origin"}).get_json()["workspace"]
    assert ws["merging"] and ws["unresolved"] == ["config.py"] and not ws["ready"]
    assert git(mine, "rev-parse", "HEAD") == before and git(mine, "status", "--porcelain") == ""  # repo untouched
    # Opening again returns the same workspace.
    assert client.post("/api/repo/mine/workspaces", json={"upstream": "origin"}).get_json()["workspace"]["path"] == ws["path"]

    r = client.post(f"/api/repo/mine/workspaces/{ws['name']}/submit", json={"summary": "x"})
    assert r.status_code == 409 and "not ready" in r.get_json()["error"]

    Path(ws["path"], "config.py").write_text("PORT = int(env)\nHOST = '0.0.0.0'\n")
    git(ws["path"], "add", "config.py")
    git(ws["path"], "-c", "user.email=a@a", "-c", "user.name=A", "commit", "-q", "--no-edit")
    st = client.get(f"/api/repo/mine/workspaces/{ws['name']}").get_json()["workspace"]
    assert st["ready"] and st["commits_beyond_focus"] == 3

    req = client.post(f"/api/repo/mine/workspaces/{ws['name']}/submit", json={"summary": "kept both"}).get_json()["request"]
    assert req["summary"] == "kept both" and req["status"] == "pending"
    assert git(mine, "rev-parse", "HEAD") == before  # still nothing until approval

    out = client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()
    assert out["ok"] and "fast-forwarded" in out["request"]["result"][0]["detail"]
    assert Path(mine, "config.py").read_text() == "PORT = int(env)\nHOST = '0.0.0.0'\n" and Path(mine, "docs.md").exists()
    assert client.get("/api/repo/mine/workspaces").get_json()["items"] == []
    assert "upkeep/" not in git(mine, "branch", "--list")


def test_leftover_conflict_markers_block_submit(client, conflicted):
    client.post("/api/repo/mine/fetch", json={})
    ws = client.post("/api/repo/mine/workspaces", json={"upstream": "origin"}).get_json()["workspace"]
    git(ws["path"], "add", "config.py")  # "resolved" with the markers still in it
    git(ws["path"], "-c", "user.email=a@a", "-c", "user.name=A", "commit", "-q", "--no-edit")
    st = client.get(f"/api/repo/mine/workspaces/{ws['name']}").get_json()["workspace"]
    assert not st["ready"] and st["conflict_markers"] == ["config.py"]
    assert client.post(f"/api/repo/mine/workspaces/{ws['name']}/submit", json={"summary": ""}).status_code == 409


def test_workspace_names_are_checked(client, conflicted):
    assert client.get("/api/repo/mine/workspaces/..%2F..%2Fetc").status_code == 404
    assert client.delete("/api/repo/mine/workspaces/not-a-workspace", json={}).status_code == 400


def test_mcp_discard_removes_the_workspace(client, conflicted, monkeypatch):
    _route_to(client, monkeypatch)
    client.post("/api/repo/mine/fetch", json={})
    ws = client.post("/api/repo/mine/workspaces", json={"upstream": "origin"}).get_json()["workspace"]
    res = _run(lambda c: c.call_tool("discard_merge_workspace", {"repo": "mine", "name": ws["name"]}))
    assert not res.is_error and not Path(ws["path"]).exists()


def _route_to(client, monkeypatch):
    """Send the MCP server's HTTP calls to the Flask test client."""
    def fake(method, url, params=None, json=None, timeout=None, headers=None):
        path = url[len(mcp_server.APP):]
        r = client.open(path, method=method, query_string=params, json=json, headers=headers)
        return SimpleNamespace(status_code=r.status_code, json=lambda: r.get_json())
    monkeypatch.setattr(mcp_server.requests, "request", fake)


def _run(coro_fn):
    from mcp import Client

    async def go():
        async with Client(mcp_server.server) as c:
            return await coro_fn(c)
    return asyncio.run(go())


def test_mcp_tools_cannot_approve_merge_or_push():
    names = _run(lambda c: c.list_tools())
    names = [t.name for t in names.tools]
    assert "open_merge_workspace" in names and "repo_briefing" in names
    assert not any(w in n for n in names for w in ("approve", "decide", "push", "deny"))


def test_mcp_tools_reach_the_app_and_pass_errors(client, conflicted, monkeypatch):
    _route_to(client, monkeypatch)
    client.post("/api/repo/mine/fetch", json={})

    async def steps(c):
        repos = await c.call_tool("list_repos", {})
        brief = await c.call_tool("repo_briefing", {"repo": "mine"})
        ws = await c.call_tool("open_merge_workspace", {"repo": "mine", "upstream": "origin"})
        early = await c.call_tool("submit_merge_workspace", {"repo": "mine", "name": json.loads(ws.content[0].text)["name"], "summary": "x"})
        ghost = await c.call_tool("repo_briefing", {"repo": "ghost"})
        return repos, brief, ws, early, ghost
    repos, brief, ws, early, ghost = _run(steps)
    assert not repos.is_error and "mine" in repos.content[0].text
    assert "Never push" in brief.content[0].text
    assert "config.py" in ws.content[0].text
    assert early.is_error and "not ready" in early.content[0].text
    assert ghost.is_error and "list_repos" in ghost.content[0].text


def test_mcp_reports_a_stopped_app(monkeypatch):
    def down(*a, **k):
        raise mcp_server.requests.ConnectionError()
    monkeypatch.setattr(mcp_server.requests, "request", down)
    res = _run(lambda c: c.call_tool("list_repos", {}))
    assert res.is_error and "not running" in res.content[0].text


def test_agent_setup_endpoint(client):
    s = client.get("/api/agent-setup").get_json()
    assert s["command"][:3] == ["uv", "run", "--project"] and s["command"][-1] == "upstream-mcp"
    assert s["app_url"].startswith("http://")


def test_agent_tools_see_components_chats_and_settings(client, conflicted, monkeypatch):
    from upstream_upkeep import llm
    _route_to(client, monkeypatch)
    sid = client.post("/api/repo/mine/sessions", json={}).get_json()["session"]
    store.add_chat_message("mine", sid, "user", "Please keep HOST on 0.0.0.0 when you merge.")

    async def steps(c):
        chats = await c.call_tool("list_chats", {"repo": "mine"})
        chat = await c.call_tool("read_chat", {"repo": "mine", "session": sid})
        note = await c.call_tool("post_chat_note", {"repo": "mine", "session": sid, "text": "Merged upstream; HOST kept."})
        comps = await c.call_tool("components_status", {"repo": "mine"})
        focus = await c.call_tool("set_focus_branch", {"repo": "mine", "branch": "main"})
        off = await c.call_tool("set_upstream_enabled", {"repo": "mine", "upstream": "origin", "enabled": False})
        stack = await c.call_tool("stack_status", {"repo": "mine"})
        on = await c.call_tool("stack_set", {"repo": "mine", "env": {"A": "1"}})
        return chats, chat, note, comps, focus, off, stack, on
    chats, chat, note, comps, focus, off, stack, on = _run(steps)
    assert sid in chats.content[0].text and "keep HOST" in chat.content[0].text
    assert not note.is_error
    msgs = client.get(f"/api/chat/mine?session={sid}").get_json()["messages"]
    assert msgs[-1]["role"] == "agent" and msgs[-1]["content"] == "Merged upstream; HOST kept."
    hist = llm._history(store.get_session("mine", sid))
    assert hist[-1] == {"role": "assistant", "content": "[Note from the coding agent] Merged upstream; HOST kept."}
    assert not comps.is_error and not focus.is_error and not off.is_error and not stack.is_error and not on.is_error
    assert registry.get_repo("mine")["upstreams"][0]["enabled"] is False


def test_agent_requests_are_marked_and_user_only_things_refused(client, conflicted, monkeypatch):
    _route_to(client, monkeypatch)
    # Even if an agent calls the app directly through the MCP helper, approving is refused.
    with pytest.raises(mcp_server.AppError, match="up to the user"):
        mcp_server._call("POST", "/api/request/mine:main/decide", {"decision": "approve"})
    with pytest.raises(mcp_server.AppError, match="up to the user"):
        mcp_server._call("POST", "/api/repo/mine/stack/settings", {"enabled": True})
    names = [t.name for t in _run(lambda c: c.list_tools()).tools]
    assert {"stack_up", "stack_request", "stack_exec", "components_status", "read_chat", "post_chat_note"} <= set(names)
