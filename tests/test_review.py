"""The nightly candidate review: forget only, and only what the guards allow.

The model is a scripted fake behind the worker's client; each test names the verdict it returns. What
is pinned: a duplicate must be a record shown, hold every specific the candidate states and not be a
session summary standing in for another type; a lasting type is never session state; a candidate
edited, promoted or forgotten during the call is left alone; a failed call keeps the candidate; the
route needs the delete scope; the run's summary reaches `/health` and survives a restart.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import datetime as dt

import review
import server as srv
from enrich import PROMPT_VERSION, OpenAIChat

NS = "gitea/heidrickla/ha-management"


class FakeJudge:
    """The worker's client: `judge` returns what `script(candidate_title, user_text)` gives."""

    api = "openai"
    model = "memory-model"
    base = "http://fake/v1"

    def __init__(self, script, up: bool = True):
        self.script = script
        self.up = up
        self.calls: list[str] = []

    def available(self) -> bool:
        return self.up

    def judge(self, system, user, schema, max_tokens, model=""):
        self.calls.append(user)
        title = user.splitlines()[1]
        out = self.script(title, user)
        if isinstance(out, Exception):
            raise out
        return out


def verdict(v: str, record: str = "", reason: str = "because") -> str:
    return json.dumps({"verdict": v, "record": record, "reason": reason})


def options(**over) -> dict:
    o = {
        "clients": [{"name": "desk", "ip": "127.0.0.1", "scopes": ["read", "write", "delete"]}],
        "github_owner": "heidrickla",
        "forge_owner": "heidrickla",
        "forge_hosts": [],
        "project_links": [],
        "hubs": [NS],
    }
    o.update(over)
    return o


def app_with(tmp_path, script, up: bool = True, **over):
    app = srv.App(options(**over), str(tmp_path))
    app.worker.ollama = FakeJudge(script, up)
    return app


def put(app, title: str, content: str, **kw) -> str:
    return app.store.store({"namespace": NS, "type": "repository-fact", "title": title, "content": content, **kw}, "desk")["id"]


def candidate(app, title: str, content: str, typ: str = "repository-fact", at: str | None = None) -> str:
    rid = app.store.store_candidate({"namespace": NS, "type": typ, "title": title, "content": content}, client="enrichment")["id"]
    if at:
        app.store.db.execute("update memories set created_at=? where id=?", (at, rid))
    return rid


def status(app, rid: str) -> str:
    return app.store.db.execute("select status from memories where id=?", (rid,)).fetchone()["status"]


def test_a_duplicate_of_an_active_record_is_forgotten_with_its_reason(tmp_path):
    held = None
    app = app_with(tmp_path, lambda title, user: verdict("duplicate", held[:8], "same fact"))
    held = put(app, "Encoder card", "The encoder runs on card one of the rack.")
    c = candidate(app, "Encoder placement", "The encoder runs on card one.")
    out = app.reviewer.run()
    assert status(app, c) == "deleted" and out["forgotten"] == {"duplicate": 1, "session-state": 0} and out["kept"] == 0
    row = app.store.db.execute("select client, action, detail from audit where record_id=? and action='forget'", (c,)).fetchone()
    assert row["client"] == "review" and row["detail"].startswith(f"review-v1 duplicate of {held[:8]}, model memory-model at http://fake/v1: same fact")
    assert c not in {r["record_id"] for r in app.store.db.execute("select record_id from embeddings")}


def test_a_candidate_adding_a_specific_the_record_lacks_is_kept(tmp_path):
    held = None
    app = app_with(tmp_path, lambda title, user: verdict("duplicate", held[:8]))
    held = put(app, "Extraction schema", "Extraction asks for a json_schema because the fork ignores json_object.")
    c = candidate(app, "Fork ignores json_object", "The fork ignores json_object; 11 of 12 replies came fenced and 0.8.1 asks for json_schema.")
    d = app.reviewer.decide(app.store.get(c))
    assert d["verdict"] == "keep" and "record lacks 0.8.1, 11, 12" in d["reason"]
    assert app.reviewer.run()["kept"] == 1 and status(app, c) == "candidate"


def test_a_quoted_phrase_counts_as_a_specific():
    assert review.specifics('fails with "expected a URL" at /local_apps/x and parse_candidates in enrich.py, v0.8.1') == {"expected a url", "local_apps/x", "parse_candidates", "enrich.py", "v0.8.1"}
    assert review.missing_specifics({"title": "t", "content": 'fails with "expected a URL"'}, {"title": "u", "content": "an empty url fails"}) == ["expected a url"]


def test_a_session_summary_does_not_hold_another_types_fact(tmp_path):
    held = None
    app = app_with(tmp_path, lambda title, user: verdict("duplicate", held[:8]))
    held = put(app, "Session needle-validation", "Tested cactus-needle for the extract job and refused it.", type="session-summary")
    c = candidate(app, "Cactus-needle refused", "Tested cactus-needle for the extract job and refused it.", typ="failure-and-fix")
    d = app.reviewer.decide(app.store.get(c))
    assert d["verdict"] == "keep" and d["reason"].startswith("named a session summary")
    s = candidate(app, "Session needle-validation again", "Tested cactus-needle for the extract job and refused it.", typ="session-summary")
    assert app.reviewer.decide(app.store.get(s))["verdict"] == "duplicate", "control: a summary can repeat a summary"


def test_session_state_forgets_but_never_a_lasting_type(tmp_path):
    app = app_with(tmp_path, lambda title, user: verdict("session-state"))
    gone = candidate(app, "Backup taken", "A backup was taken before the install.", typ="implementation-change")
    kept = {candidate(app, f"Rule {t}", "Never write to the Brain.", typ=t) for t in review.LASTING}
    out = app.reviewer.run()
    assert status(app, gone) == "deleted" and out["forgotten"]["session-state"] == 1
    assert all(status(app, k) == "candidate" for k in kept) and out["kept"] == len(review.LASTING)


def test_an_extracted_records_lasting_type_does_not_protect_it(tmp_path):
    """Extraction's type is the model's guess: on 2026-10-20 agents forgot 102 candidates the nightly
    run had kept, 89 of them extracted and 39 of those under a lasting type. The guard holds only for
    a lasting type an agent wrote."""
    app = app_with(tmp_path, lambda title, user: verdict("session-state"))
    extracted = {candidate(app, f"Pushed {t}", "The branch was pushed and CI run 15 is green.", typ=t) for t in review.LASTING}
    for rid in extracted:
        app.store.db.execute("update memories set enrichment=? where id=?", (f"{PROMPT_VERSION}:job-1", rid))
    written = candidate(app, "Rule by an agent", "Never write to the Brain.", typ="constraint")
    out = app.reviewer.run()
    assert all(status(app, rid) == "deleted" for rid in extracted) and out["forgotten"]["session-state"] == len(review.LASTING)
    assert status(app, written) == "candidate" and out["kept"] == 1


def test_a_record_that_was_not_shown_cannot_be_the_duplicate(tmp_path):
    app = app_with(tmp_path, lambda title, user: verdict("duplicate", "deadbeef"))
    put(app, "Encoder card", "The encoder runs on card one.")
    c = candidate(app, "Encoder placement", "The encoder runs on card one.")
    assert app.reviewer.run()["kept"] == 1 and status(app, c) == "candidate"


def test_of_two_duplicate_candidates_the_older_is_kept(tmp_path):
    app = app_with(tmp_path, lambda title, user: verdict("keep"))
    older = candidate(app, "Encoder card", "The encoder runs on card one.", at="2026-09-01T00:00:00+00:00")
    newer = candidate(app, "Encoder card again", "The encoder runs on card one.", at="2026-09-02T00:00:00+00:00")
    shown_for = {rid: [n["id"] for n in review.neighbours(app.store, None, app.memory, app.store.get(rid))] for rid in (older, newer)}
    assert newer not in shown_for[older] and older in shown_for[newer]


def test_a_candidate_changed_during_the_call_is_left_alone(tmp_path):
    held = None
    ids: dict = {}

    def script(title, user):
        rid = ids[title]
        if title.startswith("Edited"):
            app.memory.update(rid, {"content": "The encoder runs on card one, edited."}, "desk", app.store.get(rid)["revision"])
        elif title.startswith("Promoted"):
            app.memory.update(rid, {"status": "active"}, "desk", app.store.get(rid)["revision"])
        else:
            app.store.forget(rid, "desk")
        return verdict("duplicate", held[:8])

    app = app_with(tmp_path, script)
    held = put(app, "Encoder card", "The encoder runs on card one, edited.")
    for t in ("Edited", "Promoted", "Forgotten"):
        ids[t] = candidate(app, t, "The encoder runs on card one, edited.")
    out = app.reviewer.run()
    assert out["forgotten"]["duplicate"] == 0 and out["kept"] == 3
    assert status(app, ids["Edited"]) == "candidate" and status(app, ids["Promoted"]) == "active"
    assert app.store.db.execute("select count(*) from audit where client='review' and action='forget'").fetchone()[0] == 0


def test_a_failed_reply_keeps_the_candidate_and_counts_it(tmp_path):
    app = app_with(tmp_path, lambda title, user: "not json")
    c = candidate(app, "Backup taken", "A backup was taken.", typ="implementation-change")
    out = app.reviewer.run()
    assert out["failed"] == 1 and out["reviewed"] == 0 and status(app, c) == "candidate"


def test_an_unavailable_model_reviews_nothing(tmp_path):
    app = app_with(tmp_path, lambda title, user: verdict("session-state"), up=False)
    c = candidate(app, "Backup taken", "A backup was taken.", typ="implementation-change")
    out = app.reviewer.run()
    assert out["error"] == "model server unavailable" and out["reviewed"] == 0 and status(app, c) == "candidate"
    assert app.worker.ollama.calls == []


def days_ago(n: float) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=n)).isoformat(timespec="seconds")


def extracted_candidate(app, title: str, at: str) -> str:
    rid = candidate(app, title, f"{title} is a fact.", at=at)
    app.store.db.execute("update memories set enrichment=? where id=?", (f"{PROMPT_VERSION}:job-1", rid))
    return rid


def test_an_extracted_candidate_past_the_expiry_is_forgotten_before_the_verdicts(tmp_path):
    """No session promoted any of the 24 extracted candidates reviewed on 2026-10-21."""
    app = app_with(tmp_path, lambda title, user: verdict("keep"))
    old = extracted_candidate(app, "Old extract", days_ago(15))
    young = extracted_candidate(app, "Young extract", days_ago(13))
    written = candidate(app, "Agent rule", "Never write to the Brain.", at=days_ago(30))
    out = app.reviewer.run()
    assert out["expired"] == 1 and status(app, old) == "deleted"
    assert status(app, young) == "candidate" and status(app, written) == "candidate" and out["kept"] == 2
    assert not any("Old extract" in u for u in app.worker.ollama.calls)
    row = app.store.db.execute("select detail from audit where record_id=? and action='forget'", (old,)).fetchone()
    assert row["detail"].startswith(f"{review.REVIEW_VERSION} expired: extracted ") and "14 days" in row["detail"]


def test_expiry_runs_without_a_live_model(tmp_path):
    app = app_with(tmp_path, lambda title, user: verdict("keep"), up=False)
    old = extracted_candidate(app, "Old extract", days_ago(20))
    out = app.reviewer.run()
    assert out["error"] == "model server unavailable" and out["expired"] == 1 and status(app, old) == "deleted"


def test_candidate_expiry_days_defaults_to_14_and_0_turns_it_off(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert app_with(tmp_path / "a", lambda title, user: verdict("keep")).reviewer.expiry_days == 14
    app = app_with(tmp_path / "b", lambda title, user: verdict("keep"), up=False, candidate_expiry_days=0)
    old = extracted_candidate(app, "Old extract", days_ago(200))
    out = app.reviewer.run()
    assert out["expired"] == 0 and status(app, old) == "candidate"


def test_the_route_needs_the_delete_scope_and_health_carries_the_run(tmp_path):
    token = "desk-token-for-tests-0123456789"
    desk = {"name": "desk", "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "scopes": ["read", "write"]}
    app = app_with(tmp_path, lambda title, user: verdict("session-state"), clients=[desk])
    c = candidate(app, "Backup taken", "A backup was taken.", typ="implementation-change")
    http = srv.serve(app, 0, "127.0.0.1")
    base = f"http://127.0.0.1:{http.server_address[1]}"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    try:
        req = urllib.request.Request(f"{base}/api/review", data=b"{}", method="POST", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 403 and status(app, c) == "candidate"
        app.auth.clients[0]["scopes"].append("delete")
        with urllib.request.urlopen(urllib.request.Request(f"{base}/api/review", data=b"{}", method="POST", headers=headers), timeout=10) as resp:
            out = json.load(resp)
        assert out["forgotten"]["session-state"] == 1 and status(app, c) == "deleted"
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            block = json.load(resp)["candidate_review"]
        assert block["at"] == out["at"] and block["review_at"] == "03:00" and block["forgotten"]["session-state"] == 1
    finally:
        http.shutdown()
        http.server_close()
    again = srv.App(options(), str(tmp_path))
    assert again.health()["candidate_review"]["at"] == out["at"], "the last run is read back from the audit after a restart"


def test_review_at_defaults_and_switches_off(tmp_path, monkeypatch):
    assert srv.review_at({}) == "03:00" and srv.review_at({"review_at": "04:15"}) == "04:15" and srv.review_at({"review_at": "off"}) == "off"
    asked: list[str] = []
    monkeypatch.setattr(review, "seconds_until", lambda at, now=None: asked.append(at) or 3600.0)
    for value, expect in (("off", []), ("04:15", ["04:15"])):
        app = app_with(tmp_path, lambda title, user: verdict("keep"), review_at=value)
        stop = threading.Event()
        t = threading.Thread(target=srv.candidate_reviews, args=(app, stop), daemon=True)
        t.start()
        t.join(0.5)
        stop.set()
        t.join(2.0)
        assert asked == expect and not t.is_alive(), value
        asked.clear()


def test_seconds_until_the_next_time_of_day():
    now = dt.datetime(2026, 10, 2, 2, 0, 0)
    assert review.seconds_until("03:00", now) == 3600
    assert review.seconds_until("02:00", now) == 86400, "the same minute waits a day"
    assert review.seconds_until("01:30", now) == 23.5 * 3600


class FakeV1(BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        FakeV1.seen.append(body)
        out = json.dumps({"choices": [{"message": {"content": verdict("keep")}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def test_the_openai_judge_request_carries_the_review_schema_with_thinking_off():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeV1)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeV1.seen = []
    try:
        client = OpenAIChat(f"http://127.0.0.1:{server.server_address[1]}/v1", "memory-model")
        assert json.loads(client.judge("sys", "user", review.SCHEMA, review.REVIEW_TOKENS))["verdict"] == "keep"
        body = FakeV1.seen[0]
        assert body["response_format"]["json_schema"]["schema"] == review.SCHEMA and body["max_tokens"] == review.REVIEW_TOKENS
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        client.extract(NS, "notes")
        assert FakeV1.seen[1]["response_format"]["json_schema"]["name"] == "memories", "the extraction request is unchanged"
    finally:
        server.shutdown()
        server.server_close()
