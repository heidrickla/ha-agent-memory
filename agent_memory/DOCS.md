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
alone by source address. Scopes: read, write, delete, export, admin. What a token client
stores carries its name as the writer.

Status is the server's: `memory_store` writes `active`, extraction and session summaries
write `candidate`. `memory_update` takes the `revision` the caller read and changes nothing
when the record has moved on; the reply carries the current record. `memory_session_close`
with an `op_id` answers a replay from the same client with the same inputs with the first
result; past `retention_deleted_days` a replay is refused as expired. REST reads need the
`read` scope, as the MCP tools do.

A namespace in `hubs` is one whose sessions work on other repositories. Its `memory_search`
and the task match in `memory_project_context` also cover every other repository namespace
(`reach` in the response), at a weight below all of its own scopes.

`embed_url` (an OpenAI-style `/v1` base URL), `embed_model` and `embed_timeout_s` turn on
embedding retrieval: `memory_search` and the task match rank by cosine over record vectors
whenever the encoder answers within the timeout and every record in reach has a current vector,
and by full-text search otherwise. `ranking` in the response says which. `/health` shows
`embeddings`. Without `embed_url` nothing changes.

`namespace_renames` moves stored namespaces after an owner rename, applied at start:
`{from: gitea/example/, to: gitea/heidrickla/}` rewrites every `gitea/example/<repo>` record, job,
query and standing question to `gitea/heidrickla/<repo>`. A rerun moves nothing. Set
`forge_owner`, `project_links` and `hubs` to the new owner at the same time.
