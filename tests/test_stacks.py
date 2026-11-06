"""Demo stacks: the isolating rewrite of a compose file, the guards, and (with Docker) a real run."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from upstream_upkeep import config, registry, stacks, store

from conftest import commit_file, git

HAVE_COMPOSE = shutil.which("docker") is not None and subprocess.run(
    ["docker", "compose", "version"], capture_output=True).returncode == 0
HAVE_DAEMON = HAVE_COMPOSE and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
AGENT = {"X-Upstream-Client": "agent"}

COMPOSE = """
services:
  web:
    image: busybox:1.36
    container_name: real-web
    command: ["sh", "-c", "mkdir -p /www; echo hello-from-demo > /www/index.html; echo $$SECRET_KEY > /www/key; httpd -f -p 8080 -h /www"]
    ports: ["8080:8080"]
    environment:
      - SECRET_KEY=${SECRET_KEY:?set SECRET_KEY in .env.local}
    env_file: [./.env.local]
    volumes:
      - /mnt/user/appdata/demo-test:/data
      - ./site:/site:ro
    restart: unless-stopped
networks:
  default:
    name: real-net
"""


@pytest.fixture
def project(repos, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "state" / "state.json")
    _, mine = repos
    Path(mine, "site").mkdir()
    Path(mine, "site", "x.txt").write_text("x\n")
    commit_file(mine, "docker-compose.yml", COMPOSE, "compose")
    registry.add_repo(str(mine), None, "mine")
    return mine


def enable(client):
    assert client.post("/api/repo/mine/stack/settings", json={"enabled": True}).get_json()["ok"]


def test_stacks_are_off_until_the_user_turns_them_on(client, project):
    r = client.post("/api/repo/mine/stack/up", json={})
    assert r.status_code == 400 and "off for this repo" in r.get_json()["error"]
    r = client.post("/api/repo/mine/stack/settings", json={"enabled": True}, headers=AGENT)
    assert r.status_code == 403 and not registry.get_repo("mine").get("stack", {}).get("enabled")
    # an agent may still tune the demo (env, services), just not switch it on
    assert client.post("/api/repo/mine/stack/settings", json={"env": {"MODE": "demo"}}, headers=AGENT).get_json()["ok"]
    enable(client)
    assert registry.get_repo("mine")["stack"]["enabled"] and registry.get_repo("mine")["stack"]["env"] == {"MODE": "demo"}


def test_agents_cannot_approve_or_change_settings_or_accounts(client, project):
    assert client.post("/api/request/x/decide", json={"decision": "approve"}, headers=AGENT).status_code == 403
    assert client.post("/api/settings", json={"memory": {}}, headers=AGENT).status_code == 403
    assert client.post("/api/credentials", json={"github_token": "x"}, headers=AGENT).status_code == 403


@pytest.mark.skipif(not HAVE_COMPOSE, reason="docker compose is not installed")
def test_rewrite_isolates_the_stack(client, project):
    enable(client)
    meta = stacks.prepare(registry.get_repo("mine"))
    assert meta["generated_env"] == ["SECRET_KEY"] and meta["services"] == ["web"]
    model = json.loads((config.CONFIG_PATH.parent / "stacks" / "mine" / "main" / "compose.json").read_text())
    web = model["services"]["web"]
    assert "container_name" not in web and web["restart"] == "no"
    assert web["ports"] == [{"target": 8080, "protocol": "tcp", "host_ip": "127.0.0.1"}]
    binds = {v["target"]: v["source"] for v in web["volumes"]}
    assert binds["/site"] == str(Path(project, "site").resolve())  # the project's own files stay
    assert "/stacks/mine/main/data/" in binds["/data"] and not binds["/data"].startswith("/mnt")
    assert "name" not in model["networks"]["default"] and model["name"] == "upkeep-mine-main"
    # the generated secret is kept, so it is the same next time
    first = registry.get_repo("mine")["stack_generated"]["SECRET_KEY"]
    stacks.prepare(registry.get_repo("mine"))
    assert registry.get_repo("mine")["stack_generated"]["SECRET_KEY"] == first
    assert not Path(project, ".env.local").exists() and not Path("/mnt/user/appdata/demo-test").exists()


def test_request_needs_a_running_service(client, project):
    enable(client)
    r = client.post("/api/repo/mine/stack/request", json={"service": "web", "path": "/"})
    assert r.status_code == 400


@pytest.mark.skipif(not HAVE_DAEMON, reason="no Docker daemon")
def test_a_demo_stack_runs_answers_and_stops(client, project):
    enable(client)
    try:
        assert client.post("/api/repo/mine/stack/up", json={"build": False}).get_json()["ok"]
        for _ in range(120):
            st = client.get("/api/repo/mine/stack").get_json()
            if not st["busy"]:
                break
            time.sleep(1)
        assert st["services"][0]["state"] == "running", st
        port = st["services"][0]["ports"][0]
        assert port["target"] == 8080 and port["published"] != 8080
        r = client.post("/api/repo/mine/stack/request", json={"service": "web", "path": "/index.html"}).get_json()
        assert r["status"] == 200 and r["body"].strip() == "hello-from-demo"
        key = client.post("/api/repo/mine/stack/request", json={"service": "web", "path": "/key"}).get_json()["body"].strip()
        assert key == registry.get_repo("mine")["stack_generated"]["SECRET_KEY"]
        e = client.post("/api/repo/mine/stack/exec", json={"service": "web", "command": "cat /www/index.html"}).get_json()
        assert e["exit_code"] == 0 and "hello-from-demo" in e["stdout"]
        logs = client.get("/api/repo/mine/stack/logs?service=web").get_json()
        assert logs["ok"] and "errors" in logs
    finally:
        client.post("/api/repo/mine/stack/down", json={"reset": True})
    assert client.get("/api/repo/mine/stack").get_json()["services"] == []
