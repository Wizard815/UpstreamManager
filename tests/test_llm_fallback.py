"""Model servers that start models on demand: several URLs, the first up that serves the model wins."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from upstream_upkeep import llm, store


def server(models):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"data": [{"id": m} for m in models]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_port}/v1"


def dark_url():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return f"http://127.0.0.1:{port}/v1"


def test_the_first_server_up_that_serves_the_model_is_used(client):
    other, other_url = server(["qwen3-35b"])
    right, right_url = server(["hermes4-14b", "qwen3vl-30b"])
    dark = dark_url()
    try:
        store.save_llm_config({"base_url": dark, "fallback_urls": [other_url, right_url], "model": "hermes4-14b"})
        llm._picked["at"] = 0
        assert llm.config()["base_url"] == right_url  # dark is down, other doesn't serve it
        s = client.get("/api/llm/status?refresh=1").get_json()
        assert s["base_url"] == right_url and s["up"] and s["serves_model"]
        assert s["servers"][dark]["up"] is False and s["servers"][other_url]["models"] == ["qwen3-35b"]
        # no model chosen: the first server that is up, and its first model
        store.save_llm_config({"model": ""})
        llm._picked["at"] = 0
        cfg = llm.config()
        assert (cfg["base_url"], cfg["model"]) == (other_url, "qwen3-35b")
    finally:
        other.shutdown(); right.shutdown()
        store.save_llm_config({"fallback_urls": [], "model": ""})


def test_one_url_works_as_before_and_bad_entries_are_ignored(client):
    up, url = server(["m"])
    try:
        store.save_llm_config({"base_url": url + "/", "fallback_urls": [url, "", 7, None], "model": "m"})
        llm._picked["at"] = 0
        cfg = llm.config()
        assert cfg["base_url"] == url and "servers" not in cfg
    finally:
        up.shutdown()
        store.save_llm_config({"fallback_urls": []})


def test_status_reports_a_dark_server(client):
    store.save_llm_config({"base_url": dark_url(), "fallback_urls": [], "model": "x"})
    s = client.get("/api/llm/status").get_json()
    assert s["up"] is False and not s["serves_model"]
