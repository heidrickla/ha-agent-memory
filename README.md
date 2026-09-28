# ha-agent-memory

Home Assistant add-on: one shared, bounded memory for Claude Code, Codex and other agents,
so a session starts from what earlier sessions learned instead of re-reading the repository.
Canonical storage and retrieval never depend on a paid model or on a model server: an encoder,
when configured, improves ranking and search answers from full-text search whenever it does not.

From the plan `Shared Agent Memory on Home Assistant` (2026-09-19): phases 1 to 4 are built here:
storage, namespaces, FTS5 retrieval, progressive MCP tools, HA packaging, and enrichment through a
local model server (`enrich.py`: redacted notes in, validated candidates out, candidates never
active until promoted, a job done only after its records are written).

| Piece | Choice |
|---|---|
| Storage | SQLite in WAL mode under `/data`, FTS5 over title and content (stemmed, `porter`), append-only audit, durable job queue |
| Agent interface | MCP over streamable HTTP with JSON responses: `POST http://<ha>:8765/mcp`; the same tools as REST under `/api/` |
| Namespaces | `github/<owner>/<repo>` and `gitea/<owner>/<repo>` from git remotes (other owners are refused), `projects/<name>`, `topics/<name>`, `global`, `local/<name>` |
| Retrieval order | repository, its linked projects, given topics, global; a hub's search and task match then reach every other repository namespace |
| Ranking | cosine over record embeddings when the encoder answers, FTS5 otherwise ([Embedding retrieval](#embedding-retrieval)) |
| Budgets | bootstrap 500 tokens over the whole response (2,000 bytes), search 750, records 2000, packet 4000 (hard limit 8000); every response reports its estimate and truncation |
| Auth | per-client bearer token (sha256 in options) or source address; scopes read, write, delete, export, admin; a token client is recorded as the writer |
| Filtering | every persisted field and queued payload: credentials, key material, JWTs, URL passwords and the `do-not-store` marker are refused and audited by field and pattern name |
| Page | ingress only, dark: health, namespaces, audit tail |

## Tools

`memory_namespace`, `memory_bootstrap`, `memory_search`, `memory_get`, `memory_timeline`,
`memory_project_context`, `memory_answer`, `memory_question_add`, `memory_questions`,
`memory_store`, `memory_update`, `memory_corroborate`, `memory_forget`, `memory_session_close`,
`memory_export`, `memory_anchors`, `memory_anchor_report`, `memory_anchor_sweep`. Flow: `bootstrap` → `search` → `get` selected ids, or
`project_context(namespace, task)` for one bounded packet with a reason per record.
`bootstrap` lists active records and counts candidates in `candidates_pending`; `memory_timeline`
lists candidates for review with their revisions, `memory_update(id, revision, {status: active})`
promotes one, `memory_forget` drops it.

## Write protocol

| | |
|---|---|
| Status | set by the server: `memory_store` writes `active`; extraction and import write `candidate` through `Store.store_candidate`; session summaries are `candidate`. States `active`, `candidate`, `superseded`, `deleted`; `memory_update` moves `candidate` to `active` and back; only a supersession makes `superseded` and only `memory_forget` makes `deleted` |
| Writer | a token client's name replaces `agent`; an address client's `agent` is kept |
| Revision | an integer on every record, advanced by every mutation (content, anchors, evidence, status, supersession, forget, rename). `memory_update(id, revision, changes)` applies only at that revision, anchors and status in the same transaction; a mismatch returns `revision` and `current` (MCP `isError`, REST 409) |
| Supersede | `supersedes` on a store retires a live active record in the same namespace; never the record itself, never a cycle, and only from an active replacement; checked in the store's transaction |
| Screen | every persisted string, list and job payload; a URL anchor with userinfo or a credential-named query or fragment parameter is refused; session notes carrying `do-not-store` are refused, and the summary is still stored |
| Session close | `op_id` (8-64 of `A-Za-z0-9_-`) keeps the result as a receipt in the close's transaction; a replay returns it with `replayed: true`; the id reused for another close is refused (409); receipts are purged with `retention_deleted_days` |
| Bootstrap | the 500-token budget is 2,000 bytes of the serialized response, envelope included; an item that does not fit is left out, the first included, and `left_out` counts them |

## Anchors

| Anchor | Checked by | Where |
|---|---|---|
| `entity:` | the add-on's sweep, every `anchor_sweep_hours`, and `memory_anchor_sweep` | the add-on, against Home Assistant's states through the Supervisor |
| `path:` | a client that can see the path, through `memory_anchor_report`; on desktop, ha-management's `tools/memory_anchor_sweep.py --hook` at every Claude Code SessionStart | the host holding the path |
| `host:` | a client, through `memory_anchor_report` | any host on the LAN |
| `url:` | a client, through `memory_anchor_report` | any host that can reach the URL |

Each anchor keeps its last definitive state, `unchecked`, `ok` or `missing`, and `unavailable_since`
while its checker could not look. A report of `unavailable` keeps the state and files nothing. A
change between `ok` and `missing` files one observation on the record. A record with a missing anchor
carries `missing_anchors` in search, get, timeline, bootstrap and project context.

## Embedding retrieval

`embed_url` names an OpenAI-style `/v1` server with an embedding model. On the frozen eval
(`tools/retrieval-eval/`, 72 queries over 24 records) cosine alone scored MRR 0.94 against FTS5's
0.83; paraphrases went from 0.60 to 0.86.

| | |
|---|---|
| Used when | the encoder answered its last probe, the query's vector arrives within `embed_timeout_s`, and every record the search could return has a current vector |
| Otherwise | FTS5, unchanged; `ranking` in the response says which answered |
| Records ranked | every live record in the scopes and hub reach, the same set full-text search reads; cosine alone, ties to the lower id |
| Query text | `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:` then the query |
| Record text | `title\ncontent`, no instruction |
| Storage | table `embeddings(record_id, digest, model, vector)`, float32; `digest` is sha256 of model, snapshot and text |
| Stale | an edit, another model or another snapshot changes the digest; search uses FTS until the worker re-embeds |
| Worker | embeds missing and stale records at start and after each store or edit; probes the encoder every 30 s |
| Snapshot | the last path segment of `root` in the server's `/v1/models`, or from llama-swap's `/upstream/<model>/v1/models` |
| Removed | `memory_forget` and purge drop the vector |
| Not sent | a query that screens as carrying a secret |
| Not affected | the demand signal counts FTS hits; standing answers take FTS sources |

A miss on a query's vector marks the encoder down until the next probe, so a dead encoder costs one
search `embed_timeout_s` and later searches nothing.

```yaml
embed_url: http://192.0.2.20:8080/v1
embed_model: qwen3-emb-0.6b
embed_timeout_s: 2
```

Measure a retrieval change offline before shipping it: `python tools/retrieval-eval/eval.py . --embeddings tools/retrieval-eval/embeddings.json`
ranks the frozen queries through `agent_memory/embed.py` with the vectors `embed_eval.py` fetched.

## Standing questions

`bootstrap` returns an index and `project_context` retrieves afresh every call. Neither is a
maintained answer, so a session needing settled knowledge rediscovers it. A standing question is
answered once in the background and read for nothing afterwards.

| | |
|---|---|
| Where questions come from | `memory_questions` ranks what sessions actually asked; `memory_question_add` declares one |
| Demand signal | asked often AND answered badly; a well-answered question scores near zero |
| Who answers | the existing job queue and model worker, one refresh per idle poll, no scheduler |
| Reading | `memory_answer` is a database read: no retrieval, no model call |
| Freshness | `basis` digests the id, timestamp and text of every source record, so staleness is a string comparison |
| Grounding | the answer cites `source_ids`; nothing in scope gives `No record in scope answers this.` |
| Brevity | at most four sentences, and cut to `ANSWER_CHARS` on the way into the store |

A stale answer stays readable and is labelled stale, never silently served as current. Model output
passes the same secret screen as a record.

## Model server

| `ollama_url` | Client | Availability | Chat | Thinking off | Extraction JSON |
|---|---|---|---|---|---|
| ends in `/v1` | OpenAI-style (llama-server, llama-swap) | `GET /v1/models` | `POST /v1/chat/completions` | `chat_template_kwargs.enable_thinking: false` | `response_format` `json_schema` requiring a `memories` array |
| anything else | Ollama | `GET /api/tags` | `POST /api/chat` | `think: false` | `format: json` |

Both use temperature 0.1. The OpenAI-style client caps output at 2048 tokens for extraction and 512
for an answer. Model names are what the server serves: for llama-swap, its model IDs and aliases.
Extraction asks for a schema because the PrismML llama.cpp fork does not enforce `json_object`; a
reply inside one Markdown code fence is read inside it.

## Model routing

`ollama_models` names a chain per job kind, most preferred first. Extraction pulls typed fields out
of messy notes; answering writes four sentences of prose. Without the option the worker uses
`ollama_model` for both, exactly as before.

```yaml
ollama_models:
  - "extract:qwen3.5:9b"
  - "extract:ornith:9b"
  - "answer:ornith:9b"
  - "answer:granite4.1:8b"
```

The chain above was measured on 2026-09-28 (7 real notes, 18 frozen questions, 3 runs). Both
requests set `think: false`: a thinking model fills Ollama's 4096-token default context before it
writes the reply.

The chain is also the failure path: a model missing from the host, or one whose reply will not
parse, costs the next model rather than the job. The configured default always ends the chain, and
a job every model failed still fails. Records and audit name the model that actually served.

The option is a flat list, not a nested map: the Supervisor treats a nested dict in the schema as
required and refuses to update an add-on whose stored options predate the key.

## Evidence

`confidence` was a number the writer asserted. A second session seeing the same thing had nowhere
to say so, and one finding a record wrong could only supersede it, which erases the history and
leaves no trace of the disagreement. `memory_corroborate(id, quote, stance)` attaches one dated,
attributed, quoted observation instead.

| | |
|---|---|
| Stances | `corroborates`, `contradicts` |
| Derived | `proof_count`, `refutation_count`, `effective_confidence` |
| Unwitnessed record | keeps the confidence it was given; nothing already stored changes meaning |
| Each corroboration | closes a third of the remaining distance to 1.0 |
| Each contradiction | halves what is left, so one refusal outweighs two agreements |
| Ranking | search uses `effective_confidence`; `why` reads `N for/M against` or `asserted, unwitnessed` |

A contradicted record stays `active` and readable: disagreement is recorded, not resolved by
deletion. Quotes pass the same secret screen as a store.

## Clients

Claude Code, user scope: `claude mcp add --scope user --transport http agent-memory http://192.0.2.10:8765/mcp`.
Codex (`~/.codex/config.toml`): `[mcp_servers.agent_memory]` with `url = "http://192.0.2.10:8765/mcp"`.
A client authenticating by token adds `Authorization: Bearer <token>` and its sha256 to the options.

## Develop

```
python -m pytest -q tests
python -m ruff check . && python -m ruff format --check .
```

Deploy: `git archive --format=tar HEAD agent_memory | ssh root@<ha> 'tar -x -C /local_apps && ha store reload'`, then install or rebuild `local_agent_memory`.
