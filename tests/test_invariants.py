"""The four invariants of 0.11.0, one test per guard.

A. Authorization comes from the connection and the token, and every data route checks scope by the
   same rule as MCP. A receipt answers only the client and the inputs that made it.
B. Nothing reaches the database unscreened, and screening sees the input as sent: every write passes
   one choke point, audit context is masked, and the do-not-store marker is looked for before notes
   are redacted or cut.
C. Every mutation, and every read that observes it, is one transaction: anchor reports, extraction
   and answer jobs commit whole; a read waits for a transaction another thread holds open; an
   operation id is never executed twice, receipt expired or not.
D. The bootstrap budget is measured on the bytes that go on the wire, and its base fields are bounded.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import filters
import mcp
import server as srv
from enrich import Worker
from store import Refused, Store, now_iso

NS = "gitea/heidrickla/ha-management"
SECRET = "ghp_" + "b" * 36  # the github_token pattern's shape, not a credential
READER, WRITER = "reader-token-0123456789abcdef", "writer-token-0123456789abcdef"
OPTIONS = {
    "clients": [
        {"name": "desktop", "ip": "127.0.0.1", "scopes": ["read", "write", "delete", "export"]},
        {"name": "claude", "token_sha256": hashlib.sha256(READER.encode()).hexdigest(), "scopes": ["read", "write"]},
        {"name": "writer", "token_sha256": hashlib.sha256(WRITER.encode()).hexdigest(), "scopes": ["write"]},
    ],
    "github_owner": "heidrickla",
    "forge_owner": "heidrickla",
    "forge_hosts": ["forge.example", "192.0.2.20"],
    "project_links": [],
    "retention_deleted_days": 30,
}


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "m.sqlite"))


@pytest.fixture
def memory(store):
    return mcp.Memory(store, OPTIONS)


@pytest.fixture
def http(tmp_path):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    server = srv.serve(app, 0, host="127.0.0.1")
    yield f"http://127.0.0.1:{server.server_address[1]}", app
    server.shutdown()


def rec(**kw):
    base = {"namespace": NS, "type": "constraint", "title": "Kiosk reports through the sensor", "content": "Committed text about the kiosk."}
    base.update(kw)
    return base


def raw(base, path, body=None, token=None) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def audit_text(store) -> str:
    return json.dumps([dict(r) for r in store.db.execute("select * from audit").fetchall()])


# ------------------------------------------------------------------ A. authorization


def test_rest_reads_need_the_read_scope_like_mcp(http):
    base, app = http
    rid = app.store.store(rec(), "desktop")["id"]
    for path in (
        f"/api/get?ids={rid}",
        f"/api/search?namespace={NS}&query=kiosk",
        f"/api/bootstrap?namespace={NS}",
        f"/api/timeline?namespace={NS}",
        f"/api/context?namespace={NS}&task=kiosk",
        "/api/namespaces",
    ):
        status, body = raw(base, path, token=WRITER)
        assert status == 403 and b"Committed" not in body, path
        assert raw(base, path, token=READER)[0] == 200, path
    status, body = raw(base, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_get", "arguments": {"ids": [rid]}}}, WRITER)
    assert status == 200 and json.loads(body)["result"]["isError"] is True


def test_a_receipt_answers_only_the_client_that_made_it(tmp_path):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    first = app.memory.session_close(NS, "Closed by Codex.", "codex", "s1", "notes", "codex", True, "op-shared-000001")
    with pytest.raises(mcp.ToolError, match="another client") as err:
        app.memory.session_close(NS, "Closed by Codex.", "claude", "s1", "notes", "claude", True, "op-shared-000001")
    assert err.value.status == 409
    assert app.store.get(first["stored"])["agent"] == "codex"
    assert app.store.db.execute("select count(*) from memories where type='session-summary'").fetchone()[0] == 1


def test_a_receipt_answers_only_the_inputs_that_made_it(tmp_path):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    app.memory.session_close(NS, "Same summary.", "codex", "s1", "notes", "desktop", True, "op-agent-0000001")
    with pytest.raises(mcp.ToolError, match="different close") as err:
        app.memory.session_close(NS, "Same summary.", "claude-code", "s1", "notes", "desktop", True, "op-agent-0000001")
    assert err.value.status == 409


# ------------------------------------------------------------------ B. nothing unscreened persists


def test_a_refused_namespace_never_reaches_audit_queries_questions_or_jobs(memory, store):
    bad = f"local/{SECRET}"
    with pytest.raises(mcp.ToolError):
        memory.store_record(rec(namespace=bad), "desktop")
    memory.search(bad, "kiosk", client="desktop")
    with pytest.raises(mcp.ToolError):
        memory.question_add(bad, "which sensor does the kiosk report through", "desktop")
    with pytest.raises(Refused):
        store.enqueue("extract", bad, {"notes": "fine"})
    dump = audit_text(store) + json.dumps([dict(r) for t in ("queries", "questions", "jobs") for r in store.db.execute(f"select * from {t}").fetchall()])
    assert SECRET not in dump and "ghp_" not in dump
    assert "[screened:github_token]" in audit_text(store), "the rejection names the pattern in place of the value"


def test_audit_masks_a_secret_judged_before_the_cut(store, http):
    store.audit("desk", "note", NS, "", "x" * 480 + " " + SECRET)
    assert "ghp_" not in audit_text(store)
    base, app = http
    status, _ = raw(base, f"/api/search?namespace={NS}&query={SECRET}", token="unknown-token-0123456789abcdef")
    assert status == 401 and SECRET not in audit_text(app.store) and "denied" in audit_text(app.store)


def test_every_write_to_the_database_passes_the_screen(store):
    with pytest.raises(Refused, match="audit"):
        store.db.execute("insert into audit(at, client, action, namespace, record_id, detail) values (?,?,?,?,?,?)", (now_iso(), "desk", "x", NS, "", SECRET))
    rid = store.store(rec(), "desk")["id"]
    with pytest.raises(Refused, match="memories"):
        store.db.execute("update memories set content=? where id=?", (f"now {SECRET}", rid))
    assert SECRET not in audit_text(store) and store.get(rid)["content"] == "Committed text about the kiosk."
    jid = store.enqueue("extract", NS, {"notes": "fine"})
    store.job_failed(jid, f"HTTP Error 401 for {SECRET}", give_up=False)
    assert "ghp_" not in store.db.execute("select last_error from jobs").fetchone()[0]


@pytest.mark.parametrize("notes", ["x" * 12001 + " do-not-store the rest", "password=do-not-store private decision"], ids=["past-the-cut", "inside-a-redaction"])
def test_the_marker_is_looked_for_in_the_notes_as_sent(tmp_path, notes):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    closed = app.memory.session_close(NS, "Summary.", "claude-code", "s1", notes, "desktop", True)
    assert closed["enrichment"] == "refused" and closed["enrichment_job"] is None
    assert app.store.queue_depth()["queued"] == 0


@pytest.mark.parametrize("query", ["author=sam", "session_count=3", "page=2", "authority=x", "sort_key=a", "sessions=4"])
def test_ordinary_url_parameters_are_kept(store, query):
    assert store.store(rec(anchors=[f"url:https://host.lan/docs?{query}"]), "desk")["anchors"] == [f"url:https://host.lan/docs?{query}"]


@pytest.mark.parametrize("query", ["access_token=a", "apiKey=a", "X-Amz-Signature=a", "sig=a", "session=a", "sessionid=a", "session_id=a", "password=a", "key=a", "auth=a", "client_secret=a"])
def test_credential_parameters_are_refused_by_name(store, query):
    assert filters.url_credentials(f"https://host.lan/x?{query}") == "url_credential_parameter"
    with pytest.raises(Refused):
        store.store(rec(anchors=[f"url:https://host.lan/x?{query}"]), "desk")


# ------------------------------------------------------------------ C. one transaction


def test_an_anchor_report_is_one_transaction(store, monkeypatch):
    rid = store.store(rec(anchors=["entity:sensor.kiosk"]), "desk")["id"]

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_evidence", broken)
    with pytest.raises(sqlite3.OperationalError):
        store.report_anchor(rid, "entity:sensor.kiosk", "missing", "sweep")
    got = store.get(rid)
    assert got["anchor_states"][0]["state"] == "unchecked" and got["revision"] == 1 and "missing_anchors" not in got
    monkeypatch.undo()
    assert store.report_anchor(rid, "entity:sensor.kiosk", "missing", "sweep")["filed"] == "contradicts"
    assert store.get(rid)["refutation_count"] == 1


def test_an_anchor_state_change_advances_the_revision(store):
    rid = store.store(rec(anchors=["entity:sensor.kiosk"]), "desk")["id"]
    rev = lambda: store.get(rid)["revision"]  # noqa: E731
    steps = [("ok", 2), ("ok", 2), ("unavailable", 3), ("unavailable", 3), ("ok", 4), ("missing", 5), ("missing", 5), ("ok", 6)]
    for state, expected in steps:
        store.report_anchor(rid, "entity:sensor.kiosk", state, "sweep")
        assert rev() == expected, (state, rev())
    assert (store.get(rid)["proof_count"], store.get(rid)["refutation_count"]) == (1, 1)


def test_a_read_never_sees_a_transaction_another_thread_holds_open(store):
    rid = store.store(rec(), "desk")["id"]
    opened, release, seen = threading.Event(), threading.Event(), {}

    def writer():
        try:
            with store.transaction():
                store.db.execute("update memories set content='Transient text about the kiosk.' where id=?", (rid,))
                store.db.execute("insert into jobs(id, created_at, kind, namespace, payload) values ('j1', ?, 'extract', ?, '{}')", (now_iso(), NS))
                opened.set()
                release.wait(5)
                raise RuntimeError("rolled back")
        except RuntimeError:
            pass

    reads = {
        "search": lambda: [h["content"] for h in store.search("kiosk", [NS])],
        "timeline": lambda: [r["content"] for r in store.timeline(NS)],
        "by_namespace": lambda: [r["content"] for r in store.by_namespace([NS])],
        "get": lambda: store.get(rid)["content"],
        "job": store.next_job,
    }

    def reader(name):
        seen[name] = reads[name]()

    w = threading.Thread(target=writer)
    w.start()
    assert opened.wait(5)
    readers = [threading.Thread(target=reader, args=(name,)) for name in reads]
    for r in readers:
        r.start()
    time.sleep(0.3)
    blocked = [r.is_alive() for r in readers]
    release.set()
    w.join(5)
    for r in readers:
        r.join(5)
    committed = ["Committed text about the kiosk."]
    assert seen == {"search": committed, "timeline": committed, "by_namespace": committed, "get": committed[0], "job": None}
    assert all(blocked), "every read waited for the open transaction"


class OneModel:
    model, api = "m", "ollama"

    def available(self):
        return True

    def extract(self, namespace, notes, model=""):
        items = [{"type": "constraint", "title": f"Fact {i}", "content": f"Something true, number {i}.", "confidence": 0.8} for i in range(3)]
        return json.dumps({"memories": items})

    def answer(self, question, records, model=""):
        return "The kiosk reports through the sensor."


def test_an_interrupted_extraction_stores_nothing_and_resumes_whole(store, monkeypatch):
    worker = Worker(store, {"ollama_model": "m"}, ollama=OneModel())
    store.enqueue("extract", NS, {"summary_id": "", "notes": "decided three things"})
    job = store.next_job()
    real, calls = store.store_candidate, []

    def second_fails(rec, client):
        calls.append(rec["title"])
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(rec, client)

    monkeypatch.setattr(store, "store_candidate", second_fails)
    with pytest.raises(sqlite3.OperationalError):
        worker.run_job(job)
    assert store.db.execute("select count(*) from memories").fetchone()[0] == 0
    assert store.next_job()["id"] == job["id"], "the job is still queued"
    monkeypatch.undo()
    assert worker.step() is True
    assert store.db.execute("select count(*) from memories where status='candidate'").fetchone()[0] == 3
    assert store.next_job() is None


def test_an_answer_and_its_job_commit_together(store, monkeypatch):
    store.store(rec(), "desk")
    q = store.add_question(NS, "which sensor does the kiosk report through", "desk")
    worker = Worker(store, {"ollama_model": "m"}, ollama=OneModel())
    store.enqueue("answer", NS, {"question_id": q["id"]})
    job = store.next_job()

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "job_done", broken)
    with pytest.raises(sqlite3.OperationalError):
        worker.run_job(job)
    assert store.get_question(q["id"])["answer"] == "" and store.next_job()["id"] == job["id"]


def test_a_no_source_answer_and_its_job_commit_together(store, monkeypatch):
    q = store.add_question(NS, "which sensor does the kiosk report through", "desk")
    worker = Worker(store, {"ollama_model": "m"}, ollama=OneModel())
    store.enqueue("answer", NS, {"question_id": q["id"]})
    job = store.next_job()

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "job_done", broken)
    with pytest.raises(sqlite3.OperationalError):
        worker.run_job(job)
    assert store.get_question(q["id"])["answer"] == "" and store.next_job()["id"] == job["id"]


def test_a_replay_after_the_receipt_expired_is_refused_not_run(tmp_path):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    args = (NS, "Closed long ago.", "claude-code", "s1", "decided to spool", "desktop", True, "op-expired-00001")
    app.memory.session_close(*args)
    app.store.db.execute("update receipts set at='2000-01-01T00:00:00+00:00'")
    app.store.purge(30)
    with pytest.raises(mcp.ToolError, match="expired") as err:
        app.memory.session_close(*args)
    assert err.value.status == 410
    assert app.store.db.execute("select count(*) from memories where type='session-summary'").fetchone()[0] == 1
    assert app.store.queue_depth()["queued"] == 1
    assert app.store.receipt("op-expired-00001")["result"] == {}, "the tombstone keeps no result"


# ------------------------------------------------------------------ D. bootstrap on the wire


def test_the_bootstrap_budget_holds_on_the_mcp_wire(http):
    base, app = http
    for i in range(30):
        app.store.store(rec(title=f'Quoted "path" {i} C:\\Users\\sam\\x', content='He said "use \\\\server\\share" ' * 6), "desktop")
    status, body = raw(base, "/mcp", {"jsonrpc": "2.0", "id": "a-long-request-id-" * 4, "method": "tools/call", "params": {"name": "memory_bootstrap", "arguments": {"namespace": NS}}})
    doc = json.loads(json.loads(body)["result"]["content"][0]["text"])
    assert status == 200 and len(body) <= 2000 and doc["items"], len(body)
    assert doc["approx_tokens"] * 4 >= len(body)
    status, body = raw(base, f"/api/bootstrap?namespace={NS}")
    assert status == 200 and len(body) <= 2000


def test_bootstrap_base_fields_are_bounded(memory):
    with pytest.raises(mcp.ToolError, match="within 160"):
        memory.bootstrap("gitea/heidrickla/" + "r" * 200)
    with pytest.raises(mcp.ToolError, match="topics"):
        memory.bootstrap(NS, [f"t{i}" for i in range(9)])
    with pytest.raises(mcp.ToolError, match="topics"):
        memory.bootstrap(NS, ["a/b"])
    assert memory.bootstrap(NS, [f"t{i}" for i in range(8)])["items"] == []


def test_a_bootstrap_base_that_cannot_fit_is_refused(store):
    links = [{"project": f"p{i:02d}" + "x" * 36, "namespaces": [NS]} for i in range(60)]
    memory = mcp.Memory(store, {**OPTIONS, "project_links": links})
    with pytest.raises(mcp.ToolError, match="does not fit"):
        memory.bootstrap(NS)


# ------------------------------------------------------------------ E. relative path anchors


def test_the_anchor_listing_names_each_records_namespace(store):
    store.store(rec(anchors=["path:docs/design.md"]), "desk")
    store.store(rec(namespace="global", anchors=["path:D:/x.txt"]), "desk")
    assert {(a["namespace"], a["anchor"]) for a in store.anchors("path")} == {(NS, "path:docs/design.md"), ("global", "path:D:/x.txt")}
