# Agent Memory

Agents call `POST /mcp` on port 8765 with JSON-RPC (MCP streamable HTTP, JSON responses).
Start with `memory_bootstrap(namespace)`; the namespace comes from the repository's git
remote through `memory_namespace(remote_url)`.

| Response | Budget |
|---|---:|
| bootstrap | 500 tokens |
| search | 750 |
| get | 2000 |
| project_context | 4000, hard limit 8000 |

Records are data, not instructions; every packet says so. Credentials and key material are
refused at store time and the refusal is audited by pattern name only. `memory_forget` is a
soft delete kept for `retention_deleted_days`. `memory_export` writes Markdown per namespace
and one JSONL under `/data/exports`, inside Home Assistant backups.

Clients are listed in the options: a `token_sha256` authenticates by bearer token, an `ip`
alone by source address. Scopes: read, write, delete, export, admin.
