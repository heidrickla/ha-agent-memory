"""A model per job kind, and a chain to fall back down when one fails.

One model served both kinds and there was no second choice if it failed: a model missing from the
host, or one reply that would not parse, cost the whole job. Extraction and answering are not the
same problem, so they should not be forced to share a model.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
from enrich import NOTHING_KNOWN, Worker
from store import Store

NS = "gitea/example/ha-management"
GOOD_JSON = '{"memories": [{"type": "constraint", "title": "A fact", "content": "Something true.", "confidence": 0.8}]}'


class Fleet:
    """An Ollama whose models each behave differently, recording which was asked."""

    model = "default-model"

    def __init__(self, behaviour: dict):
        self.behaviour, self.asked = behaviour, []

    def available(self) -> bool:
        return True

    def _serve(self, model):
        self.asked.append(model)
        out = self.behaviour.get(model, "ok")
        if isinstance(out, Exception):
            raise out
        return out

    def answer(self, question, records, model=""):
        return self._serve(model or self.model)

    def extract(self, namespace, notes, model=""):
        return self._serve(model or self.model)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "m.db"))


def options(**over):
    o = {"github_owner": "heidrickla", "forge_owner": "example", "forge_hosts": ["forge.example"], "project_links": []}
    o.update(over)
    return o


def test_an_unconfigured_worker_keeps_its_single_model(store):
    """The control for every live deployment: without `ollama_models` nothing changes."""
    w = Worker(store, options(ollama_model="qwen3.5:9b"), ollama=Fleet({}))
    assert w.models_for("extract") == ["default-model"]
    assert w.models_for("answer") == ["default-model"]


def test_each_job_kind_gets_its_own_model(store):
    w = Worker(store, options(ollama_models={"extract": ["needle"], "answer": ["qwen3.5:9b"]}), ollama=Fleet({}))
    assert w.models_for("extract")[0] == "needle"
    assert w.models_for("answer")[0] == "qwen3.5:9b"


def test_the_configured_default_always_ends_the_chain(store):
    """A chain of models that are all missing from the host must still fall through to the one the
    deployment already knows works."""
    w = Worker(store, options(ollama_models={"extract": ["needle"]}), ollama=Fleet({}))
    assert w.models_for("extract") == ["needle", "default-model"]


def test_the_default_is_not_repeated_when_it_is_named(store):
    w = Worker(store, options(ollama_models={"answer": ["default-model"]}), ollama=Fleet({}))
    assert w.models_for("answer") == ["default-model"]


def test_a_missing_model_costs_the_next_one_not_the_job(store):
    """A chain names what would be good, not what is pulled on the host, so a model that is not
    there is the ordinary case."""
    fleet = Fleet({"needle": urllib.error.URLError("model not found"), "default-model": GOOD_JSON})
    w = Worker(store, options(ollama_models={"extract": ["needle"]}), ollama=fleet)
    store.enqueue("extract", NS, {"notes": "some notes", "session": "s1"})
    assert w.step() is True
    assert fleet.asked == ["needle", "default-model"], f"chain not walked in order: {fleet.asked}"
    rows = store.by_namespace([NS], statuses=("candidate",))
    assert rows and rows[0]["agent"] == "ollama:default-model", "the record must name the model that actually served"


def test_an_unparseable_reply_also_falls_through(store):
    """Extraction's characteristic failure is a reply that will not parse, and that is a property of
    the model, so it must cost the next model rather than the job."""
    fleet = Fleet({"chatty": "Sure! Here are the memories:", "default-model": GOOD_JSON})
    w = Worker(store, options(ollama_models={"extract": ["chatty"]}), ollama=fleet)
    store.enqueue("extract", NS, {"notes": "some notes", "session": "s1"})
    assert w.step() is True
    assert fleet.asked == ["chatty", "default-model"]


def test_the_whole_chain_failing_still_fails_the_job(store):
    """The control on falling through: a fallback that swallows every failure would mark a job done
    having stored nothing."""
    fleet = Fleet({"a": ValueError("no"), "default-model": ValueError("no")})
    w = Worker(store, options(ollama_models={"extract": ["a"]}), ollama=fleet)
    store.enqueue("extract", NS, {"notes": "some notes", "session": "s1"})
    assert w.step() is False, "a job every model failed must not report success"
    assert fleet.asked == ["a", "default-model"]
    assert store.by_namespace([NS], statuses=("candidate",)) == []


def test_the_answer_job_routes_on_its_own_kind(store):
    fleet = Fleet({"prose": "Prod-5 carries the HA VM.", "default-model": "wrong model served this"})
    store.store({"namespace": NS, "title": "HA VM host", "content": "Prod-5 carries the HA VM."}, "desk")
    store.add_question(NS, "which host carries the HA VM", "desk")
    w = Worker(store, options(ollama_models={"answer": ["prose"], "extract": ["other"]}), ollama=fleet)
    assert w.step() is True
    assert fleet.asked == ["prose"], f"the answer job must use the answer chain, got {fleet.asked}"
    q = store.find_question([NS], "which host carries the HA VM")
    assert q["answer"] == "Prod-5 carries the HA VM." and q["model"] == "ollama:prose"


def test_a_question_with_no_records_still_names_no_model(store):
    """Nothing to summarise is settled without calling anything, so no chain is walked."""
    fleet = Fleet({})
    store.add_question(NS, "what colour is the garage door opener", "desk")
    w = Worker(store, options(ollama_models={"answer": ["prose"]}), ollama=fleet)
    assert w.step() is True
    assert fleet.asked == []
    assert store.find_question([NS], "what colour is the garage door opener")["answer"] == NOTHING_KNOWN
