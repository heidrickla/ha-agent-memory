"""Offline retrieval evaluation: the live records in a scratch store built by the repo's own code,
the frozen query set, and the rank of each query's target record.

  python fetch.py                    # records.json from the service's get route, which logs nothing
  python eval.py <checkout> [label] [-v]
  python eval.py <checkout> [label] --hub <namespace>

Query classes: K uses the record's own words, M other forms of them, P a paraphrase sharing none
of its distinctive words. queries.json is frozen by queries.sha256; a changed set is refused, so a
result cannot be tuned by editing the questions. Scopes mimic the service: the target's namespace,
then global. Nothing touches the live search route, so no test query lands in the demand log.

--hub asks every query from one namespace instead, scopes hub then global, once without reach and
once reaching every other repository namespace, split by where the target lives.
"""

import hashlib
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
repo = pathlib.Path(sys.argv[1])
hub = sys.argv[sys.argv.index("--hub") + 1] if "--hub" in sys.argv else None
label = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else repo.name
sys.path.insert(0, str(repo))
from agent_memory.store import Store  # noqa: E402

FROZEN = (HERE / "queries.sha256").read_text().split()[0]
if hashlib.sha256((HERE / "queries.json").read_bytes()).hexdigest() != FROZEN:
    sys.exit("queries.json changed after it was frozen")

records = json.loads((HERE / "records.json").read_text(encoding="utf-8"))
queries = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))
ns_of = {r["id"][:8]: r["namespace"] for r in records}

with tempfile.TemporaryDirectory() as tmp:
    store = Store(str(pathlib.Path(tmp) / "memory.db"))
    for r in records:
        store.store(r, "eval")
    results = {}
    hub_runs: dict[tuple[str, str], list] = {}
    reach = tuple(sorted({n for n in ns_of.values() if n.split("/")[0] in ("gitea", "github") and n != hub}))
    for q in queries:
        # Candidates included: this measures ranking, and 14 of the 24 targets are unpromoted
        # candidates that default search hides whatever the ranking.
        if hub:
            scopes = [hub, "global"]
            where = "hub" if ns_of[q["target"]] in scopes else "other"
            for variant, extra in (("scopes", ()), ("reach", reach)):
                hits = store.search(q["q"], scopes, limit=20, statuses=("active", "candidate"), reach=extra)
                ids = [h["id"][:8] for h in hits]
                rank = ids.index(q["target"]) + 1 if q["target"] in ids else None
                hub_runs.setdefault((where, variant), []).append((rank, len(hits), q["q"]))
            continue
        scopes = [ns_of[q["target"]]] + (["global"] if ns_of[q["target"]] != "global" else [])
        hits = store.search(q["q"], scopes, limit=20, statuses=("active", "candidate"))
        ids = [h["id"][:8] for h in hits]
        rank = ids.index(q["target"]) + 1 if q["target"] in ids else None
        results.setdefault(q["class"], []).append((rank, len(hits), q["q"]))
    store.db.close()


def line(name: str, rows: list, width: int = 6) -> str:
    n = len(rows)
    h1 = sum(1 for r, _, _ in rows if r == 1) / n
    h3 = sum(1 for r, _, _ in rows if r and r <= 3) / n
    mrr = sum(1 / r for r, _, _ in rows if r) / n
    miss = sum(1 for r, _, _ in rows if r is None)
    mean_hits = sum(h for _, h, _ in rows) / n
    return f"{name:<{width}}{n:>4}{h1:>8.2f}{h3:>8.2f}{mrr:>8.2f}{miss:>6}{mean_hits:>11.1f}"


if hub:
    print(f"== {label}, asked from {hub}; targets in the hub's scopes (hub) or elsewhere (other)")
    print(f"{'set':<14}{'n':>4}{'hit@1':>8}{'hit@3':>8}{'MRR':>8}{'miss':>6}{'mean hits':>11}")
    for key in (("hub", "scopes"), ("hub", "reach"), ("other", "scopes"), ("other", "reach")):
        print(line(f"{key[0]} {key[1]}", hub_runs.get(key, []), 14))
    sys.exit(0)

print(f"== {label}")
print(f"{'class':<6}{'n':>4}{'hit@1':>8}{'hit@3':>8}{'MRR':>8}{'miss':>6}{'mean hits':>11}")
total = []
for cls in ("K", "M", "P"):
    total += results.get(cls, [])
    print(line(cls, results.get(cls, [])))
print(line("all", total))
if "-v" in sys.argv:
    for cls in ("K", "M", "P"):
        for r, h, q in results.get(cls, []):
            if r != 1:
                print(f"  {cls} rank={r} hits={h}  {q}")
