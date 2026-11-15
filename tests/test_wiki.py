"""Upstream Manager feeds the LLMWIKI vault's short-term memory through its MCP memory tools."""
import socket
import threading
import time

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from upstream_upkeep import memory, store, wiki


def test_summarize_daily_names_what_changed():
    results = [
        {"repo_id": "omi", "repo_name": "omi-local", "status": "merged", "behind": 12},
        {"repo_id": "argos", "repo_name": "argos", "status": "request_raised", "conflicts": ["upstream", "fork"]},
        {"repo_id": "x", "repo_name": "x", "status": "skipped"},
        {"repo_id": "y", "status": "error", "error": "boom"},
    ]
    assert wiki.summarize_daily(results) == "Upkeep: omi-local fast-forwarded 12; argos request raised (2 conflicts); y failed"
    assert wiki.summarize_daily([{"repo_id": "x", "status": "skipped"}]) == ""


def test_day_source_puts_each_repo_under_its_name():
    text = wiki.day_source("2026-11-14", [("argos", "Log for 2026-11-14:\n\n**Worked on** - MCP"), ("General chats", "plain")])
    assert "## argos\n\n**Worked on** - MCP" in text and "## General chats\n\nplain" in text
    assert "Log for" not in text


def test_off_without_a_url(monkeypatch):
    monkeypatch.delenv("LLMWIKI_MCP_URL", raising=False)
    monkeypatch.setattr(wiki, "call_tool", lambda *a: pytest.fail("called the wiki while it is off"))
    wiki.log_daily([{"repo_id": "a", "status": "merged", "behind": 1}])
    wiki.send_days({"2026-11-14": [("a", "x")]})


def test_due_logs_go_to_the_wiki_once_every_log_of_the_day_is_written(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(memory, "_settings", lambda: {"enabled": True, "auto_cleanup": True, "daily_log": True})
    monkeypatch.setattr(wiki, "send_days", lambda days: sent.append(days))
    monkeypatch.setattr(memory, "get", lambda: _FakeDB())
    monkeypatch.setattr(store, "all_repos", lambda: [{"id": "a"}, {"id": "b"}])
    ok = {"a": True, "b": False}
    monkeypatch.setattr(memory, "write_day_log", lambda repo, day: {"ok": ok[repo], "error": "" if ok[repo] else "model call failed: down"})
    monkeypatch.setattr(memory, "logs_of_day", lambda day: [("a", f"log {day}")])
    memory.write_due_logs()
    assert sent == [{"2026-11-12": [("a", "log 2026-11-12")]}], "a day with a failed log waits for the next pass"


class _FakeDB:
    def execute(self, sql, params=()):
        rows = [{"repo": "a", "day": "2026-11-12"}, {"repo": "a", "day": "2026-11-13"}, {"repo": "b", "day": "2026-11-13"}]
        return type("C", (), {"fetchall": lambda self: rows})()


@pytest.fixture
def vault(monkeypatch):
    """A stand-in for the vault's MCP endpoint, served over real streamable HTTP."""
    calls, keys = [], []
    fake = MCPServer("llmwiki")
    sources = set()

    @fake.tool()
    def memory_log(text: str, section: str = "worked_on", source: str = "") -> str:
        calls.append(("memory_log", text, source))
        return "Logged"

    @fake.tool()
    def memory_add_source(title: str, content: str, date: str = "") -> str:
        path = f"raw/sources/{date} - {title}.md"
        if path in sources:
            raise ToolError(f"{path} already exists and sources are never overwritten; use another title.")
        sources.add(path)
        calls.append(("memory_add_source", path, content))
        return f"Added {path}"

    app = fake.streamable_http_app(stateless_http=True, json_response=True)

    async def check_key(scope, receive, send):
        if scope["type"] == "http":
            keys.append(dict(scope["headers"]).get(b"authorization", b"").decode())
        await app(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(check_key, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    monkeypatch.setenv("LLMWIKI_MCP_URL", f"http://127.0.0.1:{port}/mcp")
    monkeypatch.setenv("LLMWIKI_API_KEY", "k-test")
    yield calls, keys
    server.should_exit = True


def test_calls_reach_the_vault_over_mcp_with_the_key(vault):
    calls, keys = vault
    assert wiki.call_tool("memory_log", {"text": "Upkeep: argos fast-forwarded 3", "source": wiki.SOURCE}) == "Logged"
    assert calls == [("memory_log", "Upkeep: argos fast-forwarded 3", "upstream-manager")]
    assert keys and all(k == "Bearer k-test" for k in keys)


def test_a_day_is_sent_once_and_a_repeat_is_not_an_error(vault):
    calls, _ = vault
    wiki._send_day("2026-11-14", [("argos", "Log for 2026-11-14:\n\nMCP fixes")])
    wiki._send_day("2026-11-14", [("argos", "again")])  # already there: fine
    assert [c[1] for c in calls] == ["raw/sources/2026-11-14 - Upstream Manager.md"]
    assert "## argos\n\nMCP fixes" in calls[0][2]


# ---------------------------------------------------------------- reading wiki/ (folder or Local REST API)
import json as _json
import threading as _threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

from upstream_upkeep import config, credentials, llm, registry, store

json = _json
threading = _threading

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
def folder_vault(tmp_path, repos, monkeypatch, client):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "state" / "state.json")
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    v = make_vault(tmp_path)
    assert client.post("/api/wiki/settings", json={"enabled": True, "mode": "folder", "path": str(v)}).get_json()["ok"]
    wiki._CACHE.clear()
    return v


def test_reads_only_the_long_term_tier(folder_vault):
    notes = wiki.about(registry.get_repo("mine"))
    assert [n["note"] for n in notes] == ["mine", "GitHub Repos"]  # its own note first
    assert notes[0]["status"] == "draft" and "Ships on Fridays." in notes[0]["lines"]
    repos_lines = notes[1]["lines"]
    assert any("mine-app" in l for l in repos_lines) and not any("mine-extra" in l for l in repos_lines)
    text = wiki.about_text(registry.get_repo("mine"))
    assert "[[mine]] (draft)" in text and "raw source" not in text and "touched mine" not in text


def test_chat_and_agent_briefing_get_the_wiki_facts(folder_vault, client):
    msgs = llm.build_messages(registry.get_repo("mine"), {"messages": []}, "where does this run?")
    assert "From the user's LLM wiki" in msgs[0]["content"] and "mine-app" in msgs[0]["content"]
    brief = client.get("/api/repo/mine/briefing").get_json()["text"]
    assert "The user's LLM wiki says" in brief and "[[GitHub Repos]]" in brief


def test_secrets_never_reach_the_vault():
    text = ("token ghp_" + "a" * 36 + " and Bearer abcdefghijklmnopqrstuvwx and password=hunter2hunter "
            "and sk-" + "b" * 30 + " and eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk")
    out = wiki.redact(text)
    for leaked in ("ghp_aaa", "abcdefghijklmnopqrstuvwx", "hunter2", "sk-bbb", "eyJhbGci"):
        assert leaked not in out
    assert out.count("[redacted]") == 5


def test_wiki_settings_are_the_users(folder_vault, client):
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


def test_reading_reuses_the_writers_address_and_key(monkeypatch):
    from upstream_upkeep import wiki
    monkeypatch.setenv("UPSTREAM_SECRETS_BACKEND", "file")
    monkeypatch.setenv("LLMWIKI_MCP_URL", "http://192.168.20.5:27123/mcp")
    monkeypatch.setenv("LLMWIKI_API_KEY", "from-env")
    store.save_settings({"wiki": {"enabled": True, "mode": "rest"}})
    assert wiki.rest_url() == "http://192.168.20.5:27123" and wiki.rest_key() == "from-env"
    st = wiki.status()
    assert st["writer"] is True and st["writer_url"].endswith("/mcp") and st["key_set"]
    assert "from-env" not in _json.dumps(st)
