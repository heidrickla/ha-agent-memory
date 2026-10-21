"""The ingress page: the data it counts and the cards it draws."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import page
from test_memory import NS, call, http, rec  # noqa: F401  (http is a fixture)

NOW = dt.datetime(2026, 10, 2, 23, 30, tzinfo=dt.UTC)

HEALTH = {
    "status": "ok",
    "uptime_s": 66105,
    "records": 142,
    "candidates": 59,
    "namespaces": 14,
    "fts5": True,
    "db_bytes": 1196032,
    "jobs": {"done": 69, "failed": 2},
    "enrichment": {
        "configured": True,
        "api": "openai",
        "model": "bonsai2-memory",
        "ollama": "available",
        "servers": [
            {"url": "http://local-voice-bridge:8789/v1", "tier": 0, "live": True, "in_flight": 0},
            {"url": "http://192.0.2.2:8080/v1", "tier": 1, "live": True, "in_flight": 1},
            {"url": "http://192.0.2.20:8080/v1", "tier": 1, "live": False, "in_flight": 0},
        ],
        "prompt": "extract-v1",
        "queued": 0,
        "oldest": None,
        "processed": 1,
        "failed": 0,
    },
    "embeddings": {"encoder": "available", "model": "qwen3-emb-0.6b", "snapshot": "97b0c614be4d", "records": 140, "current": 140, "refused": 0, "error": None},
    "anchor_sweep": {"checked": 3, "missing": 0, "filed": 0, "entities": 4101, "at": "2026-10-02T23:24:48"},
    "candidate_review": {
        "at": "2026-10-02T05:55:11+00:00",
        "version": "review-v1",
        "reviewed": 4,
        "expired": 2,
        "forgotten": {"duplicate": 1, "session-state": 0},
        "kept": 3,
        "failed": 0,
        "servers": {"http://local-voice-bridge:8789/v1": 4},
        "error": None,
        "seconds": 9.9,
        "review_at": "03:00",
    },
    "requests": 1730,
    "denied": 0,
    "clients": ["desktop", "claude-code", "codex"],
}

COUNTS = {
    "namespaces": {NS: {"status": {"active": 20, "candidate": 9}, "last": "2026-10-02T22:30:00+00:00"}, "global": {"status": {"active": 3}, "last": "2026-09-30T10:00:00+00:00"}},
    "types": {"constraint": {"active": 12, "candidate": 2}, "session-summary": {"candidate": 7}, "architectural-decision": {"active": 11}},
    "hours": {"2026-10-02T22": 40, "2026-10-02T23": 12},
    "actions": {"search": 30, "bootstrap": 10, "store": 8, "get": 4, "session_close": 3, "update": 2, "forget": 1, "export": 1},
    "clients": {"claude-code": 48, "codex": 11},
    "searches": {"total": 30, "hits": 24},
}

AUDIT = [{"seq": 2, "at": "2026-10-02T23:20:00+00:00", "client": "claude-code", "action": "search", "namespace": NS, "record_id": "", "detail": "<b>kiosk</b>"}]


def data(**over):
    d = {"now": NOW, "health": HEALTH, "counts": COUNTS, "audit": AUDIT}
    d.update(over)
    return d


def test_page_draws_every_card_from_live_shaped_data():
    out = page.render(data())
    for title in ("Records", "Namespaces", "Audit, last 24 h", "Models", "Maintenance"):
        assert f"<span>{title}</span>" in out
    assert "2 of 3 live" in out  # the model servers tile counts live servers
    assert "1 in flight" in out and "tier 0" in out
    assert "searches that found records" in out and "24 of 30" in out
    assert "vectors current" in out and "140 of 140" in out
    assert "forgotten, duplicate" in out and "expired" in out and "next at 03:00" in out
    assert "3 anchors checked" in out and "4101 entities" in out
    assert "architectural decision" in out  # type names read as words
    assert "59 rows" in out  # 24 h total in the audit card's corner
    assert "other <b>2</b>" in out  # actions past the top six are folded


def test_hour_columns_carry_their_count_and_the_busiest_hour():
    out = page.render(data())
    assert "busiest hour 40" in out
    assert ": 40 audit rows</title>" in out and ": 12 audit rows</title>" in out
    assert out.count("<rect") == 24 + 2  # a hover target per hour, a column per hour with calls


def test_colours_in_svg_go_through_style():
    out = page.render(data())
    assert 'fill="var(' not in out and 'stroke="var(' not in out
    assert 'style="fill:var(--s1)"' in out


def test_denied_calls_and_a_dead_encoder_are_flagged():
    h = {**HEALTH, "denied": 2, "embeddings": {**HEALTH["embeddings"], "encoder": "unavailable", "error": "connection refused"}}
    out = page.render(data(health=h))
    assert 'class="tile alert"' in out
    assert "connection refused" in out
    assert 'class="tile alert"' not in page.render(data())


def test_values_are_escaped():
    out = page.render(data(counts={**COUNTS, "namespaces": {"<script>x</script>": {"status": {"active": 1}, "last": ""}}}))
    assert "<script>x</script>" not in out and "&lt;script&gt;" in out
    assert "<b>kiosk</b>" not in out


def test_empty_store_draws_empty_states():
    empty = {"namespaces": {}, "types": {}, "hours": {}, "actions": {}, "clients": {}, "searches": {"total": 0, "hits": 0}}
    h = {**HEALTH, "enrichment": {"configured": False}, "embeddings": {"encoder": "off"}, "candidate_review": {"review_at": "03:00"}, "anchor_sweep": {"at": None}}
    out = page.render(data(counts=empty, audit=[], health=h))
    assert out.count("Nothing stored yet.") == 2
    assert "No audit rows yet." in out and "busiest hour 0" in out
    assert "Off: no model server configured" in out and "search is full-text only" in out
    assert "No run yet; next at 03:00." in out and "No sweep yet." in out
    assert ">-</text>" in out  # the search ring with nothing to count


def test_counts_split_status_and_type_and_keep_to_the_window(http):  # noqa: F811
    base, app = http
    app.memory.store_record(rec(), "desk")
    app.store.store(rec(title="Candidate fact", content="Something a session found.", status="candidate", type="repository-fact"), "desk")
    app.store.db.execute("insert into audit(at, client, action) values (?,?,?)", ("2026-01-01T00:00:00+00:00", "old", "search"))
    app.memory.search(NS, "restarts")
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    c = app.store.panel_counts(since)
    assert c["namespaces"][NS]["status"] == {"active": 1, "candidate": 1}
    assert c["types"] == {"constraint": {"active": 1}, "repository-fact": {"candidate": 1}}
    assert "old" not in c["clients"]  # older than the window
    assert c["searches"]["total"] >= 1
    s, html_page = call(base, "/", headers={"X-Ingress-Path": "/api/hassio_ingress/x"})
    assert s == 200 and "<span>Namespaces</span>" in html_page and NS in html_page


def test_the_unused_card_waits_for_its_window_then_lists_records():
    waiting = {"days": 30, "counting_since": "2026-10-01T12:00:00+00:00", "complete": False, "count": 0, "records": []}
    out = page.render(data(unused=waiting))
    assert "<span>Unused records</span>" in out and "the list starts 2026-10-31" in out
    row = {
        "id": "aaaaaaaa-0000-4000-8000-000000000001",
        "namespace": NS,
        "type": "repository-fact",
        "title": "<b>Old</b> fact",
        "created_at": "2026-08-01T00:00:00+00:00",
        "returned": 0,
        "fetched": 0,
        "last_returned_at": None,
    }
    out = page.render(data(unused={**waiting, "complete": True, "count": 3, "records": [row]}))
    assert "3 active record(s) older than 30 days" in out and "aaaaaaaa<" in out and "repository fact" in out
    assert "2 more not listed." in out and "<b>Old</b>" not in out
    none = page.render(data(unused={**waiting, "complete": True}))
    assert "Every active record older than 30 days was returned" in none
    assert "Unused records" not in page.render(data()), "no card without the data"


def test_the_panel_carries_the_unused_list(http):  # noqa: F811
    base, app = http
    assert app.panel_data()["unused"]["complete"] is False
    s, html_page = call(base, "/", headers={"X-Ingress-Path": "/api/hassio_ingress/x"})
    assert s == 200 and "<span>Unused records</span>" in html_page
