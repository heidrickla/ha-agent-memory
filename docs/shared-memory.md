# Shared memory for Claude Code and Codex

Status: proposed 2026-09-28, not built. Decisions marked OPEN are the owner's.

Goal (the owner, 2026-09-28): one memory both agents read and write, "reads and writes through an mcp server that queues changes or something". agent-memory already is that server for part of the job; this document covers what it must add before each agent's own memory can be switched off.

## Where memory lives today

| Store | Written by | Read by | Loaded how | Size (2026-09-28) |
|---|---|---|---|---|
| Claude Code auto-memory (`~/.claude/projects/<cwd>/memory/`) | Claude | Claude | `MEMORY.md` index in every session, notes on demand | 70 notes, 217 KB; index 12.9 KB, about 3,200 tokens |
| Codex memories (`~/.codex/memories/`) | Codex, generated from its rollouts | Codex | Codex's `memories` feature | `MEMORY.md` 984 lines plus rollout summaries |
| agent-memory (this add-on) | both, through MCP | both | SessionStart hook bootstrap (Claude since 2026-09-19, Codex since 2026-09-28), search on demand | about 81 records |
| `AGENTS.md` (ha-management) | The owner, both agents | both | natively in ha-management; a pointer elsewhere | 270 lines, 40 KB |
| `records/` (ha-management) | both agents | both | read when needed | one file per subject |

The two native memories are where drift comes from. On 2026-09-28 a rule stated to Claude (one model per GPU) existed only in Claude's memory, and an audit moved 70 notes' rules into `AGENTS.md` and their facts into `records/`.

## Boundaries

| Kind of knowledge | Home | Why |
|---|---|---|
| Rules and the owner's standing instructions | `AGENTS.md` | loaded natively by both agents, reviewed in git |
| The estate's measured record (hosts, devices, versions, measurements) | `records/` | one file per subject, git history, `[v]` dates |
| What an agent learned: decisions, preferences, lessons, traps, session summaries, open questions | agent-memory | searchable, scoped by repository, reachable from any repo |
| Anything else in a native memory | nowhere, after migration | native memories are switched off |

A preference the owner states becomes a rule in `AGENTS.md` when it binds both agents everywhere; agent-memory keeps the context around it.

## Read path

1. Session start: the SessionStart hook calls `GET /api/bootstrap` for the working repository's namespace and prints the block as context. Both agents have this hook.
2. Pinned records (new): a record flag `pinned`, set on preferences and constraints that must be in every session. Bootstrap always includes pinned records in scope (global, the hub, the repository) inside a pinned budget; over budget it lists titles and ids only. OPEN: the pinned budget. The measured reference is Claude's index, about 3,200 tokens; bootstrap today is capped at 500.
3. Search on demand: `memory_search`, ranked by embedding cosine when the encoder answers, FTS5 otherwise (0.9.0; offline eval MRR 0.94 against 0.83).
4. Hub reach: a hub namespace (`gitea/heidrickla/ha-management`) searches every repository namespace, as today.

## Write path (the queue)

| Step | Behaviour |
|---|---|
| 1. Write | `memory_store` / `memory_update` over MCP, carrying `agent`, `source_session`, `repository` (fields exist today) |
| 2. Screen | the existing credential screen refuses a secret before anything is stored |
| 3. Duplicate check (new) | before insert, the nearest records by embedding (FTS when the encoder is away) are compared; above a threshold the tool answers with the likely duplicate and requires `supersedes: <id>` or `new: true`. Threshold set on labelled pairs, not guessed |
| 4. Status | direct writes land `active` (today's behaviour), and a preference the owner states is written `active` with no review (the owner, 2026-09-28: "directly written preferences I give don't need review"); extraction from `memory_session_close` lands `candidate` and waits for review (today's behaviour) |
| 5. Concurrency (new) | `memory_update` takes the record's `updated_at` as read; a mismatch is refused with the current version, so two agents cannot overwrite each other silently |
| 6. Outbox (new) | when the service cannot be reached, a local `outbox.jsonl` (under the user's local app data) takes the write with a client-generated id; the SessionStart hook flushes it when the service answers; the id makes a replay idempotent |

The queue that exists today (session close, then an extraction job on the local model, then candidates) stays as it is.

## Migration

| Source | Items | Mapping | Pinned |
|---|---|---|---|
| Claude memory, `feedback` (45) | how the owner wants work done | `preference` or `constraint`, namespace global or the hub | yes for standing preferences |
| Claude memory, `project` (14) | ongoing work, decisions | `architectural-decision`, `next-step`, `repository-fact`, in the repository's namespace | no |
| Claude memory, `reference` (7) | pointers to systems | `repository-fact` or `operational-procedure` | no |
| Claude memory, `user` (4) | who the owner is | `preference`, global | yes |
| Codex memories | generated task summaries | durable items only, imported as candidates and reviewed before they become active (the owner, 2026-09-28: "Codex's memories should be reviewed before imported"); task-scoped summaries are not imported | no |

Every migrated record carries `migrated from <store> <file>` and the date. Items already moved to `AGENTS.md` or `records/` on 2026-09-28 are linked, not duplicated.

## Switch-off order

1. Pinned bootstrap live for both agents; the bootstrap token cost measured per session.
2. Migration imported, candidates reviewed.
3. Parallel week: native memories still on; each session's bootstrap compared with what the native memory would have loaded; anything missing is a defect.
4. Codex: `features.memories = false` in `~/.codex/config.toml`.
5. Claude: `autoMemoryEnabled: false` in `~/.claude/settings.json` (Claude Code 2.1.281: "When false, Claude will not read or write to the auto-memory directory"). Steps 4 and 5 are agent-settings changes: presented as the exact edit for the owner's approval.

## Failure modes

| Condition | Behaviour |
|---|---|
| HA VM or add-on down | sessions start without bootstrap (the hook prints one line saying so); writes go to the outbox; search unavailable |
| Encoder down (rack off) | FTS search, duplicate check by FTS; embeddings back-filled when it returns |
| Bootstrap over budget | titles and ids only, and the count |
| A pinned record goes stale | the anchor sweep files evidence when an anchored path or entity stops resolving |

## Evaluation

- Retrieval: `tools/retrieval-eval/` (72 frozen queries). A fresh snapshot at the current store size with a new frozen query set, before and after migration.
- Bootstrap cost: tokens per session start, against Claude's index today (about 3,200).
- Coverage during the parallel week: pinned items present in every bootstrap they should be in.
- Use: the query log (the demand signal) shows whether sessions search at all.

## Storage

SQLite with FTS5 and a float32 embeddings table, in the add-on. Measured 2026-09-28: bootstrap over the LAN 4-6 ms; reading all 72 Claude memory files 9 ms; one model turn several seconds. A separate database server (Redis, Postgres) would save milliseconds and add a service to run; retrieval quality and tokens in context are what matter.

## Decided

- Preferences the owner states are written `active` with no review (the owner, 2026-09-28).
- Codex memories are reviewed before import: durable items come in as candidates (the owner, 2026-09-28).

## Open

1. Pinned budget size.
2. Who reviews candidates: the agent at session start, or the owner.
