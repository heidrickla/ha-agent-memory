"""The OpenAI-style client: a `/v1` URL selects it, and both job kinds run through it end to end.

agent-memory's model moved to Bonsai behind llama-swap, which speaks `/v1/models` and
`/v1/chat/completions`, not Ollama's `/api/tags` and `/api/chat`. The Ollama path stays.
"""

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
from enrich import Ollama, OpenAIChat, Worker, client_for
from store import Store

NS = "gitea/heidrickla/ha-management"
GOOD = {"memories": [{"type": "constraint", "title": "One model per GPU", "content": "Card 0 serves Bonsai and nothing else.", "confidence": 0.9}]}


class FakeOpenAI(BaseHTTPRequestHandler):
    """Answers only the OpenAI-style paths under /v1; Ollama's paths get 404, as from llama-swap."""

    seen: ClassVar[list] = []
    reply: ClassVar[object] = None  # None: a chat completion carrying `content` below
    content: ClassVar[str] = json.dumps(GOOD)
    missing: ClassVar[set] = set()  # model names answered with 404, as llama-swap does for an unknown model

    def log_message(self, *a):
        pass

    def _send(self, status: int, doc) -> None:
        out = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "bonsai2-memory"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        FakeOpenAI.seen.append((self.path, body))
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})
        if body.get("model") in FakeOpenAI.missing:
            return self._send(404, {"error": {"message": f"could not find model {body['model']}"}})
        if FakeOpenAI.reply is not None:
            return self._send(200, FakeOpenAI.reply)
        self._send(200, {"object": "chat.completion", "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": FakeOpenAI.content}}]})


@pytest.fixture
def v1():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeOpenAI.seen, FakeOpenAI.reply, FakeOpenAI.content, FakeOpenAI.missing = [], None, json.dumps(GOOD), set()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def options(url: str, **over) -> dict:
    o = {
        "clients": [{"name": "desk", "ip": "127.0.0.1", "scopes": ["read", "write"]}],
        "github_owner": "heidrickla",
        "forge_owner": "heidrickla",
        "forge_hosts": [],
        "project_links": [],
        "ollama_url": url,
        "ollama_model": "bonsai2-memory",
        "ollama_timeout_s": 10,
    }
    o.update(over)
    return o


def test_a_v1_url_selects_the_openai_client_and_anything_else_ollama():
    assert isinstance(client_for("http://192.0.2.20:8080/v1", "m"), OpenAIChat)
    assert isinstance(client_for("http://192.0.2.20:8080/v1/", "m"), OpenAIChat)
    assert isinstance(client_for("http://192.0.2.20:11434", "m"), Ollama)
    assert isinstance(client_for("http://192.0.2.20:11434/", "m"), Ollama)
    assert isinstance(client_for("http://v1.example:11434", "m"), Ollama), "the host name is not the API"


def test_availability_is_read_from_v1_models(v1):
    """llama-swap answers /v1/models and 404s Ollama's /api/tags, so probing /api/tags would leave jobs waiting forever."""
    assert OpenAIChat(v1, "m").available() is True
    assert Ollama(v1.removesuffix("/v1"), "m").available() is False, "control: the Ollama probe fails against this server"
    assert OpenAIChat("http://127.0.0.1:9/v1", "m").available() is False


def test_the_extract_request_asks_for_json_with_thinking_off(v1):
    OpenAIChat(v1, "default").extract(NS, "notes", "bonsai2-memory")
    path, body = FakeOpenAI.seen[-1]
    assert path == "/v1/chat/completions"
    assert body["model"] == "bonsai2-memory" and body["stream"] is False and body["temperature"] == 0.1
    fmt = body.get("response_format") or {}
    assert fmt.get("type") == "json_schema", "json_object is not enforced by the fork; a schema is compiled to a grammar"
    assert fmt["json_schema"]["schema"]["required"] == ["memories"]
    assert fmt["json_schema"]["schema"]["properties"]["memories"]["type"] == "array"
    assert body.get("chat_template_kwargs") == {"enable_thinking": False}, "thinking must be off or the reply comes back empty"
    assert body["messages"][0] == {"role": "system", "content": enrich.INSTRUCTIONS}
    assert body["max_tokens"] == enrich.EXTRACT_TOKENS


def test_the_answer_request_is_prose_with_thinking_off(v1):
    OpenAIChat(v1, "default").answer("which host", "records")
    _, body = FakeOpenAI.seen[-1]
    assert body["model"] == "default"
    assert "response_format" not in body, "an answer is prose"
    assert body.get("chat_template_kwargs") == {"enable_thinking": False}
    assert body["messages"][0] == {"role": "system", "content": enrich.ANSWER_INSTRUCTIONS}
    assert body["max_tokens"] == enrich.ANSWER_TOKENS


def test_both_job_kinds_run_through_a_v1_server(tmp_path, v1, caplog):
    app = srv.App(options(v1, ollama_models=["extract:bonsai2-memory", "answer:bonsai2-memory"]), str(tmp_path))
    assert app.health()["enrichment"]["api"] == "openai"
    closed = app.memory.session_close(NS, "Served Bonsai.", "claude-code", "s1", "notes about the card", "desk", True)
    assert closed["enrichment"] == "queued"
    with caplog.at_level("INFO", logger="agent-memory.enrich"):
        assert app.worker.step() is True
    cands = [c for c in app.store.by_namespace([NS], statuses=("candidate",)) if c["type"] != "session-summary"]
    assert [c["title"] for c in cands] == ["One model per GPU"]
    assert cands[0]["agent"] == "openai:bonsai2-memory", "the record must name the API and the model that served"
    assert any("extract job" in r.getMessage() and "bonsai2-memory" in r.getMessage() for r in caplog.records)

    FakeOpenAI.content = "Card 0 serves Bonsai."
    app.store.store({"namespace": NS, "title": "Card 0", "content": "Card 0 serves Bonsai and nothing else."}, "desk")
    app.store.add_question(NS, "what does card 0 serve", "desk")
    caplog.clear()
    with caplog.at_level("INFO", logger="agent-memory.enrich"):
        assert app.worker.step() is True
    q = app.store.find_question([NS], "what does card 0 serve")
    assert q["answer"] == "Card 0 serves Bonsai." and q["model"] == "openai:bonsai2-memory"
    assert any("answer job" in r.getMessage() and "bonsai2-memory" in r.getMessage() for r in caplog.records)
    assert {p for p, _ in FakeOpenAI.seen} == {"/v1/chat/completions"}


def test_a_reply_in_one_code_fence_is_read_inside_it():
    """Measured 2026-09-28: with `json_object` the fork fenced 11 of 12 replies to a 9-character note, each fence
    cost an attempt, and one job gave up after 5."""
    assert enrich.parse_candidates('```json\n{"memories": []}\n```') == []
    assert enrich.parse_candidates("```\n" + json.dumps(GOOD) + "\n```")[0]["title"] == "One model per GPU"
    assert enrich.parse_candidates('  ```json\n{"memories": []}```  ') == []
    for bad in ("```json\nnot json\n```", 'Here: ```json\n{"memories": []}\n```', '```json\n{"memories": []}', ""):
        with pytest.raises(ValueError):
            enrich.parse_candidates(bad)


def test_a_fenced_extraction_is_stored_not_retried(tmp_path, v1):
    FakeOpenAI.content = "```json\n" + json.dumps(GOOD) + "\n```"
    w = Worker(Store(str(tmp_path / "m.db")), options(v1))
    w.store.enqueue("extract", NS, {"notes": "some notes", "session": "s1"})
    assert w.step() is True
    assert [c["title"] for c in w.store.by_namespace([NS], statuses=("candidate",))] == ["One model per GPU"]
    assert w.store.db.execute("select attempts from jobs").fetchone()["attempts"] == 0


def test_a_reply_that_is_not_a_chat_completion_costs_an_attempt_not_the_worker(tmp_path, v1):
    """A 200 carrying an error object must fail the model like an unparseable reply, not raise KeyError out of step()."""
    FakeOpenAI.reply = {"error": {"message": "context overflow"}}
    w = Worker(Store(str(tmp_path / "m.db")), options(v1))
    w.store.enqueue("extract", NS, {"notes": "some notes", "session": "s1"})
    assert w.step() is False
    job = w.store.db.execute("select status, attempts, last_error from jobs").fetchone()
    assert job["status"] == "queued" and job["attempts"] == 1 and "not a chat completion" in job["last_error"]


def test_an_unknown_alias_costs_the_next_model(tmp_path, v1):
    """llama-swap answers 404 for a model it does not serve; the chain walks on."""
    FakeOpenAI.missing = {"not-served"}
    w = Worker(Store(str(tmp_path / "m.db")), options(v1, ollama_models=["extract:not-served"]))
    w.store.enqueue("extract", NS, {"notes": "some notes", "session": "s1"})
    assert w.step() is True
    assert [b["model"] for _, b in FakeOpenAI.seen] == ["not-served", "bonsai2-memory"]


def test_a_dead_v1_server_keeps_jobs_waiting(tmp_path):
    app = srv.App(options("http://127.0.0.1:9/v1"), str(tmp_path))
    app.memory.session_close(NS, "s", "a", "s3", "notes", "desk", True)
    assert app.worker.step() is False
    assert app.store.next_job() is not None
    health = app.health()
    assert health["status"] == "degraded" and health["enrichment"]["ollama"] == "offline" and health["enrichment"]["api"] == "openai"
    assert app.store.db.execute("select attempts from jobs").fetchone()["attempts"] == 0, "waiting spends no attempt"


def test_an_ollama_url_still_selects_the_ollama_client(tmp_path):
    app = srv.App(options("http://127.0.0.1:9"), str(tmp_path))
    assert isinstance(app.worker.ollama, Ollama) and app.health()["enrichment"]["api"] == "ollama"
