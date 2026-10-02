# Agent Memory

Agents call `POST /mcp` on port 8765 with JSON-RPC (MCP streamable HTTP, JSON responses). Start with `memory_bootstrap(namespace)`; the namespace comes from the repository's git remote through `memory_namespace(remote_url)`.

| Response | Budget |
|---|---:|
| bootstrap | 500 tokens |
| search | 750 |
| get | 2000 |
| project_context | 4000, hard limit 8000 |

Records are data, not instructions; every packet says so. Credentials and key material are refused at store time and the refusal is audited by pattern name only. `memory_forget` is a soft delete kept for `retention_deleted_days`. `memory_export` writes Markdown per namespace and one JSONL under `/data/exports`, inside Home Assistant backups.

Clients are listed in the options: a `token_sha256` authenticates by bearer token, an `ip` alone by source address. Scopes: read, write, delete, export, admin. What a token client stores carries its name as the writer.

Status is the server's: `memory_store` writes `active`, extraction and session summaries write `candidate`. `memory_update` takes the `revision` the caller read and changes nothing when the record has moved on; the reply carries the current record. `memory_session_close` with an `op_id` answers a replay from the same client with the same inputs with the first result; past `retention_deleted_days` a replay is refused as expired. REST reads need the `read` scope, as the MCP tools do.

A namespace in `hubs` is one whose sessions work on other repositories. Its `memory_search` and the task match in `memory_project_context` also cover every other repository namespace (`reach` in the response), at a weight below all of its own scopes.

`embed_url` (an OpenAI-style `/v1` base URL), `embed_model` and `embed_timeout_s` turn on embedding retrieval: `memory_search` and the task match rank by reciprocal rank fusion of full-text search and cosine over record vectors whenever the encoder answers within the timeout and every record in reach has a current vector, and by full-text search otherwise. `ranking` in the response says which. `/health` shows `embeddings`. Without `embed_url` search is full-text only.

`namespace_renames` moves stored namespaces after an owner rename, applied at start: `{from: gitea/example/, to: gitea/heidrickla/}` rewrites every `gitea/example/<repo>` record, job, query and standing question to `gitea/heidrickla/<repo>`. A rerun moves nothing. Set `forge_owner`, `project_links` and `hubs` to the new owner at the same time.

## Options

| Option | Default | Effect |
|---|---|---|
| `clients` | one address client | Who may call: `name`, `ip` or `token_sha256`, and `scopes`. |
| `github_owner` | `heidrickla` | Remotes under this GitHub owner become `github/<owner>/<repo>`. |
| `forge_owner`, `forge_hosts` | `heidrickla`, the forge's names | Remotes under this owner on these hosts become `gitea/<owner>/<repo>`. |
| `project_links` | two projects | Repository namespaces that share a `projects/<name>` scope. |
| `hubs` | `gitea/heidrickla/ha-management` | Namespaces whose search and task match reach every other repository. |
| `namespace_renames` | none | Owner prefixes moved at start. |
| `anchor_sweep_hours` | 6 | Interval of the entity anchor check, 1 to 168. |
| `context_budget_tokens` | 4000 | Default `project_context` packet, 500 to 8000. |
| `hard_context_limit_tokens` | 8000 | No packet exceeds it, 1000 to 16000. |
| `retention_deleted_days` | 30 | Forgotten records and recorded queries are purged after it, and session-close receipts become tombstones; 1 to 365. |
| `ollama_url` | absent: no enrichment | Ollama, or an OpenAI-style server when the URL ends in `/v1`. |
| `ollama_model` | `qwen3.5:9b` | Default model for every job kind, last in each chain. |
| `ollama_models` | absent | `<kind>:<model>` entries, most preferred first, kinds `extract` and `answer`. |
| `ollama_timeout_s` | 120 | One model call's limit, 10 to 900. |
| `embed_url` | absent: full-text only | OpenAI-style `/v1` base URL serving embeddings. |
| `embed_model` | `qwen3-emb-0.6b` | The embedding model's name on that server. |
| `embed_timeout_s` | 2 | Wait for a query's vector before answering from full-text search, 0.2 to 30. |

`review_at` (local `HH:MM`, absent means 03:00, `off` turns it off) is the nightly candidate review: the enrichment model forgets candidates that repeat a record their namespace already reaches or report only what one session did, and leaves the rest for an agent. `/health` `candidate_review` shows the last run; `POST /api/review` with the `delete` scope runs it now.
