"""Integrations: built-in connections, your own services (other containers), and repo marks."""
import http.server
import socket
import threading

import pytest

from upstream_upkeep import services, store


@pytest.fixture
def web():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code = {"/health": 200, "/broken": 503, "/missing": 404}.get(self.path, 200)
            self.send_response(code); self.end_headers(); self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown(); srv.server_close()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def test_add_check_edit_remove_a_service(client, web):
    r = client.post("/api/services", json={"name": "Firefly", "url": web, "health": "/health"}).get_json()
    assert r["ok"] and r["status"]["ok"], r
    sid = r["item"]["id"]
    o = client.get("/api/services").get_json()
    row = [s for s in o["services"] if s.get("id") == sid][0]
    assert row["status"]["ok"] and row["kind"] == "service"
    assert {s["id"] for s in o["services"] if s["kind"] == "builtin"} == {"herdr", "studio", "hermes", "orb", "wiki"}
    assert client.post("/api/services", json={"id": sid, "name": "Firefly", "url": web, "health": "/broken"}).get_json()["status"]["ok"] is False
    assert client.post("/api/services", json={"name": "firefly", "url": web}).status_code == 400   # same name
    assert client.post("/api/services/delete", json={"id": sid}).get_json()["ok"]
    assert not services.custom()


@pytest.mark.parametrize("body", [{"name": "", "url": "http://x"}, {"name": "a", "url": "ftp://x"}, {"name": "a", "url": "x:80"},
                                  {"name": "a", "url": "http://x", "health": "health"}, {"name": "a", "url": "http://x", "repo": "nope"}])
def test_bad_services_are_refused(client, body):
    assert client.post("/api/services", json=body).status_code == 400


def test_health_results(web):
    assert services.check({"url": web, "health": "/missing"})["ok"] is False
    assert services.check({"url": web, "health": ""})["ok"] is True
    assert services.check({"url": web, "health": "/broken"})["error"].startswith("answers, but")
    assert "nothing is listening" in services.check({"url": f"http://127.0.0.1:{free_port()}", "health": ""})["error"]
    port = web.rsplit(":", 1)[1]
    assert services.check({"url": f"tcp://127.0.0.1:{port}"})["ok"]
    assert services.check({"url": f"tcp://127.0.0.1:{free_port()}"})["ok"] is False


def test_agents_can_read_and_mark_but_not_add_services(client, web, repos):
    agent = {"X-Upstream-Client": "agent"}
    assert client.post("/api/services", json={"name": "x", "url": web}, headers=agent).status_code == 403
    assert client.get("/api/services?live=0", headers=agent).get_json()["ok"]


def test_marks_and_what_a_repo_runs(client, web, repos):
    upr, mine = repos
    store.save_repo({"id": "ff", "name": "firefly-iii", "path": str(mine), "upstreams": []})
    store.save_repo({"id": "orbrepo", "name": "pondering-orb", "path": str(mine), "upstreams": []})
    store.save_repo({"id": "coin", "name": "coinpile", "path": str(mine), "upstreams": []})
    client.post("/api/services", json={"name": "Firefly", "url": web, "repo": "ff"})
    assert client.post("/api/repo/coin/integration", json={"state": "planned", "note": "needs the Orb connector"}).get_json()["ok"]
    assert client.post("/api/repo/coin/integration", json={"state": "maybe"}).status_code == 400
    assert client.post("/api/repo/nope/integration", json={"state": "planned"}).status_code == 404
    o = client.get("/api/services").get_json()
    by = {r["id"]: r for r in o["repos"]}
    assert by["ff"]["state"] == "integrated" and by["ff"]["auto"] and by["ff"]["works"] is True
    assert by["orbrepo"]["services"][0]["name"] == "Pondering Orb" and by["orbrepo"]["state"] == ""   # Orb is off here: not counted
    assert by["coin"]["state"] == "planned" and by["coin"]["note"] == "needs the Orb connector"
    assert [r["id"] for r in o["repos"]][:2] == ["ff", "coin"]     # integrated first, then to-integrate
    text = services.for_agents(live=False)
    assert "To integrate: coinpile (needs the Orb connector)" in text and "Firefly (firefly-iii)" in text
    # the mark shows in the repo list, and clearing it removes it
    client.post("/api/repo/coin/integration", json={"state": ""})
    assert "integration" not in store.get_repo("coin")


def test_guess_repo_from_container_names(repos):
    store.save_repo({"id": "orb", "name": "pondering-orb", "path": str(repos[1]), "upstreams": []})
    store.save_repo({"id": "h", "name": "hermes", "path": str(repos[1]), "upstreams": []})
    store.save_repo({"id": "hs", "name": "hermes-studio", "path": str(repos[1]), "upstreams": []})
    assert services._guess_repo("pondering_orb-web-1", "ghcr.io/me/pondering-orb:latest") == "orb"
    assert services._guess_repo("studio", "hermes-studio:1.2") == "hs"          # the longer, closer name wins
    assert services._guess_repo("postgres", "postgres:16") is None


def test_containers_without_docker(monkeypatch):
    monkeypatch.setattr(services.shutil, "which", lambda *_: None)
    assert services.containers()["available"] is False


def test_containers_from_docker_ps(monkeypatch, tmp_path, repos):
    store.save_repo({"id": "orb", "name": "pondering-orb", "path": str(repos[1]), "upstreams": []})
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\necho \'{"Names":"orb-web-1","Image":"pondering-orb:latest","Status":"Up 2 hours","Ports":"0.0.0.0:8095->8095/tcp, :::8095->8095/tcp"}\'\n'
                    'echo \'{"Names":"db","Image":"postgres:16","Status":"Up","Ports":"5432/tcp"}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    c = services.containers()
    assert c["available"]
    web, db = c["items"]
    assert web["ports"] == [8095] and web["url"] == "http://127.0.0.1:8095" and web["repo"] == "orb"
    assert db["ports"] == [] and db["url"] is None
