"""Model servers routed like a load balancer: a probe no call waits on decides which servers are live, each
call goes to the live one with the fewest calls in flight (the preferred one on a tie), a server that does
not answer is marked down until its next probe, and a model failing on a server that answered costs the
next model, not the server. The nightly review splits its candidates between the live servers.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import server as srv
from enrich import Pool, Worker
from store import Store

NS = "gitea/heidrickla/ha-management"
GOOD = json.dumps({"memories": [{"type": "constraint", "title": "One model per GPU", "content": "Card 0 serves Bonsai only.", "confidence": 0.9}]})


class FakeServer:
    """One model server: `up` is what its probe answers; `fail` is raised by a call; `delay` holds each call."""

    api = "openai"
    model = "bonsai2-memory"

    def __init__(self, base: str, up: bool = True, fail: Exception | None = None, delay: float = 0.0):
        self.base, self.up, self.fail, self.delay = base, up, fail, delay
        self.probes = 0
        self.calls: list[str] = []

    def available(self) -> bool:
        self.probes += 1
        return self.up

    def _serve(self, kind: str, model: str, reply: str) -> str:
        self.calls.append(f"{kind}:{model}")
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return reply

    def extract(self, namespace, notes, model=""):
        return self._serve("extract", model, GOOD)

    def answer(self, question, records, model=""):
        return self._serve("answer", model, "An answer.")

    def judge(self, system, user, schema, max_tokens, model=""):
        return self._serve("review", model, json.dumps({"verdict": "keep", "record": "", "reason": "fine"}))


def worker_with(tmp_path, *servers, **options) -> tuple[Worker, Store]:
    store = Store(str(tmp_path / "m.db"))
    w = Worker(store, {"ollama_model": "bonsai2-memory", **options}, poll_s=30.0)
    w.pool = Pool(list(servers))
    return w, store


def queue_extract(store: Store) -> None:
    store.enqueue("extract", NS, {"notes": "Card 0 serves Bonsai only.", "session": "s1"})


def test_options_give_the_preferred_server_then_the_fallback(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    w = Worker(store, {"ollama_url": "http://desk:8080/v1", "ollama_fallback_url": "http://rack:8080/v1"})
    assert [c.base for c in w.pool.clients] == ["http://desk:8080/v1", "http://rack:8080/v1"]
    same = Worker(store, {"ollama_url": "http://desk:8080/v1", "ollama_fallback_url": "http://desk:8080/v1"})
    assert len(same.pool.clients) == 1
    assert [s["url"] for s in w.state()["servers"]] == ["http://desk:8080/v1", "http://rack:8080/v1"]


def test_the_call_goes_to_the_server_the_probe_found_live(tmp_path):
    desk = FakeServer("http://desk/v1", up=False, delay=3.0)
    rack = FakeServer("http://rack/v1")
    w, store = worker_with(tmp_path, desk, rack)
    queue_extract(store)
    t0 = time.monotonic()
    assert w.step() is True
    assert time.monotonic() - t0 < 1.0, "a dead server costs the call nothing"
    assert desk.calls == [] and rack.calls == ["extract:bonsai2-memory"]


def test_the_preferred_server_serves_when_both_are_live(tmp_path):
    desk, rack = FakeServer("http://desk/v1"), FakeServer("http://rack/v1")
    w, store = worker_with(tmp_path, desk, rack)
    queue_extract(store)
    assert w.step() is True and desk.calls == ["extract:bonsai2-memory"] and rack.calls == []


def test_a_server_that_does_not_answer_is_marked_down_and_the_call_moves_on(tmp_path):
    desk = FakeServer("http://desk/v1", fail=urllib.error.URLError(ConnectionRefusedError(111, "refused")))
    rack = FakeServer("http://rack/v1")
    w, store = worker_with(tmp_path, desk, rack)
    queue_extract(store)
    assert w.step() is True and rack.calls == ["extract:bonsai2-memory"]
    assert [s["live"] for s in w.pool.state()] == [False, True]
    queue_extract(store)
    probes = desk.probes
    assert w.step() is True and desk.calls == ["extract:bonsai2-memory"], "down until its next probe: not asked again"
    assert desk.probes == probes, "and the next call did not probe it either"


def test_an_http_error_is_the_models_failure_not_the_servers(tmp_path):
    missing = urllib.error.HTTPError("http://desk/v1/chat/completions", 404, "model not found", None, None)  # type: ignore[arg-type]
    desk, rack = FakeServer("http://desk/v1", fail=missing), FakeServer("http://rack/v1")
    w, store = worker_with(tmp_path, desk, rack, ollama_models=["extract:needle"])
    queue_extract(store)
    assert w.step() is True
    assert desk.calls == ["extract:needle", "extract:bonsai2-memory"], "the next model on the same server"
    assert rack.calls == ["extract:needle"], "no model served there: the chain starts again on the next server"
    assert [s["live"] for s in w.pool.state()] == [True, True], "a server that answered stays live"


def test_a_server_that_answers_gets_the_call_whether_or_not_its_model_is_running(tmp_path):
    """Liveness is the server answering its model list; llama-swap loads a configured model on its first request."""
    desk = FakeServer("http://desk/v1", delay=0.3)  # the first request pays the load
    w, store = worker_with(tmp_path, desk, FakeServer("http://rack/v1"))
    queue_extract(store)
    assert w.step() is True and desk.calls == ["extract:bonsai2-memory"]


def test_the_probe_runs_on_its_own_clock_not_per_call(tmp_path):
    desk = FakeServer("http://desk/v1")
    w, store = worker_with(tmp_path, desk)
    for _ in range(3):
        queue_extract(store)
        assert w.step() is True
    assert desk.probes == 1 and len(desk.calls) == 3, "one probe in the probe interval, three calls"
    w.pool.checked = time.monotonic() - w.pool.probe_s
    w.step()
    assert desk.probes == 2


def test_the_least_busy_live_server_takes_the_next_call():
    desk, rack = FakeServer("http://desk/v1"), FakeServer("http://rack/v1")
    pool = Pool([desk, rack])
    assert pool.order() == [0, 1], "unprobed counts as live; the preferred one first"
    with pool.using(0):
        assert pool.order() == [1, 0]
        with pool.using(1), pool.using(1):
            assert pool.order() == [0, 1]
    pool.mark_down(0)
    assert pool.order() == [1]


def test_the_review_divides_its_candidates_between_live_servers(tmp_path):
    app = srv.App({"clients": [], "github_owner": "heidrickla", "forge_owner": "heidrickla", "forge_hosts": [], "project_links": [], "hubs": [NS]}, str(tmp_path))
    desk, rack = FakeServer("http://desk/v1", delay=0.2), FakeServer("http://rack/v1", delay=0.2)
    app.worker.pool = Pool([desk, rack])
    for i in range(6):
        app.store.store_candidate({"namespace": NS, "type": "repository-fact", "title": f"Fact {i}", "content": f"Fact number {i}."}, client="enrichment")
    out = app.reviewer.run()
    assert out["reviewed"] == 6 and out["kept"] == 6
    assert set(out["servers"]) == {"http://desk/v1", "http://rack/v1"} and sum(out["servers"].values()) == 6
    assert len(desk.calls) >= 2 and len(rack.calls) >= 2


def test_a_review_with_one_live_server_uses_only_it(tmp_path):
    app = srv.App({"clients": [], "github_owner": "heidrickla", "forge_owner": "heidrickla", "forge_hosts": [], "project_links": [], "hubs": [NS]}, str(tmp_path))
    desk, rack = FakeServer("http://desk/v1", up=False), FakeServer("http://rack/v1")
    app.worker.pool = Pool([desk, rack])
    app.store.store_candidate({"namespace": NS, "type": "repository-fact", "title": "Fact", "content": "A fact."}, client="enrichment")
    out = app.reviewer.run()
    assert out["servers"] == {"http://rack/v1": 1} and desk.calls == []


def test_a_probe_of_a_dead_host_runs_off_the_call_path(tmp_path):
    """The probe is the only thing that waits on a dead host, and a call never does."""
    desk = FakeServer("http://desk/v1", up=False)
    rack = FakeServer("http://rack/v1")
    pool = Pool([desk, rack])
    pool.probe()
    started = threading.Event()

    def slow_probe() -> bool:
        started.set()
        time.sleep(0.5)
        return False

    desk.available = slow_probe  # type: ignore[method-assign]
    t = threading.Thread(target=pool.probe)
    t.start()
    started.wait(1.0)
    assert pool.order() == [1], "while a slow probe runs, routing reads the last known state"
    t.join()
