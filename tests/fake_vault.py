"""A stand-in LLMWIKI vault for tests and trials: a folder served the way the real one is.

- MCP at /mcp with the vault's memory tools: memory_log (a line in today's daily note), memory_add_source
  (raw/sources/<date> - <title>.md, never overwritten), memory_capture (a note in inbox/).
- The Obsidian Local REST API: GET/PUT /vault/<path> (a folder lists as {"files": [...]}) and
  POST /search/simple/?query= (filename, score, matches with context).

Both want `Authorization: Bearer <key>`. Run it on its own: python fake_vault.py <folder> <port> <key>
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route


def make_app(root: Path, key: str):
    root = Path(root)
    mcp = MCPServer("fake-llmwiki")
    calls: list[dict] = []

    @mcp.tool()
    def memory_log(text: str, source: str = "") -> str:
        """Append a line to today's daily note."""
        calls.append({"tool": "memory_log", "text": text, "source": source})
        day = root / "daily" / f"{dt.date.today().isoformat()}.md"
        day.parent.mkdir(parents=True, exist_ok=True)
        with day.open("a", encoding="utf-8") as f:
            f.write(f"- {text}" + (f" ({source})" if source else "") + "\n")
        return f"logged to daily/{day.name}"

    @mcp.tool()
    def memory_add_source(title: str, content: str, date: str = "") -> str:
        """Add a source for Ingest (never overwritten)."""
        calls.append({"tool": "memory_add_source", "title": title, "date": date})
        p = root / "raw" / "sources" / f"{date or dt.date.today().isoformat()} - {title}.md"
        if p.exists():
            raise ValueError(f"{p.name} already exists")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"added raw/sources/{p.name}"

    @mcp.tool()
    def memory_capture(title: str, content: str) -> str:
        """Capture a note into inbox/."""
        calls.append({"tool": "memory_capture", "title": title, "content": content})
        p = root / "inbox" / f"{dt.datetime.now().strftime('%Y-%m-%d %H%M%S')} - {title.replace('/', ' ')}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"captured inbox/{p.name}"

    def authed(request: Request) -> bool:
        return request.headers.get("authorization", "") == f"Bearer {key}"

    async def vault(request: Request):
        if not authed(request):
            return PlainTextResponse("unauthorized", 401)
        rel = request.path_params["path"]
        p = (root / rel).resolve()
        if root.resolve() not in [p, *p.parents]:
            return PlainTextResponse("outside", 400)
        if request.method == "PUT":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(await request.body())
            return Response(status_code=204)
        if rel.endswith("/") or p.is_dir():
            if not p.is_dir():
                return JSONResponse({"errorCode": 40400}, 404)
            return JSONResponse({"files": sorted(c.name + ("/" if c.is_dir() else "") for c in p.iterdir())})
        if not p.is_file():
            return JSONResponse({"errorCode": 40400}, 404)
        return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown")

    async def search(request: Request):
        if not authed(request):
            return PlainTextResponse("unauthorized", 401)
        q = (request.query_params.get("query") or "").lower().split()
        out = []
        for p in sorted(root.rglob("*.md")):
            text = p.read_text(encoding="utf-8").lower()
            if q and all(w in text or w in p.stem.lower() for w in q):
                i = text.find(q[0])
                out.append({"filename": str(p.relative_to(root)), "score": sum(text.count(w) for w in q),
                            "matches": [{"match": {"start": max(i, 0), "end": max(i, 0) + len(q[0])}, "context": text[max(i - 60, 0):i + 60]}]})
        return JSONResponse(out)

    mcp_app = mcp.streamable_http_app(transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def gate(scope, receive, send):        # the MCP endpoint wants the key too
        if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization", b"") != f"Bearer {key}".encode():
            await PlainTextResponse("unauthorized", 401)(scope, receive, send)
            return
        await mcp_app(scope, receive, send)

    app = Starlette(routes=[Route("/vault/{path:path}", vault, methods=["GET", "PUT"]),
                            Route("/search/simple/", search, methods=["POST"]),
                            Mount("/", app=gate)],
                    lifespan=lambda app: mcp_app.router.lifespan_context(app))
    app.state.calls = calls
    return app


def serve_in_thread(root: Path, key: str):
    """Start it on a free port in this process; returns (base_url, app, server)."""
    import socket
    import threading
    import time

    import uvicorn
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    app = make_app(root, key)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", app, server


if __name__ == "__main__":
    import uvicorn
    root, port, key = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    root.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"vault": str(root), "mcp": f"http://127.0.0.1:{port}/mcp", "rest": f"http://127.0.0.1:{port}"}), flush=True)
    uvicorn.run(make_app(root, key), host="127.0.0.1", port=port, log_level="warning")
