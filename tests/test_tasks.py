"""Tasks: work on something here, hand it to Hermes, or park it in the wiki or Pondering Orb."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from upstream_upkeep import credentials, registry, store, tasks, wiki


class _Fake(BaseHTTPRequestHandler):
    calls: list = []
    sessions: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        type(self).calls.append(("GET", self.path, None, self.headers.get("Authorization")))
        if self.path.startswith("/v1/capabilities"):
            return self._send(200, {"object": "hermes.capabilities"})
        if self.path.startswith("/api/sessions/") and "/messages" in self.path:
            sid = self.path.split("/")[3]
            return self._send(200, {"data": type(self).sessions.get(sid, [])})
        if self.path.startswith("/api/c/tasks"):
            return self._send(200, [])
        self._send(404, {})

    def do_POST(self):
        body = self._body()
        type(self).calls.append(("POST", self.path, body, self.headers.get("Authorization")))
        if self.path == "/api/sessions":
            sid = "api_1"
            type(self).sessions[sid] = []
            return self._send(201, {"session": {"id": sid, "title": body.get("title")}})
        if self.path.endswith("/chat"):
            sid = self.path.split("/")[3]
            type(self).sessions[sid] += [{"role": "user", "content": body["message"]},
                                         {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "terminal"}}]},
                                         {"role": "assistant", "content": "Fixed it and committed."}]
            return self._send(200, {"message": {"role": "assistant", "content": "Fixed it and committed."}})
        if self.path == "/api/c/tasks":
            return self._send(201, {"id": "t-9", **body})
        self._send(404, {})


@pytest.fixture
def fake():
    _Fake.calls, _Fake.sessions = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def repo(repos):
    up, mine = repos
    return registry.add_repo(str(mine), None, "mine")


def test_create_validates_and_links_what_it_is_about(client, repo):
    bad = client.post("/api/tasks", json={"repo": repo["id"], "title": " "}).get_json()
    assert bad["ok"] is False and "title" in bad["error"]
    bad = client.post("/api/tasks", json={"repo": repo["id"], "title": "x", "about": [{"type": "nope", "id": "1"}]})
    assert bad.status_code == 400
    r = client.post("/api/tasks", json={"repo": repo["id"], "title": "Fix the parser", "kind": "bug",
                                        "about": [{"type": "commit", "id": "abc1234", "label": "abc1234 parser"}]}).get_json()
    t = r["task"]
    assert t["status"] == "open" and t["about"][0]["type"] == "commit" and t["repo_name"] == "mine"
    assert [x["id"] for x in client.get(f"/api/tasks?repo={repo['id']}").get_json()["items"]] == [t["id"]]
    assert client.post("/api/tasks", json={"repo": "nope", "title": "x"}).status_code == 400


def test_work_here_opens_a_branch_and_a_chat_about_it(client, repo):
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Fix the parser", "kind": "fix",
                                        "about": [{"type": "commit", "id": "abc1234", "label": "abc1234"}]}).get_json()["task"]
    r = client.post(f"/api/tasks/{t['id']}/send", json={"to": "here"}).get_json()
    assert r["ok"] and r["task"]["status"] == "working"
    sid, ws = r["where"]["session"], r["where"]["workspace"]
    assert ws and "Fix the parser" in r["draft"] and "workspace" in r["draft"]
    sess = store.get_session(repo["id"], sid)
    assert sess["title"] == "Fix the parser" and sess["task"] == t["id"] and sess["tags"][0]["id"] == "abc1234"
    # sending it here again reuses the same workspace
    again = client.post(f"/api/tasks/{t['id']}/send", json={"to": "here"}).get_json()
    assert again["where"]["workspace"] == ws
    # an idea needs no branch
    idea = client.post("/api/tasks", json={"repo": repo["id"], "title": "Maybe a dark mode", "kind": "idea", "to": "here"}).get_json()
    assert idea["ok"] and idea["where"]["workspace"] is None


def test_hand_to_hermes_runs_in_the_background_and_shows_the_conversation(client, repo, fake):
    client.post("/api/integrations/settings", json={"hermes": {"enabled": True, "url": fake}})
    credentials.set("hermes_api_key", "k" * 40)
    try:
        t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Port the fetch code", "kind": "feature"}).get_json()["task"]
        # an agent may not hand work to Hermes
        r = client.post(f"/api/tasks/{t['id']}/send", json={"to": "hermes"}, headers={"X-Upstream-Client": "agent"})
        assert r.status_code == 400 and "up to the user" in r.get_json()["error"]
        r = client.post(f"/api/tasks/{t['id']}/send", json={"to": "hermes"}).get_json()
        assert r["ok"] and r["where"]["hermes"] == "api_1" and r["where"]["workdir"] != repo["path"]
        for _ in range(400):
            h = client.get(f"/api/tasks/{t['id']}/hermes").get_json()
            if not h["run"]["running"] and len(h["messages"]) == 3:
                break
            time.sleep(0.05)
        assert [m["role"] for m in h["messages"]] == ["user", "tool", "assistant"]
        assert h["messages"][1]["content"] == "used terminal"
        made = next(c for c in _Fake.calls if c[1] == "/api/sessions")
        assert r["where"]["workdir"] in made[2]["system_prompt"] and "Never push" in made[2]["system_prompt"]
        assert made[3] == "Bearer " + "k" * 40
        # follow-up message goes to the same session
        r2 = client.post(f"/api/tasks/{t['id']}/send", json={"to": "hermes", "message": "also add a test"}).get_json()
        assert r2["ok"] and r2["where"]["hermes"] == "api_1"
        assert [c[1] for c in _Fake.calls if c[1] == "/api/sessions"] == ["/api/sessions"]
    finally:
        credentials.delete("hermes_api_key")


def test_hermes_off_or_unreachable_is_a_clear_error(client, repo):
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "x"}).get_json()["task"]
    r = client.post(f"/api/tasks/{t['id']}/send", json={"to": "hermes"}).get_json()
    assert not r["ok"] and "Hermes isn't set up" in r["error"]
    client.post("/api/integrations/settings", json={"hermes": {"enabled": True, "url": "http://127.0.0.1:9"}})
    r = client.post(f"/api/tasks/{t['id']}/send", json={"to": "hermes"}).get_json()
    assert not r["ok"] and "can't reach Hermes" in r["error"]
    assert tasks.get(t["id"])["status"] == "open"


def test_park_in_pondering_orb(client, repo, fake):
    client.post("/api/integrations/settings", json={"orb": {"url": fake, "list": "Upstream"}})
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Look at the flaky test", "kind": "bug",
                                        "body": "token = abcdefghijklmnop happens", "to": "orb"}).get_json()
    assert t["ok"] and t["task"]["status"] == "parked" and t["where"]["id"] == "t-9"
    sent = next(c for c in _Fake.calls if c[0] == "POST" and c[1] == "/api/c/tasks")[2]
    assert sent["title"] == "Look at the flaky test" and sent["list"] == "Upstream" and "Repo: mine" in sent["notes"]
    assert client.get("/api/integrations").get_json()["orb"]["ok"]


def test_park_in_the_wiki_inbox_never_wiki_and_never_secrets(client, repo, tmp_path, monkeypatch):
    monkeypatch.delenv("LLMWIKI_MCP_URL", raising=False)
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    store.save_settings({"wiki": {"enabled": True, "mode": "folder", "path": str(vault), "url": "", "read": True}})
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Idea: cache/fetch results?", "kind": "idea",
                                        "body": "the key is sk-abcdefghijklmnopqrstuv", "to": "wiki"}).get_json()
    assert t["ok"], t
    (note,) = list((vault / "inbox").iterdir())
    assert note.name.endswith("Idea cache fetch results.md")
    text = note.read_text()
    assert "sk-abc" not in text and "[redacted]" in text and "Repo: mine" in text
    assert list((vault / "wiki").iterdir()) == []
    t2 = client.post("/api/tasks", json={"repo": repo["id"], "title": "Idea: cache/fetch results?", "to": "wiki"}).get_json()
    assert t2["ok"] and len(list((vault / "inbox").iterdir())) == 2


def test_park_in_the_wiki_uses_memory_capture_when_the_writer_is_set(client, repo, monkeypatch):
    seen = {}
    monkeypatch.setenv("LLMWIKI_MCP_URL", "http://vault/mcp")
    monkeypatch.setattr(wiki, "call_tool", lambda name, args: seen.update(name=name, **args) or "inbox/x.md")
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Later", "to": "wiki"}).get_json()
    assert t["ok"] and seen["name"] == "memory_capture" and seen["title"] == "Later" and "Repo: mine" in seen["content"]


def test_status_changes_and_deletes(client, repo):
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "x"}).get_json()["task"]
    r = client.post(f"/api/tasks/{t['id']}", json={"status": "done", "note": "shipped"}).get_json()
    assert r["task"]["status"] == "done" and r["task"]["log"][-1]["text"] == "shipped"
    assert client.post(f"/api/tasks/{t['id']}", json={"status": "weird"}).status_code == 400
    assert client.delete(f"/api/tasks/{t['id']}", json={}, headers={"X-Upstream-Client": "agent"}).status_code == 403
    assert client.delete(f"/api/tasks/{t['id']}", json={}).get_json()["ok"]
    assert client.get(f"/api/tasks/{t['id']}").status_code == 404


def test_a_merge_task_works_in_a_merge_workspace_for_its_upstream(client, repos):
    from conftest import commit_file, git
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    commit_file(up, "b.txt", "b\n", "upstream moves")
    git(mine, "fetch", "-q", "origin")
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Merge origin", "kind": "merge", "upstream": "origin", "to": "here"}).get_json()
    assert t["ok"], t
    ws = client.get(f"/api/repo/{repo['id']}/workspaces/{t['where']['workspace']}").get_json()
    assert "origin" in json.dumps(ws)
