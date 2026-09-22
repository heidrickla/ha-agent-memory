"""Standing answers: written once in the background, read for nothing, and known to be stale.

Two properties carry the feature. Reading must never retrieve or call a model, or it is just
`project_context` with extra steps. And a stale answer must announce itself, because an answer that
reads current and is not is worse than no answer at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import mcp as mcp_mod
from enrich import NOTHING_KNOWN, Worker
from store import Store

NS = "gitea/example/ha-management"
OPTIONS = {"github_owner": "heidrickla", "forge_owner": "example", "forge_hosts": ["forge.example"], "project_links": []}
QUESTION = "which host carries the HA VM"


class FakeOllama:
    """Records what it was asked. The worker must not call it when there is nothing to summarise."""

    model = "fake-model"

    def __init__(self, reply="The HA VM runs on ESXi-Prod-5."):
        self.reply, self.calls = reply, []

    def available(self) -> bool:
        return True

    def answer(self, question, records):
        self.calls.append({"question": question, "records": records})
        return self.reply


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "m.db"))


def record(store, title, content):
    return store.store({"namespace": NS, "title": title, "content": content, "type": "repository-fact"}, "desk")["id"]


def drain(store, ollama) -> int:
    w = Worker(store, OPTIONS, ollama=ollama, poll_s=0.01)
    done = 0
    while w.step():
        done += 1
        if done > 20:
            raise AssertionError("worker did not settle")
    return done


def test_a_question_starts_unanswered_and_stale(store):
    q = store.add_question(NS, QUESTION, "desk")
    assert q["answered_at"] is None and q["answer"] == ""
    assert q["stale"] is True, "never answered is stale, so the worker picks it up"


def test_the_worker_writes_an_answer_and_cites_its_sources(store):
    rid = record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM and it returns there.")
    store.add_question(NS, QUESTION, "desk")
    ollama = FakeOllama()
    assert drain(store, ollama) >= 1
    q = store.find_question([NS], QUESTION)
    assert q["answer"] == "The HA VM runs on ESXi-Prod-5."
    assert rid in q["source_ids"], "an answer nobody can trace back to records is an assertion"
    assert q["model"] == "ollama:fake-model" and q["answered_at"]
    assert q["stale"] is False


def test_reading_an_answer_calls_no_model_and_runs_no_search(store):
    """The whole point: a settled answer costs a database read."""
    record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM.")
    store.add_question(NS, QUESTION, "desk")
    drain(store, FakeOllama())

    mem = mcp_mod.Memory(store, OPTIONS)
    calls = []
    real_search = store.search
    store.search = lambda *a, **k: (calls.append(a) or real_search(*a, **k))
    out = mem.answer(NS, "what host is the HA VM on?")
    store.search = real_search
    assert out["answered"] is True and "Prod-5" in out["answer"]
    # basis_for runs one search to judge freshness; retrieval to BUILD the answer must not happen
    assert len(calls) <= 1, f"reading should not retrieve for content, saw {len(calls)} searches"


def test_an_edited_source_makes_the_answer_stale_without_a_model(store):
    rid = record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM.")
    store.add_question(NS, QUESTION, "desk")
    drain(store, FakeOllama())
    assert store.find_question([NS], QUESTION)["stale"] is False

    store.update(rid, {"content": "The HA VM was moved to ESXi-Prod-3."}, "desk")
    q = store.find_question([NS], QUESTION)
    assert q["stale"] is True, "an edited source must move the basis"
    assert q["answer"], "the old answer is still readable; it is labelled, not deleted"


def test_a_new_matching_record_also_makes_it_stale(store):
    record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM.")
    store.add_question(NS, QUESTION, "desk")
    drain(store, FakeOllama())
    record(store, "HA VM host note", "The HA VM has a second disk on Prod-5.")
    assert store.find_question([NS], QUESTION)["stale"] is True


def test_a_stale_answer_says_so_when_read(store):
    rid = record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM.")
    store.add_question(NS, QUESTION, "desk")
    drain(store, FakeOllama())
    store.update(rid, {"content": "Moved to Prod-3."}, "desk")
    out = mcp_mod.Memory(store, OPTIONS).answer(NS, QUESTION)
    assert out["stale"] is True
    assert "changed since" in out["why"]


def test_the_worker_rewrites_a_stale_answer(store):
    rid = record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM.")
    store.add_question(NS, QUESTION, "desk")
    drain(store, FakeOllama("On Prod-5."))
    store.update(rid, {"content": "Moved to ESXi-Prod-3 on 2026-09-22."}, "desk")
    drain(store, FakeOllama("On Prod-3."))
    q = store.find_question([NS], QUESTION)
    assert q["answer"] == "On Prod-3." and q["stale"] is False


def test_nothing_to_summarise_is_answered_without_calling_the_model(store):
    """A question no record answers gets the honest reply, not an invented one."""
    store.add_question(NS, "what colour is the garage door opener", "desk")
    ollama = FakeOllama()
    drain(store, ollama)
    q = store.find_question([NS], "what colour is the garage door opener")
    assert q["answer"] == NOTHING_KNOWN
    assert ollama.calls == [], "the model must not be asked to summarise nothing"


def test_an_answer_carrying_a_secret_is_refused(store):
    record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM.")
    store.add_question(NS, QUESTION, "desk")
    w = Worker(store, OPTIONS, ollama=FakeOllama("the token is ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), poll_s=0.01)
    w.step()
    q = store.find_question([NS], QUESTION)
    assert q["answer"] == "", "model output goes through the same secret screen as a record"


def test_the_same_question_twice_is_one_question(store):
    a = store.add_question(NS, "which host carries the HA VM", "desk")
    b = store.add_question(NS, "which host carries the HA VM", "desk")
    assert a["id"] == b["id"]


def test_a_question_of_filler_is_refused(store):
    with pytest.raises(ValueError):
        store.add_question(NS, "what is the", "desk")


def test_an_unknown_question_is_not_answered_with_the_nearest_one(store):
    """The control on fuzzy matching: close enough to merge phrasings, not close enough to answer a
    different question from the wrong record."""
    record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM.")
    store.add_question(NS, QUESTION, "desk")
    drain(store, FakeOllama())
    out = mcp_mod.Memory(store, OPTIONS).answer(NS, "what is the zigbee coordinator firmware version")
    assert out["answered"] is False
    assert "no standing question matches" in out["why"]


def test_each_job_kind_runs_its_own_handler(store):
    """`run_job` used to run every job as an extraction whatever its kind said. With two kinds in
    the queue that would answer a question with the extraction prompt and store the result as
    records, so this asserts which handler each kind reaches."""

    class Both(FakeOllama):
        def __init__(self):
            super().__init__()
            self.extracted = []

        def extract(self, namespace, notes):
            self.extracted.append(notes)
            return '{"memories": []}'

    record(store, "The HA VM lives on ESXi-Prod-5", "Prod-5 carries the HA VM.")
    store.add_question(NS, QUESTION, "desk")
    ollama = Both()
    w = Worker(store, OPTIONS, ollama=ollama, poll_s=0.01)

    store.enqueue("extract", NS, {"notes": "some session notes", "session": "s1"})
    w.step()
    assert ollama.extracted and not ollama.calls, "an extract job must reach the extraction handler"

    ollama.extracted.clear()
    w.step()  # nothing queued, so the idle sweep picks up the unanswered question
    assert ollama.calls and not ollama.extracted, "an answer job must reach the answer handler"
