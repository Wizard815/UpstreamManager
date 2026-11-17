"""Upstream Manager follows herdr's rules for agents: names, readiness, blocked dialogs, running agents, sessions."""
import json
import os
import socket
import tempfile
import threading

import pytest

from upstream_upkeep import herdr, registry, store, tasks


class FakeHerdr:
    """A herdr server that behaves like the real one where it matters: it refuses to prompt a blocked
    agent, starts some agents blocked (a trust dialog), and knows each agent's own session."""

    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="hd"), "h.sock")
        self.calls, self.agents, self.workspaces, self.block_on_start = [], {}, {}, False
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def info(self, a):
        return {"name": a["name"], "agent": a["kind"], "agent_status": a["status"], "pane_id": a["pane"], "workspace_id": a["pane"].split(":")[0],
                "cwd": a["cwd"], **({"agent_session": {"source": "hook", "agent": a["kind"], "kind": "id", "value": a["session"]}} if a.get("session") else {})}

    def handle(self, m, p):
        if m == "ping":
            return {"type": "pong", "version": "0.9", "protocol": 1}
        if m == "workspace.create":
            wid = f"w{len(self.workspaces) + 1}"
            self.workspaces[wid] = p
            return {"type": "workspace_created", "workspace": {"workspace_id": wid}, "tab": {}, "root_pane": {"pane_id": f"{wid}:p1"}}
        if m == "workspace.close":
            self.workspaces.pop(p["workspace_id"])
            return {"type": "ok"}
        if m == "agent.start":
            cwd = self.workspaces[p["pane_id"].split(":")[0]]["cwd"]
            self.agents[p["name"]] = {"name": p["name"], "kind": p["kind"], "pane": p["pane_id"], "cwd": cwd, "prompts": [], "keys": [],
                                      "status": "blocked" if self.block_on_start else "idle",
                                      "session": "20261117_abc123" if p["kind"] == "hermes" else None}
            if self.block_on_start:
                raise KeyError("agent_not_ready")
            return {"type": "agent_started", "agent": self.info(self.agents[p["name"]]), "argv": [p["kind"]]}
        if m == "agent.list":
            return {"type": "agent_list", "agents": [self.info(a) for a in self.agents.values()]}
        a = self.agents.get(p.get("target"))
        if a is None:
            raise KeyError("agent_not_found")
        if m == "agent.prompt":
            if a["status"] == "blocked":
                raise KeyError("agent_blocked")
            a["prompts"].append(p["text"])
            a["status"] = "working"
            return {"type": "agent_prompted", "agent": self.info(a)}
        if m == "agent.send_keys":
            a["keys"] += p["keys"]
            a["status"] = "idle"
            return {"type": "ok"}
        if m == "agent.get":
            return {"type": "agent_info", "agent": self.info(a)}
        if m == "agent.read":
            return {"type": "pane_read", "read": {"text": "Do you trust the files in this folder? (y/n)" if a["status"] == "blocked" else "working…"}}
        raise KeyError("unknown_method")

    def serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                req = json.loads(c.makefile().readline())
                self.calls.append(req)
                try:
                    out = {"id": req["id"], "result": self.handle(req["method"], req["params"])}
                except KeyError as e:
                    out = {"id": req["id"], "error": {"code": e.args[0], "message": e.args[0].replace("_", " ")}}
                c.sendall((json.dumps(out) + "\n").encode())


@pytest.fixture
def hd(client, repos):
    f = FakeHerdr()
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    client.post("/api/integrations/settings", json={"herdr": {"enabled": True, "socket": f.path, "kind": "claude"}})
    yield f, repo
    f.srv.close()


def new_task(client, repo, title="Fix the parser!", **extra):
    return client.post("/api/tasks", json={"repo": repo["id"], "title": title, "kind": "bug", **extra}).get_json()


def test_agent_names_are_readable_and_valid():
    import re
    for title in ["Fix the parser!", "修正 ünï", "123 go", "x" * 80, ""]:
        n = herdr.agent_name(title, "8F2A77aa")
        assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", n), n
    assert herdr.agent_name("Fix the parser!", "8f2a77") == "fix-the-parser-8f2a"


def test_you_pick_the_kind_and_the_workspace_knows_its_task(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr", agent_kind="hermes")
    assert t["ok"], t
    w = t["where"]
    assert w["kind"] == "hermes" and w["agent"].startswith("fix-the-parser-") and w["ours"] and w["pending"] is None
    made = f.workspaces[w["workspace_id"]]
    assert made["focus"] is False and made["env"]["UPSTREAM_TASK_ID"] == t["task"]["id"] and made["cwd"] == w["workdir"]
    assert "Do not commit" in f.agents[w["agent"]]["prompts"][0]


def test_an_agent_that_starts_blocked_gets_the_task_after_you_answer(client, hd):
    f, repo = hd
    f.block_on_start = True
    t = new_task(client, repo, to="herdr")
    assert t["ok"], t
    name, tid = t["where"]["agent"], t["task"]["id"]
    assert t["where"]["pending"] and f.agents[name]["prompts"] == []
    a = client.get(f"/api/tasks/{tid}/agent").get_json()
    assert a["status"] == "blocked" and a["pending"] is True and "trust the files" in a["output"]
    assert "asking you something" in tasks.get(tid)["log"][-1]["text"]
    # only you answer an agent; and only with real keys
    assert client.post(f"/api/tasks/{tid}/keys", json={"keys": ["y"]}, headers={"X-Upstream-Client": "agent"}).status_code == 403
    assert client.post(f"/api/tasks/{tid}/keys", json={"keys": ["rm -rf /"]}).status_code == 400
    assert client.post(f"/api/tasks/{tid}/keys", json={"keys": ["y", "enter"]}).get_json()["ok"]
    assert f.agents[name]["keys"] == ["y", "enter"]
    a = client.get(f"/api/tasks/{tid}/agent").get_json()  # now idle: the held task goes out
    assert a["pending"] is False and len(f.agents[name]["prompts"]) == 1
    assert tasks.get(tid)["where"]["pending"] is None


def test_a_follow_up_to_a_blocked_agent_waits_instead_of_failing(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr")
    name, tid = t["where"]["agent"], t["task"]["id"]
    f.agents[name]["status"] = "blocked"
    r = client.post(f"/api/tasks/{tid}/send", json={"to": "herdr", "message": "also add a test"}).get_json()
    assert r["ok"] and r["where"]["pending"] == "also add a test" and len(f.agents[name]["prompts"]) == 1
    f.agents[name]["status"] = "idle"
    client.get(f"/api/tasks/{tid}/agent")
    assert f.agents[name]["prompts"][-1] == "also add a test"


def test_a_task_can_go_to_an_agent_that_is_already_running(client, hd):
    f, repo = hd
    f.workspaces["w9"] = {"cwd": "/somewhere"}
    f.agents["reviewer"] = {"name": "reviewer", "kind": "codex", "pane": "w9:p1", "cwd": "/somewhere", "prompts": [], "keys": [], "status": "idle"}
    roster = client.get("/api/herdr/agents").get_json()["items"]
    assert [(a["name"], a["kind"], a["status"], a["task"]) for a in roster] == [("reviewer", "codex", "idle", None)]
    t = new_task(client, repo, to="herdr", agent="reviewer")
    assert t["ok"], t
    assert t["where"]["agent"] == "reviewer" and t["where"]["ours"] is False and len(f.workspaces) == 1  # no new workspace
    assert t["where"]["workdir"] in f.agents["reviewer"]["prompts"][0]  # it is told where the task's branch is
    assert client.get("/api/herdr/agents").get_json()["items"][0]["task"]["id"] == t["task"]["id"]
    # we never close a workspace we didn't open
    assert client.post(f"/api/tasks/{t['task']['id']}/herdr/close", json={}).status_code == 400 and "w9" in f.workspaces
    # an agent that doesn't exist is a clear error
    bad = new_task(client, repo, title="Other", to="herdr", agent="nobody")
    assert not bad["ok"] and "agent not found" in bad["error"]


def test_when_the_agent_is_gone_a_new_one_is_started(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr")
    name, tid = t["where"]["agent"], t["task"]["id"]
    del f.agents[name]  # its pane was closed in herdr
    a = client.get(f"/api/tasks/{tid}/agent").get_json()
    assert a["ok"] is False and a["gone"] and "no longer running" in a["error"]
    assert next(x for x in client.get("/api/tasks").get_json()["items"] if x["id"] == tid)["agent_status"] == "gone"
    r = client.post(f"/api/tasks/{tid}/send", json={"to": "herdr", "message": "carry on"}).get_json()
    assert r["ok"] and name in f.agents and len(f.workspaces) == 2


def test_finishing_is_noted_and_its_own_workspace_can_be_closed(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr")
    name, tid, wid = t["where"]["agent"], t["task"]["id"], t["where"]["workspace_id"]
    client.get(f"/api/tasks/{tid}/agent")           # working
    f.agents[name]["status"] = "done"
    client.get(f"/api/tasks/{tid}/agent")
    assert "finished" in tasks.get(tid)["log"][-1]["text"]
    assert client.post(f"/api/tasks/{tid}/herdr/close", json={}, headers={"X-Upstream-Client": "agent"}).status_code == 403
    assert client.post(f"/api/tasks/{tid}/herdr/close", json={}).get_json()["ok"] and wid not in f.workspaces


def test_a_hermes_agent_links_to_its_session_in_studio(client, hd):
    f, repo = hd
    store.save_settings({"studio": {"enabled": True, "url": "http://studio.lan:8648"}})
    t = new_task(client, repo, to="herdr", agent_kind="hermes")
    a = client.get(f"/api/tasks/{t['task']['id']}/agent").get_json()
    assert a["studio_url"] == "http://studio.lan:8648/#/hermes/session/20261117_abc123"
    t2 = new_task(client, repo, title="Other", to="herdr", agent_kind="codex")
    assert client.get(f"/api/tasks/{t2['task']['id']}/agent").get_json()["studio_url"] is None


def test_a_failed_start_leaves_no_empty_workspace(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr", agent_kind="Bad Kind")
    assert not t["ok"] and f.workspaces == {}
