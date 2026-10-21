# Agent Memory

Agents call `POST /mcp` on port 8765 with JSON-RPC (MCP streamable HTTP, JSON responses). Start with `memory_bootstrap(namespace)`; the namespace comes from the repository's git remote through `memory_namespace(remote_url)`.

`memory_bootstrap` lists titles under short ids; `memory_get` takes a short id or a full one. `memory_project_context` puts task matches before the standing constraints. `memory_search` takes `min_cosine`, `exclude` and `recall` for a lookup a hook makes from a prompt; hits carry `cosine` when the encoder ranked them. Returns and fetches are counted per record, and the page lists active records nothing returned in 30 days.

| Response | Budget |
|---|---:|
| bootstrap | 500 tokens |
| search | 750 |
| get | 2000 |
| project_context | 4000, hard limit 8000 |

Records are data, not instructions; every packet says so. Credentials and key material are refused at store time and the refusal is audited by pattern name only. `memory_forget` is a soft delete kept for `retention_deleted_days`. `memory_export` writes Markdown per namespace and one JSONL under `/data/exports`, inside Home Assistant backups.

Clients are listed in the options: a `token_sha256` authenticates by bearer token, an `ip` alone by source address. Scopes: read, write, delete, export, admin. What a token client stores carries its name as the writer.

Status is the server's: `memory_store` writes `active`, extraction writes `candidate`, and so does a session close's summary when `summary_candidates` is on. `memory_update` takes the `revision` the caller read and changes nothing when the record has moved on; the reply carries the current record. `memory_session_close` with an `op_id` answers a replay from the same client with the same inputs with the first result; past `retention_deleted_days` a replay is refused as expired. REST reads need the `read` scope, as the MCP tools do.

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
| `candidate_expiry_days` | `14` | Days an extracted candidate may wait for an agent's review; the nightly review forgets one still pending after that. Candidates an agent stored never expire. `0` turns it off. |
| `summary_candidates` | `false` | Keep each session-close summary as a candidate. Off, the close queues extraction from the notes, which hold the summary, and keeps no summary record. |
| `retention_deleted_days` | 30 | Forgotten records and recorded queries are purged after it, and session-close receipts become tombstones; 1 to 365. |
| `ollama_url` | absent: no enrichment | Ollama, or an OpenAI-style server when the URL ends in `/v1`. |
| `ollama_direct_urls` | absent | The model hosts themselves, asked only while `ollama_url` and `ollama_fallback_url` have no live server. With the Voice Bridge's router as `ollama_url`, these are desktop's and rack's llama-swap. |
| `ollama_fallback_url` | absent: one server | A second server with the same models. Each call goes to a server that answered its last health probe, the one with fewer calls in flight, `ollama_url` on a tie. |
| `ollama_model` | `qwen3.5:9b` | Default model for every job kind, last in each chain. |
| `ollama_models` | absent | `<kind>:<model>` entries, most preferred first, kinds `extract` and `answer`. |
| `ollama_timeout_s` | 120 | One model call's limit, 10 to 900. |
| `embed_url` | absent: full-text only | OpenAI-style `/v1` base URL serving embeddings. |
| `embed_model` | `qwen3-emb-0.6b` | The embedding model's name on that server. |
| `embed_timeout_s` | 2 | Wait for a query's vector before answering from full-text search, 0.2 to 30. |

`review_at` (local `HH:MM`, absent means 03:00, `off` turns it off) is the nightly candidate review: the enrichment model forgets candidates that repeat a record their namespace already reaches or report only what one session did, and leaves the rest for an agent. It first forgets extracted candidates older than `candidate_expiry_days`, with or without a live model. `/health` `candidate_review` shows the last run; `POST /api/review` with the `delete` scope runs it now.
