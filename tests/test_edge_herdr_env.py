"""Edge cases: herdr routing (odd names, machines, restarts), the env file, slow or missing connections."""
import os
import stat
import time

import pytest

from upstream_upkeep import config, herdr, registry, store, tasks

from test_herdr_logic import FakeHerdr, new_task


@pytest.fixture
def hd(client, repos):
    f = FakeHerdr()
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    client.post("/api/integrations/settings", json={"herdr": {"enabled": True, "socket": f.path, "kind": "claude"}})
    yield f, repo
    f.srv.close()


# ---------------------------------------------------------------- the folder map
def test_folder_map_picks_the_most_specific_pair_and_whole_folders_only():
    store.save_settings({"herdr": {"enabled": True, "path_map": "/home/me=/srv/me, /home/me/repos/=/data/repos/ ,bad, =x, /only="}})
    m = "box"
    assert herdr.remote_path("/home/me/repos/app", m) == "/data/repos/app"      # the longer pair wins, whatever the order
    assert herdr.remote_path("/home/me/notes", m) == "/srv/me/notes"
    assert herdr.remote_path("/home/me", m) == "/srv/me"
    assert herdr.remote_path("/home/meme/x", m) == "/home/meme/x"               # not a folder under /home/me
    assert herdr.remote_path("/home/me/repos/app", None) == "/home/me/repos/app"  # this machine: never mapped


# ---------------------------------------------------------------- names that could be read as options
@pytest.mark.parametrize("opts", [{"machine": "-x"}, {"machine": "--session"}, {"agent": "-rf"}, {"agent": "--wait"},
                                  {"agent": "a b; rm"}, {"agent_kind": "--help"}])
def test_names_that_look_like_options_are_refused(client, hd, opts):
    f, repo = hd
    t = new_task(client, repo, to="herdr", **opts)
    assert t["ok"] is False and t["error"], opts
    assert f.workspaces == {} and not any(c["method"] == "agent.prompt" for c in f.calls)


def test_a_running_agent_can_be_named_by_its_pane(client, hd):
    f, repo = hd
    f.workspaces["w9"] = {"cwd": "/x"}
    f.agents["w9:p1"] = {"name": "w9:p1", "kind": "codex", "pane": "w9:p1", "cwd": "/x", "prompts": [], "keys": [], "status": "idle"}
    assert new_task(client, repo, to="herdr", agent="w9:p1")["ok"]


def test_cli_text_that_starts_with_a_dash_is_not_an_option():
    argv = herdr._argv("agent.prompt", {"target": "reviewer", "text": "--wait and see"})
    assert argv[:3] == ["agent", "prompt", "reviewer"] and not argv[3].startswith("-") and argv[3].strip() == "--wait and see"


# ---------------------------------------------------------------- answering
@pytest.mark.parametrize("keys", ["y", {"k": "y"}, [], ["y"] * 21, [1], ["enter", "rm -rf"], None])
def test_only_real_key_lists_are_sent(client, hd, keys):
    f, repo = hd
    t = new_task(client, repo, to="herdr")
    r = client.post(f"/api/tasks/{t['task']['id']}/keys", json={"keys": keys})
    assert r.status_code == 400, keys
    assert not any(c["method"] == "agent.send_keys" for c in f.calls)


# ---------------------------------------------------------------- restarts and collisions
def test_a_second_agent_for_the_same_task_gets_its_own_name(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr")
    first = t["where"]["agent"]
    r = client.post(f"/api/tasks/{t['task']['id']}/send", json={"to": "herdr", "agent_kind": "codex"}).get_json()
    assert r["ok"], r
    assert r["where"]["agent"] != first and {first, r["where"]["agent"]} <= set(f.agents)
    import re
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", r["where"]["agent"])


def test_a_task_record_without_its_agent_is_explained_not_a_crash(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr")
    tasks.update(t["task"]["id"], where={"type": "herdr"})   # a damaged record
    for path in ("agent",):
        r = client.get(f"/api/tasks/{t['task']['id']}/{path}")
        assert r.status_code == 200 and r.get_json()["ok"] is False
    assert client.post(f"/api/tasks/{t['task']['id']}/keys", json={"keys": ["y"]}).status_code == 400
    assert client.get("/api/tasks").status_code == 200 and client.get("/api/herdr/agents").status_code == 200


def test_herdr_stopping_mid_task_is_reported_everywhere_without_errors(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr")
    f.srv.close()
    os.unlink(f.path)
    assert client.get("/api/tasks").status_code == 200
    a = client.get(f"/api/tasks/{t['task']['id']}/agent").get_json()
    assert a["ok"] is False and "isn't running" in a["error"] and not a.get("gone")   # not "gone": herdr itself is down
    r = client.get("/api/herdr/agents").get_json()
    assert r["items"] == [] and r["error"]
    assert client.get("/api/integrations").get_json()["herdr"]["ok"] is False
    again = client.post(f"/api/tasks/{t['task']['id']}/send", json={"to": "herdr", "message": "hi"}).get_json()
    assert again["ok"] is False and "isn't running" in again["error"]
    assert tasks.get(t["task"]["id"])["where"]["agent"]   # the task still knows its agent for when herdr is back


def test_another_machine_without_the_herdr_command_says_so(client, hd, monkeypatch):
    f, repo = hd
    monkeypatch.setenv("UPSTREAM_HERDR_BIN", "/nonexistent/herdr")
    t = new_task(client, repo, to="herdr", machine="workbox")
    assert t["ok"] is False and ("herdr command" in t["error"] or "can't run" in t["error"])
    assert client.get("/api/integrations").get_json()["herdr"]["ok"]      # this machine is unaffected
    assert client.get("/api/herdr/agents").status_code == 200


def test_a_big_screen_and_a_slow_answer_are_read_whole(client, hd):
    f, repo = hd
    t = new_task(client, repo, to="herdr")
    orig = f.handle

    def slow(m, p):
        if m == "agent.read":
            time.sleep(0.3)
            return {"type": "pane_read", "read": {"text": "line\n" * 60000}}
        return orig(m, p)
    f.handle = slow
    a = client.get(f"/api/tasks/{t['task']['id']}/agent").get_json()
    assert a["ok"] and len(a["output"]) == 20000 and a["output"].endswith("line\n")


def test_a_stale_socket_file_is_not_mistaken_for_herdr(client, repos, tmp_path, monkeypatch):
    stale = tmp_path / "herdr.sock"
    stale.write_text("")                       # a leftover file, nothing listening
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(stale))
    st = client.get("/api/integrations").get_json()["herdr"]
    assert st["ok"] is False and st["error"]
    assert not stat.S_ISSOCK(os.stat(stale).st_mode)


# ---------------------------------------------------------------- the env file
def test_env_file_oddities(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_bytes("﻿FIRST_KEY=one\r\nEMPTY=\r\nWITH_EQ=a=b=c\r\nSPACED = padded value \r\nQUOTED=\"keep # this\"\r\n"
                  "lower_ok=1\r\nDUP=first\r\nDUP=second\r\n1BAD=x\r\nBAD-KEY=x\r\nUNQUOTED=has # comment\r\nHALF=\"open\r\n".encode())
    for k in ("FIRST_KEY", "EMPTY", "WITH_EQ", "SPACED", "QUOTED", "lower_ok", "DUP", "UNQUOTED", "HALF"):
        monkeypatch.delenv(k, raising=False)
    keys = config.load_env_file(f)
    try:
        e = os.environ
        assert e["FIRST_KEY"] == "one"                    # a BOM at the start of the file doesn't hide the first key
        assert e["EMPTY"] == "" and e["WITH_EQ"] == "a=b=c" and e["SPACED"] == "padded value"
        assert e["QUOTED"] == "keep # this" and e["UNQUOTED"] == "has" and e["lower_ok"] == "1"
        assert e["DUP"] == "second"                        # like every .env reader: the last one wins
        assert e["HALF"] == '"open' and "1BAD" not in e and "BAD-KEY" not in e
        assert keys.count("DUP") == 1
    finally:
        for k in keys:
            os.environ.pop(k, None)
    assert config.load_env_file(tmp_path / "missing.env") == [] and config.load_env_file(tmp_path) == []


def test_a_named_env_file_that_is_missing_is_reported(client, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", None)
    monkeypatch.setattr(config, "ENV_MISSING", "/etc/upstream/nope.env", raising=False)
    ef = client.get("/api/integrations").get_json()["env_file"]
    assert ef["path"] is None and ef["missing"] == "/etc/upstream/nope.env"


def test_connections_that_hang_dont_hang_the_page(client, monkeypatch):
    """A host that swallows packets must not make Settings (or the task dialog) wait many seconds per service."""
    import socket as sk
    srv = sk.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)              # accepts the connection, never answers
    url = f"http://127.0.0.1:{srv.getsockname()[1]}"
    client.post("/api/integrations/settings", json={"hermes": {"enabled": True, "url": url}, "studio": {"enabled": True, "url": url},
                                                    "orb": {"url": url}})
    t = time.time()
    d = client.get("/api/integrations").get_json()
    took = time.time() - t
    srv.close()
    assert took < 9, f"{took:.1f}s"
    assert not d["hermes"]["ok"] and not d["studio"]["ok"] and not d["orb"]["ok"]
    assert "timed out" in d["hermes"]["error"]


# ---------------------------------------------------------------- inside Hermes Studio's /upstream tab
def test_writes_through_studios_proxy_are_accepted_and_forgeries_are_not(client, monkeypatch):
    from upstream_upkeep import app as appmod
    studio_origin = {"Origin": "http://192.168.20.5:6060"}
    # a page on another site: refused as before
    assert client.post("/api/tasks", json={"title": "x"}, headers=studio_origin).status_code == 403
    # Studio's proxy on this machine adds X-Upstream-Base: accepted
    ok = client.post("/api/tasks", json={"title": "from the Studio tab"}, headers={**studio_origin, "X-Upstream-Base": "/upstream"})
    assert ok.status_code == 200 and ok.get_json()["ok"]
    # the same header from another machine is not enough...
    far = {"REMOTE_ADDR": "172.18.0.5"}
    r = client.post("/api/tasks", json={"title": "x"}, headers={**studio_origin, "X-Upstream-Base": "/upstream"}, environ_overrides=far)
    assert r.status_code == 403
    # ...unless that address is listed (Studio in a container)
    monkeypatch.setattr(appmod, "_TRUSTED_PROXIES", ["172.18.0.0/16", "not-an-ip"])
    r = client.post("/api/tasks", json={"title": "from Studio in docker"}, headers={**studio_origin, "X-Upstream-Base": "/upstream"}, environ_overrides=far)
    assert r.status_code == 200
    # a form post is still refused whatever the headers
    r = client.post("/api/tasks", data="title=x", content_type="text/plain", headers={"X-Upstream-Base": "/upstream"})
    assert r.status_code == 415


def test_hermes_sessions_use_the_source_studio_lists(client, monkeypatch):
    from upstream_upkeep import hermes
    sent = {}
    monkeypatch.setattr(hermes, "_req", lambda m, p, **kw: sent.update(kw.get("json") or {}) or {"session": {"id": "s1"}})
    assert hermes.start("Fix it", "be careful") == "s1"
    assert sent["source"] == "api_server" and sent["title"] == "⇅ Fix it"
