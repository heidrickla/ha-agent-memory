# ha-agent-memory

Home Assistant add-on: one shared, bounded memory for Claude Code, Codex and other agents,
so a session starts from what earlier sessions learned instead of re-reading the repository.
Canonical storage and retrieval never depend on a paid model or on Ollama.

From the plan `Shared Agent Memory on Home Assistant` (2026-09-19): phases 1 to 4 are built here:
storage, namespaces, FTS5 retrieval, progressive MCP tools, HA packaging, and enrichment through
Ollama on desktop (`enrich.py`: redacted notes in, validated candidates out, candidates never
active until promoted, a job done only after its records are written).

| Piece | Choice |
|---|---|
| Storage | SQLite in WAL mode under `/data`, FTS5 over title and content, append-only audit, durable job queue |
| Agent interface | MCP over streamable HTTP with JSON responses: `POST http://<ha>:8765/mcp`; the same tools as REST under `/api/` |
| Namespaces | `github/<owner>/<repo>` and `gitea/<owner>/<repo>` from git remotes (other owners are refused), `projects/<name>`, `topics/<name>`, `global`, `local/<name>` |
| Retrieval order | repository, its linked projects, given topics, global |
| Budgets | bootstrap 500 tokens, search 750, records 2000, packet 4000 (hard limit 8000); every response reports its estimate and truncation |
| Auth | per-client bearer token (sha256 in options) or source address; scopes read, write, delete, export, admin |
| Filtering | credentials, key material, JWTs, URL passwords and the `do-not-store` marker are refused and audited by pattern name |
| Page | ingress only, dark: health, namespaces, audit tail |

## Tools

`memory_namespace`, `memory_bootstrap`, `memory_search`, `memory_get`, `memory_timeline`,
`memory_project_context`, `memory_store`, `memory_update`, `memory_forget`,
`memory_session_close`, `memory_export`. Flow: `bootstrap` → `search` → `get` selected ids,
or `project_context(namespace, task)` for one bounded packet with a reason per record.

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
