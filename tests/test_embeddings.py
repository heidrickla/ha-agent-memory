"""Embedding retrieval: cosine when the encoder answers, FTS whenever it does not.

The encoder here is a fake OpenAI-style server with bag-of-words vectors. What is pinned: the texts
are the offline test's, a dead or slow encoder never fails or delays a search past the timeout, a
vector goes stale on an edit or a model change, forget and purge drop it, the backfill covers every
record search can return, and cosine reaches exactly the records FTS can.
"""

from __future__ import annotations

import array
import hashlib
import importlib.util
import json
import math
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import embed
import server as srv

ROOT = Path(__file__).resolve().parent.parent

NS = "gitea/heidrickla/ha-management"
OTHER = "gitea/heidrickla/ha-loops"
MODEL = "qwen3-emb-0.6b"
SNAP = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
ROOT_PATH = f"/hf/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots/{SNAP}"


def vector(text: str) -> list[float]:
    """Bag of words over 64 dimensions, unit length: shared words mean a higher cosine."""
    v = [0.0] * 64
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeEncoder(BaseHTTPRequestHandler):
    """vLLM's `/v1/models` (with `root`) and `/v1/embeddings`; with `list_root` off, llama-swap's listing,
    which carries no `root`, plus its `/upstream/<model>/` passthrough, which does."""

    models: ClassVar[tuple] = (MODEL, "other-emb")
    root: ClassVar[str] = ROOT_PATH
    list_root: ClassVar[bool] = True
    delay: ClassVar[float] = 0.0  # seconds before the reply starts
    trickle: ClassVar[float] = 0.0  # seconds over which the reply body arrives, in 20 pieces
    seen: ClassVar[list] = []
    refuse: ClassVar[set] = set()

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
            data = [{"id": m, "object": "model", **({"root": FakeEncoder.root} if FakeEncoder.list_root else {})} for m in FakeEncoder.models]
            return self._send(200, {"object": "list", "data": data})
        for m in FakeEncoder.models:
            if self.path == f"/upstream/{m}/v1/models":
                return self._send(200, {"object": "list", "data": [{"id": m, "root": FakeEncoder.root}]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if self.path != "/v1/embeddings" or body.get("model") not in FakeEncoder.models:
            return self._send(404, {"error": "not found"})
        text = body["input"][0]
        FakeEncoder.seen.append(text)
        if FakeEncoder.delay:
            time.sleep(FakeEncoder.delay)
        if text in FakeEncoder.refuse:
            return self._send(400, {"error": "input too long"})
        reply = {"object": "list", "model": body["model"], "data": [{"index": 0, "object": "embedding", "embedding": vector(text)}]}
        if not FakeEncoder.trickle:
            return self._send(200, reply)
        out = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        step = len(out) // 20 + 1
        for i in range(0, len(out), step):
            self.wfile.write(out[i : i + step])
            self.wfile.flush()
            time.sleep(FakeEncoder.trickle / 20)


@pytest.fixture
def enc():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEncoder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeEncoder.root, FakeEncoder.list_root, FakeEncoder.delay, FakeEncoder.trickle, FakeEncoder.seen, FakeEncoder.refuse = ROOT_PATH, True, 0.0, 0.0, [], set()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", server
    FakeEncoder.delay = FakeEncoder.trickle = 0.0
    server.shutdown()
    server.server_close()


def options(url: str | None, **over) -> dict:
    o = {
        "clients": [{"name": "desk", "ip": "127.0.0.1", "scopes": ["read", "write", "delete"]}],
        "github_owner": "heidrickla",
        "forge_owner": "heidrickla",
        "forge_hosts": [],
        "project_links": [],
        "hubs": [NS],
        "embed_model": MODEL,
        "embed_timeout_s": 2,
    }
    if url is not None:
        o["embed_url"] = url
    o.update(over)
    return o


def put(app, title: str, content: str, **kw) -> str:
    rec = {"namespace": NS, "type": "constraint", "title": title, "content": content, **kw}
    return app.store.store(rec, "desk")["id"]


def vectors(app) -> dict:
    return {r["record_id"]: dict(r) for r in app.store.db.execute("select * from embeddings")}


def load_embed_eval():
    spec = importlib.util.spec_from_file_location("embed_eval", ROOT / "tools" / "retrieval-eval" / "embed_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_texts_are_the_offline_tests(tmp_path, enc):
    """The query instruction and the record format come from embed_eval.py, the script that measured MRR 0.94."""
    ee = load_embed_eval()
    assert embed.QUERY_PROMPT == ee.QUERY_PROMPT
    app = srv.App(options(enc[0]), str(tmp_path))
    put(app, "  Encoder on card 1 ", "Qwen3-Embedding-0.6B serves on the A2000.  ")
    app.indexer.step()
    assert FakeEncoder.seen[-1] == "Encoder on card 1\nQwen3-Embedding-0.6B serves on the A2000.", "title, newline, content, no instruction"
    app.memory.search(NS, "which card serves the encoder")
    assert FakeEncoder.seen[-1] == ee.QUERY_PROMPT + "which card serves the encoder"
    # The digest is the offline test's cache key: its own Encoder finds the vector without a request.
    # Port 9 has no server, so a key that differed would try the network and fail.
    text = embed.document_text("Encoder on card 1", "Qwen3-Embedding-0.6B serves on the A2000.")
    unit = [1.0] + [0.0] * (ee.DIM - 1)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({embed.digest(ee.MODEL, ee.SNAPSHOT, text): {"kind": "record", "seconds": 0, "tokens": 1, "v": unit}}))
    assert ee.Encoder("http://127.0.0.1:9", cache).vector(text, "record") == unit


def test_the_first_step_embeds_every_record_search_can_return(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    live = [put(app, f"Fact {i}", f"zebra fact number {i}") for i in range(3)]
    live.append(app.store.store({"namespace": NS, "title": "Unreviewed", "content": "zebra candidate", "status": "candidate"}, "desk")["id"])
    old = put(app, "Old", "zebra superseded")
    live.append(put(app, "New", "zebra replacement", supersedes=old))
    gone = put(app, "Gone", "zebra forgotten")
    app.store.forget(gone, "desk")
    assert app.indexer.step() == len(live)
    rows = vectors(app)
    assert set(rows) == set(live), "active and candidate records, not superseded or forgotten ones"
    assert {r["model"] for r in rows.values()} == {f"{MODEL}@{SNAP[:12]}"}
    assert all(len(r["vector"]) == 64 * 4 for r in rows.values()), "float32"
    state = app.health()["embeddings"]
    assert state["encoder"] == "available" and state["records"] == state["current"] == len(live) and state["snapshot"] == SNAP[:12]
    assert app.indexer.step() == 0


def test_the_worker_backfills_at_start_without_waiting_for_a_poll(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    ids = {put(app, f"Fact {i}", f"zebra fact {i}") for i in range(5)}
    app.store.changed.clear()  # at start nothing has been written yet
    stop = threading.Event()
    threading.Thread(target=app.indexer.loop, args=(stop,), daemon=True).start()
    deadline = time.monotonic() + 5
    while set(vectors(app)) != ids and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    app.store.changed.set()
    assert set(vectors(app)) == ids


def test_search_ranks_by_cosine_when_the_encoder_answers(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    # Facts, not constraints: project_context lists constraints first whatever the task.
    a = put(app, "Garage door", "the garage door opener pairs over z-wave", type="repository-fact")
    b = put(app, "Encoder card", "the encoder runs on card one of the rack", type="repository-fact")
    app.indexer.step()
    res = app.memory.search(NS, "which card runs the encoder")
    assert res["ranking"] == "cosine"
    assert [h["id"] for h in res["hits"]] == [b, a], "every live record in scope, nearest first"
    assert "cosine" in res["hits"][0]["why"]
    ctx = app.memory.project_context(NS, "encoder card")
    assert ctx["ranking"] == "cosine" and any(r["why"] == "nearest to the task by embedding" for r in ctx["records"])


def test_a_dead_encoder_costs_nothing_and_search_answers_from_fts(tmp_path, enc):
    url, server = enc
    app = srv.App(options(url, embed_timeout_s=0.5), str(tmp_path))
    put(app, "Zebra crossing", "zebra stripes mark the crossing")
    put(app, "Other", "nothing to see")
    app.indexer.step()
    assert app.memory.search(NS, "zebra")["ranking"] == "cosine", "control: cosine while it answers"
    server.shutdown()
    server.server_close()
    t0 = time.monotonic()
    res = app.memory.search(NS, "zebra")
    assert time.monotonic() - t0 < 1.0, "bounded by embed_timeout_s"
    assert res["ranking"] == "fts" and [h["title"] for h in res["hits"]] == ["Zebra crossing"]
    t0 = time.monotonic()
    assert app.memory.search(NS, "zebra")["ranking"] == "fts"
    assert time.monotonic() - t0 < 0.2, "a known-down encoder is not asked again before the next probe"
    assert app.indexer.step() == 0
    health = app.health()
    assert health["embeddings"]["encoder"] == "offline" and health["embeddings"]["error"] and health["status"] == "ok"


def test_an_encoder_that_never_answered_leaves_search_as_it_was(tmp_path):
    app = srv.App(options("http://127.0.0.1:9/v1", embed_timeout_s=0.5), str(tmp_path))  # nothing listens there
    put(app, "Zebra crossing", "zebra stripes mark the crossing")
    assert app.indexer.step() == 0
    t0 = time.monotonic()
    res = app.memory.search(NS, "zebra")
    assert time.monotonic() - t0 < 0.5 and res["ranking"] == "fts" and res["hits"]
    assert app.health()["embeddings"]["encoder"] == "offline" and app.health()["status"] == "ok"


def test_a_slow_query_embedding_is_cut_off_at_the_timeout(tmp_path, enc):
    app = srv.App(options(enc[0], embed_timeout_s=0.3), str(tmp_path))
    put(app, "Zebra crossing", "zebra stripes mark the crossing")
    app.indexer.step()
    FakeEncoder.delay = 2.0
    t0 = time.monotonic()
    res = app.memory.search(NS, "zebra")
    elapsed = time.monotonic() - t0
    assert res["ranking"] == "fts" and res["hits"]
    assert 0.25 <= elapsed < 1.0, f"waited {elapsed:.2f} s against a 0.3 s timeout"
    asked = len(FakeEncoder.seen)
    assert app.memory.search(NS, "zebra")["ranking"] == "fts"
    assert len(FakeEncoder.seen) == asked, "down until the next probe: no second request"
    FakeEncoder.delay = 0.0
    app.indexer.step()  # the probe brings it back
    assert app.memory.search(NS, "zebra")["ranking"] == "cosine"


def test_the_timeout_bounds_the_whole_reply_not_each_read(tmp_path, enc):
    """A reply that keeps arriving never trips a socket read timeout; the search still stops waiting."""
    app = srv.App(options(enc[0], embed_timeout_s=0.3), str(tmp_path))
    put(app, "Zebra crossing", "zebra stripes mark the crossing")
    app.indexer.step()
    FakeEncoder.trickle = 2.0
    t0 = time.monotonic()
    res = app.memory.search(NS, "zebra")
    elapsed = time.monotonic() - t0
    assert res["ranking"] == "fts" and res["hits"]
    assert elapsed < 1.0, f"waited {elapsed:.2f} s against a 0.3 s timeout"


def test_an_edited_record_is_stale_until_it_is_embedded_again(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    rid = put(app, "Zebra fact", "zebra one")
    put(app, "Other", "unrelated words")
    app.indexer.step()
    before = vectors(app)[rid]
    assert app.memory.search(NS, "zebra")["ranking"] == "cosine"
    app.store.changed.clear()
    app.memory.update(rid, {"content": "zebra two, edited"}, "desk", app.store.get(rid)["revision"])
    assert app.store.changed.is_set(), "an edit wakes the worker"
    assert app.memory.search(NS, "zebra")["ranking"] == "fts", "a stale vector is not ranked"
    assert [p[0] for p in app.indexer.pending(app.encoder.identity)] == [rid]
    assert app.indexer.step() == 1
    after = vectors(app)[rid]
    assert after["digest"] != before["digest"] and after["vector"] != before["vector"]
    assert app.memory.search(NS, "zebra")["ranking"] == "cosine"
    app.memory.update(rid, {"confidence": 0.9}, "desk", app.store.get(rid)["revision"])
    assert app.indexer.pending(app.encoder.identity) == [], "a change that leaves the text alone leaves the vector current"


def test_a_new_snapshot_or_model_makes_every_vector_stale(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    ids = {put(app, f"Fact {i}", f"zebra fact {i}") for i in range(3)}
    assert app.indexer.step() == 3
    FakeEncoder.root = ROOT_PATH.replace(SNAP, "ffff0000" * 5)
    assert app.encoder.probe() and app.encoder.identity == (MODEL, "ffff0000" * 5)
    assert app.memory.search(NS, "zebra")["ranking"] == "fts", "vectors from the old snapshot are not ranked against the new one"
    assert app.indexer.step() == 3
    assert {r["model"] for r in vectors(app).values()} == {f"{MODEL}@ffff0000ffff"}
    assert app.memory.search(NS, "zebra")["ranking"] == "cosine"
    other = srv.App(options(enc[0], embed_model="other-emb"), str(tmp_path))
    other.encoder.probe()
    assert {p[0] for p in other.indexer.pending(other.encoder.identity)} == ids, "another model: every vector stale"


def test_forget_and_purge_drop_the_vector(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    a, b, c, d = (put(app, f"Fact {i}", f"zebra fact {i}") for i in range(4))
    app.indexer.step()
    app.memory.forget(a, "desk")
    assert a not in vectors(app)
    assert app.store.put_embedding(a, "late", "m", bytes(8)) == 0 and a not in vectors(app), "a vector that lands after the forget is refused"
    app.store.forget(b, "desk", hard=True)
    assert b not in vectors(app)
    # A record deleted without its vector (as before 0.9.0) loses it at the next purge.
    app.store.db.execute("update memories set deleted_at='2000-01-01T00:00:00+00:00', status='deleted' where id=?", (c,))
    assert c in vectors(app)
    app.store.purge(30)
    assert set(vectors(app)) == {d}


def test_cosine_reaches_exactly_the_records_fts_can(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    put(app, "Scope fact", "zebra in scope")
    put(app, "Scope step", "zebra next step", type="next-step")
    app.store.store({"namespace": NS, "title": "Candidate", "content": "zebra candidate", "status": "candidate"}, "desk")
    old = put(app, "Old", "zebra superseded")
    put(app, "New", "zebra replacement", supersedes=old)
    app.store.forget(put(app, "Gone", "zebra forgotten"), "desk")
    put(app, "Expired", "zebra expired", expires_at="2000-01-01T00:00:00+00:00")
    put(app, "Other repo", "zebra in another repository", namespace=OTHER)
    put(app, "Global", "zebra everywhere", namespace="global")
    put(app, "Topic", "zebra topic", namespace="topics/networking")
    put(app, "Elsewhere", "zebra elsewhere", namespace="gitea/heidrickla/web-access")
    app.indexer.step()
    cases = [
        ([NS, "global"], ("active",), (), ()),
        ([NS, "global"], ("active", "candidate"), (), ()),
        ([NS, "global"], ("active",), (), (OTHER,)),
        ([NS, "topics/networking"], ("active",), ("constraint",), ()),
        ([OTHER], ("active",), (), (NS, "global")),
    ]
    for scopes, statuses, types, reach in cases:
        fts = app.store.search("zebra", scopes, limit=50, statuses=statuses, types=types, reach=reach)
        hits, ranking, _ = embed.retrieve(app.store, app.encoder, "zebra", scopes, 50, statuses, types, reach)
        assert ranking == "cosine" and fts
        assert {h["id"] for h in hits} == {h["id"] for h in fts}, (scopes, statuses, types, reach)


def test_a_hub_search_reaches_other_repositories_by_cosine_too(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    put(app, "Loops", "the loops add-on runs the nightly checks", namespace=OTHER)
    app.indexer.step()
    res = app.memory.search(NS, "when do the nightly checks run")
    assert res["ranking"] == "cosine" and res["reach"] == [OTHER]
    assert [h["namespace"] for h in res["hits"]] == [OTHER] and res["hits"][0]["why"].startswith("reach ")


def test_a_query_carrying_a_secret_is_not_sent_to_the_encoder(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    put(app, "Zebra", "zebra facts")
    app.indexer.step()
    asked = len(FakeEncoder.seen)
    assert app.memory.search(NS, "password=hunter2hunter2")["ranking"] == "fts"
    assert len(FakeEncoder.seen) == asked
    assert app.memory.search(NS, "zebra")["ranking"] == "cosine" and len(FakeEncoder.seen) == asked + 1, "control"


def test_the_demand_signal_counts_fts_hits_whichever_ranking_answered(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    for i in range(3):
        put(app, f"Fact {i}", f"unrelated fact {i}")
    app.indexer.step()
    res = app.memory.search(NS, "zebra stripes", client="desk")
    assert res["ranking"] == "cosine" and len(res["hits"]) == 3
    row = app.store.db.execute("select hits, top_rank from queries order by at desc limit 1").fetchone()
    assert row["hits"] == 0 and row["top_rank"] == 0.0, "no record answers it, so it still counts as answered badly"


def test_a_record_the_encoder_refuses_keeps_its_scopes_on_fts(tmp_path, enc):
    app = srv.App(options(enc[0]), str(tmp_path))
    put(app, "Fine", "zebra fine")
    bad = put(app, "Refused", "zebra refused")
    FakeEncoder.refuse = {"Refused\nzebra refused"}
    assert app.indexer.step() == 1 and app.health()["embeddings"]["refused"] == 1
    assert bad not in vectors(app)
    assert app.memory.search(NS, "zebra")["ranking"] == "fts", "an unembedded record would be invisible to cosine"
    assert app.indexer.step() == 0 and FakeEncoder.seen.count("Refused\nzebra refused") == 1, "not retried under the same model"


def test_the_snapshot_is_read_through_llama_swaps_passthrough(enc):
    FakeEncoder.list_root = False
    e = embed.Encoder(enc[0], MODEL)
    assert e.probe() and e.identity == (MODEL, SNAP)
    assert not embed.Encoder(enc[0], "not-served").probe(), "a model the server does not list"
    assert not embed.Encoder(enc[0].removesuffix("/v1"), MODEL).probe(), "control: no /v1 base, no passthrough"


def test_off_without_embed_url(tmp_path):
    app = srv.App(options(None), str(tmp_path))
    put(app, "Zebra", "zebra facts")
    assert app.encoder is None and app.indexer is None
    assert app.health()["embeddings"] == {"encoder": "off"}
    assert app.memory.search(NS, "zebra")["ranking"] == "fts"


def test_vectors_round_trip_as_float32_exactly():
    v = [float(array.array("f", [x])[0]) for x in (0.1, -0.25, 0.5, 0.7)]
    assert list(embed.unpack(embed.pack(v))) == v
    assert embed.unit([1.0005, 0.0]) == [1.0005, 0.0], "a vector within 1e-3 of unit length is kept as sent"
    assert embed.unit([3.0, 4.0]) == [0.6, 0.8]
    with pytest.raises(ValueError):
        embed.unit([0.0, 0.0])
