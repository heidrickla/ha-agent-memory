"""Fetch every live record in full from the memory service's get route (which logs nothing) into
records.json, for an offline retrieval evaluation.

The frozen queries target the 2026-09-24 snapshot, whose records still carry the `gitea/example/`
namespaces from before the owner rename. The live store holds them under `gitea/heidrickla/`, so a
fresh snapshot needs a fresh query set. An existing records.json is never overwritten.
"""

import json
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).parent
BASE = "http://192.0.2.10:8765/api"
NAMESPACES = [
    "gitea/heidrickla/ability-per-turn",
    "gitea/heidrickla/ha-agent-memory",
    "gitea/heidrickla/ha-loops",
    "gitea/heidrickla/ha-management",
    "gitea/heidrickla/web-access",
    "global",
]
OUT = HERE / "records.json"
if OUT.exists():
    sys.exit(f"{OUT.name} exists and is the snapshot the frozen queries target; move it aside first")


def get(path: str) -> object:
    with urllib.request.urlopen(f"{BASE}/{path}", timeout=15) as r:
        return json.load(r)


records = []
for ns in NAMESPACES:
    for item in get(f"timeline?namespace={ns}&limit=200")["items"]:
        full = get(f"get?ids={item['id']}")
        recs = full.get("records", full) if isinstance(full, dict) else full
        records.extend(r for r in recs if isinstance(r, dict) and r.get("id") == item["id"])
OUT.write_text(json.dumps(records, indent=1), encoding="utf-8")
print(len(records), "records;", sum(len(r.get("content", "")) for r in records), "content chars")
print("keys:", sorted(records[0].keys()))
