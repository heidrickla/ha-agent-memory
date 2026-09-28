"""Embedding retrieval against the frozen eval: FTS5 alone, cosine alone, and two fusions of them.

  python embed_eval.py <checkout> [--url URL] [--cache FILE] [--hub NAMESPACE]... [-v]

Method, fixed before any result (2026-09-28):

| Config | Ranking |
|---|---|
| a fts | `Store.search` as shipped: the baseline |
| b cos | cosine over every live record in the searched namespaces |
| c rrf | sum of 1/(60 + rank) over the a and b lists |
| d wsum | 0.5 * a's `rank` + 0.5 * cosine, each min-max normalised over its own list, 0 when absent |

- Encoder: Qwen3-Embedding-0.6B (`qwen3-emb-0.6b`, snapshot 97b0c614) over its OpenAI-style route.
- Query text: the model card's query prompt (`config_sentence_transformers.json` "query") plus the
  query. Record text: title, newline, content, no prompt, as the card says for documents.
- Scopes, statuses, limit 20 and the hub split are eval.py's. Lists are cut to 20 before fusion.
  Ties: higher cosine, then id.
- Bootstrap: paired MRR difference against a, resampling the 24 target records (their K, M and P
  queries travel together), 5000 draws, seed 0, 2.5 and 97.5 percentiles.
- Decision: better when the overall interval is above zero and neither K nor M MRR drops by more
  than 0.02. No weight or k is tuned on these queries.
- Cost: one request per text, so each latency is one text's. Vectors are cached by sha256 of model,
  snapshot and text; latencies are from the run that fetched them.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import pathlib
import random
import statistics
import sys
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).parent
MODEL = "qwen3-emb-0.6b"
SNAPSHOT = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
DIM = 1024
QUERY_PROMPT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
STATUSES = ("active", "candidate")
LIMIT = 20
RRF_K = 60
WEIGHT = 0.5
DRAWS = 5000
CONFIGS = ("a fts", "b cos", "c rrf", "d wsum")
CLASSES = ("K", "M", "P")

dot = math.sumprod  # vectors arrive unit-norm, so this is the cosine


class Encoder:
    def __init__(self, url: str, cache: pathlib.Path):
        self.url, self.cache_path = url, cache
        self.cache = json.loads(cache.read_text()) if cache.exists() else {}
        self.fetched: dict[str, list[float]] = {"record": [], "query": []}
        self.checked = False

    def _post(self, path: str, body: dict | None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def check(self) -> None:
        entry = next((m for m in self._post("/v1/models", None)["data"] if m["id"] == MODEL), None)
        if entry is None or SNAPSHOT not in entry.get("root", ""):
            sys.exit(f"encoder does not serve {MODEL} at snapshot {SNAPSHOT}: {entry}")
        self.checked = True

    def vector(self, text: str, kind: str) -> list[float]:
        key = hashlib.sha256(f"{MODEL}|{SNAPSHOT}|{text}".encode()).hexdigest()
        if key not in self.cache:
            if not self.checked:
                self.check()
            t0 = time.perf_counter()
            reply = self._post("/v1/embeddings", {"model": MODEL, "input": [text], "encoding_format": "float"})
            seconds = time.perf_counter() - t0
            vec = reply["data"][0]["embedding"]
            self.cache[key] = {"kind": kind, "seconds": round(seconds, 5), "tokens": reply["usage"]["prompt_tokens"], "v": vec}
            self.fetched[kind].append(seconds)
        vec = self.cache[key]["v"]
        norm = math.sqrt(dot(vec, vec))
        if len(vec) != DIM or abs(norm - 1.0) > 1e-3:
            sys.exit(f"vector of dim {len(vec)} and norm {norm:.4f}, expected {DIM} and 1.0")
        return vec

    def save(self) -> None:
        self.cache_path.write_text(json.dumps(self.cache))


def minmax(scores: dict[str, float]) -> dict[str, float]:
    lo, hi = min(scores.values()), max(scores.values())
    return {k: (v - lo) / (hi - lo) if hi > lo else 1.0 for k, v in scores.items()}


def rank_all(store, qvec, query, scopes, reach, docs) -> dict[str, list[str]]:
    """The four ranked id lists for one search, each at most LIMIT long."""
    fts = store.search(query, scopes, limit=LIMIT, statuses=STATUSES, reach=reach)
    fts_ids = [h["id"] for h in fts]
    fts_score = {h["id"]: h["rank"] for h in fts}
    spaces = [*scopes, *(n for n in reach if n not in scopes)]
    live = store.by_namespace(spaces, limit=100000, statuses=STATUSES)
    cos = {r["id"]: dot(qvec, docs[r["id"]]) for r in live}

    def order(score: dict[str, float]) -> list[str]:
        return sorted(score, key=lambda i: (-score[i], -cos[i], i))[:LIMIT]

    cos_ids = order(cos)
    rrf: dict[str, float] = {}
    for ids in (fts_ids, cos_ids):
        for n, i in enumerate(ids, 1):
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (RRF_K + n)
    f = minmax(fts_score) if fts_score else {}
    c = minmax({i: cos[i] for i in cos_ids})
    wsum = {i: WEIGHT * f.get(i, 0.0) + (1 - WEIGHT) * c.get(i, 0.0) for i in set(fts_ids) | set(cos_ids)}
    return {"a fts": fts_ids, "b cos": cos_ids, "c rrf": order(rrf), "d wsum": order(wsum)}


def position(ids: list[str], target: str) -> int | None:
    short = [i[:8] for i in ids]
    return short.index(target) + 1 if target in short else None


def rr(r: int | None) -> float:
    return 1.0 / r if r else 0.0


def percentile(sorted_vals: list[float], p: float) -> float:
    pos = p * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def paired_ci(rows: list[dict], config: str) -> tuple[float, float]:
    """95% interval on MRR(config) - MRR(a), resampling targets with their queries."""
    by_target: dict[str, list[float]] = {}
    for row in rows:
        by_target.setdefault(row["target"], []).append(rr(row[config]) - rr(row["a fts"]))
    groups = list(by_target.values())
    rng = random.Random(0)
    diffs = []
    for _ in range(DRAWS):
        pick = [d for _ in groups for d in rng.choice(groups)]
        diffs.append(sum(pick) / len(pick))
    diffs.sort()
    return percentile(diffs, 0.025), percentile(diffs, 0.975)


def summary(rows: list[dict], config: str) -> dict:
    n = len(rows)
    ranks = [row[config] for row in rows]
    out = {
        "n": n,
        "hit1": sum(1 for r in ranks if r == 1) / n,
        "hit3": sum(1 for r in ranks if r and r <= 3) / n,
        "mrr": sum(rr(r) for r in ranks) / n,
        "miss": sum(1 for r in ranks if r is None),
    }
    if config != "a fts":
        out["delta"] = out["mrr"] - summary(rows, "a fts")["mrr"]
        out["ci"] = paired_ci(rows, config)
        out["better_on"] = sum(1 for row in rows if rr(row[config]) > rr(row["a fts"]))
        out["worse_on"] = sum(1 for row in rows if rr(row[config]) < rr(row["a fts"]))
    return out


def table(title: str, sets: list[tuple[str, list[dict]]]) -> list[str]:
    lines = [f"== {title}", f"{'config':<8}{'set':<14}{'n':>4}{'hit@1':>7}{'hit@3':>7}{'MRR':>7}{'miss':>5}  {'dMRR vs a [95% CI]':<26}{'+/-':>7}"]
    for config in CONFIGS:
        for name, rows in sets:
            if not rows:
                continue
            s = summary(rows, config)
            extra = ""
            if "delta" in s:
                extra = f"  {s['delta']:+.3f} [{s['ci'][0]:+.3f}, {s['ci'][1]:+.3f}]    {s['better_on']:>3}/{s['worse_on']:<3}"
            lines.append(f"{config:<8}{name:<14}{s['n']:>4}{s['hit1']:>7.2f}{s['hit3']:>7.2f}{s['mrr']:>7.2f}{s['miss']:>5}{extra}")
    return lines


def decision(rows: list[dict]) -> list[str]:
    lines = ["== decision (default eval): better = overall CI above 0 and K, M MRR each down by at most 0.02"]
    for config in CONFIGS[1:]:
        s = summary(rows, config)
        moves = {c: summary([r for r in rows if r["class"] == c], config)["delta"] for c in CLASSES}
        ok = s["ci"][0] > 0 and moves["K"] >= -0.02 and moves["M"] >= -0.02
        verdict = "better" if ok else "no measured improvement"
        lines.append(f"{config:<8}{verdict:<25} overall {s['delta']:+.3f} [{s['ci'][0]:+.3f}, {s['ci'][1]:+.3f}]  " + "  ".join(f"{c} {moves[c]:+.3f}" for c in CLASSES))
    return lines


def cost(enc: Encoder, docs: dict[str, list[float]], qvecs: list[list[float]], score_times: list[float]) -> list[str]:
    lines = ["== cost"]
    for kind in ("record", "query"):
        entries = [e for e in enc.cache.values() if e["kind"] == kind]
        secs = sorted(e["seconds"] for e in entries)
        toks = [e["tokens"] for e in entries]
        if secs:
            lines.append(
                f"embed {kind:<6} n={len(secs):<3} median {statistics.median(secs) * 1000:.1f} ms  p90 {percentile(secs, 0.9) * 1000:.1f} ms  "
                f"max {secs[-1] * 1000:.1f} ms  tokens median {statistics.median(toks):.0f} max {max(toks)}"
            )
    lines.append(f"fetched this run: {len(enc.fetched['record'])} records, {len(enc.fetched['query'])} queries (latency is from the fetching run)")
    lines.append(f"cosine rank of the in-scope records per default-eval search (2 to 11 of {len(docs)}): median {statistics.median(score_times) * 1e6:.0f} us")
    rng = random.Random(1)
    synthetic = []
    for _ in range(1000):
        v = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
        n = math.sqrt(dot(v, v))
        synthetic.append([x / n for x in v])
    blobs = [array.array("f", v).tobytes() for v in synthetic]
    q = qvecs[0]
    for label, fn in (
        ("dot over 1000 lists", lambda: sorted((dot(q, v) for v in synthetic), reverse=True)[:LIMIT]),
        ("unpack 1000 float32 blobs + dot", lambda: sorted((dot(q, array.array("f", b)) for b in blobs), reverse=True)[:LIMIT]),
    ):
        times = []
        for _ in range(21):
            t0 = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t0)
        lines.append(f"{label}: median {statistics.median(times) * 1000:.2f} ms over 21 runs (math.sumprod)")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("checkout")
    ap.add_argument("--url", default="http://127.0.0.1:18090")
    ap.add_argument("--cache", default=str(HERE / "embeddings.json"))
    ap.add_argument("--hub", action="append", default=[])
    ap.add_argument("-v", action="store_true", help="list queries whose rank moved")
    a = ap.parse_args()
    sys.path.insert(0, str(pathlib.Path(a.checkout)))
    from agent_memory.store import Store

    frozen = (HERE / "queries.sha256").read_text().split()[0]
    if hashlib.sha256((HERE / "queries.json").read_bytes()).hexdigest() != frozen:
        sys.exit("queries.json changed after it was frozen")
    records = json.loads((HERE / "records.json").read_text(encoding="utf-8"))
    queries = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))
    ns_of = {r["id"][:8]: r["namespace"] for r in records}
    enc = Encoder(a.url, pathlib.Path(a.cache))
    out: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(str(pathlib.Path(tmp) / "memory.db"))
        for r in records:
            store.store(r, "eval")
        docs = {}
        for r in records:
            row = store.get(r["id"])
            docs[row["id"]] = enc.vector(f"{row['title']}\n{row['content']}", "record")
        qvecs = [enc.vector(QUERY_PROMPT + q["q"], "query") for q in queries]
        enc.save()
        score_times: list[float] = []
        rows = []
        for q, qv in zip(queries, qvecs, strict=True):
            ns = ns_of[q["target"]]
            scopes = [ns] + (["global"] if ns != "global" else [])
            ids = [r["id"] for r in store.by_namespace(scopes, limit=100000, statuses=STATUSES)]
            t0 = time.perf_counter()
            sorted(ids, key=lambda i: -dot(qv, docs[i]))
            score_times.append(time.perf_counter() - t0)
            lists = rank_all(store, qv, q["q"], scopes, (), docs)
            rows.append({"target": q["target"], "class": q["class"], "q": q["q"], **{c: position(lists[c], q["target"]) for c in CONFIGS}})
        out += table(f"{a.checkout}: default eval, scopes = target namespace then global", [*((c, [r for r in rows if r["class"] == c]) for c in CLASSES), ("all", rows)])
        out += decision(rows)
        for hub in a.hub:
            scopes = [hub, "global"]
            reach = tuple(sorted({n for n in ns_of.values() if n.split("/")[0] in ("gitea", "github") and n != hub}))
            cells: dict[tuple[str, str], list[dict]] = {}
            for q, qv in zip(queries, qvecs, strict=True):
                where = "hub" if ns_of[q["target"]] in scopes else "other"
                for variant, extra in (("scopes", ()), ("reach", reach)):
                    lists = rank_all(store, qv, q["q"], scopes, extra, docs)
                    cells.setdefault((where, variant), []).append({"target": q["target"], "class": q["class"], "q": q["q"], **{c: position(lists[c], q["target"]) for c in CONFIGS}})
            out += table(f"asked from {hub}; targets in the hub's scopes (hub) or elsewhere (other)", [(f"{w} {v}", cells.get((w, v), [])) for w in ("hub", "other") for v in ("scopes", "reach")])
        store.db.close()
    out += cost(enc, docs, qvecs, score_times)
    if a.v:
        out.append("== default eval, queries whose rank moved against a (rank a -> b cos / c rrf / d wsum)")
        for row in rows:
            if any(row[c] != row["a fts"] for c in CONFIGS[1:]):
                out.append(f"  {row['class']} {row['target']} {row['a fts']} -> {row['b cos']} / {row['c rrf']} / {row['d wsum']}  {row['q']}")
    print("\n".join(out))


if __name__ == "__main__":
    main()
