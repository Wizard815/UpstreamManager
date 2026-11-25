"""The LLM wiki: search and read for agents, the priorities page the Overseer follows, finished tasks
recorded in the vault, and the connection Studio hands to Hermes. Against a stand-in vault (fake_vault.py)
served over HTTP: MCP writer tools and the Local REST API, both behind a key."""
import time
from pathlib import Path

import pytest

from upstream_upkeep import board, overseer, registry, store, tasks, wiki

from fake_vault import serve_in_thread

AGENT = {"X-Upstream-Client": "agent"}
KEY = "vault-key-123"


@pytest.fixture
def vault(tmp_path, monkeypatch, repos):
    root = tmp_path / "vault"
    (root / "wiki" / "projects").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / "wiki" / "projects" / "mine.md").write_text(
        "---\nstatus: stable\n---\n# mine\nThe repo mine runs the house dashboard.\nDeploy with `make ship`; token: abcdef123456 never.\n")
    (root / "wiki" / "Release process.md").write_text("# Release process\nWe tag releases as vX.Y.Z and wait a day before deploying.\n")
    (root / "wiki" / "Upstream priorities.md").write_text(
        "# Upstream priorities\n\n## This week\n- mine: the dashboard login is broken, urgent\n- other-repo is fine\n\n"
        "## Later\n- #tools when there's time\n")
    (root / "daily").mkdir()
    base, app, server = serve_in_thread(root, KEY)
    monkeypatch.setenv("LLMWIKI_MCP_URL", base + "/mcp")
    monkeypatch.setenv("LLMWIKI_API_KEY", KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1"); monkeypatch.setenv("no_proxy", "127.0.0.1")
    store.settings().pop("wiki", None)
    wiki._CACHE.clear()
    registry.add_repo(str(repos[1]), None, "mine")
    yield root, app
    server.should_exit = True


def _wait(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def test_reading_turns_on_from_env_and_agents_can_search_and_read(client, vault):
    st = client.get("/api/wiki").get_json()
    assert st["ok"] and st["mode"] == "rest" and st["notes"] == 3, st
    hits = client.get("/api/wiki/search?q=release deploying").get_json()["items"]
    assert hits[0]["note"] == "Release process" and "wait a day" in hits[0]["lines"][0]
    note = client.get("/api/wiki/note?path=projects/mine").get_json()
    assert note["ok"] and "house dashboard" in note["text"] and "abcdef123456" not in note["text"]   # secret removed
    assert client.get("/api/wiki/note?path=../daily/x").status_code == 400
    assert client.get("/api/wiki/note?path=nope").status_code == 404
    # and through the agents' MCP tools
    # and through the agents' MCP tools (only the MCP server's calls go to the test app; the app's own
    # calls to the vault stay real)
    from types import SimpleNamespace
    import requests as real
    from upstream_upkeep import mcp_server
    from test_agents import _run

    def fake(method, url, params=None, json=None, timeout=None, headers=None):
        r = client.open(url[len(mcp_server.APP):], method=method, query_string=params, json=json, headers=headers)
        return SimpleNamespace(status_code=r.status_code, json=lambda: r.get_json())
    saved = mcp_server.requests
    mcp_server.requests = SimpleNamespace(request=fake, ConnectionError=real.ConnectionError)
    try:
        async def go(c):
            return await c.call_tool("wiki_search", {"query": "dashboard"}), await c.call_tool("wiki_read", {"path": "wiki/projects/mine.md"})
        found, read = _run(go)
        assert not found.is_error and "mine" in found.content[0].text, found.content[0].text
        assert not read.is_error and "make ship" in read.content[0].text
    finally:
        mcp_server.requests = saved


def test_the_overseer_follows_the_wiki_priorities_page(client, vault, tmp_path):
    p = wiki.priorities()
    assert p["note"] == "Upstream priorities" and p["repos"]["mine"]["word"] == "urgent", p
    rep = overseer.run("test")
    mine = next(r for r in rep["repos"] if r["id"] == "mine")
    assert mine["score"] >= 60 and any("wiki's priorities page: urgent" in w for w in mine["why"])
    assert "What the user wrote in the wiki" in client.get("/api/overseer/plan").get_json()["text"]
    # pause it in the wiki: agents get no task from it unless they ask for that repo
    root, _ = vault
    (root / "wiki" / "Upstream priorities.md").write_text("# Upstream priorities\n- mine: on hold until the move\n")
    wiki._CACHE.clear()
    t = client.post("/api/tasks", json={"repo": "mine", "title": "Something", "kind": "fix"}).get_json()["task"]
    assert client.post("/api/overseer/next", json={"agent": "claude-1"}, headers=AGENT).get_json()["task"] is None
    got = client.post("/api/overseer/next", json={"agent": "claude-1", "repo": "mine"}, headers=AGENT).get_json()
    assert got["task"]["id"] == t["id"]
    assert overseer.run("paused")["repos"][0]["paused"]


def test_a_finished_task_is_recorded_in_the_vault(client, vault):
    root, app = vault
    t = client.post("/api/tasks", json={"repo": "mine", "title": "Fix the dashboard login", "kind": "bug",
                                        "body": "Logging in loops back to the login page."}).get_json()["task"]
    client.post(f"/api/tasks/{t['id']}/claim", json={"agent": "claude-1"}, headers=AGENT)
    client.post(f"/api/tasks/{t['id']}/comments", json={"agent": "claude-1", "text": "The session cookie had the wrong path; fixed in auth.py."}, headers=AGENT)
    client.post(f"/api/tasks/{t['id']}/release", json={"agent": "claude-1", "status": "done", "note": "submitted"}, headers=AGENT)
    assert _wait(lambda: any(c["tool"] == "memory_capture" for c in app.state.calls))
    log = next(c for c in app.state.calls if c["tool"] == "memory_log")
    assert log["text"].startswith("Upkeep task done: Fix the dashboard login (mine, bug, by claude-1)") and log["source"] == "upstream-manager"
    cap = next(c for c in app.state.calls if c["tool"] == "memory_capture")
    assert cap["title"] == "Done: Fix the dashboard login"
    assert "session cookie had the wrong path" in cap["content"] and "Logging in loops" in cap["content"]
    assert list((root / "inbox").glob("*Done*Fix the dashboard login*.md"))
    assert not any(p.name.startswith("Done") for p in (root / "wiki").rglob("*"))      # never into the long-term tier
    # switched off: nothing more is sent
    client.post("/api/wiki/settings", json={"record_tasks": False})
    n = len(app.state.calls)
    t2 = client.post("/api/tasks", json={"repo": "mine", "title": "Quiet", "kind": "fix"}).get_json()["task"]
    client.post(f"/api/tasks/{t2['id']}", json={"status": "done"})
    time.sleep(1)
    assert len(app.state.calls) == n


def test_studio_gets_the_wiki_connection_and_agents_never_do(client, vault, monkeypatch):
    assert client.get("/api/wiki/connect").status_code == 403
    monkeypatch.setattr("upstream_upkeep.app._via_trusted_proxy", lambda: True)
    assert client.get("/api/wiki/connect", headers=AGENT).status_code == 403
    c = client.get("/api/wiki/connect").get_json()
    assert c["ok"] and c["url"].endswith("/mcp") and c["key"] == KEY


def test_search_takes_a_sentence(client, vault):
    hits = client.get("/api/wiki/search?q=how do we deploy the house dashboard repo").get_json()["items"]
    assert hits and hits[0]["path"] == "wiki/projects/mine.md", hits
    hits = client.get("/api/wiki/search?q=how do we tag releases").get_json()["items"]
    assert hits[0]["note"] == "Release process"
