"""Security defaults (local bind, masked API key) and the chat Markdown renderer."""
import shutil
import subprocess
from pathlib import Path

import pytest

from upstream_upkeep import llm, store

ROOT = Path(__file__).resolve().parents[1]


def test_server_listens_on_localhost_by_default():
    for f in ("src/upstream_upkeep/run.py", "daemonize_upkeep.py"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert 'os.environ.get("HOST", "127.0.0.1")' in text, f
        assert '"0.0.0.0"' not in text.replace('HOST=0.0.0.0', ''), f


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
