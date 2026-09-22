"""Standing questions, derived from what sessions actually asked rather than declared by hand.

The signal being protected is "asked often AND answered badly". A question the store already
answers well needs no standing answer however popular it is, and near-duplicate phrasings of one
question must not split into several weak ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
from store import Store, jaccard, normalise

NS = "gitea/example/ha-management"


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "m.db"))


def ask(store, query, hits=0, rank=0.0, kind="search"):
    store.note_query(NS, "desk", kind, query, hits, rank)


def test_normalise_strips_filler_and_keeps_the_names(store):
    """Normalisation drops filler and orders what is left; it does NOT make two phrasings equal.
    Closing the rest of the gap is the clustering threshold's job, checked below."""
    assert normalise("Which host runs the HA VM?") == "ha host runs vm"
    assert normalise("HA VM host?") == "ha host vm"
    # the identifiers ARE the question; losing them would merge unrelated ones
    assert "prod-5" in normalise("is Prod-5 up")
    assert "192.0.2.10" in normalise("what is at 192.0.2.10")
    assert normalise("the is are of") == "", "a query of nothing but filler is not a question"


def test_the_threshold_merges_phrasings_and_separates_topics():
    """Where the 0.6 in question_candidates has to sit: above the unrelated pair, below the alike
    one. Asserting both bounds is what stops a later tweak quietly merging everything."""
    a = frozenset(normalise("Which host runs the HA VM?").split())
    b = frozenset(normalise("HA VM host?").split())
    c = frozenset(normalise("what is the zigbee coordinator firmware").split())
    assert jaccard(a, b) >= 0.6, f"phrasings of one question must merge, got {jaccard(a, b):.2f}"
    assert jaccard(a, c) < 0.6, f"unrelated questions must not merge, got {jaccard(a, c):.2f}"


def test_jaccard_is_symmetric_and_bounded():
    a, b = frozenset(["ha", "vm", "host"]), frozenset(["ha", "vm"])
    assert jaccard(a, b) == jaccard(b, a)
    assert jaccard(a, a) == 1.0 and jaccard(a, frozenset()) == 0.0


def test_a_question_asked_once_is_not_a_standing_question(store):
    ask(store, "where does the HA VM live")
    assert store.question_candidates([NS]) == []


def test_repeated_and_unanswered_beats_repeated_and_answered(store):
    for _ in range(3):
        ask(store, "where does the HA VM live", hits=0)
    for _ in range(9):
        ask(store, "what is the forge address", hits=6, rank=12.0)
    cands = store.question_candidates([NS])
    top = cands[0]
    assert "vm" in top["question"], f"the unanswered question should lead, got {[c['question'] for c in cands]}"
    assert top["zero_hit_asks"] == 3 and top["asked"] == 3
    answered = next(c for c in cands if "forge" in c["question"])
    assert answered["need"] < top["need"], "a question asked three times as often but answered well needs less"


def test_a_well_answered_question_scores_near_nothing(store):
    for _ in range(8):
        ask(store, "what is the forge address", hits=5, rank=9.0)
    assert store.question_candidates([NS])[0]["need"] == pytest.approx(0.0)


def test_phrasings_of_one_question_are_one_candidate(store):
    """Counting near-duplicates separately is how a real demand signal gets buried."""
    ask(store, "which host runs the HA VM")
    ask(store, "HA VM host?")
    ask(store, "what host is the ha vm on")
    cands = store.question_candidates([NS])
    assert len(cands) == 1, f"expected one question, got {[c['question'] for c in cands]}"
    assert cands[0]["asked"] == 3
    assert len(cands[0]["examples"]) == 3


def test_unrelated_questions_stay_separate(store):
    """The control for the test above: merging must not be so eager that everything is one group."""
    for _ in range(2):
        ask(store, "which host runs the HA VM")
        ask(store, "what is the zigbee coordinator firmware")
    cands = store.question_candidates([NS])
    assert len(cands) == 2, f"expected two questions, got {[c['question'] for c in cands]}"


def test_context_tasks_count_as_demand(store):
    """A task asked for repeatedly is an information need whether or not it was phrased as one."""
    for _ in range(2):
        ask(store, "rebuild the zigbee map", hits=0, kind="context")
    assert store.question_candidates([NS])[0]["asked"] == 2


def test_the_queries_themselves_are_kept_as_evidence(store):
    for _ in range(2):
        ask(store, "where does the HA VM live")
    assert "where does the HA VM live" in store.question_candidates([NS])[0]["examples"]


def test_measuring_demand_never_breaks_the_read(store):
    """A failure to record what was asked must not fail the search a session is waiting on. Reading
    the candidates back is a different matter and is allowed to raise: nothing is waiting on it."""
    store.db.execute("drop table queries")
    ask(store, "anything")  # the point of the test: this must not raise


def test_candidates_are_scoped(store):
    for _ in range(2):
        ask(store, "where does the HA VM live")
    assert store.question_candidates(["gitea/example/other"]) == []
    assert store.question_candidates([]) == []
