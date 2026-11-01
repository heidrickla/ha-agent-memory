"""Agent Memory: shared, bounded context for coding agents, served from Home Assistant.

    python3 server.py --options /data/options.json --data /data --port 8765

One HTTP server carries three things: the MCP endpoint (`POST /mcp`, JSON-RPC over
streamable HTTP with JSON responses), a REST mirror of the same tools (`/api/...`) for
scripts, and the admin page plus `/health`. Clients authenticate by a bearer token whose
sha256 is in the options, or by source address for clients listed with an `ip`; each
client carries scopes (read, write, delete, export, admin). A client known by address alone gets
`read` at most. A client with a token is identified by name: what it writes carries that name as
`agent`, whatever the request says. A request with an `Origin` header is refused, and a POST must
be `application/json` of at most `MAX_BODY` bytes. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
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
from store import TYPES, Store

LOG = logging.getLogger("agent-memory")
INGRESS_NETS = ("172.30.32.", "172.30.33.")  # Home Assistant's ingress proxy addresses
INGRESS_PEER = "172.30.32.2"  # Supervisor ingress; a request header alone is not authentication.
# REST reads and the tool whose scope each needs: the same rule as the MCP tool.
REST_GET = {
    "/api/bootstrap": "memory_bootstrap",
    "/api/search": "memory_search",
    "/api/get": "memory_get",
    "/api/timeline": "memory_timeline",
    "/api/context": "memory_project_context",
    "/api/namespaces": "memory_timeline",
}
# A caller known by address alone is any process on that host, or a page in its browser: it reads only.
ADDRESS_SCOPES = frozenset({"read"})
MAX_BODY = 1 << 20  # bytes in one POST body


def review_at(options: dict) -> str:
    """`HH:MM` or `off`. Absent means 03:00: the Supervisor applies option defaults only at install."""
    return str(options.get("review_at") or "03:00").strip()


def load_options(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc if isinstance(doc, dict) else {}


def issue_tokens(clients: list[dict], path: str) -> tuple[list[dict], dict[str, str]]:
    """Clients listed by name alone get a token the add-on generates: (the clients with each hash filled in,
    the tokens generated now). Only hashes are kept in `path`; a name dropped from the options loses its token,
    so removing a client and adding it back issues a new one."""
    try:
        with open(path, encoding="utf-8") as fh:
            kept = json.load(fh)
    except FileNotFoundError:
        kept = {}
    named = {c["name"] for c in clients if not c.get("ip") and not c.get("token_sha256")}
    hashes = {n: h for n, h in kept.items() if n in named and isinstance(h, str)}
    issued: dict[str, str] = {}
    for name in sorted(named - hashes.keys()):
        issued[name] = secrets.token_urlsafe(32)
        hashes[name] = hashlib.sha256(issued[name].encode()).hexdigest()
    if hashes != kept:
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(hashes, fh)
        os.replace(tmp, path)
    filled = [{**c, "token_sha256": hashes[c["name"]]} if c["name"] in named else c for c in clients]
    return filled, issued


def forwarded_host(header: str | None) -> str:
    """The host name in an `X-Forwarded-Host` value, without its port; empty when it is not a plain host name or address."""
    host = (header or "").split(",")[0].strip()
    host = host[1 : host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host if re.fullmatch(r"[A-Za-z0-9.-]{1,253}|[0-9A-Fa-f:]{2,39}", host) else ""


def mcp_url(host: str = "") -> str:
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host or '<home-assistant-host>'}:8765/mcp"


def token_notice(name: str, token: str) -> list[str]:
    """The log lines that show a generated token once, with what each agent needs to use it."""
    return [
        f"client {name!r}: new token, shown once: {token}",
        f"  Claude Code: claude mcp add --scope user --transport http agent-memory {mcp_url()} --header 'Authorization: Bearer {token}'",
        f'  Codex: set AGENT_MEMORY_TOKEN to the token, then in ~/.codex/config.toml [mcp_servers.agent_memory] set url = "{mcp_url()}" and bearer_token_env_var = "AGENT_MEMORY_TOKEN"',
    ]


class Auth:
    def __init__(self, clients: list[dict]):
        self.clients = clients or []

    def identify(self, header: str | None, peer_ip: str) -> tuple[str, set[str], bool] | None:
        """(name, scopes, by_token). A bearer token must match a client's hash; with no token, a client
        listed by `ip` alone matches the source address and gets ADDRESS_SCOPES at most. An unknown token
        never falls back to the address."""
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
                return c["name"], set(c.get("scopes") or ["read"]) & ADDRESS_SCOPES, False
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
        listed = options.get("clients") or []
        clients, self.issued = issue_tokens(listed, os.path.join(data_dir, "client_tokens.json"))
        self.generated = [c["name"] for c in listed if not c.get("ip") and not c.get("token_sha256")]
        self.auth = Auth(clients)
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
            "version": mcp.VERSION,
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

    def client_rows(self) -> list[dict]:
        """How each client authenticates, for the page: never a hash or a token."""
        generated = set(self.generated)
        return [{"name": c.get("name"), "by": "generated token" if c.get("name") in generated else "token" if c.get("token_sha256") else "address"} for c in self.auth.clients]

    def panel_data(self, host: str = "") -> dict:
        """`host` is the name the browser used for Home Assistant, from the ingress proxy's forwarded headers."""
        now = dt.datetime.now(dt.UTC)
        since = (now - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        connect = {"url": mcp_url(host), "clients": self.client_rows()}
        return {"now": now, "health": self.health(), "counts": self.store.panel_counts(since), "audit": self.audit_tail(), "unused": self.store.unused(), "connect": connect}

    def browser_data(self, query: dict[str, list[str]]) -> dict:
        """Read-only, bounded record review for an authenticated Home Assistant ingress request."""
        one = {key: values[0] for key, values in query.items() if values}
        text = one.get("query", "")
        namespace = one.get("namespace", "")
        status = one.get("status", "")
        kind = one.get("kind", "")
        page_number = int(one.get("page", "1"))
        result = self.store.browse(text, namespace, status, kind, page_number)
        selected = next((record for record in result["records"] if record["id"] == one.get("id")), None)
        return {
            **result,
            "query": text.strip(),
            "namespace": namespace,
            "status": status,
            "kind": kind,
            "selected": selected or next(iter(result["records"]), None),
            "namespaces": self.store.namespaces(),
            "types": TYPES,
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"agent-memory/{mcp.VERSION}"
        _length = 0  # the POST body length, set by _refusal

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
            raw = self.rfile.read(self._length) if self._length else b""
            return json.loads(raw or b"{}")

        def _refusal(self, post: bool) -> tuple[int, str] | None:
            """Why a request is refused before authentication, or None. A browser sends `Origin` on every
            cross-site request and no agent client does; a page cannot send JSON cross-site without a preflight."""
            if self.headers.get("Origin") is not None:
                return 403, "cross-site requests are refused"
            if not post:
                return None
            if self.headers.get_content_type() != "application/json":
                return 415, "the body must be application/json"
            try:
                self._length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return 400, "bad Content-Length"
            if self._length < 0:
                return 400, "bad Content-Length"
            if self._length > MAX_BODY:
                return 413, f"the body is over {MAX_BODY} bytes"
            return None

        def _client(self) -> tuple[str, set[str], bool] | None:
            peer = self.client_address[0]
            ident = app.auth.identify(self.headers.get("Authorization"), peer)
            if ident is None:
                app.denied += 1
                app.store.audit(peer, "denied", detail=self.path)  # masked whole, then cut, by audit
            return ident

        def _ingress(self) -> bool:
            return self.client_address[0].startswith(INGRESS_NETS) or self.headers.get("X-Ingress-Path") is not None

        def _trusted_ingress(self) -> bool:
            return self.client_address[0] == INGRESS_PEER and bool(self.headers.get("X-Ingress-Path"))

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
                query = parse_qs(urlsplit(self.path).query)
                view = query.get("view", ["overview"])[0]
                if view == "browse" and not self._trusted_ingress():
                    self._send(404, b"not found\n", "text/plain; charset=utf-8")
                    return
                if view not in ("overview", "browse"):
                    self._send(404, b"not found\n", "text/plain; charset=utf-8")
                    return
                try:
                    browser = app.browser_data(query) if view == "browse" else None
                except ValueError:
                    self._send(400, b"invalid browser filter\n", "text/plain; charset=utf-8")
                    return
                self._send(200, page.render(app.panel_data(forwarded_host(self.headers.get("X-Forwarded-Host"))), browser).encode(), "text/html; charset=utf-8")
            elif path == "/mcp":
                self._send(405, b"", extra={"Allow": "POST"})
            elif path.startswith("/api/"):
                refusal = self._refusal(post=False)
                if refusal:
                    self._json(refusal[0], {"error": refusal[1]})
                    return
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
                    self._json(200, app.memory.timeline(one["namespace"], int(one.get("limit", 20)), one.get("status"), one.get("after")))
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
            refusal = self._refusal(post=True)
            if refusal:
                self._json(refusal[0], {"error": refusal[1]})
                return
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
    LOG.info("agent-memory %s serving on :%d, fts5 %s, %d clients, %d records, embeddings %s", mcp.VERSION, args.port, app.store.fts, len(app.auth.clients), app.store.stats()["memories"], embedding)
    for name, token in app.issued.items():
        for line in token_notice(name, token):
            LOG.info("%s", line)
    if not app.auth.clients:
        LOG.warning("no clients are configured: add one to the clients option by name, and the add-on generates its token at the next start")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
