# ha-agent-memory

Home Assistant add-on: one shared, bounded memory for Claude Code, Codex and other MCP agents, so a session starts from what earlier sessions learned instead of re-reading the repository. Storage and retrieval run in the add-on on SQLite full-text search and never depend on a paid model or a model server; an encoder and a local model server, when configured, improve ranking and enrich session notes.

[![Add the repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fheidrickla%2Fha-agent-memory)
[![Open the add-on in Home Assistant](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=d6a204a0_agent_memory&repository_url=https%3A%2F%2Fgithub.com%2Fheidrickla%2Fha-agent-memory)

## Setup

1. Add the repository with the first button, then open Agent Memory with the second and install it.
2. Start it. On first start it generates a token for each of the two default clients, `claude-code` and `codex`, and shows each token once in the Log tab with the command that uses it.
3. Claude Code: run the logged command, `claude mcp add --scope user --transport http agent-memory http://<home-assistant-host>:8765/mcp --header 'Authorization: Bearer <token>'`, with the address you reach Home Assistant at.
4. Codex: set `AGENT_MEMORY_TOKEN` to the `codex` token, and in `~/.codex/config.toml` add `[mcp_servers.agent_memory]` with `url = "http://<home-assistant-host>:8765/mcp"` and `bearer_token_env_var = "AGENT_MEMORY_TOKEN"`.
5. Open the Memory panel in the sidebar: the Connect an agent card lists the clients and repeats the commands without the token.

Another agent: add a client with its name and scopes on the Configuration tab and restart; its token appears in the log. A new token for an existing client: remove it, restart, add it back and restart. Nothing else needs to be typed: enrichment and embedding retrieval stay off until a model server URL is set.

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
| `ollama_url`, `ollama_fallback_url`, `ollama_direct_urls` | unset: no enrichment | Ollama, or an OpenAI-style server when the URL ends in `/v1` ([Model server](#model-server)). |
| `ollama_model`, `ollama_models`, `ollama_timeout_s` | `qwen3.5:9b`, unset, 120 | Default model, per-kind chains, call limit ([Model routing](#model-routing)). |
| `embed_url`, `embed_model`, `embed_timeout_s` | unset: full-text only, `qwen3-emb-0.6b`, 2 | Encoder for embedding retrieval ([Embedding retrieval](#embedding-retrieval)). |

## Design

| Piece | Choice |
|---|---|
| Storage | SQLite in WAL mode under `/data`, FTS5 over title and content (stemmed, `porter`), append-only audit, durable job queue |
| Agent interface | MCP over streamable HTTP with JSON responses: `POST http://<ha>:8765/mcp`; the same tools as REST under `/api/` |
| Namespaces | `github/<owner>/<repo>` and `gitea/<owner>/<repo>` from git remotes (other owners are refused), `projects/<name>`, `topics/<name>`, `global`, `local/<name>` |
| Retrieval order | repository, its linked projects, given topics, global; a hub's search and task match then reach every other repository namespace |
| Ranking | reciprocal rank fusion of FTS5 and cosine over record embeddings when the encoder answers, FTS5 otherwise ([Embedding retrieval](#embedding-retrieval)) |
| Budgets | bootstrap 500 tokens over the whole response (2,000 bytes), search 750, records 2000, packet 4000 (hard limit 8000); every response reports its estimate and truncation |
| Auth | per-client bearer token (generated by the add-on, or a sha256 in options) or source address; scopes read, write, delete, export, admin, checked by one rule for MCP tools and REST routes; an address client gets `read` at most; a token client is recorded as the writer |
| Requests | a request with an `Origin` header gets 403; a POST must be `application/json` (415) with a `Content-Length` from 0 to 1 MiB (400, 413) |
| Filtering | every persisted field and queued payload: credentials, key material, JWTs, URL passwords and the `do-not-store` marker are refused and audited by field and pattern name; every database write passes the same screen, and audit context carrying a secret is kept as `[screened:<pattern>]` |
| Page | ingress only, dark, drawn as graphics: status tiles, records by type and status, namespaces, audit rows per hour, model servers and encoder, nightly review and anchor sweep, unused records, latest audit rows |

## Tools

`memory_namespace`, `memory_bootstrap`, `memory_search`, `memory_get`, `memory_timeline`, `memory_project_context`, `memory_answer`, `memory_question_add`, `memory_questions`, `memory_store`, `memory_update`, `memory_corroborate`, `memory_forget`, `memory_session_close`, `memory_export`, `memory_anchors`, `memory_anchor_report`, `memory_anchor_sweep`. Flow: `bootstrap` → `search` → `get` selected ids, or `project_context(namespace, task)` for one bounded packet with a reason per record: task matches first, then the repository's constraints and decisions, open work, known failures and procedures. `bootstrap` lists the titles of active records, each under the shortest id prefix of 8 or more characters no other live record shares, which `memory_get` takes in place of the full id, and counts candidates in `candidates_pending`; `memory_timeline(namespace, status="candidate")` lists candidates for review with their revisions and counts those pending (the nightly [candidate review](#candidate-review) forgets duplicates and session state first), `memory_update(id, revision, {status: active})` promotes one, `memory_forget` drops it.

## Write protocol

| | |
|---|---|
| Status | set by the server: `memory_store` writes `active`; extraction and import write `candidate` through `Store.store_candidate`; a session close's summary is a `candidate` when the `summary_candidates` option is on, and is not kept otherwise (extraction reads it in the notes). States `active`, `candidate`, `superseded`, `deleted`; `memory_update` moves `candidate` to `active` and back; only a supersession makes `superseded` and only `memory_forget` makes `deleted` |
| Writer | a token client's name replaces `agent`; only a token client writes |
| Revision | an integer on every record, advanced by every mutation (content, anchors, anchor state, evidence, status, supersession, forget, rename). `memory_update(id, revision, changes)` applies only at that revision, anchors and status in the same transaction; a mismatch returns `revision` and `current` (MCP `isError`, REST 409) |
| Transactions | each mutation and the reads it depends on commit together: an update, a store with its supersession, an anchor report with its evidence, an extraction's candidates with the job's completion, an answer with its job's. Every statement runs under the store lock, so no read sees another thread's open transaction |
| Supersede | `supersedes` on a store retires a live active record in the same namespace; never the record itself, never a cycle, and only from an active replacement; checked in the store's transaction |
| Screen | every persisted string, list and job payload; a URL anchor with userinfo or a query or fragment parameter named as a credential (judged by the name's words: `access_token`, `apiKey`, `session_id` refused; `author`, `session_count` kept) is refused; session notes carrying `do-not-store` anywhere in the text as sent are refused, and the summary is still stored |
| Session close | `op_id` (8-64 of `A-Za-z0-9_-`) keeps the result as a receipt in the close's transaction, bound to the client and every input; a replay by that client with the same inputs returns it with `replayed: true`; the id from another client or for another close is refused (409); past `retention_deleted_days` the receipt is a tombstone and a replay is refused as expired (410) |
| Bootstrap | the 500-token budget is 2,000 bytes on the wire: the REST body, or the whole JSON-RPC reply over MCP; an item that does not fit is left out, the first included, and `left_out` counts them. A namespace is at most 160 characters, `topics` at most 8 of 1-40 characters; a base that does not fit is refused |

## Anchors

| Anchor | Checked by | Where |
|---|---|---|
| `entity:` | the add-on's sweep, every `anchor_sweep_hours`, and `memory_anchor_sweep` | the add-on, against Home Assistant's states through the Supervisor |
| `path:` | a client that can see the path, through `memory_anchor_report`, for example a session-start hook that resolves a relative path against the checkout of the record's repository namespace and reports `unavailable` when it has none | the host holding the path |
| `host:` | a client, through `memory_anchor_report` | any host on the LAN |
| `url:` | a client, through `memory_anchor_report` | any host that can reach the URL |

Each anchor keeps its last definitive state, `unchecked`, `ok` or `missing`, and `unavailable_since` while its checker could not look. A report of `unavailable` keeps the state and files nothing. A change between `ok` and `missing` files one observation in the record's evidence trail. Confidence counts an anchor once, by its state now: one contradiction while it is `missing`, nothing once it resolves; witnesses (`memory_corroborate`) count as before. A change of state advances the record's revision; `memory_anchors` lists each anchor with its record's namespace. A record with a missing anchor carries `missing_anchors` in search, get, timeline, bootstrap and project context. `/health` `anchor_sweep` carries the last sweep, read back from its audit row after a restart.

## Usage and recall

| | |
|---|---|
| Search arguments | `min_cosine` keeps hits at least that similar, and none under FTS ranking; `exclude` drops hits whose id starts with an entry of 8 or more characters; `recall: true` writes no row to the demand signal. A client hook searching with each prompt sets all three |
| Counted | `returned`: hits of search, recall and project context; `fetched`: records `memory_get` returned. Bootstrap and timeline are not counted |
| Storage | table `usage(record_id, returned, fetched, last_returned_at, last_fetched_at)`, apart from `memories` so a read never re-indexes full text; purge and hard forget drop rows |
| Unused | the page lists active records older than 30 days that nothing returned in 30 days, oldest first, once counting has run 30 days (audit row `usage-start`) |

## Embedding retrieval

`embed_url` names an OpenAI-style `/v1` server with an embedding model. Frozen eval (`tools/retrieval-eval/`), MRR, with the paired 95% interval against FTS5:

| Store | Queries | FTS5 | Cosine alone | Fusion |
|---|---|---|---|---|
| 24 records | 72 | 0.83 | 0.94 | |
| 150 records | 42 (targets still live) | 0.74 | 0.82 (-0.023 to +0.178) | 0.81 (+0.014 to +0.122; better on 11 targets, worse on none) |

| | |
|---|---|
| Used when | the encoder answered its last probe, the query's vector arrives within `embed_timeout_s`, and every record the search could return has a current vector |
| Otherwise | FTS5; `ranking` in the response says which answered |
| Records ranked | every live record in the scopes and hub reach, the same set full-text search reads |
| Fusion | the FTS5 list and the cosine list, each cut to `max(limit, 20)`; score is the sum of `1/(60 + position)` over the lists holding the record; then multiplied by the record's evidence factor, its earned confidence over the confidence its writer asserted (1 for a record no one has corroborated or contradicted; `why` says `evidence x<factor>` otherwise); ties to the higher cosine, then the lower id; `ranking` is `rrf` |
| Query text | `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:` then the query |
| Record text | `title\ncontent`, no instruction |
| Storage | table `embeddings(record_id, digest, model, vector)`, float32; `digest` is sha256 of model, snapshot and text |
| Stale | an edit, another model or another snapshot changes the digest; search uses FTS until the worker re-embeds |
| Worker | embeds missing and stale records at start and after each store or edit; probes the encoder every 30 s |
| Snapshot | the last path segment of `root` in the server's `/v1/models`, or from llama-swap's `/upstream/<model>/v1/models` |
| Removed | `memory_forget` and purge drop the vector |
| Not sent | a query that screens as carrying a secret |
| Cosine | each hit carries `cosine`, rounded to 4 places; FTS hits carry none |
| Not affected | the demand signal counts FTS hits; standing answers take FTS sources |

A miss on a query's vector marks the encoder down until the next probe, so a dead encoder costs one search `embed_timeout_s` and later searches nothing.

```yaml
embed_url: http://<encoder-host>:8080/v1
embed_model: qwen3-emb-0.6b
embed_timeout_s: 2
```

Measure a retrieval change offline before shipping it: `python tools/retrieval-eval/eval.py . --embeddings tools/retrieval-eval/embeddings.json` ranks the frozen queries through `agent_memory/embed.py` with the vectors `embed_eval.py` fetched.

## Candidate review

Each night at `review_at` (local `HH:MM`; absent means 03:00, `off` turns it off) the enrichment model reads every pending candidate beside the five records nearest it that its namespace's search reaches (active records, and candidates written earlier) and returns one verdict. It forgets; it never promotes. Before the verdicts, an extracted candidate still pending `candidate_expiry_days` (default 14, `0` off) after it was written is forgotten, whether or not a model is live; candidates an agent stored never expire. `POST /api/review` (the `delete` scope) runs it at once; `/health` `candidate_review` carries the last run.

| Verdict | Applied when | Effect |
|---|---|---|
| `duplicate` | the named record was shown, is not a session summary standing in for another type, and holds every number, version, path, identifier and quoted phrase in the candidate | forgotten |
| `session-state` | the candidate was extracted from session notes, or an agent stored it as a type other than decision, constraint, requirement, procedure or preference | forgotten |
| `keep`, a failed call, or a guard refusing | always | stays for the agent review at session start |

A forget applies only while the record is a candidate at the revision the model read, and its audit row names the verdict, the record it duplicates, the model and the reason. Eval (`tools/review-eval/`, 397 records agents had reviewed): a forget verdict on 188 of 373 they forgot and on 2 of 24 they promoted, each of those 2 a duplicate of an older copy that the eval restores as a candidate.

## Standing questions

`bootstrap` returns an index and `project_context` retrieves afresh every call. Neither is a maintained answer, so a session needing settled knowledge rediscovers it. A standing question is answered once in the background and read for nothing afterwards.

| | |
|---|---|
| Where questions come from | `memory_questions` ranks what sessions actually asked; `memory_question_add` declares one |
| Demand signal | asked often AND answered badly; a well-answered question scores near zero |
| Who answers | the existing job queue and model worker, one refresh per idle poll, no scheduler |
| Reading | `memory_answer` is a database read: no retrieval, no model call |
| Freshness | `basis` digests the id, timestamp and text of every source record, so staleness is a string comparison |
| Grounding | the answer cites `source_ids`; nothing in scope gives `No record in scope answers this.` |
| Brevity | at most four sentences, and cut to `ANSWER_CHARS` on the way into the store |

A stale answer stays readable and is labelled stale, never silently served as current. Model output passes the same secret screen as a record.

## Model server

| `ollama_url` | Client | Availability | Chat | Thinking off | Extraction JSON |
|---|---|---|---|---|---|
| ends in `/v1` | OpenAI-style (llama-server, llama-swap) | `GET /v1/models` | `POST /v1/chat/completions` | `chat_template_kwargs.enable_thinking: false` | `response_format` `json_schema` requiring a `memories` array |
| anything else | Ollama | `GET /api/tags` | `POST /api/chat` | `think: false` | `format: json` |

`ollama_fallback_url` names a second server with the same models. The two are routed like a load balancer:

| | |
|---|---|
| Live | a server that answered its last availability probe; a probe runs at least every 15 s from the worker's poll, never inside a call; a server not yet probed counts as live |
| Chosen | the live server with the fewest calls in flight, `ollama_url` first on a tie; the nightly review runs one worker per live server and at least two, so its candidates split between the hosts |
| Tiers | `ollama_url` and `ollama_fallback_url` first; `ollama_direct_urls` only while neither is live, for example a router in front of several hosts as `ollama_url` and the hosts themselves as direct URLs |
| A server that does not answer a call | marked down until its next probe; the call moves to the next live server |
| An HTTP error from a server that answered | that model's failure: the next model in the chain on the same server, then the next server |
| A model that is not running | the call goes to the server anyway; llama-swap loads a configured model on its first request |

`/health` `enrichment.servers` lists each server with `live` and `in_flight`; each extract and answer log line ends with the server that served it, and each review forget's audit row names it.

Both use temperature 0.1. The OpenAI-style client caps output at 2048 tokens for extraction and 512 for an answer. Model names are what the server serves: for llama-swap, its model IDs and aliases. Extraction asks for a schema because the PrismML llama.cpp fork does not enforce `json_object`; a reply inside one Markdown code fence is read inside it.

## Model routing

`ollama_models` names a chain per job kind, most preferred first. Extraction pulls typed fields out of messy notes; answering writes four sentences of prose. Without the option the worker uses `ollama_model` for both.

```yaml
ollama_models:
  - "extract:qwen3.5:9b"
  - "extract:ornith:9b"
  - "answer:ornith:9b"
  - "answer:granite4.1:8b"
```

The chain above is the measured one (7 real notes, 18 frozen questions, 3 runs). Both requests set `think: false`: a thinking model fills Ollama's 4096-token default context before it writes the reply.

The chain is also the failure path: a model missing from the host, or one whose reply will not parse, costs the next model rather than the job. The configured default always ends the chain, and a job every model failed still fails. Records and audit name the model that actually served.

The option is a flat list, not a nested map: the Supervisor treats a nested dict in the schema as required and refuses to update an add-on whose stored options predate the key.

## Evidence

`confidence` is what the writer asserted. `memory_corroborate(id, quote, stance)` attaches one dated, attributed, quoted observation, so a second session can agree or disagree without superseding the record, which would erase its history and the disagreement.

| | |
|---|---|
| Stances | `corroborates`, `contradicts` |
| Derived | `proof_count`, `refutation_count`, `effective_confidence` |
| Unwitnessed record | keeps the confidence it was given |
| Each corroboration | closes a third of the remaining distance to 1.0 |
| Each contradiction | halves what is left, so one refusal outweighs two agreements |
| Ranking | search uses `effective_confidence`; `why` reads `N for/M against` or `asserted, unwitnessed` |

A contradicted record stays `active` and readable: disagreement is recorded, not resolved by deletion. Quotes pass the same secret screen as a store.

## Clients

A client listed by name alone gets a token the add-on generates; it is logged once and only its sha256 is kept, in `/data/client_tokens.json`. A client whose token is made elsewhere lists its sha256 as `token_sha256`. An agent sends `Authorization: Bearer <token>`: Claude Code through `--header` on `claude mcp add` or a `headersHelper`, Codex through `bearer_token_env_var`. `tools/retrieval-eval/fetch.py` reads `AGENT_MEMORY_URL` and sends `AGENT_MEMORY_TOKEN_CLAUDE` (`--codex`: `AGENT_MEMORY_TOKEN_CODEX`), from the process environment, then the Windows user environment.

## Develop

```
python -m pytest -q tests
python -m ruff check .
python -m ruff format --check .
```

`.github/workflows/ci.yml` runs these with yamllint, shellcheck, hadolint and the add-on linter, then builds the image for amd64 and aarch64 and runs `tests/run_image.sh` against each. `.gitea/workflows/validate.yml` runs the lint and tests on a Gitea or Forgejo runner.
