"""Evidence: a belief earned from observations rather than a number the writer asserted.

The thing being protected is that a reader can tell a 0.9 standing on four witnesses from a 0.9
somebody typed, and that a session finding a record wrong can say so without erasing the record.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
from store import Store

NS = "gitea/example/ha-management"


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "m.db"))


def make(store, **over) -> str:
    rec = {"namespace": NS, "title": "Prod-5 carries the HA VM", "content": "The HA VM lives on ESXi-Prod-5.", "type": "repository-fact"}
    rec.update(over)
    return store.store(rec, "desk")["id"]


def test_an_unwitnessed_record_keeps_the_confidence_it_was_given(store):
    """The control that matters for a live database: nothing already stored changes meaning."""
    rid = make(store, confidence=0.42)
    rec = store.get(rid)
    assert rec["proof_count"] == 0 and rec["refutation_count"] == 0
    assert rec["effective_confidence"] == pytest.approx(0.42)


def test_corroboration_raises_confidence_and_keeps_the_quote(store):
    rid = make(store, confidence=0.42)
    out = store.corroborate(rid, "esxcli said the VM is registered on Prod-5", "corroborates", "desk", source_session="s1")
    assert out["proof_count"] == 1 and out["refutation_count"] == 0
    # from the first witness the asserted number stops deciding
    assert out["effective_confidence"] > 0.42
    ev = store.evidence(rid)
    assert len(ev) == 1 and ev[0]["stance"] == "corroborates" and "esxcli" in ev[0]["quote"]
    assert ev[0]["source_session"] == "s1"


def test_more_witnesses_move_it_less_each_time(store):
    rid = make(store)
    seen = []
    for i in range(4):
        seen.append(store.corroborate(rid, f"observed again, run {i}", "corroborates", "desk")["effective_confidence"])
    gaps = [b - a for a, b in itertools.pairwise(seen)]
    assert all(b < a for a, b in itertools.pairwise(gaps)), f"steps should shrink, got {gaps}"
    assert seen[-1] < 1.0, "certainty is approached, never reached"


def test_one_contradiction_outweighs_two_agreements(store):
    """The asymmetry is deliberate. A record that failed in the field is worth less than one nobody
    has checked, because acting on it costs a session."""
    rid = make(store)
    store.corroborate(rid, "held on 2026-09-01", "corroborates", "desk")
    two_for = store.corroborate(rid, "held again on 2026-09-10", "corroborates", "desk")["effective_confidence"]
    after = store.corroborate(rid, "the VM was on Prod-3 when I looked", "contradicts", "desk")
    assert after["effective_confidence"] < two_for / 1.5
    assert after["proof_count"] == 2 and after["refutation_count"] == 1


def test_a_contradicted_record_survives_and_says_so(store):
    """The point of the whole change: disagreement is recorded, not resolved by deletion."""
    rid = make(store)
    store.corroborate(rid, "found it on Prod-3", "contradicts", "desk")
    rec = store.get(rid)
    assert rec["status"] == "active", "a contradiction must not supersede or delete the record"
    assert rec["content"], "the original claim is still readable"
    assert rec["refutation_count"] == 1
    assert store.evidence(rid)[0]["quote"] == "found it on Prod-3"


def test_search_ranks_on_earned_confidence_not_the_asserted_one(store):
    """A loud unwitnessed claim must not outrank a quieter one that has held up."""
    loud = make(store, title="Prod-5 carries the HA VM", content="Prod-5 hosts it.", confidence=0.99)
    quiet = make(store, title="Prod-5 carries the HA VM too", content="Prod-5 hosts it.", confidence=0.5)
    for i in range(6):
        store.corroborate(quiet, f"checked, run {i}", "corroborates", "desk")
    store.corroborate(loud, "it was not there", "contradicts", "desk")
    hits = store.search("Prod-5", [NS])
    order = [h["id"] for h in hits]
    assert order.index(quiet) < order.index(loud), "the witnessed record should rank above the contradicted one"
    assert "for/" in next(h for h in hits if h["id"] == quiet)["why"]


def test_why_distinguishes_asserted_from_witnessed(store):
    rid = make(store)
    assert "unwitnessed" in store.search("Prod-5", [NS])[0]["why"]
    store.corroborate(rid, "seen", "corroborates", "desk")
    assert "1 for/0 against" in store.search("Prod-5", [NS])[0]["why"]


def test_a_database_written_before_evidence_existed_still_opens(tmp_path):
    """This ships to a service holding live records. Opening an older database must add the table
    and leave every existing record reading exactly as it did."""
    path = str(tmp_path / "old.db")
    old = Store(path)
    rid = old.store({"namespace": NS, "title": "written before", "content": "an older fact", "confidence": 0.77}, "desk")["id"]
    old.db.execute("drop table evidence")  # the shape the file had before this change
    old.db.close()

    reopened = Store(path)
    rec = reopened.get(rid)
    assert rec["title"] == "written before"
    assert rec["confidence"] == pytest.approx(0.77)
    assert rec["effective_confidence"] == pytest.approx(0.77), "an unwitnessed record must not move"
    assert reopened.corroborate(rid, "still true", "corroborates", "desk")["proof_count"] == 1


def test_an_unknown_stance_is_refused(store):
    rid = make(store)
    with pytest.raises(ValueError):
        store.corroborate(rid, "x", "maybe", "desk")


def test_corroborating_a_missing_record_returns_none(store):
    assert store.corroborate("no-such-id", "x", "corroborates", "desk") is None


def test_an_anchor_counts_once_by_its_state_however_often_it_flipped(store):
    rid = store.store({"namespace": NS, "title": "AGENTS.md says never ssh vault", "content": "x", "type": "constraint", "confidence": 0.95, "anchors": ["path:D:/a.md"]}, "desk")["id"]
    for _ in range(10):
        store.report_anchor(rid, "path:D:/a.md", "missing", "sweep")
        store.report_anchor(rid, "path:D:/a.md", "ok", "sweep")
    rec = store.get(rid)
    assert (rec["proof_count"], rec["refutation_count"]) == (0, 0)
    assert rec["effective_confidence"] == pytest.approx(0.95), "a flapping anchor that resolves leaves the record as written"
    store.report_anchor(rid, "path:D:/a.md", "missing", "sweep")
    assert store.get(rid)["refutation_count"] == 1
    assert store.get(rid)["effective_confidence"] == pytest.approx(0.25), "a missing anchor is one contradiction"
    store.corroborate(rid, "held on 2026-10-08", "corroborates", "desk")
    assert (store.get(rid)["proof_count"], store.get(rid)["refutation_count"]) == (1, 1), "a witness still counts beside it"


def test_anchor_rows_filed_before_the_column_existed_are_marked_on_open(tmp_path):
    import sqlite3
    import uuid

    path = str(tmp_path / "old.db")
    old = Store(path)
    rid = old.store({"namespace": NS, "title": "t", "content": "c", "type": "constraint", "confidence": 0.9, "anchors": ["path:AGENTS.md"]}, "desk")["id"]
    old.corroborate(rid, "anchor drift was seen by hand", "contradicts", "desk")
    # the table as 0.16.0 and earlier had it, with ten transitions of one anchor
    old.db.execute("alter table evidence drop column anchor")
    flips = [("contradicts", "anchor path:AGENTS.md no longer resolves"), ("corroborates", "anchor path:AGENTS.md resolves again")] * 5
    for stance, quote in flips:
        old.db.execute("insert into evidence(id, record_id, at, stance, quote) values (?,?,?,?,?)", (str(uuid.uuid4()), rid, "2026-09-28T17:41:48+00:00", stance, quote))
    old.db.execute("update anchors set last_state='ok' where record_id=?", (rid,))
    reopened = Store(path)
    marked = sqlite3.connect(path).execute("select count(*) from evidence where anchor='path:AGENTS.md'").fetchone()[0]
    assert marked == 10
    rec = reopened.get(rid)
    assert (rec["proof_count"], rec["refutation_count"]) == (0, 1), "only the hand-filed contradiction is a witness"
    assert rec["effective_confidence"] == pytest.approx(0.25)
