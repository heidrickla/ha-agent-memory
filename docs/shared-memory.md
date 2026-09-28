# Shared memory for Claude Code and Codex

Status: revision 2, 2026-09-28, after Codex review rounds 1 to 3 (recorded in ha-management `records/svc-agent-memory.md`). Built in agent-memory 0.11.1 and the ha-management hooks of the same day: per-agent tokens (Claude and Codex), receipts bound to the client and every input with tombstones past the retention, one screen for every database write, one transaction per mutation, the bootstrap budget on the wire, relative `path:` anchors resolved against their repository's checkout. The IP client `desktop` stays configured beside the token clients (owner's decision). The advisory duplicate check is deferred until labelled pairs exist.

Known gaps after the build (2026-09-28):
- Supersession happens only when a record is created, so a reviewed candidate cannot take over from an older active record.
- Every closed session adds a candidate summary, so candidates accumulate until the agent reviews them at session start.
- Whole-response budgeting shows 5 items and leaves 11 out for ha-agent-memory's bootstrap at 500 tokens (2,000 bytes).
- No per-field length limits beyond namespace and topics: an oversized title is stored, it just never fits bootstrap.
- Search, get and project-context packing still admit their first item whatever its size.
- Tokens as user environment variables are readable by every process, both agents' shells included.

Goal (the owner, 2026-09-28): one memory both agents read and write, "reads and writes through an mcp server that queues changes or something". agent-memory already is that server for part of the job; this document covers what it must change and add before each agent's own memory can be switched off.

## Where memory lives today

| Store | Written by | Read by | Loaded how | Size (2026-09-28) |
|---|---|---|---|---|
| Claude Code auto-memory (`~/.claude/projects/<cwd>/memory/`) | Claude | Claude | `MEMORY.md` index in every session, notes on demand | 70 notes, 217 KB; index 12.9 KB, about 3,200 tokens |
| Codex memories (`~/.codex/memories/`) | Codex, generated from its rollouts | Codex | Codex's `memories` feature | `MEMORY.md` 984 lines plus rollout summaries |
| agent-memory (this add-on) | both, through MCP | both | SessionStart hook bootstrap (Claude since 2026-09-19, Codex since 2026-09-28), search on demand | 110 records (75 active, 35 candidate) |
| `AGENTS.md` (ha-management) | The owner, both agents | both | natively in ha-management; a hook or global pointer elsewhere | about 270 lines, 40 KB |
| `records/` (ha-management) | both agents | both | read when needed | one file per subject |

The two native memories are where drift comes from: on 2026-09-28 a rule stated to Claude (one model per GPU) existed only in Claude's memory, and an audit moved 70 notes' rules into `AGENTS.md` and their facts into `records/`.

## Boundaries

| Kind of knowledge | Home | Loaded into every session |
|---|---|---|
| Rules and the owner's standing preferences | `AGENTS.md` | yes, the only always-loaded source |
| The estate's measured record (hosts, devices, versions, measurements) | `records/` | no, read when needed |
| What an agent learned: decisions, lessons, traps, open questions, session summaries | agent-memory | a small scoped bootstrap, then search |
| Anything else in a native memory | nowhere, after migration | native memories are switched off |

There is no pinning: anything every session must see is a rule and belongs in `AGENTS.md` (round 1, DR-001). A preference the owner states is written to agent-memory `active` and, when it binds both agents everywhere, also added to `AGENTS.md`.

## Trust

A record one agent writes becomes context in the other agent's sessions, so the service decides who wrote it and what state it is in.

| Rule | Today | Change |
|---|---|---|
| Each agent authenticates with its own bearer token | desktop is authorized by source IP (`server.py` `identify`), and both agents share that IP | a client entry per agent with `token_sha256`; the IP-only entry removed (DR-002) |
| The writer's identity is set by the server from the token | the caller's `agent` field is stored as sent | the server overwrites `agent` (DR-002) |
| Status is set by the server | a caller can send any `status` | direct writes become `active`; extraction and import go through internal operations that write `candidate`; the status is validated against an enum (DR-010) |
| Session summaries are reviewed like extractions | stored `active` and shown in the next bootstrap | stored `candidate`, or excluded from bootstrap and search until promoted (DR-003) |
| Every persisted string is screened | the screen covers title and content | screen every stored field (topics, scopes, anchors, repository, session, notes); refuse a URL anchor with credentials; a `do-not-store` note is refused, not redacted (DR-008) |
| Transport | plain HTTP on the LAN | unchanged; accepted residual: the LAN is the estate's own, and the tokens bind identity |

Token handling: each token is generated on desktop and stored where only its client reads it (Codex `bearer_token_env_var`, Claude `headersHelper`). Only its sha256 goes to the add-on options. No token value is printed, logged or passed through an agent's context. Wiring each client is an agent-settings change, presented to the owner as the exact edit.

## Read path

1. Session start: the SessionStart hook calls `GET /api/bootstrap` for the working repository's namespace. The budget is enforced on the bytes on the wire (the REST body, or the whole JSON-RPC reply over MCP), and an item that does not fit is never admitted, the first one included (DR-004).
2. Search on demand: `memory_search`, cosine over record embeddings when the encoder answers, FTS5 otherwise (0.9.0, offline eval MRR 0.94 against 0.83).
3. Hub reach: a hub namespace searches every repository namespace, as today.
4. Staleness: each anchor type names the process that checks it and where it runs. Only entity anchors are swept today; path, host and URL anchors are reported by clients. Unchecked, unavailable and missing are tracked separately, and a record whose anchor is missing is flagged in results (DR-009).

## Write path

| Step | Behaviour |
|---|---|
| 1. Write | `memory_store` / `memory_update` over MCP, authenticated per agent |
| 2. Screen | every persisted field, as under Trust |
| 3. Duplicate check | advisory only. The nearest records by embedding (FTS when the encoder is away, with its own threshold) come back as `possible_duplicate_of`, and the caller chooses to corroborate, merge or create. A weak or offline match is never proof of uniqueness. Thresholds are set per backend on labelled pairs (DR-005) |
| 4. Supersede | separate from duplicates. Allowed only for an active target in the same namespace, when the replacement is itself active, never to itself, and never in a cycle; checked in the same transaction (DR-005, DR-010) |
| 5. Status | server-owned, as under Trust. A preference the owner states is `active` with no review (settled) |
| 6. Concurrency | an integer `revision` on every record, incremented by every mutation (content, anchors, evidence, status). `memory_update` takes the revision it read, applies `UPDATE ... WHERE id=? AND revision=?` with the anchor and lifecycle changes in one transaction, and a mismatch returns the current record and revision. `updated_at` stays for display (DR-006) |
| 7. Offline | no general outbox. Direct edits fail visibly when the service is away. Only `session_close` events are spooled locally, each with an operation id and the client that made it; only that client's next session replays it. The service keeps an idempotency receipt per id, bound to the client and every input, returns the original result on replay, and keeps a tombstone past the retention so an id never runs twice (DR-007) |

The existing queue (session close, then an extraction job, then candidates) stays.

## Migration

Every native item gets one disposition in a manifest before anything is switched off (DR-011).

| Disposition | Meaning |
|---|---|
| `agents-rule` | already, or now, a rule in `AGENTS.md` (link) |
| `records-fact` | a measured fact; lives in `records/` (link), never imported into memory |
| `memory` | learned, durable knowledge; imported as `candidate`, except a preference the owner stated, which is `active` |
| `discard` | task-scoped, superseded or wrong, with the reason |
| `unresolved` | needs the owner; blocks the switch-off |

Imported records carry structured provenance fields, not prose: source store, path, content hash, import batch. Codex memories come in as candidates and are reviewed before they become active (settled).

## Switch-off

A switch-off is allowed when (DR-012):
1. every manifest item has a disposition, and none is `unresolved`;
2. every `memory` item has a retrieval query that returns it (a shadow corpus checked by the existing eval tooling), with negative controls that return nothing;
3. a fresh session in each agent, in a repository other than ha-management, shows the bootstrap and one search answering;
4. The owner approves the settings change: Codex `features.memories = false` in `~/.codex/config.toml`, then Claude `autoMemoryEnabled: false` in `~/.claude/settings.json` (Claude Code 2.1.281: "When false, Claude will not read or write to the auto-memory directory").

Elapsed time is not a criterion.

## Failure modes

| Condition | Behaviour |
|---|---|
| HA VM or add-on down | sessions start without bootstrap (the hook prints one line); session-close events spool; direct writes fail visibly |
| Encoder down (rack off) | FTS search, FTS duplicate check with its own threshold; embeddings back-filled when it returns |
| Bootstrap over budget | items that do not fit are left out, and the count is reported |
| A token leaks | regenerate it; the add-on option carries only the hash, so a rotation is one option change and one client-side store |

## Evaluation

- Retrieval: `tools/retrieval-eval/` (72 frozen queries), plus the shadow corpus built from the manifest.
- Bootstrap: the serialized size of every bootstrap, against its budget.
- Write protocol: tests for revision conflicts, supersede rules, idempotent replay, the screen over every field, and server-owned status and identity.
- Use: the query log (the demand signal) shows whether sessions search at all.

## Storage

SQLite with FTS5 and a float32 embeddings table, in the add-on. Measured 2026-09-28: bootstrap over the LAN 4-6 ms, reading all 72 Claude memory files 9 ms, one model turn several seconds. A separate database server would save milliseconds and add a service to run.

## Decided

- Preferences the owner states are written `active` with no review (the owner, 2026-09-28).
- Codex memories are reviewed before import (the owner, 2026-09-28).
- `AGENTS.md` is the only always-loaded source; no pinning; per-agent tokens; server-owned identity and status; integer revisions; only session-close events spool offline; a manifest decides migration (the owner chose Codex's simpler design, 2026-09-28).
- Candidates are reviewed by the agent at session start (the owner, 2026-09-28).
