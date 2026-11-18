"""Upstream inside Hermes Studio: the login Studio hands over, herdr agents started as Studio chats,
Studio reporting agent status back, and services / marks from the env file."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from upstream_upkeep import credentials, services, store, studio, tasks

from test_herdr_logic import FakeHerdr, new_task  # noqa: F401  (fixtures reuse)
from upstream_upkeep import registry

PROXY = {"X-Upstream-Base": "/upstream"}


class FakeStudio:
    """Just enough of Studio: its herdr bridge state and the agent launch route."""

    def __init__(self, connected=True):
        self.calls, self.connected, self.token_seen = [], connected, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)

            def do_GET(self):
                outer.token_seen.append(self.headers.get("Authorization"))
                if self.path.startswith("/api/coding-agents/herdr"):
                    return self._send(200, {"bridge": {"connected": outer.connected, "panes": [
                        {"pane_id": "w9:p1", "studioSessionId": "herdr-abc"}]}})
                self._send(200, {"items": []})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.calls.append((self.path, body, self.headers.get("Authorization")))
                if self.path == "/api/coding-agents/herdr/agents":
                    return self._send(200, {"link": {"paneId": "w9:p1", "workspaceId": "w9"}})
                self._send(404, {"error": "no"})

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


@pytest.fixture
def fake_studio(monkeypatch):
    monkeypatch.delenv("HERMES_STUDIO_URL", raising=False)
    studio._FROM_STUDIO.clear(); studio._HERDR.clear(); credentials.set("studio_proxy_token", "")
    f = FakeStudio()
    yield f
    f.close()
    studio._FROM_STUDIO.clear(); studio._HERDR.clear(); credentials.set("studio_proxy_token", "")


def test_opening_upstream_in_studio_connects_them(client, fake_studio):
    assert not studio.settings()["enabled"]
    client.get("/api/repos", headers={**PROXY, "X-Studio-Url": fake_studio.url, "X-Studio-Token": "jwt-1"})
    s = studio.settings()
    assert s["enabled"] and s["url"] == fake_studio.url and studio._token() == "jwt-1"
    assert credentials.get("studio_proxy_token") == "jwt-1"
    st = client.get("/api/integrations").get_json()["studio"]
    assert st["from_studio"] and st["herdr_via_studio"] == "auto"
    # a newer login replaces it
    client.get("/api/repos", headers={**PROXY, "X-Studio-Url": fake_studio.url, "X-Studio-Token": "jwt-2"})
    assert studio._token() == "jwt-2"


def test_a_page_cannot_hand_over_a_studio_login(client, fake_studio):
    # no proxy header: a browser request
    client.get("/api/repos", headers={"X-Studio-Url": "http://evil", "X-Studio-Token": "x"})
    # proxy header but from another machine
    client.get("/api/repos", headers={**PROXY, "X-Studio-Url": "http://evil", "X-Studio-Token": "x"},
               environ_base={"REMOTE_ADDR": "10.1.2.3"})
    assert not studio._FROM_STUDIO and not credentials.get("studio_proxy_token")
    assert not studio.adopt("javascript:alert(1)", "x") and not studio.adopt(fake_studio.url, "")


def test_an_explicit_studio_login_wins(client, fake_studio, monkeypatch):
    monkeypatch.setenv("UPSTREAM_STUDIO_TOKEN", "mine")
    studio.adopt(fake_studio.url, "from-proxy")
    assert studio._token() == "mine"


@pytest.fixture
def hd_studio(client, repos, fake_studio):
    f = FakeHerdr()
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    client.post("/api/integrations/settings", json={"herdr": {"enabled": True, "socket": f.path, "kind": "claude"}})
    studio.adopt(fake_studio.url, "jwt")
    yield f, repo, fake_studio
    f.close()


def test_herdr_agents_start_as_studio_chats_on_this_machine(client, hd_studio):
    f, repo, st = hd_studio
    t = new_task(client, repo, to="herdr")
    assert t["ok"], t
    w = t["where"]
    assert w["via"] == "studio" and w["agent"] == "w9:p1" and w["kind"] == "claude" and not w["ours"]
    path, body, auth = st.calls[-1]
    assert path == "/api/coding-agents/herdr/agents" and auth == "Bearer jwt"
    assert body["agentId"] == "claude-code" and body["workspace"] == w["workdir"] and "Do not commit" in body["prompt"]
    assert body["upstream"] == {"task": t["task"]["id"], "title": t["task"]["title"], "repo": "mine"}
    assert not f.agents   # Upstream's own herdr wasn't used
    assert studio.session_for_pane("w9:p1") == "herdr-abc"


def test_falls_back_to_its_own_herdr_when_studio_cannot(client, hd_studio):
    f, repo, st = hd_studio
    st.connected = False
    t = new_task(client, repo, to="herdr")
    assert t["where"].get("via") is None and t["where"]["ours"] and f.agents


def test_studio_reports_agent_status(client, hd_studio):
    f, repo, st = hd_studio
    t = new_task(client, repo, to="herdr")
    tid = t["task"]["id"]
    url = f"/api/tasks/{tid}/agent-status"
    assert client.post(url, json={"status": "blocked", "pane": "w9:p1"}).status_code == 403           # not from Studio
    assert client.post(url, json={"status": "blocked"}, headers={**PROXY, "X-Upstream-Client": "agent"}).status_code == 403
    assert client.post(url, json={"status": "nope"}, headers=PROXY).status_code == 400
    assert client.post(url, json={"status": "blocked", "pane": "w9:p1"}, headers=PROXY).get_json()["ok"]
    assert client.post(url, json={"status": "done", "pane": "w0:p0"}, headers=PROXY).get_json()["ignored"]
    assert client.post(url, json={"status": "gone", "pane": "w9:p1"}, headers=PROXY).get_json()["ok"]
    assert tasks.get(tid)["where"]["agent"] is None


def test_services_and_marks_from_the_env_file(client, repos, monkeypatch):
    store.save_repo({"id": "ff", "name": "firefly-iii", "path": str(repos[1]), "upstreams": []})
    store.save_repo({"id": "coin", "name": "coinpile", "path": str(repos[1]), "upstreams": []})
    monkeypatch.setenv("UPSTREAM_SERVICES", "firefly=http://127.0.0.1:1/health@firefly-iii, db = tcp://127.0.0.1:5432 ,"
                                            "bad=ftp://x, nourl, ghost=http://127.0.0.1:2@nothere")
    monkeypatch.setenv("UPSTREAM_MARKS", "coinpile:planned, firefly-iii:nonsense")
    env = {s["name"]: s for s in services.from_env()}
    assert set(env) == {"firefly", "db", "ghost"}
    assert env["firefly"]["url"] == "http://127.0.0.1:1" and env["firefly"]["health"] == "/health" and env["firefly"]["repo"] == "ff"
    assert env["db"]["url"] == "tcp://127.0.0.1:5432" and env["ghost"]["repo"] is None and "isn't tracked" in env["ghost"]["notes"]
    o = client.get("/api/services?live=0").get_json()
    rows = {r["id"]: r for r in o["repos"]}
    assert rows["coin"]["state"] == "planned" and rows["coin"]["from_env"]
    assert any(s.get("from_env") for s in o["services"])
    assert client.post("/api/services/delete", json={"id": env["db"]["id"]}).status_code == 400
    assert client.post("/api/services", json={"name": "FIREFLY", "url": "http://x:1"}).status_code == 400
    # a mark made on the page wins over the file
    client.post("/api/repo/coin/integration", json={"state": "integrated"})
    assert {r["id"]: r for r in client.get("/api/services?live=0").get_json()["repos"]}["coin"]["state"] == "integrated"
