# Changelog

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
