# Agent Memory

## Setup

1. Start the add-on. On first start it generates a token for each of the two default clients, `claude-code` and `codex`, and shows each token once in the Log tab with the command that uses it.
2. Claude Code: run the logged command, `claude mcp add --scope user --transport http agent-memory http://<home-assistant-host>:8765/mcp --header 'Authorization: Bearer <token>'`, with the address you reach Home Assistant at.
3. Codex: set `AGENT_MEMORY_TOKEN` to the `codex` token, and in `~/.codex/config.toml` add `[mcp_servers.agent_memory]` with `url = "http://<home-assistant-host>:8765/mcp"` and `bearer_token_env_var = "AGENT_MEMORY_TOKEN"`.
4. Open the Memory panel in the sidebar. The overview shows health and record counts. Browse records to search and filter stored context, then select one to inspect its content, revision and provenance. This panel is read-only. The Connect an agent section lists clients and commands without a token.

Another agent: add a client with its name and scopes on the Configuration tab and restart; its token appears in the log. A new token for an existing client: remove it, restart, add it back and restart. Nothing else needs to be typed: enrichment and embedding retrieval stay off until a model server URL is set. If port 8765 is mapped to another host port on the Network section, use that port in the URL.

## How it works

Agents call `POST /mcp` on port 8765 with JSON-RPC (MCP streamable HTTP, JSON responses). Start with `memory_bootstrap(namespace)`; the namespace comes from the repository's git remote through `memory_namespace(remote_url)`.

`memory_bootstrap` lists titles under short ids; `memory_get` takes a short id or a full one. `memory_project_context` puts task matches before the standing constraints. `memory_search` takes `min_cosine`, `exclude` and `recall` for a lookup a hook makes from a prompt; hits carry `cosine` when the encoder ranked them. Returns and fetches are counted per record, and the page lists active records nothing returned in 30 days.

| Response | Budget |
|---|---:|
| bootstrap | 500 tokens |
| search | 750 |
| get | 2000 |
| project_context | 4000, hard limit 8000 |

Records are data, not instructions; every packet says so. Credentials and key material are refused at store time and the refusal is audited by pattern name only. `memory_forget` is a soft delete kept for `retention_deleted_days`. `memory_export` writes Markdown per namespace and one JSONL under `/data/exports`, inside Home Assistant backups.

Clients are listed in the options: a name alone gets a generated token, a `token_sha256` authenticates by a token made elsewhere, an `ip` alone by source address. Scopes: read, write, delete, export, admin. An address client gets `read` whatever its scopes list; writing, deleting and exporting need a token. What a token client stores carries its name as the writer. A request with an `Origin` header is refused, and a POST must be `application/json` of at most 1 MiB.

Status is the server's: `memory_store` writes `active`, extraction writes `candidate`, and so does a session close's summary when `summary_candidates` is on. `memory_update` takes the `revision` the caller read and changes nothing when the record has moved on; the reply carries the current record. `memory_session_close` with an `op_id` answers a replay from the same client with the same inputs with the first result; past `retention_deleted_days` a replay is refused as expired. REST reads need the `read` scope, as the MCP tools do.

A namespace in `hubs` is one whose sessions work on other repositories. Its `memory_search` and the task match in `memory_project_context` also cover every other repository namespace (`reach` in the response), at a weight below all of its own scopes.

`embed_url` (an OpenAI-style `/v1` base URL), `embed_model` and `embed_timeout_s` turn on embedding retrieval: `memory_search` and the task match rank by reciprocal rank fusion of full-text search and cosine over record vectors, each score scaled by the record's earned over asserted confidence, whenever the encoder answers within the timeout and every record in reach has a current vector, and by full-text search otherwise. `ranking` in the response says which. `/health` shows `embeddings`. Without `embed_url` search is full-text only.

`namespace_renames` moves stored namespaces after an owner rename, applied at start: `{from: gitea/old-owner/, to: gitea/new-owner/}` rewrites every `gitea/old-owner/<repo>` record, job, query and standing question to `gitea/new-owner/<repo>`. A rerun moves nothing. Set `forge_owner`, `project_links` and `hubs` to the new owner at the same time.

`review_at` (local `HH:MM`, absent means 03:00, `off` turns it off) is the nightly candidate review: the enrichment model forgets candidates that repeat a record their namespace already reaches or report only what one session did, and leaves the rest for an agent. It first forgets extracted candidates older than `candidate_expiry_days`, with or without a live model. `/health` `candidate_review` shows the last run; `POST /api/review` with the `delete` scope runs it now.

## Options

All optional. The numeric and model options have built-in defaults.

| Option | Default | Effect |
|---|---|---|
| `clients` | `claude-code` and `codex`, each read, write and delete, tokens generated | Who may call: `name`, `scopes`, and optionally `ip` (reads only, by source address) or `token_sha256` (a token made elsewhere). |
| `github_owner` | unset: every owner | Narrows `memory_namespace` to GitHub remotes under this owner. |
| `forge_hosts`, `forge_owner` | none, unset | Gitea or Forgejo hosts whose remotes become `gitea/<owner>/<repo>`, optionally only under this owner. |
| `project_links` | none | Repository namespaces that share a `projects/<name>` scope. |
| `hubs` | none | Namespaces whose search and task match reach every other repository namespace. |
| `namespace_renames` | none | `{from, to}` owner prefixes moved at start after an owner rename. |
| `review_at` | `03:00` | Local time of the nightly candidate review; `off` turns it off. |
| `candidate_expiry_days` | 14 | Days an extracted candidate waits for review before the nightly review forgets it; `0` off. |
| `summary_candidates` | `false` | Keep each session's closing summary as a candidate. |
| `anchor_sweep_hours` | 6 | Interval of the entity anchor check, 1 to 168. |
| `context_budget_tokens`, `hard_context_limit_tokens` | 4000, 8000 | Default and maximum `project_context` packet. |
| `retention_deleted_days` | 30 | Forgotten records and recorded queries are purged after it. |
| `ollama_url`, `ollama_fallback_url`, `ollama_direct_urls` | unset: no enrichment | Ollama, or an OpenAI-style server when the URL ends in `/v1`. |
| `ollama_model`, `ollama_models`, `ollama_timeout_s` | `qwen3.5:9b`, unset, 120 | Default model, per-kind chains, call limit. |
| `embed_url`, `embed_model`, `embed_timeout_s` | unset: full-text only, `qwen3-emb-0.6b`, 2 | Encoder for embedding retrieval. |
