"""Security defaults (local bind, masked API key) and the chat Markdown renderer."""
import shutil
import subprocess
from pathlib import Path

import pytest

from upstream_upkeep import llm, store

ROOT = Path(__file__).resolve().parents[1]


def test_server_listens_on_localhost_by_default():
    run = (ROOT / "src/upstream_upkeep/run.py").read_text(encoding="utf-8")
    assert 'os.environ.get("HOST", "127.0.0.1")' in run
    # The daemon serves through run.main, so it inherits the same host default.
    daemon = (ROOT / "daemonize_upkeep.py").read_text(encoding="utf-8")
    assert "from upstream_upkeep.run import main" in daemon and ".run(host=" not in daemon
    for text in (run, daemon):
        assert '"0.0.0.0"' not in text.replace('HOST=0.0.0.0', '')


def test_daemon_second_fork_parent_exits():
    # os.fork() never returns < 0; checking that left two processes both serving.
    daemon = (ROOT / "daemonize_upkeep.py").read_text(encoding="utf-8")
    assert "os.fork() < 0" not in daemon
    assert daemon.count("if os.fork() > 0:") == 2


def test_api_key_is_masked_and_kept(client):
    secret = "sk-test-1234567890abcd"
    r = client.post("/api/settings", json={"llm": {"base_url": "http://127.0.0.1:9/v1", "model": "m", "api_key": secret}})
    assert secret not in r.get_data(as_text=True)
    shown = client.get("/api/settings").get_json()["llm"]["api_key"]
    assert shown.startswith("••••") and shown.endswith("abcd") and secret not in shown
    # Saving the page again sends the masked value back: the real key must survive.
    client.post("/api/settings", json={"llm": {"base_url": "http://127.0.0.1:9/v1", "model": "m", "api_key": shown}})
    assert store.load_llm_config()["api_key"] == secret
    # Clearing the field removes the key.
    client.post("/api/settings", json={"llm": {"base_url": "http://127.0.0.1:9/v1", "model": "m", "api_key": ""}})
    assert store.load_llm_config()["api_key"] == ""


def test_model_probe_uses_saved_key_for_masked_value(client, monkeypatch):
    store.save_settings({"llm": {"base_url": "http://x/v1", "model": "m", "api_key": "sk-real-key-0000"}})
    seen = {}
    monkeypatch.setattr(llm, "list_models", lambda base_url=None, api_key=None: (seen.update(key=api_key) or ([], None)))
    client.get("/api/models?base_url=http://x/v1&api_key=" + "••••0000")
    assert seen["key"] == "sk-real-key-0000"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_markdown_renderer():
    res = subprocess.run(["node", str(ROOT / "tests" / "md.test.js")], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
