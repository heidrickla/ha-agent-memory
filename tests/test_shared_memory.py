"""Shared memory, revision 2 of the design (docs/shared-memory.md): server-owned status and identity,
candidate session summaries, the bootstrap budget over the whole response, every persisted field
screened, integer revisions, supersede rules, anchor states and idempotent session closes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import mcp
import server as srv
from store import Conflict, Refused, Rejected, Store, now_iso

NS = "gitea/heidrickla/ha-management"
OTHER = "gitea/heidrickla/ha-loops"
TOKEN = "claude-token-for-tests-0123456789abcdef"
SECRET = "ghp_" + "a" * 36  # the github_token pattern's shape, not a credential
OPTIONS = {
    "clients": [
        {"name": "desktop", "ip": "127.0.0.1", "scopes": ["read", "write", "delete", "export"]},
        {"name": "claude", "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "scopes": ["read", "write"]},
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


def rec(**kw):
    base = {"namespace": NS, "type": "constraint", "title": "Restarts are pre-authorized", "content": "Home Assistant restarts never need permission."}
    base.update(kw)
    return base


def count(store, where="1=1"):
    return store.db.execute(f"select count(*) from memories where {where}").fetchone()[0]


@pytest.fixture
def http(tmp_path):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    server = srv.serve(app, 0, host="127.0.0.1")
    yield f"http://127.0.0.1:{server.server_address[1]}", app
    server.shutdown()


def call(base, path, body=None, token=None):
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def tool(base, name, arguments, token=None):
    status, reply = call(base, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, token)
    assert status == 200
    return reply["result"]["isError"], json.loads(reply["result"]["content"][0]["text"])


# ------------------------------------------------------------------ 1. status and identity


def test_a_direct_write_is_active_whatever_status_or_id_it_sends(memory, store):
    for sent in ("candidate", "activ", "superseded"):
        out = memory.store_record(rec(status=sent, id="chosen-by-caller", enrichment="extract-v1:x"), "desk")["stored"]
        assert out["status"] == "active" and out["id"] != "chosen-by-caller" and out["revision"] == 1
        assert store.get(out["id"])["enrichment"] == ""
    assert store.get("chosen-by-caller") is None


def test_status_is_one_of_the_lifecycle_states(store):
    with pytest.raises(Rejected):
        store.store(rec(status="activ"), "desk")
    with pytest.raises(Rejected):
        store.store(rec(status="superseded"), "desk")
    assert count(store) == 0
    r = store.store(rec(), "desk")
    for bad in ("superseded", "deleted", "activ"):
        with pytest.raises(Rejected):
            store.update(r["id"], {"status": bad}, "desk", store.get(r["id"])["revision"])
    assert store.get(r["id"])["status"] == "active" and store.get(r["id"])["revision"] == 1
    demoted = store.update(r["id"], {"status": "candidate"}, "desk", 1)
    promoted = store.update(r["id"], {"status": "active"}, "desk", demoted["revision"])
    assert (demoted["status"], promoted["status"], promoted["revision"]) == ("candidate", "active", 3)


def test_extraction_and_import_write_candidates_through_the_internal_operation(store):
    c = store.store_candidate(rec(status="active"), "enrichment")
    assert c["status"] == "candidate"
    target = store.store(rec(title="Older"), "desk")
    with pytest.raises(Rejected):
        store.store_candidate(rec(supersedes=target["id"]), "enrichment")
    assert store.get(target["id"])["status"] == "active"


def test_a_token_client_is_named_by_the_server_and_an_address_client_as_it_says(http):
    base, app = http
    err, out = tool(base, "memory_store", rec(agent="codex"), TOKEN)
    assert not err and out["stored"]["agent"] == "claude"
    err, out = tool(base, "memory_store", rec(agent="codex", title="From the address client"))
    assert not err and out["stored"]["agent"] == "codex"
    status, closed = call(base, "/api/session_close", {"namespace": NS, "summary": "Did things.", "agent": "codex", "session": "s1"}, TOKEN)
    assert status == 200 and app.store.get(closed["stored"])["agent"] == "claude"
    rid = out["stored"]["id"]
    err, _ = tool(base, "memory_corroborate", {"id": rid, "quote": "seen again", "agent": "codex"}, TOKEN)
    assert not err and app.store.evidence(rid)[0]["agent"] == "claude"
    assert srv.Auth(OPTIONS["clients"]).identify(f"Bearer {TOKEN}", "10.0.0.9") == ("claude", {"read", "write"}, True)
    assert srv.Auth(OPTIONS["clients"]).identify(None, "127.0.0.1") == ("desktop", {"read", "write", "delete", "export"}, False)


# ------------------------------------------------------------------ 2. session summaries


def test_a_session_summary_is_a_candidate_kept_out_of_bootstrap_search_and_answers(memory, store):
    closed = memory.session_close(NS, "Prod-5 carries the HA VM; decided to keep it there.", "claude-code", "s1", "", "desk", True)
    summary = store.get(closed["stored"])
    assert summary["status"] == "candidate" and closed["status"] == "candidate"
    boot = memory.bootstrap(NS)
    assert summary["id"] not in [i["id"] for i in boot["items"]] and boot["candidates_pending"] == 1
    assert memory.search(NS, "Prod-5 HA VM")["hits"] == []
    assert store.sources_for(NS, "which host carries the HA VM") == [], "a standing answer is not written from an unreviewed summary"
    assert [h["id"] for h in memory.search(NS, "Prod-5 HA VM", include_candidates=True)["hits"]] == [summary["id"]]


# ------------------------------------------------------------------ 3. bootstrap budget


def test_bootstrap_never_admits_an_item_that_does_not_fit_the_first_included(memory, store):
    # Constraints come before open work in bootstrap order, so the oversized constraint is the first item.
    huge = store.store(rec(title="T" * 3000, content="An oversized constraint."), "desk")
    store.store(rec(type="next-step", title="Small next step", content="Fits easily."), "desk")
    boot = memory.bootstrap(NS)
    assert huge["id"] not in [i["id"] for i in boot["items"]]
    assert [i["title"] for i in boot["items"]] == ["Small next step"]
    assert boot["left_out"] == 1 and boot["truncated"] is True
    assert len(json.dumps(boot, ensure_ascii=False).encode("utf-8")) <= 2000 and boot["approx_tokens"] <= 500


def test_the_bootstrap_budget_covers_the_whole_serialized_response(memory, store):
    for i in range(40):
        store.store(rec(title=f"Constraint {i} about the kiosk " + "é" * 30, content="The office kiosk " * 20), "desk")
    boot = memory.bootstrap(NS)
    size = len(json.dumps(boot, ensure_ascii=False).encode("utf-8"))
    assert size <= 2000, size
    assert boot["approx_tokens"] * 4 >= size and boot["approx_tokens"] <= 500
    assert boot["left_out"] == 12 - len(boot["items"]) and boot["left_out"] > 0
    assert len(json.dumps(boot, ensure_ascii=False)) <= size, "bytes bound the characters a client reads"


# ------------------------------------------------------------------ 4. screen every field


@pytest.mark.parametrize(
    "field,value",
    [
        ("topics", ["ok", SECRET]),
        ("project_scopes", [SECRET]),
        ("repository", SECRET),
        ("source_session", SECRET),
        ("agent", SECRET),
        ("commit", SECRET),
        ("sensitivity", SECRET),
        ("anchors", [f"host:{SECRET}"]),
        ("title", f"a title {SECRET}"),
        ("content", "do-not-store this"),
    ],
)
def test_every_persisted_field_of_a_store_is_screened(memory, store, field, value):
    with pytest.raises(mcp.ToolError, match=f"in {field}"):
        memory.store_record(rec(**{field: value}), "desk")
    assert count(store) == 0, "a refused store writes nothing"
    audit = store.db.execute("select detail from audit where action='rejected'").fetchall()
    assert [a[0].split(":")[0] for a in audit] == [field] and SECRET not in audit[0][0]


def test_update_corroborate_and_jobs_are_screened_too(memory, store):
    r = store.store(rec(), "desk")
    with pytest.raises(mcp.ToolError, match="in topics"):
        memory.update(r["id"], {"topics": [SECRET]}, "desk", 1)
    assert store.get(r["id"])["revision"] == 1 and store.get(r["id"])["topics"] == []
    with pytest.raises(mcp.ToolError, match="in source_session"):
        memory.corroborate(r["id"], "seen", "corroborates", "desk", "codex", SECRET)
    assert store.evidence(r["id"]) == []
    with pytest.raises(Refused):
        store.enqueue("extract", NS, {"notes": SECRET})
    assert store.queue_depth()["queued"] == 0
    with pytest.raises(mcp.ToolError, match="in session"):
        memory.session_close(NS, "fine", "claude-code", SECRET, "", "desk", True)
    assert count(store) == 1


@pytest.mark.parametrize(
    "anchor",
    ["url:https://user:pw@host.lan/x", "url:https://tok@github.com/o/r", "url:https://host.lan/x?access_token=abc", "url:https://host.lan/cb#id_token=abc", "url:http://[::1/x"],
)
def test_a_url_anchor_carrying_a_credential_is_refused(store, anchor):
    with pytest.raises(Refused):
        store.store(rec(anchors=[anchor]), "desk")
    assert count(store) == 0
    assert store.db.execute("select detail from audit where action='rejected'").fetchone()[0].startswith("anchors: url_")


def test_a_plain_url_anchor_is_kept(store):
    r = store.store(rec(anchors=["url:https://host.lan/docs?page=2#setup"]), "desk")
    assert r["anchors"] == ["url:https://host.lan/docs?page=2#setup"]


def test_notes_with_the_do_not_store_marker_are_refused_not_redacted(tmp_path):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    closed = app.memory.session_close(NS, "Summary is fine.", "claude-code", "s1", "keep this\ndo-not-store: the rest\npassword=hunter2hunter2", "desk", True)
    assert closed["enrichment"] == "refused" and closed["notes_refused"] == "do_not_store_marker" and closed["enrichment_job"] is None
    assert app.store.queue_depth()["queued"] == 0 and app.store.get(closed["stored"])["content"] == "Summary is fine."
    redacted = app.memory.session_close(NS, "Second.", "claude-code", "s2", "decided X; password=hunter2hunter2", "desk", True)
    payload = app.store.next_job()["payload"]
    assert redacted["enrichment"] == "queued" and "hunter2" not in payload["notes"] and "[redacted:assignment]" in payload["notes"]


# ------------------------------------------------------------------ 5. revisions


def test_every_mutation_advances_the_revision(store):
    r = store.store(rec(anchors=["entity:sensor.a"]), "desk")
    assert r["revision"] == 1
    r = store.update(r["id"], {"content": "Edited."}, "desk", 1)
    assert r["revision"] == 2
    before = r["updated_at"]
    r = store.update(r["id"], {"anchors": ["entity:sensor.b"]}, "desk", 2)
    assert r["revision"] == 3 and r["anchors"] == ["entity:sensor.b"] and r["updated_at"] >= before
    assert store.corroborate(r["id"], "seen", "corroborates", "desk")["revision"] == 4
    assert store.update(r["id"], {"status": "candidate"}, "desk", 4)["revision"] == 5
    store.update(r["id"], {"status": "active"}, "desk", 5)
    store.store(rec(title="Replacement", supersedes=r["id"]), "desk")
    assert store.get(r["id"])["revision"] == 7 and store.get(r["id"])["status"] == "superseded"
    other = store.store(rec(title="To forget"), "desk")
    store.forget(other["id"], "desk")
    assert store.get(other["id"], include_deleted=True)["revision"] == 2


def test_a_stale_revision_changes_nothing_and_returns_the_current_record(store):
    r = store.store(rec(anchors=["entity:sensor.a"]), "desk")
    first = store.update(r["id"], {"content": "First writer."}, "desk", 1)
    with pytest.raises(Conflict) as err:
        store.update(r["id"], {"content": "Second writer.", "anchors": ["entity:sensor.z"], "status": "candidate"}, "desk", 1)
    assert err.value.current["revision"] == 2 and err.value.current["content"] == "First writer."
    now = store.get(r["id"])
    assert (now["content"], now["anchors"], now["status"], now["revision"]) == ("First writer.", ["entity:sensor.a"], "active", first["revision"])


def test_mcp_and_rest_report_a_conflict_with_the_current_revision(http):
    base, app = http
    err, out = tool(base, "memory_store", rec())
    rid = out["stored"]["id"]
    err, ok = tool(base, "memory_update", {"id": rid, "revision": 1, "changes": {"content": "One."}})
    assert not err and ok["updated"]["revision"] == 2
    err, clash = tool(base, "memory_update", {"id": rid, "revision": 1, "changes": {"content": "Two."}})
    assert err and clash["revision"] == 2 and clash["current"]["content"] == "One."
    status, body = call(base, "/api/update", {"id": rid, "revision": 1, "changes": {"content": "Three."}})
    assert status == 409 and body["revision"] == 2
    err, missing = tool(base, "memory_update", {"id": rid, "changes": {"content": "No revision."}})
    assert err and "revision is required" in missing["error"]
    err, unknown = tool(base, "memory_update", {"id": rid, "revision": 2, "changes": {"statuss": "active"}})
    assert err and "cannot change statuss" in unknown["error"]
    assert app.store.get(rid)["content"] == "One."
    for bad in ("two", True, 2.5, "2.0"):
        err, refused = tool(base, "memory_update", {"id": rid, "revision": bad, "changes": {"content": "Bad revision."}})
        assert err and "integer revision" in refused["error"], bad
    # A client holding the tool list from before revisions sends the number as a string.
    err, ok = tool(base, "memory_update", {"id": rid, "revision": "2", "changes": {"content": "From a stale schema."}})
    assert not err and ok["updated"]["revision"] == 3
    err, clash = tool(base, "memory_update", {"id": rid, "revision": "2", "changes": {"content": "Stale again."}})
    assert err and clash["revision"] == 3


def test_a_database_from_before_revisions_opens_at_revision_one(tmp_path):
    path = str(tmp_path / "old.sqlite")
    old = Store(path)
    rid = old.store(rec(anchors=["entity:sensor.a"]), "desk")["id"]
    old.db.executescript("alter table memories drop column revision; alter table anchors drop column unavailable_since; drop table receipts;")
    old.db.close()
    reopened = Store(path)
    assert reopened.get(rid)["revision"] == 1
    assert reopened.update(rid, {"content": "Edited after the upgrade."}, "desk", 1)["revision"] == 2
    assert reopened.receipt("nothing-here") is None and reopened.integrity() == "ok"


# ------------------------------------------------------------------ 6. supersede rules


def test_supersede_needs_an_active_target_in_the_same_namespace(store):
    active = store.store(rec(title="Active target"), "desk")
    elsewhere = store.store(rec(namespace=OTHER, title="Other namespace"), "desk")
    candidate = store.store_candidate(rec(title="Candidate target"), "enrichment")
    gone = store.store(rec(title="Forgotten target"), "desk")
    store.forget(gone["id"], "desk")
    before = count(store)
    for target, why in ((elsewhere["id"], "another namespace"), (candidate["id"], "candidate"), (gone["id"], "no live record"), ("no-such-id", "no live record")):
        with pytest.raises(Rejected, match=why):
            store.store(rec(title="Replacement", supersedes=target), "desk")
        assert count(store) == before, "a refused supersession stores nothing"
    with pytest.raises(Rejected, match="only an active record"):
        store.store(rec(title="Unreviewed replacement", status="candidate", supersedes=active["id"]), "desk")
    assert store.get(active["id"])["status"] == "active" and store.get(elsewhere["id"])["status"] == "active"
    replacement = store.store(rec(title="Replacement", supersedes=active["id"]), "desk")
    assert store.get(active["id"])["status"] == "superseded" and replacement["supersedes"] == active["id"]
    with pytest.raises(Rejected, match="superseded record"):
        store.store(rec(title="Late replacement", supersedes=active["id"]), "desk")


def test_supersede_refuses_itself_and_a_cycle(store):
    a = store.store(rec(title="A"), "desk")
    b = store.store(rec(title="B"), "desk")
    # Ids are the server's, so neither can be reached through a create; a database written before
    # these rules can still hold the dangling link that would close a loop.
    store.db.execute("update memories set supersedes=? where id=?", (b["id"], a["id"]))
    with store.transaction():
        with pytest.raises(Rejected, match="itself"):
            store._supersede(a["id"], a["id"], NS, "active", now_iso())
        with pytest.raises(Rejected, match="cycle"):
            store._supersede(b["id"], a["id"], NS, "active", now_iso())
    assert store.get(a["id"])["status"] == "active" and store.get(b["id"])["status"] == "active"


def test_a_supersede_through_mcp_is_refused_with_a_reason(memory, store):
    other = store.store(rec(namespace=OTHER), "desk")
    with pytest.raises(mcp.ToolError, match="another namespace"):
        memory.store_record(rec(supersedes=other["id"]), "desk")


# ------------------------------------------------------------------ 7. anchor states


def test_unchecked_unavailable_and_missing_are_tracked_separately(memory, store):
    r = store.store(rec(anchors=["entity:sensor.a"]), "desk")
    state = lambda: store.anchors("entity")[0]  # noqa: E731
    assert (state()["state"], state()["unavailable_since"]) == ("unchecked", None)
    memory.entity_ids = None
    assert memory.anchor_sweep("sweep")["unavailable"] == 1
    assert state()["state"] == "unchecked" and state()["unavailable_since"], "could not look is not a check"
    memory.entity_ids = lambda: {"sensor.a"}
    memory.anchor_sweep("sweep")
    assert (state()["state"], state()["unavailable_since"]) == ("ok", None)
    memory.entity_ids = lambda: set()
    memory.anchor_sweep("sweep")
    assert state()["state"] == "missing" and store.get(r["id"])["refutation_count"] == 1

    def down() -> set[str]:
        raise OSError("down")

    memory.entity_ids = down
    memory.anchor_sweep("sweep")
    assert state()["state"] == "missing" and state()["unavailable_since"], "an outage keeps the missing flag"
    assert store.get(r["id"])["refutation_count"] == 1, "an outage files nothing"
    out = mcp.call_tool(memory, "memory_anchor_report", {"id": r["id"], "anchor": "entity:sensor.a", "state": "unavailable"}, "desk", "")
    assert out["filed"] == "" and state()["state"] == "missing"


def test_a_record_with_a_missing_anchor_is_flagged_in_results(memory, store):
    r = store.store(rec(title="Kiosk entity", content="The kiosk reports through sensor.kiosk.", anchors=["entity:sensor.kiosk", "path:D:/x"]), "desk")
    store.report_anchor(r["id"], "entity:sensor.kiosk", "missing", "sweep")
    hit = memory.search(NS, "kiosk")["hits"][0]
    assert hit["missing_anchors"] == ["entity:sensor.kiosk"] and "anchor missing: entity:sensor.kiosk" in hit["why"]
    got = memory.get([r["id"]])["records"][0]
    assert got["missing_anchors"] == ["entity:sensor.kiosk"]
    assert {a["anchor"]: a["state"] for a in got["anchor_states"]} == {"entity:sensor.kiosk": "missing", "path:D:/x": "unchecked"}
    assert memory.bootstrap(NS)["items"][0]["missing_anchors"] == ["entity:sensor.kiosk"]
    assert memory.project_context(NS, "kiosk")["records"][0]["missing_anchors"] == ["entity:sensor.kiosk"]
    fine = store.store(rec(title="Unanchored"), "desk")
    assert "missing_anchors" not in memory.get([fine["id"]])["records"][0]


# ------------------------------------------------------------------ 8. idempotent session close


def test_a_replayed_close_returns_the_first_result_and_acts_once(tmp_path):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    args = (NS, "Built the spool.", "claude-code", "s1", "decided to spool closes only", "desk", True, "op-0123456789")
    first = app.memory.session_close(*args)
    again = app.memory.session_close(*args)
    assert again == {**first, "replayed": True}
    assert count(app.store, "type='session-summary'") == 1 and app.store.queue_depth()["queued"] == 1
    with pytest.raises(mcp.ToolError, match="different close") as err:
        app.memory.session_close(NS, "Another summary.", "claude-code", "s1", "", "desk", True, "op-0123456789")
    assert err.value.status == 409
    with pytest.raises(mcp.ToolError, match="op_id"):
        app.memory.session_close(NS, "x", "claude-code", "s1", "", "desk", True, "bad id!")
    app.store.db.close()
    restarted = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))
    assert restarted.memory.session_close(*args)["stored"] == first["stored"], "a receipt survives a restart"


def test_a_lost_response_is_answered_by_the_replay_over_http(http):
    base, app = http
    body = {"namespace": NS, "summary": "Closed while the reply was lost.", "agent": "claude-code", "session": "s9", "notes": "n", "enrich": True, "op_id": "op-lost-000001"}
    status, first = call(base, "/api/session_close", body)
    assert status == 200 and "replayed" not in first
    status, replay = call(base, "/api/session_close", body)  # the client never saw the first reply
    assert status == 200 and replay["replayed"] is True and replay["stored"] == first["stored"]
    assert count(app.store, "type='session-summary'") == 1


def test_the_receipt_commits_with_the_close_or_not_at_all(tmp_path, monkeypatch):
    app = srv.App({**OPTIONS, "ollama_url": "http://127.0.0.1:9"}, str(tmp_path))

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(app.store, "enqueue", broken)
    args = (NS, "Half done.", "claude-code", "s1", "notes", "desk", True, "op-halfway-01")
    with pytest.raises(sqlite3.OperationalError):
        app.memory.session_close(*args)
    assert count(app.store) == 0 and app.store.receipt("op-halfway-01") is None
    monkeypatch.undo()
    done = app.memory.session_close(*args)
    assert "replayed" not in done and count(app.store) == 1


def test_receipts_age_out_with_the_retention(store):
    store.put_receipt("op-old-0000001", "desk", "session_close", "d", {"stored": "x"})
    store.db.execute("update receipts set at='2000-01-01T00:00:00+00:00'")
    store.put_receipt("op-new-0000001", "desk", "session_close", "d", {"stored": "y"})
    store.purge(30)
    assert store.receipt("op-old-0000001") is None and store.receipt("op-new-0000001")["result"] == {"stored": "y"}
