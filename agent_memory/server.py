"""Agent Memory: shared, bounded context for coding agents, served from Home Assistant.

    python3 server.py --options /data/options.json --data /data --port 8765

One HTTP server carries three things: the MCP endpoint (`POST /mcp`, JSON-RPC over
streamable HTTP with JSON responses), a REST mirror of the same tools (`/api/...`) for
scripts, and the admin page plus `/health`. Clients authenticate by a bearer token whose
sha256 is in the options, or by source address for clients listed with an `ip`; each
client carries scopes (read, write, delete, export, admin). A client with a token is identified
by name: what it writes carries that name as `agent`, whatever the request says. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed
import enrich
import mcp
import page
import review
from store import Store

LOG = logging.getLogger("agent-memory")
INGRESS_NETS = ("172.30.32.", "172.30.33.")  # Home Assistant's ingress proxy addresses
# REST reads and the tool whose scope each needs: the same rule as the MCP tool.
REST_GET = {
    "/api/bootstrap": "memory_bootstrap",
    "/api/search": "memory_search",
    "/api/get": "memory_get",
    "/api/timeline": "memory_timeline",
    "/api/context": "memory_project_context",
    "/api/namespaces": "memory_timeline",
}


def review_at(options: dict) -> str:
    """`HH:MM` or `off`. Absent means 03:00: the Supervisor applies option defaults only at install."""
    return str(options.get("review_at") or "03:00").strip()


def load_options(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc if isinstance(doc, dict) else {}


class Auth:
    def __init__(self, clients: list[dict]):
        self.clients = clients or []

    def identify(self, header: str | None, peer_ip: str) -> tuple[str, set[str], bool] | None:
        """(name, scopes, by_token). A bearer token must match a client's hash; with no token, a client
        listed by `ip` alone matches the source address. An unknown token never falls back to the address."""
        token = ""
        if header and header.lower().startswith("bearer "):
            token = header[7:].strip()
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            for c in self.clients:
                if c.get("token_sha256") and hmac.compare_digest(c["token_sha256"].lower(), digest):
                    return c["name"], set(c.get("scopes") or ["read"]), True
            return None
        for c in self.clients:
            if c.get("ip") and c["ip"] == peer_ip and not c.get("token_sha256"):
                return c["name"], set(c.get("scopes") or ["read"]), False
        return None


def supervisor_token() -> str:
    """The add-on's own token; a plain-sh run.sh does not export it, s6 keeps it on disk."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        try:
            with open("/run/s6/container_environment/SUPERVISOR_TOKEN", encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            token = ""
    return token


def ha_entity_ids() -> set[str]:
    token = supervisor_token()
    if not token:
        raise RuntimeError("no Supervisor token")
    req = urllib.request.Request("http://supervisor/core/api/states", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return {s["entity_id"] for s in json.loads(resp.read())}


class App:
    def __init__(self, options: dict, data_dir: str):
        self.options = options
        self.data_dir = data_dir
        self.store = Store(os.path.join(data_dir, "memory.sqlite"))
        # A forge or GitHub owner rename moves its stored namespaces; idempotent, so the option can stay.
        for rename in options.get("namespace_renames") or []:
            moved = self.store.rename_namespaces(rename["from"], rename["to"])
            LOG.info("namespace rename %s -> %s: %s", rename["from"], rename["to"], moved)
        self.memory = mcp.Memory(self.store, options)
        self.memory.entity_ids = ha_entity_ids
        self.auth = Auth(options.get("clients") or [])
        self.export_dir = os.path.join(data_dir, "exports")
        self.worker = enrich.Worker(self.store, options)
        # Embedding retrieval, when embed_url is set; search falls back to FTS whenever it cannot use it.
        self.encoder = embed.encoder_from(options)
        self.memory.encoder = self.encoder
        self.indexer = embed.Indexer(self.store, self.encoder) if self.encoder else None
        expiry = options.get("candidate_expiry_days")
        self.reviewer = review.Reviewer(self.store, self.worker, self.memory, self.encoder, review.EXPIRY_DAYS if expiry is None else int(expiry))
        self.started = time.time()
        self.requests = 0
        self.denied = 0

    def health(self) -> dict:
        stats = self.store.stats()
        queue = self.store.queue_depth()
        enrichment = self.worker.state()
        return {
            "status": "degraded" if (queue["queued"] and enrichment["ollama"] != "available") else "ok",
            "uptime_s": int(time.time() - self.started),
            "records": stats["memories"],
            "candidates": stats["candidates"],
            "namespaces": stats["namespaces"],
            "fts5": stats["fts5"],
            "db_bytes": stats["db_bytes"],
            "jobs": stats["jobs"],
            "enrichment": enrichment,
            # Not part of `status`: search answers from FTS whenever the encoder does not.
            "embeddings": self.indexer.state() if self.indexer else {"encoder": "off"},
            "anchor_sweep": self.memory.sweep_state(),
            "candidate_review": {**self.reviewer.state(), "review_at": review_at(self.options)},
            "requests": self.requests,
            "denied": self.denied,
            "clients": [c.get("name") for c in self.auth.clients],
        }

    def audit_tail(self, n: int = 30) -> list[dict]:
        rows = self.store.db.execute("select * from audit order by seq desc limit ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def panel_data(self) -> dict:
        now = dt.datetime.now(dt.UTC)
        since = (now - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        return {"now": now, "health": self.health(), "counts": self.store.panel_counts(since), "audit": self.audit_tail(), "unused": self.store.unused()}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-memory/0.1"

        def log_message(self, fmt, *args):
            LOG.debug(fmt, *args)

        # ---------------------------------------------------------------- plumbing
        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, code: int, doc) -> None:
            self._send(code, json.dumps(doc, ensure_ascii=False).encode())

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw or b"{}")

        def _client(self) -> tuple[str, set[str], bool] | None:
            peer = self.client_address[0]
            ident = app.auth.identify(self.headers.get("Authorization"), peer)
            if ident is None:
                app.denied += 1
                app.store.audit(peer, "denied", detail=self.path)  # masked whole, then cut, by audit
            return ident

        def _ingress(self) -> bool:
            return self.client_address[0].startswith(INGRESS_NETS) or self.headers.get("X-Ingress-Path") is not None

        # ---------------------------------------------------------------- routes
        def do_GET(self):
            app.requests += 1
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
            elif path == "/health":
                self._json(200, app.health())
            elif path in ("/", "/index.html"):
                if not self._ingress():
                    self._send(404, b"not found\n", "text/plain; charset=utf-8")
                    return
                self._send(200, page.render(app.panel_data()).encode(), "text/html; charset=utf-8")
            elif path == "/mcp":
                self._send(405, b"", extra={"Allow": "POST"})
            elif path.startswith("/api/"):
                ident = self._client()
                if not ident:
                    self._json(401, {"error": "unknown client"})
                    return
                self._api_get(path, parse_qs(urlsplit(self.path).query), ident)
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def _api_get(self, path: str, q: dict, ident: tuple[str, set[str], bool]) -> None:
            name, scopes, _ = ident
            tool = REST_GET.get(path)
            if tool is None:
                self._json(404, {"error": "no such endpoint"})
                return
            needed = mcp.TOOL_SCOPE[tool]
            if not mcp.permits(scopes, needed):
                self._json(403, {"error": f"client {name!r} lacks the {needed!r} scope"})
                return
            one = {k: v[0] for k, v in q.items()}
            try:
                if path == "/api/bootstrap":
                    self._json(200, app.memory.bootstrap(one["namespace"], one.get("topics", "").split(",") if one.get("topics") else None))
                elif path == "/api/search":
                    self._json(200, app.memory.search(one["namespace"], one.get("query", ""), None, int(one.get("limit", 12)), one.get("include_candidates") == "true", client=name))
                elif path == "/api/get":
                    self._json(200, app.memory.get(one.get("ids", "").split(",")))
                elif path == "/api/timeline":
                    self._json(200, app.memory.timeline(one["namespace"], int(one.get("limit", 20)), one.get("status")))
                elif path == "/api/context":
                    self._json(200, app.memory.project_context(one["namespace"], one.get("task", ""), None, int(one["budget"]) if one.get("budget") else None))
                elif path == "/api/namespaces":
                    self._json(200, app.store.namespaces())
                else:
                    self._json(404, {"error": "no such endpoint"})
            except (mcp.ToolError, KeyError, ValueError) as err:
                self._json(400, {"error": str(err)})

        def do_POST(self):
            app.requests += 1
            path = urlsplit(self.path).path
            ident = self._client()
            if not ident:
                self._json(401, {"error": "unknown client"})
                return
            name, scopes, verified = ident
            try:
                body = self._body()
            except ValueError:
                self._json(400, {"error": "body is not JSON"})
                return
            if path == "/mcp":
                messages = body if isinstance(body, list) else [body]
                replies = [r for r in (mcp.jsonrpc(app.memory, m, name, scopes, app.export_dir, verified) for m in messages if isinstance(m, dict)) if r]
                if not replies:
                    self._send(202, b"")
                    return
                self._json(200, replies[0] if len(replies) == 1 and not isinstance(body, list) else replies)
            elif path == "/api/review":
                # It forgets records, so it needs what memory_forget needs.
                if not mcp.permits(scopes, mcp.TOOL_SCOPE["memory_forget"]):
                    self._json(403, {"error": f"client {name!r} lacks the {mcp.TOOL_SCOPE['memory_forget']!r} scope"})
                else:
                    self._json(200, app.reviewer.run(int(body.get("limit") or 500)))
            elif path.startswith("/api/"):
                tool = "memory_" + path[len("/api/") :]
                needed = mcp.TOOL_SCOPE.get(tool)
                if needed is None:
                    self._json(404, {"error": "no such endpoint"})
                elif not mcp.permits(scopes, needed):
                    self._json(403, {"error": f"client {name!r} lacks the {needed!r} scope"})
                else:
                    try:
                        self._json(200, mcp.call_tool(app.memory, tool, body, name, app.export_dir, verified))
                    except (mcp.ToolError, KeyError, ValueError, TypeError) as err:
                        self._json(err.status if isinstance(err, mcp.ToolError) else 400, mcp.error_body(err))
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def do_DELETE(self):
            self._send(405, b"", extra={"Allow": "GET, POST"})

    return Handler


def serve(app: App, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    return server


def housekeeping(app: App, stop: threading.Event) -> None:
    while not stop.wait(6 * 3600):
        try:
            purged = app.store.purge(int(app.options.get("retention_deleted_days") or 30))
            LOG.info("housekeeping: purged %d, integrity %s", purged, app.store.integrity())
        except Exception:
            LOG.exception("housekeeping failed")


def anchor_sweeps(app: App, stop: threading.Event) -> None:
    hours = float(app.options.get("anchor_sweep_hours") or 6)
    while not stop.wait(hours * 3600):
        try:
            LOG.info("anchor sweep: %s", app.memory.anchor_sweep("anchor-sweep"))
        except Exception:
            LOG.exception("anchor sweep failed")


def candidate_reviews(app: App, stop: threading.Event) -> None:
    """The nightly candidate review at `review_at`, local time."""
    at = review_at(app.options)
    if at == "off":
        return
    while not stop.wait(review.seconds_until(at)):
        try:
            app.reviewer.run()
        except Exception:
            LOG.exception("candidate review failed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--options", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = App(load_options(args.options), args.data)
    serve(app, args.port)
    stop = threading.Event()
    threading.Thread(target=housekeeping, args=(app, stop), name="housekeeping", daemon=True).start()
    threading.Thread(target=app.worker.loop, args=(stop,), name="enrichment", daemon=True).start()
    threading.Thread(target=anchor_sweeps, args=(app, stop), name="anchors", daemon=True).start()
    threading.Thread(target=candidate_reviews, args=(app, stop), name="review", daemon=True).start()
    if app.indexer:
        threading.Thread(target=app.indexer.loop, args=(stop,), name="embeddings", daemon=True).start()
    embedding = f"{app.encoder.model} at {app.encoder.base}" if app.encoder else "off"
    LOG.info("serving on :%d, fts5 %s, %d clients, %d records, embeddings %s", args.port, app.store.fts, len(app.auth.clients), app.store.stats()["memories"], embedding)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
