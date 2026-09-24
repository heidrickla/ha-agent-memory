"""Storage and FTS, namespaces, the secret filter, budgets, MCP over HTTP, auth and isolation."""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import filters
import mcp
import namespaces as ns
import server as srv
from store import Store

OPTIONS = {
    "clients": [
        {"name": "desk", "ip": "127.0.0.1", "scopes": ["read", "write", "delete", "export"]},
        {"name": "codex", "token_sha256": hashlib.sha256(b"t0ken-for-tests-0123456789").hexdigest(), "scopes": ["read"]},
    ],
    "github_owner": "heidrickla",
    "forge_owner": "example",
    "forge_hosts": ["forge.example", "192.0.2.20"],
    "project_links": [{"project": "homelab", "namespaces": ["gitea/example/ha-management", "gitea/example/ha-loops"]}],
    "context_budget_tokens": 4000,
    "hard_context_limit_tokens": 8000,
    "retention_deleted_days": 30,
}
NS = "gitea/example/ha-management"


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "m.sqlite"))


@pytest.fixture
def memory(store):
    return mcp.Memory(store, OPTIONS)


def rec(**kw):
    base = {"namespace": NS, "type": "constraint", "title": "Restarts are pre-authorized", "content": "Home Assistant restarts never need permission; they are part of installing anything."}
    base.update(kw)
    return base


def test_namespace_normalisation_accepts_ours_and_refuses_others():
    args = ("heidrickla", "example", ("forge.example", "192.0.2.20"))
    assert ns.normalize_remote("git@github.com:heidrickla/homelab.git", *args) == "github/heidrickla/homelab"
    assert ns.normalize_remote("https://user:pw@github.com/heidrickla/homelab.git?x=1#f", *args) == "github/heidrickla/homelab"
    assert ns.normalize_remote("ssh://forge.example/example/ha-loops.git", *args) == "gitea/example/ha-loops"
    assert ns.normalize_remote("ssh://git@192.0.2.20:2222/example/ai-research.git", *args) == "gitea/example/ai-research"
    assert ns.normalize_remote("git@github.com:someoneelse/homelab.git", *args) is None
    assert ns.normalize_remote("https://github.com/heidrickla/a/b.git", *args) is None
    assert ns.normalize_remote("not a remote", *args) is None
    assert ns.scopes_for(NS, {"homelab": [NS]}, ["networking"]) == [NS, "projects/homelab", "topics/networking", "global"]


def test_secret_filter_refuses_credentials_and_marker():
    assert filters.screen("-----BEGIN RSA PRIVATE KEY-----\nabc") == "private_key"
    assert filters.screen("token: uk1_QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ") in ("kuma_key", "assignment")
    assert filters.screen("password=hunter2hunter2") == "assignment"
    assert filters.screen("https://user:secretpw@host/x") == "url_credentials"
    assert filters.screen("this line is do-not-store") == "do_not_store_marker"
    assert filters.screen("x" * 30000) == "too_large"
    assert filters.screen("The forge admin credential lives in OpenBao project rack as FORGE_ADMIN_PASSWORD.") is None
    assert "[redacted:openai_key]" in filters.redact("key sk-abcdefghijklmnopqrstuvwxyz0123")


def test_store_search_supersede_forget_and_purge(store):
    a = store.store(rec(), "desk")
    b = store.store(rec(type="failure-and-fix", title="Heredocs are blocked", content="Bash heredocs fail on this estate; write files with Write or Edit."), "desk")
    assert store.fts is True
    hits = store.search("heredocs blocked", [NS, "global"])
    assert hits and hits[0]["id"] == b["id"] and "why" in hits[0]
    assert store.search("zebra quantum", [NS]) == []
    c = store.store(rec(title="Restarts are pre-authorized, batched", content="Restart once for several pending changes.", supersedes=a["id"]), "desk")
    assert store.get(a["id"])["status"] == "superseded" and store.get(c["id"])["status"] == "active"
    assert [h["id"] for h in store.search("restarts", [NS])] == [c["id"]]
    assert store.forget(b["id"], "desk") and store.get(b["id"]) is None and store.get(b["id"], include_deleted=True)["status"] == "deleted"
    assert store.purge(30) == 0  # too recent to purge
    assert store.integrity() == "ok"
    stats = store.stats()
    assert stats["memories"] == 2 and stats["namespaces"] == 1


def test_search_matches_other_forms_of_a_word(store):
    """Stemmed: a query in one form of a word finds a record written in another."""
    a = store.store(rec(title="Reboot the gateway from the router page", content="A reboot takes two minutes."), "desk")
    b = store.store(rec(title="Nightly check of backup age", content="The loops add-on looks at the newest backup."), "desk")
    assert [h["id"] for h in store.search("rebooting", [NS])] == [a["id"]]
    assert [h["id"] for h in store.search("checks", [NS])] == [b["id"]]


def test_an_index_made_before_stemming_is_rebuilt_on_open(tmp_path):
    """A database indexed without stemming is rebuilt once when opened: every record stays findable
    and other word forms find it afterwards."""
    path = str(tmp_path / "old.sqlite")
    old = Store(path)
    r = old.store(rec(title="Reboot the gateway from the router page", content="A reboot takes two minutes."), "desk")
    old.db.executescript("""
        drop table memories_fts;
        create virtual table memories_fts using fts5(title, content, content='memories', content_rowid='rowid');
        insert into memories_fts(memories_fts) values ('rebuild');
    """)
    assert old.search("rebooting", [NS]) == [], "the plain index should not see other word forms"
    old.db.close()
    reopened = Store(path)
    assert "porter" in reopened.db.execute("select sql from sqlite_master where name='memories_fts'").fetchone()[0]
    assert [h["id"] for h in reopened.search("reboot", [NS])] == [r["id"]]
    assert [h["id"] for h in reopened.search("rebooting", [NS])] == [r["id"]]
    assert reopened.integrity() == "ok"


def test_scope_order_and_isolation(memory, store):
    store.store(rec(namespace="gitea/example/ha-loops", title="Loops runs at 03:30", content="The nightly run is at 03:30 local."), "desk")
    store.store(rec(namespace="projects/homelab", title="Shared: forge is 192.0.2.20", content="The personal forge answers on 192.0.2.20:3000."), "desk")
    store.store(rec(namespace="global", title="Dark mode by default", content="Every UI starts dark."), "desk")
    store.store(rec(namespace="gitea/example/ai-research", title="Codex is the proposer", content="Codex CLI proposes primitives."), "desk")
    out = memory.search(NS, "forge dark loops codex")
    found = {h["namespace"] for h in out["hits"]}
    assert "projects/homelab" in found and "global" in found
    assert "gitea/example/ai-research" not in found  # another repository, not linked
    assert "gitea/example/ha-loops" not in found  # a sibling repository is not in scope by default
    assert out["scopes"] == [NS, "projects/homelab", "global"]
    with pytest.raises(mcp.ToolError):
        memory.bootstrap("not/a/namespace")


def test_bootstrap_counts_candidates_instead_of_showing_them(memory, store):
    """An unreviewed candidate never appears at the start of a session as if settled; it is counted
    so it gets reviewed. An active record of the same kind still appears."""
    settled = store.store(rec(type="session-summary", title="Deployed the add-on", content="Version 0.4.2 is running."), "desk")
    pending = store.store(rec(type="session-summary", title="Fix F1 is required before proceeding", content="Stale note.", status="candidate"), "desk")
    boot = memory.bootstrap(NS)
    shown = [i["id"] for i in boot["items"]]
    assert settled["id"] in shown
    assert pending["id"] not in shown
    assert boot["candidates_pending"] == 1
    assert {r["id"] for r in store.timeline(NS)} == {pending["id"], settled["id"]}, "the review list keeps candidates"


def test_anchors_are_stored_validated_and_replaced(store):
    r = store.store(rec(anchors=["entity:sensor.b", "path:D:/x/y.txt", "entity:sensor.a"]), "desk")
    assert store.get(r["id"])["anchors"] == ["entity:sensor.a", "entity:sensor.b", "path:D:/x/y.txt"]
    with pytest.raises(ValueError):
        store.store(rec(anchors=["sensor.a"]), "desk")
    store.report_anchor(r["id"], "entity:sensor.a", "missing", "sweep")
    store.update(r["id"], {"anchors": ["entity:sensor.a", "host:10.0.0.1"]}, "desk")
    assert store.get(r["id"])["anchors"] == ["entity:sensor.a", "host:10.0.0.1"]
    kept = [a for a in store.anchors("entity") if a["record_id"] == r["id"]]
    assert kept == [dict(kept[0], anchor="entity:sensor.a", last_state="missing")], "an anchor that stays keeps its state"


def test_anchor_reports_file_evidence_only_on_transitions(store):
    r = store.store(rec(anchors=["entity:sensor.gone"]), "desk")
    assert store.report_anchor(r["id"], "entity:sensor.gone", "missing", "sweep")["filed"] == "contradicts"
    assert store.get(r["id"])["refutation_count"] == 1
    assert store.report_anchor(r["id"], "entity:sensor.gone", "missing", "sweep")["filed"] == ""
    assert store.get(r["id"])["refutation_count"] == 1, "a repeated sweep does not pile contradictions"
    assert store.report_anchor(r["id"], "entity:sensor.gone", "ok", "sweep")["filed"] == "corroborates"
    assert store.report_anchor(r["id"], "entity:sensor.gone", "ok", "sweep")["filed"] == ""
    assert store.get(r["id"])["proof_count"] == 1
    fresh = store.store(rec(anchors=["entity:sensor.new"]), "desk")
    assert store.report_anchor(fresh["id"], "entity:sensor.new", "ok", "sweep")["filed"] == "", "a first ok files nothing"


def test_anchor_sweep_checks_entities_and_trusts_no_failed_read(memory, store):
    here = store.store(rec(title="Here", anchors=["entity:sensor.here"]), "desk")
    gone = store.store(rec(title="Gone", anchors=["entity:sensor.gone"]), "desk")
    assert memory.anchor_sweep("sweep")["could_not_check"] == "no Home Assistant connection"
    memory.entity_ids = lambda: {"sensor.here", "light.x"}
    first = memory.anchor_sweep("sweep")
    assert (first["checked"], first["missing"], first["filed"]) == (2, 1, 1)
    assert store.get(gone["id"])["refutation_count"] == 1 and store.get(here["id"])["refutation_count"] == 0
    assert memory.anchor_sweep("sweep")["filed"] == 0

    def broken() -> set[str]:
        raise OSError("down")

    memory.entity_ids = broken
    assert memory.anchor_sweep("sweep") == {"checked": 0, "missing": 0, "filed": 0, "could_not_check": "OSError"}
    assert store.get(here["id"])["refutation_count"] == 0, "an unreadable source is not evidence"


def test_anchor_tools_and_forgotten_records(memory, store):
    r = store.store(rec(anchors=["path:D:/a.txt"]), "desk")
    listed = mcp.call_tool(memory, "memory_anchors", {"kind": "path"}, "desk", "")
    assert [a["anchor"] for a in listed["anchors"]] == ["path:D:/a.txt"]
    out = mcp.call_tool(memory, "memory_anchor_report", {"id": r["id"], "anchor": "path:D:/a.txt", "state": "missing"}, "desk", "")
    assert out["filed"] == "contradicts"
    got = memory.get([r["id"]])["records"][0]
    assert got["anchors"] == ["path:D:/a.txt"] and got["refutation_count"] == 1, "memory_get shows anchors and evidence"
    assert "anchors" in dict(mcp.tool_list()[[t["name"] for t in mcp.tool_list()].index("memory_store")])["inputSchema"]["properties"]
    assert mcp.TOOL_SCOPE["memory_anchor_report"] == "write" and mcp.TOOL_SCOPE["memory_anchors"] == "read"
    store.forget(r["id"], "desk")
    assert mcp.call_tool(memory, "memory_anchors", {"kind": "path"}, "desk", "")["anchors"] == []


def test_a_hub_reaches_other_repositories_below_its_own_scopes(store):
    """A hub's sessions work on other repositories, whose records are filed there. Search and the
    task match reach them below every scope of the hub; bootstrap and a plain namespace do not."""
    hub, plain = mcp.Memory(store, {**OPTIONS, "hubs": [NS]}), mcp.Memory(store, OPTIONS)
    same = {"title": "Rebuild the installer after a change", "content": "The installer is rebuilt after every change; an earlier copy is out of date."}
    own = store.store(rec(**same), "desk")
    shared = store.store(rec(namespace="global", **same), "desk")
    sibling = store.store(rec(namespace="gitea/example/web-access", **same), "desk")
    store.store(rec(namespace="topics/windows", **same), "desk")
    found = hub.search(NS, "installer rebuilt")
    assert [h["id"] for h in found["hits"]] == [own["id"], shared["id"], sibling["id"]]
    assert found["reach"] == ["gitea/example/web-access"], "only repository namespaces are reached"
    assert found["hits"][2]["why"].startswith("reach gitea/example/web-access")
    assert [h["id"] for h in plain.search(NS, "installer rebuilt")["hits"]] == [own["id"], shared["id"]]
    assert sibling["id"] in [r["id"] for r in hub.project_context(NS, "installer rebuilt")["records"]]
    assert sibling["id"] not in [r["id"] for r in plain.project_context(NS, "installer rebuilt")["records"]]
    assert sibling["id"] not in [i["id"] for i in hub.bootstrap(NS)["items"]]
    assert [h["id"] for h in hub.search("gitea/example/web-access", "installer rebuilt")["hits"]] == [sibling["id"], shared["id"]], "a namespace not in hubs keeps its scopes"


def test_budgets_are_enforced_and_reported(memory, store):
    for i in range(60):
        store.store(rec(title=f"Fact {i} about the kiosk", content="The office kiosk " * 40 + f"number {i}."), "desk")
    boot = memory.bootstrap(NS)
    assert boot["approx_tokens"] <= 500 and boot["truncated"] is True
    ctx = memory.project_context(NS, "kiosk", budget=1500)
    assert ctx["approx_tokens"] <= 1500 and ctx["truncated"] is True and ctx["records"]
    assert all("why" in r for r in ctx["records"])
    big = memory.project_context(NS, "kiosk", budget=99999)
    assert big["budget"] == 8000  # the hard limit wins
    got = memory.get([r["id"] for r in ctx["records"]][:3])
    assert len(got["records"]) == 3 and got["missing"] == []


@pytest.fixture
def http(tmp_path):
    app = srv.App(OPTIONS, str(tmp_path))
    server = srv.serve(app, 0, host="127.0.0.1")
    yield f"http://127.0.0.1:{server.server_address[1]}", app
    server.shutdown()


def call(base: str, path: str, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"), headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, _decode(err.read())


def _decode(raw: bytes):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw.decode("utf-8", "replace")


def rpc(base, method, params=None, mid=1, headers=None):
    return call(base, "/mcp", {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, headers)


def test_mcp_over_http_end_to_end(http):
    base, app = http
    s, init = rpc(base, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert s == 200 and init["result"]["serverInfo"]["name"] == "agent-memory"
    s, _ = call(base, "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert s == 202
    s, tools = rpc(base, "tools/list")
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {
        "memory_bootstrap",
        "memory_search",
        "memory_get",
        "memory_store",
        "memory_project_context",
        "memory_session_close",
        "memory_forget",
        "memory_export",
        "memory_namespace",
        "memory_timeline",
        "memory_update",
    } <= names
    s, stored = rpc(base, "tools/call", {"name": "memory_store", "arguments": rec()})
    doc = json.loads(stored["result"]["content"][0]["text"])
    assert stored["result"]["isError"] is False and doc["stored"]["type"] == "constraint"
    s, found = rpc(base, "tools/call", {"name": "memory_search", "arguments": {"namespace": NS, "query": "restarts permission"}})
    hits = json.loads(found["result"]["content"][0]["text"])["hits"]
    assert hits and hits[0]["id"] == doc["stored"]["id"]
    s, refused = rpc(base, "tools/call", {"name": "memory_store", "arguments": rec(content="the token is uk1_QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ")})
    assert refused["result"]["isError"] is True and "not stored" in refused["result"]["content"][0]["text"]
    assert any(a["action"] == "rejected" for a in app.audit_tail())
    s, closed = rpc(
        base,
        "tools/call",
        {"name": "memory_session_close", "arguments": {"namespace": NS, "summary": "Built the loops add-on.", "agent": "claude-code", "session": "s1", "notes": "details", "enrich": True}},
    )
    assert json.loads(closed["result"]["content"][0]["text"])["enrichment"] == "off"  # no ollama_url configured
    s, unknown = rpc(base, "nothing/here")
    assert unknown["error"]["code"] == -32601
    s, health = call(base, "/health")
    assert s == 200 and health["records"] == 2 and health["fts5"] is True
    s, exported = rpc(base, "tools/call", {"name": "memory_export", "arguments": {}})
    out = json.loads(exported["result"]["content"][0]["text"])
    assert out["records"] == 2 and (Path(out["dir"]) / "memories.jsonl").exists()


def test_auth_by_token_hash_and_scopes(http):
    base, app = http
    bad = {"Authorization": "Bearer wrong-token-value-0000000000"}
    s, _ = rpc(base, "tools/list", headers=bad)
    assert s == 401 and app.denied == 1
    good = {"Authorization": "Bearer t0ken-for-tests-0123456789"}
    s, _ = rpc(base, "tools/list", headers=good)
    assert s == 200
    s, denied = rpc(base, "tools/call", {"name": "memory_store", "arguments": rec()}, headers=good)
    assert denied["result"]["isError"] is True and "lacks the 'write' scope" in denied["result"]["content"][0]["text"]
    s, ok = rpc(base, "tools/call", {"name": "memory_bootstrap", "arguments": {"namespace": NS}}, headers=good)
    assert ok["result"]["isError"] is False
    s, rest = call(base, f"/api/bootstrap?namespace={NS}", headers=good)
    assert s == 200 and rest["namespace"] == NS
    s, _ = call(base, "/api/store", rec(), headers=good)
    assert s == 403
    s, _ = call(base, "/")
    assert s == 404  # the page is for ingress only


def test_restart_preserves_writes(tmp_path):
    app = srv.App(OPTIONS, str(tmp_path))
    app.memory.store_record(rec(), "desk")
    del app
    again = srv.App(OPTIONS, str(tmp_path))
    assert again.store.stats()["memories"] == 1 and again.memory.search(NS, "restarts")["hits"]
