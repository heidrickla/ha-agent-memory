"""Offline retrieval evaluation: the live records in a scratch store built by the repo's own code,
the frozen query set, and the rank of each query's target record.

  python fetch.py                    # records.json from the service's get route, which logs nothing
  python eval.py <checkout> [label] [-v]
  python eval.py <checkout> [label] --hub <namespace>
  python eval.py <checkout> [label] --embeddings embeddings.json [--hub <namespace>]

Query classes: K uses the record's own words, M other forms of them, P a paraphrase sharing none
of its distinctive words. queries.json is frozen by queries.sha256; a changed set is refused, so a
result cannot be tuned by editing the questions. Scopes mimic the service: the target's namespace,
then global. Nothing touches the live search route, so no test query lands in the demand log.

--hub asks every query from one namespace instead, scopes hub then global, once without reach and
once reaching every other repository namespace, split by where the target lives.

--embeddings ranks every query a second time through the checkout's embedding path
(`agent_memory/embed.py`: the worker's backfill into the embeddings table, then `retrieve`), with
vectors from the cache `embed_eval.py` wrote. The production client builds every request and the
cache answers it, so nothing leaves the machine. A text the cache does not hold stops the run:
production would be embedding a text the offline test never measured.
"""

import hashlib
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
repo = pathlib.Path(sys.argv[1])
hub = sys.argv[sys.argv.index("--hub") + 1] if "--hub" in sys.argv else None
emb_file = sys.argv[sys.argv.index("--embeddings") + 1] if "--embeddings" in sys.argv else None
label = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else repo.name
sys.path.insert(0, str(repo))
from agent_memory.store import Store  # noqa: E402

FROZEN = (HERE / "queries.sha256").read_text().split()[0]
if hashlib.sha256((HERE / "queries.json").read_bytes()).hexdigest() != FROZEN:
    sys.exit("queries.json changed after it was frozen")

records = json.loads((HERE / "records.json").read_text(encoding="utf-8"))
queries = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))
ns_of = {r["id"][:8]: r["namespace"] for r in records}
STATUSES = ("active", "candidate")
# The encoder the offline test measured (embed_eval.py's MODEL and SNAPSHOT).
EMB_MODEL, EMB_SNAPSHOT = "qwen3-emb-0.6b", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"


def cached_encoder():
    from agent_memory import embed

    cache = json.loads(pathlib.Path(emb_file).read_text())

    class CachedEncoder(embed.Encoder):
        def _get(self, url, timeout):
            return {"data": [{"id": EMB_MODEL, "root": f"models--Qwen--Qwen3-Embedding-0.6B/snapshots/{EMB_SNAPSHOT}"}]}

        def _post(self, url, body, timeout):
            text = body["input"][0]
            key = hashlib.sha256(f"{EMB_MODEL}|{EMB_SNAPSHOT}|{text}".encode()).hexdigest()
            if key not in cache:
                sys.exit(f"production embeds a text the offline test did not measure: {text[:80]!r}")
            return {"data": [{"embedding": cache[key]["v"]}]}

    return embed, CachedEncoder("http://cache.invalid/v1", EMB_MODEL, timeout_s=60)


def position(hits: list, target: str) -> int | None:
    ids = [h["id"][:8] for h in hits]
    return ids.index(target) + 1 if target in ids else None


with tempfile.TemporaryDirectory() as tmp:
    store = Store(str(pathlib.Path(tmp) / "memory.db"))
    for r in records:
        store.store(r, "eval")
    configs = ["fts"]
    if emb_file:
        embed, encoder = cached_encoder()
        indexer = embed.Indexer(store, encoder)
        while indexer.step():
            pass
        state = indexer.state()
        if state["current"] != state["records"]:
            sys.exit(f"backfill left {state['records'] - state['current']} of {state['records']} records without a vector")
        configs.append("prod")

    def ranked(q: str, scopes: list, reach: tuple) -> dict:
        # Candidates included: this measures ranking, and 14 of the 24 targets are unpromoted
        # candidates that default search hides whatever the ranking.
        out = {"fts": store.search(q, scopes, limit=20, statuses=STATUSES, reach=reach)}
        if emb_file:
            hits, ranking, _ = embed.retrieve(store, encoder, q, scopes, 20, STATUSES, (), reach)
            if ranking != "rrf":
                sys.exit(f"production fell back to FTS for {q!r}")
            out["prod"] = hits
        return out

    results: dict[tuple[str, str], list] = {}
    hub_runs: dict[tuple[str, str, str], list] = {}
    reach_all = tuple(sorted({n for n in ns_of.values() if n.split("/")[0] in ("gitea", "github") and n != hub}))
    for q in queries:
        if hub:
            scopes = [hub, "global"]
            where = "hub" if ns_of[q["target"]] in scopes else "other"
            for variant, extra in (("scopes", ()), ("reach", reach_all)):
                for config, hits in ranked(q["q"], scopes, extra).items():
                    hub_runs.setdefault((config, where, variant), []).append((position(hits, q["target"]), len(hits), q["q"]))
            continue
        scopes = [ns_of[q["target"]]] + (["global"] if ns_of[q["target"]] != "global" else [])
        for config, hits in ranked(q["q"], scopes, ()).items():
            results.setdefault((config, q["class"]), []).append((position(hits, q["target"]), len(hits), q["q"]))
    store.db.close()


def line(name: str, rows: list, width: int = 6) -> str:
    n = len(rows)
    h1 = sum(1 for r, _, _ in rows if r == 1) / n
    h3 = sum(1 for r, _, _ in rows if r and r <= 3) / n
    mrr = sum(1 / r for r, _, _ in rows if r) / n
    miss = sum(1 for r, _, _ in rows if r is None)
    mean_hits = sum(h for _, h, _ in rows) / n
    return f"{name:<{width}}{n:>4}{h1:>8.2f}{h3:>8.2f}{mrr:>8.2f}{miss:>6}{mean_hits:>11.1f}"


TITLES = {"fts": "", "prod": ", production ranking (FTS5 fused with cosine) through agent_memory/embed.py"}
if hub:
    for config in configs:
        print(f"== {label}{TITLES[config]}, asked from {hub}; targets in the hub's scopes (hub) or elsewhere (other)")
        print(f"{'set':<14}{'n':>4}{'hit@1':>8}{'hit@3':>8}{'MRR':>8}{'miss':>6}{'mean hits':>11}")
        for key in (("hub", "scopes"), ("hub", "reach"), ("other", "scopes"), ("other", "reach")):
            print(line(f"{key[0]} {key[1]}", hub_runs.get((config, *key), []), 14))
    sys.exit(0)

for config in configs:
    print(f"== {label}{TITLES[config]}")
    print(f"{'class':<6}{'n':>4}{'hit@1':>8}{'hit@3':>8}{'MRR':>8}{'miss':>6}{'mean hits':>11}")
    total = []
    for cls in ("K", "M", "P"):
        total += results.get((config, cls), [])
        print(line(cls, results.get((config, cls), [])))
    print(line("all", total))
    if "-v" in sys.argv:
        for cls in ("K", "M", "P"):
            for r, h, q in results.get((config, cls), []):
                if r != 1:
                    print(f"  {cls} rank={r} hits={h}  {q}")
