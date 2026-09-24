"""Fetch every live record in full from the memory service's get route (which logs nothing) into
records.json, for an offline retrieval evaluation."""

import json
import pathlib
import urllib.request

HERE = pathlib.Path(__file__).parent
BASE = "http://192.0.2.10:8765/api"
NAMESPACES = [
    "gitea/example/ability-per-turn",
    "gitea/example/ha-agent-memory",
    "gitea/example/ha-loops",
    "gitea/example/ha-management",
    "gitea/example/web-access",
    "global",
]


def get(path: str) -> object:
    with urllib.request.urlopen(f"{BASE}/{path}", timeout=15) as r:
        return json.load(r)


records = []
for ns in NAMESPACES:
    for item in get(f"timeline?namespace={ns}&limit=200")["items"]:
        full = get(f"get?ids={item['id']}")
        recs = full.get("records", full) if isinstance(full, dict) else full
        records.extend(r for r in recs if isinstance(r, dict) and r.get("id") == item["id"])
(HERE / "records.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
print(len(records), "records;", sum(len(r.get("content", "")) for r in records), "content chars")
print("keys:", sorted(records[0].keys()))
