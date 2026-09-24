# Changelog

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
