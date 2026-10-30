"""The nightly candidate review scored against the reviews agents already made.

  python review_eval.py <checkout> <memory.sqlite> --llm URL --model NAME --embed-url URL [--out FILE] [-v]

Method, fixed before any result (2026-10-02):

| | |
|---|---|
| promoted | an extracted record (`enrichment` extract-*) now active, or a session summary an agent promoted (an audit `update` naming `status`, now active) |
| forgotten | an extracted record or a session summary now deleted |
| Store | a scratch copy of the database; every labelled record is a candidate again, written when it was; the rest keep their status; vectors from the live encoder |
| Verdicts | the checkout's `review.Reviewer.decide`, which changes nothing |
| Decision | ship when no promoted record gets a forget verdict; report the share of forgotten records that do |

The database is a copy (an add-on backup's `data/memory.sqlite`); this script never opens the original.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import tempfile
import time

import yaml

ap = argparse.ArgumentParser()
ap.add_argument("checkout")
ap.add_argument("db")
ap.add_argument("--llm", required=True, help="OpenAI-style /v1 base URL of the review model")
ap.add_argument("--model", required=True)
ap.add_argument("--embed-url", required=True, help="OpenAI-style /v1 base URL of the encoder")
ap.add_argument("--out")
ap.add_argument("-v", action="store_true")
args = ap.parse_args()

src = pathlib.Path(args.checkout) / "agent_memory"
sys.path.insert(0, str(src))
import embed  # noqa: E402
import enrich  # noqa: E402
import mcp  # noqa: E402
import review  # noqa: E402
from store import Store  # noqa: E402

work = pathlib.Path(tempfile.mkdtemp(prefix="review-eval-"))
for name in ("memory.sqlite", "memory.sqlite-wal", "memory.sqlite-shm"):
    p = pathlib.Path(args.db).with_name(name)
    if p.exists():
        shutil.copy(p, work / name)
if not (work / "memory.sqlite").exists():
    sys.exit(f"no database at {args.db}")
store = Store(str(work / "memory.sqlite"))

rows = store.db.execute("select id, status, type, enrichment from memories where (enrichment like 'extract%' or type='session-summary') and status in ('active','deleted')").fetchall()
promoted_summaries = {r[0] for r in store.db.execute("select distinct record_id from audit where action='update' and detail like '%status%'")}
labels = {}
for r in rows:
    extracted = (r["enrichment"] or "").startswith("extract")
    if r["status"] == "deleted":
        labels[r["id"]] = "forgotten"
    elif extracted or r["id"] in promoted_summaries:
        labels[r["id"]] = "promoted"
if not labels:
    sys.exit("no labelled records: is this the memory database?")
with store.transaction():
    for i in labels:
        store.db.execute("update memories set status='candidate', deleted_at=null where id=?", (i,))

options = yaml.safe_load((src / "config.yaml").read_text(encoding="utf-8"))["options"]
memory = mcp.Memory(store, options)
encoder = embed.Encoder(args.embed_url, embed.DEFAULT_MODEL)
if not encoder.probe():
    sys.exit(f"encoder at {args.embed_url} did not answer: {encoder.error}")
indexer = embed.Indexer(store, encoder)
while indexer.step():
    pass
state = indexer.state()
if state["current"] != state["records"]:
    sys.exit(f"backfill left {state['records'] - state['current']} of {state['records']} records without a vector")
worker = enrich.Worker(store, {"ollama_url": args.llm, "ollama_model": args.model, "ollama_timeout_s": 120})
if not worker.ollama.available():
    sys.exit(f"model server at {args.llm} did not answer")
reviewer = review.Reviewer(store, worker, memory, encoder)

results = []
t0 = time.monotonic()
for rid, label in sorted(labels.items(), key=lambda kv: store.get(kv[0])["created_at"]):
    cand = store.get(rid)
    t = time.monotonic()
    try:
        d = reviewer.decide(cand)
    except Exception as err:  # a failed call is a kept candidate in production; count it here
        d = {"id": rid, "verdict": "failed", "record": "", "reason": f"{type(err).__name__}: {err}", "shown": []}
    d.update(label=label, type=cand["type"], kind="summary" if cand["type"] == "session-summary" else "extracted", title=cand["title"], seconds=round(time.monotonic() - t, 2))
    results.append(d)
    if args.v:
        print(f"{label:<9} {d['verdict']:<13} {rid[:8]} {cand['title'][:70]} | {d['reason'][:120]}")

print(f"{len(results)} labelled records, {state['records']} records embedded, {time.monotonic() - t0:.0f} s, model {args.model}")
print(f"{'kind':<10}{'label':<11}{'n':>4}{'duplicate':>11}{'session':>9}{'keep':>6}{'failed':>8}")
for kind in ("extracted", "summary"):
    for label in ("promoted", "forgotten"):
        sel = [r for r in results if r["kind"] == kind and r["label"] == label]
        if sel:
            n = {v: sum(1 for r in sel if r["verdict"] == v) for v in ("duplicate", "session-state", "keep", "failed")}
            print(f"{kind:<10}{label:<11}{len(sel):>4}{n['duplicate']:>11}{n['session-state']:>9}{n['keep']:>6}{n['failed']:>8}")
wrong = [r for r in results if r["label"] == "promoted" and r["verdict"] in review.FORGETS]
caught = [r for r in results if r["label"] == "forgotten" and r["verdict"] in review.FORGETS]
forgotten = [r for r in results if r["label"] == "forgotten"]
print(f"promoted given a forget verdict: {len(wrong)}; forgotten given one: {len(caught)} of {len(forgotten)}")
for r in wrong:
    print(f"  WRONG {r['id'][:8]} {r['verdict']} {r['record'][:8]} {r['title'][:70]} | {r['reason'][:160]}")
print("decision:", "ship" if not wrong else "do not ship")
if args.out:
    pathlib.Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
store.db.close()
shutil.rmtree(work, ignore_errors=True)
