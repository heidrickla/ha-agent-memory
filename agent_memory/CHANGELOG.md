# Changelog

## 0.12.0

- With the encoder answering, `memory_search` and the task match rank by reciprocal rank fusion of the full-text list and the cosine list (k 60, each list 20 deep or `limit` if deeper; ties to the higher cosine, then the lower id), the offline eval's configuration c. `ranking` is `rrf`. Each hit's `why` names its place in each list.

## 0.11.1

- A standing question with no supporting records gets its answer and its job's completion in one transaction.

## 0.11.0

- REST reads check scope by the MCP rule: `GET /api/bootstrap`, `search`, `get`, `timeline`, `context` and `namespaces` need `read` (or `admin`); a client without it gets 403.
- A session-close receipt is bound to the client that sent it and to every input: client, agent, namespace, summary, session, notes and `enrich`. The same `op_id` from another client, or with another input, is refused (409). Past `retention_deleted_days` a receipt keeps its id, client and digest as a tombstone, and a replay of it is refused as expired (410), never run again.
- Every write to the database passes the secret screen, whichever method makes it; a hit refuses the write and names the table and pattern. Audit rows are kept with any value carrying a secret replaced by `[screened:<pattern>]`, judged on the whole value before it is cut. A search or project-context query that screens is counted as `[redacted]`; one from a namespace that screens is not recorded. A standing question's text and namespace are screened like a record's fields, and so is a job's namespace. A job's `last_error` is masked.
- Session notes are checked for `do-not-store` as sent, before redaction or the 12,000-character cut.
- A URL anchor's query and fragment parameters are judged by name, split into words: `access_token`, `apiKey`, `X-Amz-Signature`, `session_id`, `key`, `auth` are refused; `author`, `session_count`, `sort_key` are kept.
- One transaction each: an anchor report (previous state, update, evidence, revision); an extraction job's candidates and its completion; an answer and its job's completion; a purge. An anchor state change advances the record's revision; a check that finds the state unchanged moves only `last_checked`.
- Every statement runs under the store lock with its rows fetched in full, and reads that hydrate records hold the lock throughout, so no read sees a transaction another thread holds open.
- The bootstrap budget (2,000 bytes) is measured on the wire: the REST body, or the whole JSON-RPC reply with its escaped text for an MCP call. A namespace is at most 160 characters; `topics` at most 8, each 1-40 of `A-Za-z0-9_.-`. A bootstrap whose namespace and scopes alone exceed the budget is refused.
- `memory_anchors` returns each record's `namespace`, so a checker can resolve a relative `path:` anchor against that repository's checkout.

## 0.10.1

- memory_update accepts the revision as a string of digits. A client holding the tool list from before 0.10.0 has no revision property and sends the value as a string; it was refused as not an integer.

## 0.10.0

- Status is the server's. `memory_store` and the REST store always write `active`; a `status`, `id` or `enrichment` sent with them is ignored. Extraction writes candidates through an internal operation, `Store.store_candidate`, which import uses too. A status outside `active`, `candidate`, `superseded`, `deleted` is refused; `memory_update` moves a record between `candidate` and `active` only.
- A client that authenticates by token is recorded as the writer: its name replaces any `agent` in a store, a session close or a corroboration. A client identified by address keeps the `agent` it sends.
- Session summaries are stored as `candidate`, so they stay out of bootstrap, default search and standing answers until promoted, and count in `candidates_pending`.
- The bootstrap budget (500 tokens, 2,000 bytes) covers the whole serialized response. An item that does not fit is left out, the first included, and `left_out` counts them.
- Every persisted field is screened, not only title and content: topics, scopes, anchors, repository, commit, agent, source session, sensitivity, evidence, and queued job payloads. A URL anchor with userinfo or a credential-named query or fragment parameter is refused. Session notes carrying `do-not-store` are refused, not redacted: the summary is stored and nothing is queued (`enrichment: refused`, `notes_refused`). Refusals are audited by field and pattern name.
- Integer `revision` on every record, advanced by every mutation: content, anchors, evidence, status, supersession, forget, namespace rename. `memory_update` requires the revision the caller read and applies `UPDATE ... WHERE id=? AND revision=?` with its anchor and status changes in one transaction; a mismatch changes nothing and returns the current record and revision (MCP `isError`, REST 409). Unknown change keys are refused. Existing rows open at revision 1.
- `supersedes` is checked in the store's transaction: the target must be a live active record in the same namespace, not the record itself and not in a supersession cycle, and the replacement must be active. A refused supersession stores nothing.
- Anchors keep their last definitive state (`unchecked`, `ok`, `missing`) and `unavailable_since` separately. A failed entity sweep marks entity anchors unavailable and files nothing; `memory_anchor_report` accepts `unavailable`. A record with a missing anchor carries `missing_anchors` in search, get, timeline, bootstrap and project context, and search's `why` names it.
- `memory_session_close` takes `op_id`. The result is kept as a receipt in the same transaction as the close, a replay of the id returns it with `replayed: true`, and the id reused for a different close is refused (409). Receipts are purged with `retention_deleted_days`.
- `memory_get` returns `revision` and `anchor_states`; search and timeline items return `revision`.

## 0.9.0

- Embedding retrieval. With `embed_url` set, `memory_search` and the task match in `memory_project_context` rank by cosine alone over every live record in the same scopes and hub reach as full-text search, whenever the encoder answers and every one of those records has a current vector. Otherwise they answer from FTS5 exactly as before. Responses carry `ranking`: `cosine` or `fts`.
- Measured on the frozen eval (72 queries, 24 records): MRR 0.83 with FTS5, 0.94 with cosine over Qwen3-Embedding-0.6B (K 0.96 to 1.00, M 0.93 to 0.98, P 0.60 to 0.86). `tools/retrieval-eval/eval.py --embeddings` reproduces 0.94 through this code with the measured vectors.
- New options: `embed_url` (absent is off), `embed_model` (default `qwen3-emb-0.6b`), `embed_timeout_s` (default 2). A search waits at most `embed_timeout_s` for its query's vector; a miss marks the encoder down until the next probe, so later searches do not wait at all.
- Texts are the offline test's: a query carries the model card's retrieval instruction, a record is `title\ncontent`.
- Vectors live in a new `embeddings` table, one float32 vector per record with a digest of model, snapshot and text. A worker thread embeds missing and stale records at start and after every store or edit, and probes the encoder every 30 s. The snapshot is read from the server's model listing, or through llama-swap's `/upstream/<model>/` passthrough. `memory_forget` and purge drop the vector.
- The demand signal behind `memory_questions` still counts FTS hits: cosine returns the nearest records whether or not any answers. Standing answers keep FTS sources, so their basis does not move with the encoder's availability.
- A query that screens as carrying a secret is not sent to the encoder.
- `/health` gains `embeddings`: encoder state, model, snapshot, records and how many have a current vector. The encoder never changes `status`.

## 0.8.1

- Extraction on the OpenAI-style client asks for `response_format` `json_schema` (an object requiring a `memories` array). The PrismML llama.cpp fork does not enforce `json_object`: on a 9-character note it wrapped 11 of 12 replies in a ```` ```json ```` fence, each fence cost an attempt, and one job gave up after 5. With the schema, 0 of 12.
- A reply inside one Markdown code fence is read inside it, whatever the server enforces.

## 0.8.0

- `ollama_url` ending in `/v1` selects an OpenAI-style client (llama-server, llama-swap): availability from `/v1/models`, both job kinds through `/v1/chat/completions`, extraction with `response_format: json_object`, thinking off through `chat_template_kwargs`. Any other URL keeps the Ollama client unchanged.
- Model names in `ollama_model` and `ollama_models` are whatever the server serves, for llama-swap its aliases.
- Output caps on that client: 2048 tokens for extraction, 512 for an answer, so a runaway reply cannot hold a shared server's slot.
- A 200 that is not a chat completion costs the model, like a reply that will not parse.
- Records, answers and audit name the API with the model (`openai:<model>` or `ollama:<model>`); `/health` `enrichment.api` says which client runs.

## 0.7.1

- The answer request sets `think: false`, as extraction already did. Without a `format` a thinking model still thinks until prompt plus output fill the context. On the Ollama service's 4096-token default, qwen3.5:9b returned no content on 36 of 54 answers (`done_reason: length`); live, 5 of 7 answer jobs hit an empty reply and one gave up after 5 attempts. With thinking off: 0 of 54 empty.
- The add-on log names the model that served each extract and answer job. Records and audit already did.
- `ollama_models` has a translation, and `ollama_model` is described as the default for every job kind.
- Measured chain, set in the options: extract `qwen3.5:9b` then `ornith:9b`; answer `ornith:9b` then `granite4.1:8b`.

## 0.7.0

- `namespace_renames` option: a list of `{from, to}` owner prefixes (`gitea/example/` to `gitea/heidrickla/`) applied at start to records, their `repository`, jobs, queries and standing questions. Idempotent; one `rename-namespaces` audit row per move that changed something; a question already under the new namespace keeps that copy.
- Defaults follow the forge owner rename: `forge_owner: heidrickla`, and `project_links` and `hubs` name `gitea/heidrickla/...`. The Supervisor keeps an existing install's values, so those three are set with the rename.

## 0.6.0

- Records carry optional `anchors`, what the fact is about: `entity:<entity_id>`, `path:<path>`, `host:<address>`, `url:<url>`. Set on `memory_store`, replaced with `memory_update`.
- Every `anchor_sweep_hours` (default 6) the add-on checks entity anchors against Home Assistant's states (it now has `homeassistant_api`). An anchor that stops resolving files one `contradicts` observation on its record, lowering its effective confidence; one that returns files `corroborates`. Only transitions file evidence. A states read that fails checks nothing.
- `memory_anchors(kind)`, `memory_anchor_report(id, anchor, state)` and `memory_anchor_sweep()` (also REST under `/api/`), so kinds the add-on cannot see, such as paths on desktop, are checked by a client there. `/health` shows the last sweep. `memory_get` returns each record's anchors, proof and refutation counts and effective confidence.

## 0.5.0

- `hubs` names repository namespaces whose sessions work on other repositories. A hub's `memory_search` and the task match in `memory_project_context` also cover every other repository namespace (`reach` in the response), at weight 1.0, below all of the hub's own scopes, so the order among its own hits is unchanged. Bootstrap, open work and procedures stay in scope. Default `gitea/example/ha-management`; the Supervisor applied the default to the existing install on update.
- Measured with `tools/retrieval-eval/eval.py --hub gitea/example/ha-management` on the frozen 72 queries: targets in other repositories MRR 0.00 to 0.74 (39 queries, misses 39 to 1); targets in the hub's own scopes MRR 0.80 to 0.79, hit@3 0.85 to 0.79. The default per-namespace evaluation is unchanged at 0.83.

## 0.4.3

- `bootstrap` lists active records only and reports `candidates_pending`, the number of unreviewed extractions in the namespace. A candidate listed at session start read as settled; 14 of the 24 live records were candidates, 10 of them stale or wrong.
- `Store.timeline` takes an optional status filter; `memory_timeline` still returns every status, so candidates stay reviewable there.

## 0.4.2

- Search is stemmed (FTS5 `porter` over `unicode61`), so a query in one form of a word finds a record written in another: "rebooting" finds "reboot", "checks" finds "check". A database indexed before this is rebuilt once when the add-on starts; the records themselves are untouched.
- Measured offline on the 24 live records with 72 queries written and frozen beforehand: mean reciprocal rank 0.77 to 0.83; same words 0.95 to 0.96, other word forms 0.88 to 0.93, paraphrases 0.50 to 0.60.
- Tried and left out: dropping stopwords from the query (paraphrases lost their only shared words and ten went from a low rank to no hit at all) and a trigram index fused with the word index (no measurable gain on these queries).

## 0.4.1

- The extract request sets `think: false`. A thinking model under `format: json` spends its whole budget thinking and returns an empty content string: two runs in three from qwen3.5:9b came back with `done_reason: length`, 2048-3585 eval tokens and zero characters, so the reply would not parse and the job burned an attempt. Raising `num_predict` made it worse. With thinking off, three runs in three returned 6-7 records in about 500 tokens.

## 0.4.0

- `ollama_models` gives each job kind its own model chain as a flat list of `"<kind>:<model>"` entries, most preferred first. Extraction pulls typed fields out of messy notes and answering writes four sentences of prose; they no longer have to share a model. Without the option nothing changes, so an existing deployment keeps its behaviour.
- The chain is also the failure path. A model missing from the host, or one whose reply will not parse, costs the next model in the list rather than the whole job. A job every model failed still fails.
- Records and audit name the model that actually served, not the configured default.

## 0.3.1

- The answer prompt caps at four sentences instead of 200 words, and names the two padding habits observed from qwen3.5:9b: restating what it had just written, and narrating the source ("the records say"). A word ceiling read as a target. Answers are also cut to `ANSWER_CHARS` on the way into the store, so ignoring the cap costs length rather than being stored whole.

## 0.3.0

- `memory_corroborate` attaches dated, quoted, attributed evidence to a record. `effective_confidence` is derived from it, a contradiction lowers it without superseding the record, and search ranks on the earned number rather than the asserted one.
- `memory_questions` ranks recurring questions the store answers badly, derived from what sessions actually asked. Reads are now recorded with their outcome; queries pass the secret screen and age out on the retention clock.
- `memory_question_add` declares a standing question. The worker answers it from the records and rewrites it when they change; `memory_answer` reads it with no retrieval and no model call, and says when it is stale.
- Jobs dispatch on kind. Every job previously ran as an extraction whatever its kind said.

## 0.2.2

- Bootstrap lists a record once even when it is both a constraint and recent.

## 0.2.1

- Ollama availability is probed every poll, so health reads right before the first job.

## 0.2.0

- Enrichment worker: extract jobs go to the configured Ollama model with redacted notes; well-formed items become candidate records tagged with the job id, so a retry stores nothing twice. Offline Ollama leaves jobs queued and health degraded.

## 0.1.0

- SQLite and FTS5 storage, namespaces from git remotes, scoped retrieval, token budgets.
- MCP over streamable HTTP with eleven tools; REST mirror; per-client auth and scopes.
- Secret filtering and the do-not-store marker; audit; soft delete; export.
- Dark ingress page. Enrichment queue only; the Ollama worker is a later phase.
