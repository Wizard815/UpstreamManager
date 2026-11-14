"""The LLM wiki: read long-term notes (wiki/), write only Upstream Manager's section of today's daily note."""
import json
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

import pytest

from upstream_upkeep import config, credentials, llm, memory, registry, store, wiki

TODAY = date.today().isoformat()
TEMPLATE = """---
title: "{{date}}"
type: daily
tags: [daily]
created: {{date}}
status: open
related: []
---

# {{date}}

## Worked on

-
"""


def make_vault(root: Path) -> Path:
    v = root / "vault"
    for d in ("wiki/3-resources", "wiki/1-projects", "wiki/logs", "daily", "raw/sources", "schema/templates", ".obsidian"):
        (v / d).mkdir(parents=True)
    (v / "schema/templates/DAILY.md").write_text(TEMPLATE)
    (v / "wiki/3-resources/GitHub Repos.md").write_text(
        "---\ntitle: GitHub Repos\nstatus: reviewed\n---\n# GitHub Repos\n"
        "| `mine` | **original** (Python) | runs on RaidLab as the `mine-app` container | |\n"
        "| `other` | fork | unrelated | |\n| `mine-extra` | something else that only shares a prefix | |\n")
    (v / "wiki/1-projects/mine.md").write_text("---\ntitle: mine\nstatus: draft\n---\n# mine\n\nShips on Fridays.\n")
    (v / "wiki/logs/Log - 2026-11-01.md").write_text("touched mine today\n")
    (v / "raw/sources/x.md").write_text("mine is mentioned in a raw source\n")
    return v


@pytest.fixture
def vault(tmp_path, repos, monkeypatch, client):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "state" / "state.json")
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    v = make_vault(tmp_path)
    assert client.post("/api/wiki/settings", json={"enabled": True, "mode": "folder", "path": str(v)}).get_json()["ok"]
    wiki._CACHE.clear()
    return v


def test_reads_only_the_long_term_tier(vault):
    notes = wiki.about(registry.get_repo("mine"))
    assert [n["note"] for n in notes] == ["mine", "GitHub Repos"]  # its own note first
    assert notes[0]["status"] == "draft" and "Ships on Fridays." in notes[0]["lines"]
    repos_lines = notes[1]["lines"]
    assert any("mine-app" in l for l in repos_lines) and not any("mine-extra" in l for l in repos_lines)
    text = wiki.about_text(registry.get_repo("mine"))
    assert "[[mine]] (draft)" in text and "raw source" not in text and "touched mine" not in text


def test_chat_and_agent_briefing_get_the_wiki_facts(vault, client):
    msgs = llm.build_messages(registry.get_repo("mine"), {"messages": []}, "where does this run?")
    assert "From the user's LLM wiki" in msgs[0]["content"] and "mine-app" in msgs[0]["content"]
    brief = client.get("/api/repo/mine/briefing").get_json()["text"]
    assert "The user's LLM wiki says" in brief and "[[GitHub Repos]]" in brief


def test_publish_creates_today_from_the_template_and_keeps_human_text(vault, client, monkeypatch):
    monkeypatch.setenv("UPSTREAM_MEMORY", "on")
    memory.reload()
    try:
        memory.remember("mine", f"{TODAY}: Daily check brought in 2 commit(s) from upstream origin.", kind="event")
        r = client.post("/api/wiki/publish", json={}).get_json()
        assert r["ok"] and r["written"] and r["path"] == f"daily/{TODAY}.md"
        note = (vault / "daily" / f"{TODAY}.md").read_text()
        assert note.startswith('---\ntitle: "' + TODAY) and "status: open" in note
        assert "## Upstream Manager" in note and "**mine**: Daily check brought in 2 commit(s)" in note
        # a person writes under their own heading; publishing again rewrites only our section
        edited = note.replace("## Worked on\n\n-", "## Worked on\n\n- my own line", 1)
        assert edited != note
        (vault / "daily" / f"{TODAY}.md").write_text(edited)
        memory.remember("mine", f"{TODAY}: Merge request for branch main was approved.", kind="decision")
        client.post("/api/wiki/publish", json={})
        note = (vault / "daily" / f"{TODAY}.md").read_text()
        assert "- my own line" in note and note.count("## Upstream Manager") == 1 and "was approved" in note
        assert not any(p.name.startswith(TODAY) for p in (vault / "wiki").rglob("*"))  # never into wiki/
    finally:
        memory.reload()
        monkeypatch.setenv("UPSTREAM_MEMORY", "off")


def test_rolled_over_notes_and_past_days_are_left_alone(vault):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (vault / "daily" / f"{TODAY}.md").write_text(TEMPLATE.replace("{{date}}", TODAY).replace("status: open", "status: rolled-over"))
    r = wiki.publish()
    assert not r["ok"] and "rolled-over" in r["error"]
    r = wiki.publish(yesterday)
    assert not r["ok"] and not (vault / "daily" / f"{yesterday}.md").exists()


def test_secrets_never_reach_the_vault():
    text = ("token ghp_" + "a" * 36 + " and Bearer abcdefghijklmnopqrstuvwx and password=hunter2hunter "
            "and sk-" + "b" * 30 + " and eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk")
    out = wiki.redact(text)
    for leaked in ("ghp_aaa", "abcdefghijklmnopqrstuvwx", "hunter2", "sk-bbb", "eyJhbGci"):
        assert leaked not in out
    assert out.count("[redacted]") == 5


def test_wiki_settings_are_the_users(vault, client):
    r = client.post("/api/wiki/settings", json={"enabled": False}, headers={"X-Upstream-Client": "agent"})
    assert r.status_code == 403 and wiki.settings()["enabled"]


def test_a_folder_that_is_not_a_vault_is_reported(client, tmp_path):
    client.post("/api/wiki/settings", json={"enabled": True, "mode": "folder", "path": str(tmp_path)})
    st = client.get("/api/wiki").get_json()
    assert not st["ok"] and "doesn't look like a vault" in st["error"]


def test_rest_mode_talks_to_the_local_rest_api(tmp_path, repos, client, monkeypatch):
    """A stand-in for the Obsidian Local REST API plugin: GET/PUT /vault/<path>, folder listings, bearer key."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "state" / "state.json")
    monkeypatch.setenv("UPSTREAM_SECRETS_BACKEND", "file")
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    v = make_vault(tmp_path)
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            seen.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != "Bearer vault-key-123":
                self.send_response(401); self.end_headers(); return False
            return True

        def do_GET(self):
            if not self._auth():
                return
            rel = unquote(self.path[len("/vault/"):])
            p = v / rel
            if rel.endswith("/") and p.is_dir():
                body = json.dumps({"files": sorted(c.name + ("/" if c.is_dir() else "") for c in p.iterdir())}).encode()
            elif p.is_file():
                body = p.read_bytes()
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_PUT(self):
            if not self._auth():
                return
            p = v / unquote(self.path[len("/vault/"):])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204); self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client.post("/api/wiki/settings", json={"enabled": True, "mode": "rest", "url": f"http://127.0.0.1:{srv.server_port}"})
        wiki._CACHE.clear()
        assert "key" in client.get("/api/wiki").get_json()["error"]  # no key yet: refused
        credentials.set("wiki_api_key", "vault-key-123")
        st = client.get("/api/wiki").get_json()
        assert st["ok"] and st["notes"] == 3 and st["key_set"]
        assert any(n["note"] == "GitHub Repos" for n in wiki.about(registry.get_repo("mine")))
        store.add_chat_message("mine", store.new_session("mine")["id"], "user", "hi")
        assert "vault-key-123" not in json.dumps(client.get("/api/wiki").get_json())  # never sent back
    finally:
        srv.shutdown()
        credentials.set("wiki_api_key", "")
