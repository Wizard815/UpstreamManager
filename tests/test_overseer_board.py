"""The board (claims, comments, messages), the Overseer, and notifications."""
import datetime as dt
import http.server
import json
import threading

import pytest

from upstream_upkeep import board, notify, overseer, registry, store, tasks

from conftest import commit_file
from test_agents import conflicted  # noqa: F401

AGENT = {"X-Upstream-Client": "agent"}


@pytest.fixture
def repo(repos):
    registry.add_repo(str(repos[1]), None, "mine")
    return repos


def _task(client, title="Fix it", priority="normal", kind="fix"):
    return client.post("/api/tasks", json={"repo": "mine", "title": title, "kind": kind, "priority": priority}).get_json()["task"]


# ------------------------------------------------------------ claims
def test_one_agent_per_task_and_claims_run_out(client, repo):
    t = _task(client)
    a = client.post(f"/api/tasks/{t['id']}/claim", json={"agent": "claude-1"}, headers=AGENT).get_json()
    assert a["ok"] and a["task"]["status"] == "working" and a["task"]["claimed_by"]["agent"] == "claude-1"
    b = client.post(f"/api/tasks/{t['id']}/claim", json={"agent": "codex-1"}, headers=AGENT).get_json()
    assert not b["ok"] and "claude-1 is on it" in b["error"]
    assert client.post(f"/api/tasks/{t['id']}/claim", json={"agent": "user"}, headers=AGENT).status_code == 400
    # the claim runs out: the Overseer puts it back, and another agent can take it
    with store.lock:
        tasks.get(t["id"])["claimed_by"]["until"] = "2000-01-01T00:00:00+00:00"
    rep = overseer.run("test")
    assert any("took back" in x for x in rep["actions"])
    back = tasks.get(t["id"])
    assert back["status"] == "open" and back["claimed_by"] is None and "went quiet" in back["comments"][-1]["text"]
    assert client.post(f"/api/tasks/{t['id']}/claim", json={"agent": "codex-1"}, headers=AGENT).get_json()["ok"]


def test_heartbeat_release_and_comments_reach_other_agents(client, repo):
    t = _task(client)
    client.post(f"/api/tasks/{t['id']}/claim", json={"agent": "claude-1", "minutes": 10}, headers=AGENT)
    before = tasks.get(t["id"])["claimed_by"]["until"]
    hb = client.post(f"/api/tasks/{t['id']}/heartbeat", json={"agent": "claude-1", "note": "tests pass", "minutes": 60}, headers=AGENT).get_json()
    assert hb["task"]["claimed_by"]["until"] > before and "claude-1: tests pass" in hb["task"]["log"][-1]["text"]
    c = client.post(f"/api/tasks/{t['id']}/comments", json={"agent": "claude-1", "text": "codex: can you take the docs?", "to": "codex-1"}, headers=AGENT).get_json()
    assert c["ok"] and c["comment"]["by"] == "claude-1"
    inbox = client.get("/api/messages?agent=codex-1", headers=AGENT).get_json()["items"]
    assert len(inbox) == 1 and "take the docs" in inbox[0]["text"] and inbox[0]["task"] == t["id"]
    assert client.get("/api/messages?agent=codex-1", headers=AGENT).get_json()["items"] == []     # read now
    # broadcast and messages to the user (rings)
    client.post("/api/messages", json={"agent": "codex-1", "to": "*", "text": "I'm changing the API, hold off"}, headers=AGENT)
    assert client.get("/api/messages?agent=claude-1", headers=AGENT).get_json()["items"][0]["from"] == "codex-1"
    client.post("/api/messages", json={"agent": "codex-1", "to": "user", "text": "need a token"}, headers=AGENT)
    assert any(n["event"] == "message" and "need a token" in n["message"] for n in notify.feed()["items"])
    # someone else can't release it
    assert client.post(f"/api/tasks/{t['id']}/release", json={"agent": "codex-1", "status": "done"}, headers=AGENT).status_code == 400
    r = client.post(f"/api/tasks/{t['id']}/release", json={"agent": "claude-1", "status": "done", "note": "submitted"}, headers=AGENT).get_json()
    assert r["task"]["status"] == "done" and r["task"]["claimed_by"] is None
    names = [a["name"] for a in client.get("/api/agents").get_json()["items"]]
    assert {"claude-1", "codex-1"} <= set(names)


# ------------------------------------------------------------ next_task and the order
def test_next_task_gives_the_most_important_open_one(client, repo):
    low = _task(client, "Tidy", "low", "chore")
    urgent = _task(client, "Prod is down", "urgent", "bug")
    idea = _task(client, "Maybe", "high", "idea")
    overseer.run("test")
    b = client.get("/api/board").get_json()["columns"]["open"]
    assert [c["id"] for c in b][:2] == [urgent["id"], idea["id"]] or b[0]["id"] == urgent["id"]
    got = client.post("/api/overseer/next", json={"agent": "claude-1"}, headers=AGENT).get_json()
    assert got["task"]["id"] == urgent["id"] and "Why now" in got["brief"] and got["task"]["claimed_by"]["agent"] == "claude-1"
    again = client.post("/api/overseer/next", json={"agent": "claude-1"}, headers=AGENT).get_json()
    assert again["already_yours"] and again["task"]["id"] == urgent["id"]       # finish what you have first
    other = client.post("/api/overseer/next", json={"agent": "codex-1"}, headers=AGENT).get_json()
    assert other["task"]["id"] == low["id"]                                    # ideas are never handed out
    none = client.post("/api/overseer/next", json={"agent": "third"}, headers=AGENT).get_json()
    assert none["task"] is None
    assert idea["id"]


def test_a_card_you_drag_keeps_its_place(client, repo):
    a, b = _task(client, "A", "high"), _task(client, "B", "low")
    client.post(f"/api/tasks/{b['id']}/move", json={"status": "open", "rank": 99999})
    overseer.run("test")
    assert client.get("/api/board").get_json()["columns"]["open"][0]["id"] == b["id"]
    client.post(f"/api/tasks/{a['id']}/move", json={"status": "parked"})
    assert tasks.get(a["id"])["status"] == "parked"


# ------------------------------------------------------------ findings
def test_overseer_makes_a_task_of_a_conflict_once_and_closes_it_when_gone(client, conflicted):  # noqa: F811
    client.post("/api/repo/mine/fetch", json={})
    overseer.run("test")
    mine = [t for t in tasks.list_tasks("mine") if (t.get("source") or {}).get("type") == "overseer"]
    assert len(mine) == 1 and mine[0]["kind"] == "merge" and mine[0]["priority"] == "high" and mine[0].get("upstream") == "origin"
    overseer.run("again")
    assert len([t for t in tasks.list_tasks("mine") if (t.get("source") or {}).get("type") == "overseer"]) == 1
    rep = overseer.last()
    assert rep["repos"][0]["id"] == "mine" and any("conflicts with origin" in w for w in rep["repos"][0]["why"])
    # the user disables the upstream: the problem is gone, the task closes
    client.post("/api/repo/mine/upstream/origin/enable", json={"enabled": False})
    overseer.run("gone")
    assert tasks.get(mine[0]["id"])["status"] == "done"


def test_repo_priority_is_the_users_and_weighs_first(client, repo):
    assert client.post("/api/repo/mine/priority", json={"priority": "critical"}, headers=AGENT).status_code == 403
    assert client.post("/api/repo/mine/priority", json={"priority": "critical"}).get_json()["ok"]
    rep = overseer.run("test")
    assert rep["repos"][0]["priority"] == "critical" and rep["repos"][0]["score"] >= 60
    assert "you marked it critical" in rep["repos"][0]["why"]


def test_dispatch_hands_out_up_to_the_limit(client, repo, monkeypatch):
    for i in range(3):
        _task(client, f"Task {i}", "high")
    sent = []
    monkeypatch.setattr(overseer, "SEND", lambda tid, to, m=None, o=None: (sent.append((tid, to)), tasks.update(tid, status="working", where={"type": to}), {"ok": True})[-1])
    assert client.post("/api/overseer/settings", json={"dispatch": "herdr", "max_agents": 2}, headers=AGENT).status_code == 403
    client.post("/api/overseer/settings", json={"dispatch": "herdr", "max_agents": 2})
    overseer.run("test")
    assert len(sent) == 2 and all(to == "herdr" for _, to in sent)
    overseer.run("again")
    assert len(sent) == 2      # both still busy: no more


def test_plan_text_tells_agents_how_to_work_together(client, repo):
    _task(client, "Something", "high")
    text = client.get("/api/overseer/plan?agent=claude-1").get_json()["text"]
    assert "next_task" in text and "heartbeat_task" in text and "send_message" in text and "Something" in text


# ------------------------------------------------------------ notifications
class _Catch(http.server.BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        _Catch.got.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n).decode()})
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def catcher():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Catch)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Catch.got = []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_notifications_go_to_ntfy_and_the_webhook_once(client, repo, catcher, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1"); monkeypatch.setenv("no_proxy", "127.0.0.1")
    assert client.post("/api/notify/settings", json={"ntfy_url": "ftp://x"}).status_code == 400
    assert client.post("/api/notify/settings", json={"ntfy_url": catcher + "/topic"}, headers=AGENT).status_code == 403
    s = client.post("/api/notify/settings", json={"ntfy_url": catcher + "/topic", "webhook_url": catcher + "/hook",
                                                  "public_url": "http://box:5001"}).get_json()
    assert s["channels"] == ["ntfy", "webhook"]
    r = client.post("/api/notify/test", json={}).get_json()
    assert all(x["ok"] for x in r["sent"]) and len(r["sent"]) == 2
    ntfy = next(g for g in _Catch.got if g["path"] == "/topic")
    assert ntfy["headers"]["Title"] == "Upstream Manager test" and ntfy["headers"]["Click"] == "http://box:5001/overseer"
    hook = json.loads(next(g for g in _Catch.got if g["path"] == "/hook")["body"])
    assert hook["event"] == "overseer" and hook["url"] == "http://box:5001/overseer"
    # the same key twice: once
    assert notify.notify("overseer", "x", key="k1", wait=True) and notify.notify("overseer", "x", key="k1") is None
    # events turned off stay in the feed only
    client.post("/api/notify/settings", json={"events": {k: False for k in notify.EVENTS}})
    before = len(_Catch.got)
    notify.notify("request", "quiet", wait=True)
    assert len(_Catch.got) == before and notify.feed()["items"][0]["title"] == "quiet"
    assert client.post("/api/notifications/read", json={}).get_json()["marked"] >= 1 and notify.feed()["unread"] == 0


def test_a_new_request_rings_unless_a_rule_takes_it(client, repo):
    up, mine = repo
    commit_file(up, "n.txt", "n\n", "up"); commit_file(mine, "m.txt", "m\n", "mine")
    client.post("/api/repo/mine/fetch", json={})
    client.post("/api/repo/mine/upstream/origin/request-pull", json={"target": "origin/main"})
    assert any(n["event"] == "request" and n["repo"] == "mine" for n in notify.feed()["items"])


def test_an_agent_upstream_starts_holds_its_task_on_the_board(client, repo, monkeypatch):
    """herdr agents started for a task (by Upstream, or through Studio) show as holding it; their status keeps it."""
    t = _task(client, "Work for herdr")
    with store.lock:
        tasks.get(t["id"])["status"] = "working"
        tasks.get(t["id"])["where"] = {"type": "herdr", "via": "studio", "agent": "w1:p2", "machine": ""}
    board.hold(t["id"], tasks.board_name(tasks.get(t["id"])["where"]))
    assert tasks.get(t["id"])["claimed_by"]["agent"] == "herdr:w1:p2"
    assert "held by you as \"herdr:w1:p2\"" in tasks.agent_instructions(tasks.get(t["id"]), store.get_repo("mine"), "/x")
    # an agent elsewhere can't take it meanwhile
    assert not client.post(f"/api/tasks/{t['id']}/claim", json={"agent": "codex-1"}, headers=AGENT).get_json()["ok"]
    # Studio's report keeps it held; the pane closing lets it go
    monkeypatch.setattr("upstream_upkeep.app._via_trusted_proxy", lambda: True)
    client.post(f"/api/tasks/{t['id']}/agent-status", json={"status": "working", "pane": "w1:p2"})
    assert board.claim_alive(tasks.get(t["id"]))
    client.post(f"/api/tasks/{t['id']}/agent-status", json={"status": "gone", "pane": "w1:p2"})
    assert tasks.get(t["id"])["claimed_by"] is None
    m = client.get("/api/mcp").get_json()
    assert m["stdio_command"][1:] == ["-m", "upstream_upkeep.mcp_server"] and m["stdio_env"]["UPSTREAM_APP_URL"].startswith("http://127.0.0.1:")
