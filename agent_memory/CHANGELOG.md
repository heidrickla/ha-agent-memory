# Changelog

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
