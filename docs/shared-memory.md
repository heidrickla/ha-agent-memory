# Shared memory for coding agents

One memory that several agents (Claude Code, Codex, others speaking MCP) read and write through this add-on, so a lesson one agent learns reaches the next session of any agent.

## Boundaries

| Kind of knowledge | Home | Loaded into every session |
|---|---|---|
| Rules and standing preferences | the repository's agent rules file (`AGENTS.md` or the agent's own) | yes, the only always-loaded source |
| Measured facts kept by hand (hosts, devices, versions, measurements) | the user's own records | no, read when needed |
| What an agent learned: decisions, lessons, traps, open questions | agent-memory | a small scoped bootstrap, then search |

There is no pinning: anything every session must see is a rule and belongs in the rules file.

## Trust

A record one agent writes becomes context in another agent's sessions, so the service decides who wrote it and what state it is in.

| Rule | Behaviour |
|---|---|
| Each agent authenticates with its own bearer token | a client per agent; a client listed by name alone gets a token the add-on generates, logs once and keeps only as a sha256 |
| The writer's identity is set by the server from the token | the server overwrites `agent` with the client's name |
| An address client reads only | a client known by source address gets `read` whatever its scopes list |
| Status is set by the server | direct writes become `active`; extraction and import write `candidate`; the status is validated against an enum |
| Session summaries are reviewed like extractions | kept only as `candidate` when `summary_candidates` is on, and out of bootstrap and search until promoted |
| Every persisted string is screened | every stored field (title, content, topics, scopes, anchors, repository, session, notes); a URL anchor with credentials is refused; a `do-not-store` note is refused, not redacted |
| Browsers are refused | a request with an `Origin` header gets 403; a POST must be `application/json` |

Token handling: a generated token is shown once in the add-on log; the agent keeps it where only that agent reads it (Codex `bearer_token_env_var`, Claude Code `--header` or a `headersHelper`). A token made elsewhere goes into the options as its sha256 only. A token is replaced by removing its client, restarting and adding it back.

## Read path

1. Session start: a session-start hook or the agent calls `memory_bootstrap` for the working repository's namespace. The budget is enforced on the bytes on the wire (the REST body, or the whole JSON-RPC reply over MCP), and an item that does not fit is never admitted, the first one included.
2. Search on demand: `memory_search`, reciprocal rank fusion of FTS5 and cosine over record embeddings when the encoder answers, FTS5 otherwise.
3. Hub reach: a hub namespace searches every repository namespace.
4. Staleness: each anchor type names the process that checks it and where it runs. Entity anchors are swept by the add-on; path, host and URL anchors are reported by clients. Unchecked, unavailable and missing are tracked separately, and a record whose anchor is missing is flagged in results.

## Write path

| Step | Behaviour |
|---|---|
| 1. Write | `memory_store` / `memory_update` over MCP, authenticated per agent |
| 2. Screen | every persisted field, as under Trust |
| 3. Evidence | a second agent agreeing or disagreeing files `memory_corroborate` on the record instead of storing it again |
| 4. Supersede | allowed only for an active target in the same namespace, when the replacement is itself active, never to itself, and never in a cycle; checked in the same transaction |
| 5. Status | server-owned, as under Trust |
| 6. Concurrency | an integer `revision` on every record, incremented by every mutation (content, anchors, evidence, status). `memory_update` takes the revision it read, applies `UPDATE ... WHERE id=? AND revision=?` with the anchor and lifecycle changes in one transaction, and a mismatch returns the current record and revision. `updated_at` stays for display |
| 7. Offline | no general outbox. Direct edits fail visibly when the service is away. Only `session_close` events are spooled by the client, each with an operation id; only that client replays it. The service keeps an idempotency receipt per id, bound to the client and every input, returns the original result on replay, and keeps a tombstone past the retention so an id never runs twice |

Session close queues an extraction job; the worker stores candidates; the nightly review forgets duplicates and session state; an agent promotes or forgets the rest at session start.

## Failure modes

| Condition | Behaviour |
|---|---|
| Home Assistant or the add-on down | sessions start without bootstrap; session-close events spool on the client; direct writes fail visibly |
| Encoder down | full-text search; embeddings back-filled when it returns |
| Model server down | extraction and answer jobs stay queued; `/health` reads degraded while jobs wait |
| Bootstrap over budget | items that do not fit are left out, and the count is reported |
| A token is replaced | the client is removed and added back for a new token; the add-on keeps only hashes |

## Evaluation

- Retrieval: `tools/retrieval-eval/` (frozen queries against a frozen snapshot).
- Nightly review: `tools/review-eval/` (verdicts against the reviews agents made).
- Bootstrap: the serialized size of every bootstrap, against its budget.
- Write protocol: tests for revision conflicts, supersede rules, idempotent replay, the screen over every field, and server-owned status and identity.
- Use: the query log (the demand signal) shows whether sessions search at all.

## Storage

SQLite with FTS5 and a float32 embeddings table, in the add-on. Measured 2026-09-28: bootstrap over the LAN 4-6 ms, one model turn several seconds. A separate database server would save milliseconds and add a service to run.
