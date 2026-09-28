"""Phase 4 exit criteria: usable offline, jobs resume without duplicates, malformed output
rejected, extraction preserves decisions, constraints, failures and next steps."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import enrich
import server as srv

NS = "gitea/example/ha-management"
GOOD = {
    "memories": [
        {"type": "architectural-decision", "title": "Loops live in an add-on", "content": "Recurring work runs in the ha-loops add-on, not on the desktop.", "confidence": 0.9},
        {"type": "constraint", "title": "No token on the command line", "content": "Secrets travel on stdin, never argv.", "confidence": 0.95},
        {"type": "failure-and-fix", "title": "s6 hides the Supervisor token", "content": "Read it from /run/s6/container_environment in run.sh.", "confidence": 0.9},
        {"type": "next-step", "title": "Fix the six core log errors", "content": "Start with homeassistant.helpers.entity.", "confidence": 0.7},
        {"type": "nonsense-type", "title": "dropped", "content": "wrong type", "confidence": 0.5},
        {"type": "repository-fact", "title": "leaked", "content": "the key is uk1_QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ", "confidence": 0.5},
    ]
}


class FakeOllama(BaseHTTPRequestHandler):
    mode: ClassVar[str] = "good"
    seen: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        FakeOllama.seen.append(body)
        content = json.dumps(GOOD) if FakeOllama.mode == "good" else "not json at all"
        out = json.dumps({"message": {"role": "assistant", "content": content}, "model": body["model"]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeOllama.mode, FakeOllama.seen = "good", []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def options(url: str) -> dict:
    return {
        "clients": [{"name": "desk", "ip": "127.0.0.1", "scopes": ["read", "write"]}],
        "github_owner": "heidrickla",
        "forge_owner": "example",
        "forge_hosts": [],
        "project_links": [],
        "ollama_url": url,
        "ollama_model": "test-model",
        "ollama_timeout_s": 10,
    }


def test_extraction_stores_validated_candidates_once(tmp_path, ollama):
    app = srv.App(options(ollama), str(tmp_path))
    closed = app.memory.session_close(NS, "Built loops.", "claude-code", "s1", "notes with a password=hunter2hunter2 inside and decisions", "desk", True)
    assert closed["enrichment"] == "queued"
    assert app.worker.step() is True
    sent = FakeOllama.seen[0]["messages"][1]["content"]
    assert "[redacted:assignment]" in sent and "hunter2" not in sent  # redacted before it left
    assert FakeOllama.seen[0]["format"] == "json" and FakeOllama.seen[0]["model"] == "test-model"
    cands = [c for c in app.store.by_namespace([NS], statuses=("candidate",)) if c["type"] != "session-summary"]
    assert {c["title"] for c in cands} == {"Loops live in an add-on", "No token on the command line", "s6 hides the Supervisor token", "Fix the six core log errors"}
    assert all(c["agent"] == "ollama:test-model" and c["enrichment"].startswith("extract-v1:") for c in cands)
    assert app.memory.search(NS, "token argv")["hits"] == []  # candidates stay out of search by default
    assert app.memory.search(NS, "token argv", include_candidates=True)["hits"]
    assert app.store.queue_depth()["queued"] == 0 and app.worker.state()["processed"] == 1
    # a crash after storing but before job_done: the retry stores nothing twice
    job_id = cands[0]["enrichment"].split(":", 1)[1]
    app.store.db.execute("update jobs set status='queued' where id=?", (job_id,))
    assert app.worker.step() is True
    assert len([c for c in app.store.by_namespace([NS], statuses=("candidate",)) if c["type"] != "session-summary"]) == 4 and len(FakeOllama.seen) == 1
    promoted = app.memory.update(cands[0]["id"], {"status": "active"}, "desk", cands[0]["revision"])
    assert promoted["updated"]["status"] == "active" and app.memory.search(NS, "add-on desktop")["hits"]


def test_malformed_output_is_retried_then_given_up(tmp_path, ollama):
    FakeOllama.mode = "bad"
    app = srv.App(options(ollama), str(tmp_path))
    app.memory.session_close(NS, "s", "a", "s2", "notes", "desk", True)
    for _ in range(enrich.MAX_ATTEMPTS - 1):
        assert app.worker.step() is False
        assert app.store.next_job() is not None  # still queued
    assert app.worker.step() is False
    assert app.store.next_job() is None and app.worker.state()["failed"] == 1
    job = app.store.db.execute("select status, attempts, last_error from jobs").fetchone()
    assert job["status"] == "failed" and job["attempts"] == enrich.MAX_ATTEMPTS and "JSONDecodeError" in job["last_error"]
    assert [c for c in app.store.by_namespace([NS], statuses=("candidate",)) if c["type"] != "session-summary"] == []


def test_offline_keeps_jobs_and_degrades_health(tmp_path):
    app = srv.App(options("http://127.0.0.1:9"), str(tmp_path))  # nothing listens there
    app.memory.session_close(NS, "s", "a", "s3", "notes", "desk", True)
    assert app.worker.step() is False
    assert app.store.next_job() is not None
    health = app.health()
    assert health["status"] == "degraded" and health["enrichment"]["ollama"] == "offline" and health["enrichment"]["queued"] == 1
    assert app.memory.store_record({"namespace": NS, "type": "constraint", "title": "still writable", "content": "writes succeed while Ollama is away"}, "desk")
    assert app.memory.search(NS, "writable")["hits"]


def test_worker_off_without_a_url(tmp_path):
    opts = options("")
    opts.pop("ollama_url")
    app = srv.App(opts, str(tmp_path))
    assert app.worker.ollama is None and app.worker.step() is False
    assert app.health()["enrichment"]["ollama"] == "off"


def test_parse_candidates_shapes():
    assert enrich.parse_candidates(json.dumps({"memories": []})) == []
    with pytest.raises(ValueError):
        enrich.parse_candidates(json.dumps({"other": 1}))
    many = {"memories": [{"type": "constraint", "title": f"t{i}", "content": "c", "confidence": 2} for i in range(20)]}
    out = enrich.parse_candidates(json.dumps(many))
    assert len(out) == enrich.MAX_ITEMS and all(c["confidence"] == 1.0 for c in out)
