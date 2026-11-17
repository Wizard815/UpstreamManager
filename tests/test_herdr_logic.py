"""Upstream Manager follows herdr's rules for agents: names, readiness, blocked dialogs, running agents, sessions."""
import json
import os
import socket
import tempfile
import threading

import pytest

from upstream_upkeep import config, herdr, registry, store, tasks


class HerdrSays(Exception):
    """An error the real herdr would answer with (its code)."""


class FakeHerdr:
    """A herdr server that behaves like the real one where it matters: it refuses to prompt a blocked
    agent, starts some agents blocked (a trust dialog), and knows each agent's own session."""

    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="hd"), "h.sock")
        self.calls, self.agents, self.workspaces, self.block_on_start = [], {}, {}, False
        self.next_ws = 1          # ids are never reused, as in herdr
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        self.stopped = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self):
        """Stop for good. The serving thread must be out of accept() before the socket is closed: a thread
        left blocked there retries on the same descriptor number after a signal, and that number will by
        then belong to the next test's fake, whose connections it would answer with this one's state."""
        if self.stopped:
            return
        self.stopped = True
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as poke:   # wake accept()
                poke.settimeout(1)
                poke.connect(self.path)
        except OSError:
            pass
        self.thread.join(timeout=5)
        self.srv.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def info(self, a):
        return {"name": a["name"], "agent": a["kind"], "agent_status": a["status"], "pane_id": a["pane"], "workspace_id": a["pane"].split(":")[0],
                "cwd": a["cwd"], **({"agent_session": {"source": "hook", "agent": a["kind"], "kind": "id", "value": a["session"]}} if a.get("session") else {})}

    def handle(self, m, p):
        if m == "ping":
            return {"type": "pong", "version": "0.9", "protocol": 1}
        if m == "workspace.create":
            while f"w{self.next_ws}" in self.workspaces:
                self.next_ws += 1
            wid = f"w{self.next_ws}"
            self.next_ws += 1
            self.workspaces[wid] = p
            return {"type": "workspace_created", "workspace": {"workspace_id": wid}, "tab": {}, "root_pane": {"pane_id": f"{wid}:p1"}}
        if m == "workspace.close":
            if self.workspaces.pop(p["workspace_id"], None) is None:
                raise HerdrSays("workspace_not_found")
            return {"type": "ok"}
        if m == "agent.start":
            if p["pane_id"].split(":")[0] not in self.workspaces:
                raise HerdrSays("pane_not_found")
            cwd = self.workspaces[p["pane_id"].split(":")[0]]["cwd"]
            self.agents[p["name"]] = {"name": p["name"], "kind": p["kind"], "pane": p["pane_id"], "cwd": cwd, "prompts": [], "keys": [],
                                      "status": "blocked" if self.block_on_start else "idle",
                                      "session": "20261117_abc123" if p["kind"] == "hermes" else None}
            if self.block_on_start:
                raise HerdrSays("agent_not_ready")
            return {"type": "agent_started", "agent": self.info(self.agents[p["name"]]), "argv": [p["kind"]]}
        if m == "agent.list":
            return {"type": "agent_list", "agents": [self.info(a) for a in self.agents.values()]}
        a = self.agents.get(p.get("target"))
        if a is None:
            raise HerdrSays("agent_not_found")
        if m == "agent.prompt":
            if a["status"] == "blocked":
                raise HerdrSays("agent_blocked")
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
        raise HerdrSays("unknown_method")

    def serve(self):
        while not self.stopped:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                if self.stopped:
                    return
                try:
                    req = json.loads(c.makefile().readline())
                    self.calls.append(req)
                    out = {"id": req["id"], "result": self.handle(req["method"], req["params"])}
                except HerdrSays as e:
                    out = {"id": req["id"], "error": {"code": e.args[0], "message": e.args[0].replace("_", " ")}}
                except Exception as e:  # a bug in this fake, or a broken request: say exactly what, never pose as herdr
                    import traceback
                    out = {"id": "?", "error": {"code": "fake_bug", "message": f"{type(e).__name__}: {e} | {traceback.format_exc()[-400:]}"}}
                try:
                    c.sendall((json.dumps(out) + "\n").encode())
                except OSError:
                    pass


@pytest.fixture
def hd(client, repos):
    f = FakeHerdr()
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    client.post("/api/integrations/settings", json={"herdr": {"enabled": True, "socket": f.path, "kind": "claude"}})
    yield f, repo
    f.close()


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
    assert r["ok"] and r["where"]["agent"] == name + "-2" and r["where"]["agent"] in f.agents and len(f.workspaces) == 2


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


# ---------------------------------------------------------------- one box, or another machine
def test_on_the_same_box_herdr_is_found_without_any_setting(client, repos, monkeypatch):
    f = FakeHerdr()
    monkeypatch.setenv("HERDR_SOCKET_PATH", f.path)
    st = client.get("/api/integrations").get_json()["herdr"]
    assert st["ok"] and st["auto"] and st["where"] == "on this machine"
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/nonexistent/herdr.sock")
    st = client.get("/api/integrations").get_json()["herdr"]
    assert st["ok"] is False and st["enabled"] is False and "isn't running here" in st["error"]
    monkeypatch.setenv("UPSTREAM_HERDR", "off")
    monkeypatch.setenv("HERDR_SOCKET_PATH", f.path)
    assert client.get("/api/integrations").get_json()["herdr"]["enabled"] is False
    f.close()


@pytest.fixture
def remote(client, repos, tmp_path, monkeypatch):
    """Another machine: a `herdr` command that forwards `--machine workbox ...` to a second herdr."""
    far = FakeHerdr()
    log = tmp_path / "cli.log"
    exe = tmp_path / "herdr"
    exe.write_text(f'''#!/usr/bin/env python3
import json, socket, sys
a = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(a) + "\\n")
if a[:3] == ["machine", "list", "--json"]:
    print(json.dumps([{{"id": "m1", "label": "workbox", "target": "ssh://workbox", "session": "default", "enabled": True, "selected": False}},
                      {{"id": "m2", "label": "old", "target": "ssh://old", "session": "default", "enabled": False, "selected": False}}])); sys.exit(0)
assert a[0] == "--machine" and a[1] == "workbox", a
c = a[2:]
def opt(name, default=None):
    return c[c.index(name) + 1] if name in c else default
if c[:2] == ["agent", "list"]: m, p = "agent.list", {{}}
elif c[:2] == ["workspace", "create"]:
    m, p = "workspace.create", {{"cwd": opt("--cwd"), "label": opt("--label"), "focus": False,
                                "env": dict(x.split("=", 1) for i, x in enumerate(c) if i and c[i - 1] == "--env")}}
elif c[:2] == ["workspace", "close"]: m, p = "workspace.close", {{"workspace_id": c[2]}}
elif c[:2] == ["agent", "start"]: m, p = "agent.start", {{"name": c[2], "kind": opt("--kind"), "pane_id": opt("--pane")}}
elif c[:2] == ["agent", "prompt"]: m, p = "agent.prompt", {{"target": c[2], "text": c[3]}}
elif c[:2] == ["agent", "get"]: m, p = "agent.get", {{"target": c[2]}}
elif c[:2] == ["agent", "read"]: m, p = "agent.read", {{"target": c[2]}}
elif c[:2] == ["agent", "send-keys"]: m, p = "agent.send_keys", {{"target": c[2], "keys": c[3:]}}
else: sys.exit(2)
s = socket.socket(socket.AF_UNIX); s.connect({far.path!r}); s.sendall((json.dumps({{"id": "x", "method": m, "params": p}}) + "\\n").encode())
r = json.loads(s.makefile().readline())
if r.get("error"):
    print(json.dumps(r), file=sys.stderr); sys.exit(1)
print(r["result"]["read"]["text"] if m == "agent.read" else json.dumps(r), end="")
''')
    exe.chmod(0o755)
    near = FakeHerdr()
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    client.post("/api/integrations/settings", json={"herdr": {"enabled": True, "socket": near.path, "kind": "claude",
                                                              "path_map": f"{config.CONFIG_PATH.parent}=/srv/far"}})
    monkeypatch.setenv("UPSTREAM_HERDR_BIN", str(exe))
    yield near, far, repo, tmp_path
    near.close()
    far.close()


def test_another_machine_is_reached_through_herdr_itself(client, remote):
    near, far, repo, tmp = remote
    st = client.get("/api/integrations").get_json()["herdr"]
    assert [m["label"] for m in st["machines"]] == ["workbox"]          # the disabled one is left out
    far.workspaces["w9"] = {"cwd": "/x"}
    far.agents["builder"] = {"name": "builder", "kind": "codex", "pane": "w9:p1", "cwd": "/x", "prompts": [], "keys": [], "status": "idle"}
    roster = client.get("/api/herdr/agents").get_json()["items"]
    assert [(a["machine"], a["name"]) for a in roster] == [("workbox", "builder")]
    # a new agent over there: the folder is named as that machine sees it
    t = new_task(client, repo, to="herdr", machine="workbox", agent_kind="hermes")
    assert t["ok"], t
    w = t["where"]
    assert w["machine"] == "workbox" and near.workspaces == {} and len(far.workspaces) == 2
    made = far.workspaces[w["workspace_id"]]
    assert made["cwd"].startswith("/srv/far/") and made["env"]["UPSTREAM_TASK_ID"] == t["task"]["id"]
    assert "/srv/far/" in far.agents[w["agent"]]["prompts"][0] and str(config.CONFIG_PATH.parent) not in far.agents[w["agent"]]["prompts"][0]
    # following, answering and closing all go to that machine
    a = client.get(f"/api/tasks/{t['task']['id']}/agent").get_json()
    assert a["ok"] and a["machine"] == "workbox" and a["status"] == "working" and a["output"] == "working…"
    assert client.post(f"/api/tasks/{t['task']['id']}/keys", json={"keys": ["esc"]}).get_json()["ok"]
    assert far.agents[w["agent"]]["keys"] == ["esc"]
    assert client.post(f"/api/tasks/{t['task']['id']}/herdr/close", json={}).get_json()["ok"] and w["workspace_id"] not in far.workspaces
    # this machine still works beside it
    t2 = new_task(client, repo, title="Local one", to="herdr", machine="")
    assert t2["ok"] and t2["where"]["machine"] == "" and len(near.workspaces) == 1
    # a running agent over there
    t3 = new_task(client, repo, title="For builder", to="herdr", machine="workbox", agent="builder")
    assert t3["ok"] and "/srv/far/" in far.agents["builder"]["prompts"][0]


def test_a_remote_error_keeps_herdrs_reason(client, remote):
    near, far, repo, tmp = remote
    far.block_on_start = True
    t = new_task(client, repo, to="herdr", machine="workbox")
    assert t["ok"] and t["where"]["pending"]          # blocked over there too: the task waits
    bad = new_task(client, repo, title="x", to="herdr", machine="workbox", agent="nobody")
    assert not bad["ok"] and "agent not found" in bad["error"] and "workbox" in bad["error"]


def test_the_env_file_sets_connections_and_real_env_wins(tmp_path, monkeypatch):
    from upstream_upkeep import config, hermes, orb, studio
    f = tmp_path / ".env"
    f.write_text('# comment\nexport HERMES_STUDIO_URL="http://studio.box:8648"\nORB_URL=http://orb.box:8095  # inline note\n'
                 "HERMES_API_KEY='k1'\nUPSTREAM_HERDR_KIND=codex\nBAD LINE\nORB_TASK_LIST=Upstream\nPATH=/nope\n")
    for k in ("HERMES_STUDIO_URL", "ORB_URL", "HERMES_API_KEY", "UPSTREAM_HERDR_KIND", "ORB_TASK_LIST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORB_TASK_LIST", "FromRealEnv")
    keys = config.load_env_file(f)
    try:
        assert "PATH" not in keys and "ORB_TASK_LIST" not in keys and os.environ["PATH"] != "/nope"
        assert studio.settings()["url"] == "http://studio.box:8648" and studio.settings()["enabled"] is True
        assert orb.settings()["url"] == "http://orb.box:8095" and orb.settings()["list"] == "FromRealEnv"
        assert hermes._key() == "k1" and herdr.settings()["kind"] == "codex"
        store.save_settings({"studio": {"enabled": True, "url": "http://saved:1"}})   # what you save in Settings wins
        assert studio.settings()["url"] == "http://saved:1"
    finally:
        for k in keys:
            os.environ.pop(k, None)


# ---------------------------------------------------------------- agents handing work on (Studio's agent, a herdr agent...)
AGENT = {"X-Upstream-Client": "agent"}


def test_an_agent_can_hand_a_task_to_herdr_and_follow_it(client, hd):
    f, repo = hd
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "From Studio's agent", "kind": "bug"}, headers=AGENT).get_json()["task"]
    assert t["by"] == "agent"
    r = client.post(f"/api/tasks/{t['id']}/send", json={"to": "herdr", "agent_kind": "codex"}, headers=AGENT).get_json()
    assert r["ok"], r
    assert r["where"]["kind"] == "codex" and f.agents[r["where"]["agent"]]["prompts"]
    task = tasks.get(t["id"])
    assert task["dispatched_by"] == "agent" and "sent to herdr by an agent" in [x["text"] for x in task["log"]]
    assert client.get(f"/api/tasks/{t['id']}/agent", headers=AGENT).get_json()["status"] == "working"
    assert client.get("/api/herdr/agents", headers=AGENT).get_json()["items"][0]["task"]["id"] == t["id"]
    # what stays with the person: answering the agent, committing, deleting
    assert client.post(f"/api/tasks/{t['id']}/keys", json={"keys": ["y"]}, headers=AGENT).status_code == 403
    assert client.post(f"/api/tasks/{t['id']}/commit", json={"message": "x"}, headers=AGENT).status_code == 403
    assert client.delete(f"/api/tasks/{t['id']}", json={}, headers=AGENT).status_code == 403


def test_the_switch_and_the_limit_hold_agents_back(client, hd):
    from upstream_upkeep import app as appmod
    f, repo = hd
    ids = []
    for i in range(appmod.AGENT_DISPATCH_LIMIT):
        t = client.post("/api/tasks", json={"repo": repo["id"], "title": f"Job {i}"}, headers=AGENT).get_json()["task"]
        assert client.post(f"/api/tasks/{t['id']}/send", json={"to": "herdr"}, headers=AGENT).get_json()["ok"]
        ids.append(t["id"])
    extra = client.post("/api/tasks", json={"repo": repo["id"], "title": "One too many"}, headers=AGENT).get_json()["task"]
    r = client.post(f"/api/tasks/{extra['id']}/send", json={"to": "herdr"}, headers=AGENT)
    assert r.status_code == 400 and "limit" in r.get_json()["error"] and len(f.workspaces) == appmod.AGENT_DISPATCH_LIMIT
    # a follow-up to a task that is already running is not a new one
    assert client.post(f"/api/tasks/{ids[0]}/send", json={"to": "herdr", "message": "also this"}, headers=AGENT).get_json()["ok"]
    # parking never counts, and you are never limited
    assert client.post(f"/api/tasks/{extra['id']}/send", json={"to": "herdr"}).get_json()["ok"]
    # when one finishes there is room again
    client.post(f"/api/tasks/{ids[1]}", json={"status": "done"})
    more = client.post("/api/tasks", json={"repo": repo["id"], "title": "Now there is room"}, headers=AGENT).get_json()["task"]
    assert client.post(f"/api/tasks/{more['id']}/send", json={"to": "herdr"}, headers=AGENT).get_json()["ok"]
    # the switch: off means only you hand tasks to agents; agents can't flip it
    assert client.post("/api/integrations/settings", json={"agents_dispatch": False}, headers=AGENT).status_code == 403
    client.post("/api/integrations/settings", json={"agents_dispatch": False})
    assert client.get("/api/integrations").get_json()["agents_dispatch"] is False
    last = client.post("/api/tasks", json={"repo": repo["id"], "title": "Refused"}, headers=AGENT).get_json()["task"]
    r = client.post(f"/api/tasks/{last['id']}/send", json={"to": "herdr"}, headers=AGENT)
    assert r.status_code == 400 and "switched off" in r.get_json()["error"]
    assert client.post(f"/api/tasks/{last['id']}/send", json={"to": "herdr"}).get_json()["ok"]


def test_the_mcp_tools_for_handing_work_on_exist_and_describe_themselves():
    import asyncio
    from upstream_upkeep import mcp_server
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    for name in ("herdr_agents", "send_task", "task_progress", "create_task", "list_tasks", "update_task"):
        assert name in tools, name
    props = tools["send_task"].input_schema["properties"]
    assert set(props) == {"task", "to", "agent_kind", "agent", "machine", "message"} and all(p.get("description") for p in props.values())
