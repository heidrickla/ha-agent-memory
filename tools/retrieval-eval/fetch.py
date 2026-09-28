"""Fetch every live record in full from the memory service's get route (which logs nothing) into
records.json, for an offline retrieval evaluation.

    python fetch.py            # as Claude: AGENT_MEMORY_TOKEN_CLAUDE
    python fetch.py --codex    # as Codex: AGENT_MEMORY_TOKEN_CODEX

The token is read from the process environment, then on Windows from the user environment
(HKCU\\Environment), and sent as a bearer token; without one the service identifies the caller by
address. A token the service refuses stops the fetch. The value is never printed.

The frozen queries target the 2026-09-24 snapshot, whose records still carry the `gitea/example/`
namespaces from before the owner rename. The live store holds them under `gitea/heidrickla/`, so a
fresh snapshot needs a fresh query set. An existing records.json is never overwritten.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).parent
BASE = "http://192.0.2.10:8765/api"
VARIABLE = "AGENT_MEMORY_TOKEN_CODEX" if "--codex" in sys.argv else "AGENT_MEMORY_TOKEN_CLAUDE"
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


def token(name: str) -> str:
    """The process environment first (set empty means no token), then the Windows user environment,
    for a shell started before the variable was set."""
    if name in os.environ:
        return os.environ[name].strip()
    if os.name != "nt":
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0]).strip()
    except OSError:
        return ""


TOKEN = token(VARIABLE)


def get(path: str) -> object:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{BASE}/{path}", headers=headers), timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as err:
        sys.exit(f"the service refused {'the ' + VARIABLE + ' token' if TOKEN else 'this address'}: HTTP {err.code}")


records = []
for ns in NAMESPACES:
    for item in get(f"timeline?namespace={ns}&limit=200")["items"]:
        full = get(f"get?ids={item['id']}")
        recs = full.get("records", full) if isinstance(full, dict) else full
        records.extend(r for r in recs if isinstance(r, dict) and r.get("id") == item["id"])
OUT.write_text(json.dumps(records, indent=1), encoding="utf-8")
print(len(records), "records;", sum(len(r.get("content", "")) for r in records), "content chars")
print("keys:", sorted(records[0].keys()))
