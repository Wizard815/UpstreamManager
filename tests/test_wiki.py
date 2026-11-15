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
