"""Canonical storage: SQLite in WAL mode, FTS5 over title and content, an append-only audit,
a durable job queue. Everything here works with Ollama absent.

Tokens are approximated as characters / 4 throughout; every packet reports its estimate.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import uuid

TYPES = (
    "architectural-decision",
    "constraint",
    "requirement",
    "implementation-change",
    "failure-and-fix",
    "open-question",
    "next-step",
    "repository-fact",
    "operational-procedure",
    "preference",
    "session-summary",
)
TYPE_WEIGHT = {
    "constraint": 1.5,
    "architectural-decision": 1.4,
    "failure-and-fix": 1.3,
    "operational-procedure": 1.2,
    "preference": 1.1,
    "open-question": 1.0,
    "next-step": 1.0,
    "requirement": 1.0,
    "repository-fact": 0.9,
    "implementation-change": 0.8,
    "session-summary": 0.6,
}
SCHEMA = """
create table if not exists memories (
  id text primary key, created_at text not null, updated_at text not null,
  namespace text not null, project_scopes text not null default '[]', topics text not null default '[]',
  type text not null, agent text not null default '', source_session text not null default '',
  repository text not null default '', commit_sha text not null default '',
  title text not null, content text not null, confidence real not null default 0.8,
  status text not null default 'active', sensitivity text not null default 'internal',
  supersedes text, expires_at text, deleted_at text, enrichment text not null default ''
);
create index if not exists memories_ns on memories(namespace, status);
create table if not exists audit (
  seq integer primary key autoincrement, at text not null, client text not null, action text not null,
  namespace text not null default '', record_id text not null default '', detail text not null default ''
);
create table if not exists jobs (
  id text primary key, created_at text not null, kind text not null, namespace text not null,
  payload text not null, status text not null default 'queued', attempts integer not null default 0,
  last_error text not null default '', done_at text, model text not null default ''
);
create index if not exists jobs_status on jobs(status, created_at);
"""
FTS = """
create virtual table if not exists memories_fts using fts5(title, content, content='memories', content_rowid='rowid');
create trigger if not exists memories_ai after insert on memories begin
  insert into memories_fts(rowid, title, content) values (new.rowid, new.title, new.content);
end;
create trigger if not exists memories_ad after delete on memories begin
  insert into memories_fts(memories_fts, rowid, title, content) values ('delete', old.rowid, old.title, old.content);
end;
create trigger if not exists memories_au after update on memories begin
  insert into memories_fts(memories_fts, rowid, title, content) values ('delete', old.rowid, old.title, old.content);
  insert into memories_fts(rowid, title, content) values (new.rowid, new.title, new.content);
end;
"""


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=normal")
        self.db.executescript(SCHEMA)
        try:
            self.db.executescript(FTS)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    # ------------------------------------------------------------- helpers
    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["project_scopes"] = json.loads(d.pop("project_scopes") or "[]")
        d["topics"] = json.loads(d.pop("topics") or "[]")
        d["commit"] = d.pop("commit_sha")
        return d

    def audit(self, client: str, action: str, namespace: str = "", record_id: str = "", detail: str = "") -> None:
        with self.lock:
            self.db.execute("insert into audit(at, client, action, namespace, record_id, detail) values (?,?,?,?,?,?)", (now_iso(), client, action, namespace, record_id, detail[:500]))

    # ------------------------------------------------------------- writes
    def store(self, rec: dict, client: str) -> dict:
        rid = rec.get("id") or str(uuid.uuid4())
        ts = now_iso()
        row = (
            rid,
            ts,
            ts,
            rec["namespace"],
            json.dumps(sorted(set(rec.get("project_scopes") or []))),
            json.dumps(sorted(set(rec.get("topics") or []))),
            rec.get("type") or "repository-fact",
            rec.get("agent") or client,
            rec.get("source_session") or "",
            rec.get("repository") or "",
            rec.get("commit") or "",
            rec["title"].strip(),
            rec["content"].strip(),
            float(rec.get("confidence") if rec.get("confidence") is not None else 0.8),
            rec.get("status") or "active",
            rec.get("sensitivity") or "internal",
            rec.get("supersedes"),
            rec.get("expires_at"),
            None,
            rec.get("enrichment") or "",
        )
        with self.lock:
            self.db.execute(
                "insert into memories(id, created_at, updated_at, namespace, project_scopes, topics, type, agent, source_session, repository, commit_sha, title, content, confidence, status, sensitivity, supersedes, expires_at, deleted_at, enrichment) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            if rec.get("supersedes"):
                self.db.execute("update memories set status='superseded', updated_at=? where id=? and status='active'", (ts, rec["supersedes"]))
        self.audit(client, "store", rec["namespace"], rid, rec.get("type") or "")
        return self.get(rid)

    def update(self, rid: str, changes: dict, client: str) -> dict | None:
        allowed = {"title", "content", "type", "confidence", "status", "topics", "project_scopes", "expires_at", "sensitivity"}
        sets, vals = [], []
        for k, v in changes.items():
            if k not in allowed:
                continue
            if k in ("topics", "project_scopes"):
                v = json.dumps(sorted(set(v or [])))
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return self.get(rid)
        sets.append("updated_at=?")
        vals += [now_iso(), rid]
        with self.lock:
            cur = self.db.execute(f"update memories set {', '.join(sets)} where id=? and deleted_at is null", vals)
        if cur.rowcount == 0:
            return None
        self.audit(client, "update", record_id=rid, detail=",".join(sorted(k for k in changes if k in allowed)))
        return self.get(rid)

    def forget(self, rid: str, client: str, hard: bool = False) -> bool:
        with self.lock:
            if hard:
                cur = self.db.execute("delete from memories where id=?", (rid,))
            else:
                cur = self.db.execute("update memories set deleted_at=?, status='deleted' where id=? and deleted_at is null", (now_iso(), rid))
        if cur.rowcount:
            self.audit(client, "forget_hard" if hard else "forget", record_id=rid)
        return bool(cur.rowcount)

    def purge(self, deleted_days: int) -> int:
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=deleted_days)).isoformat(timespec="seconds")
        with self.lock:
            cur = self.db.execute("delete from memories where deleted_at is not null and deleted_at < ?", (cutoff,))
        return cur.rowcount

    # ------------------------------------------------------------- reads
    def get(self, rid: str, include_deleted: bool = False) -> dict | None:
        r = self.db.execute("select * from memories where id=?", (rid,)).fetchone()
        if not r or (r["deleted_at"] and not include_deleted):
            return None
        return self._row(r)

    def _live(self, statuses: tuple[str, ...]) -> str:
        marks = ",".join("?" for _ in statuses)
        return f"deleted_at is null and status in ({marks}) and (expires_at is null or expires_at > '{now_iso()}')"

    def search(self, query: str, scopes: list[str], limit: int = 20, statuses: tuple[str, ...] = ("active",), types: tuple[str, ...] = ()) -> list[dict]:
        """Ranked hits: FTS relevance times scope, type and confidence weights. Snippets, not records."""
        marks = ",".join("?" for _ in scopes)
        live = self._live(statuses)
        params: list = []
        if self.fts and query.strip():
            sql = f"select m.*, bm25(memories_fts, 2.0, 1.0) as score from memories_fts join memories m on m.rowid = memories_fts.rowid where memories_fts match ? and m.namespace in ({marks}) and {live}"
            params = [fts_query(query), *scopes, *statuses]
        else:
            like = f"%{query.strip()}%"
            sql = f"select m.*, 0.0 as score from memories m where (m.title like ? or m.content like ?) and m.namespace in ({marks}) and {live}"
            params = [like, like, *scopes, *statuses]
        if types:
            sql += " and m.type in (" + ",".join("?" for _ in types) + ")"
            params += list(types)
        rows = self.db.execute(sql, params).fetchall()
        hits = []
        for r in rows:
            d = self._row(r)
            base = -float(r["score"]) if self.fts else 1.0  # bm25 is negative, better is lower
            scope_rank = scopes.index(d["namespace"]) if d["namespace"] in scopes else len(scopes)
            scope_w = 1.0 + 0.5 * max(0, len(scopes) - scope_rank) / max(1, len(scopes))
            d["rank"] = round((base + 0.1) * scope_w * TYPE_WEIGHT.get(d["type"], 1.0) * (0.5 + d["confidence"]), 4)
            d["why"] = f"scope #{scope_rank + 1} {d['namespace']}, type {d['type']}, confidence {d['confidence']:.2f}"
            hits.append(d)
        hits.sort(key=lambda h: -h["rank"])
        return hits[:limit]

    def by_namespace(self, scopes: list[str], types: tuple[str, ...] = (), limit: int = 50, statuses: tuple[str, ...] = ("active",)) -> list[dict]:
        marks = ",".join("?" for _ in scopes)
        sql = f"select * from memories where namespace in ({marks}) and {self._live(statuses)}"
        params: list = [*scopes, *statuses]
        if types:
            sql += " and type in (" + ",".join("?" for _ in types) + ")"
            params += list(types)
        sql += " order by updated_at desc limit ?"
        params.append(limit)
        return [self._row(r) for r in self.db.execute(sql, params).fetchall()]

    def timeline(self, namespace: str, limit: int = 30) -> list[dict]:
        rows = self.db.execute("select * from memories where namespace=? and deleted_at is null order by created_at desc limit ?", (namespace, limit)).fetchall()
        return [self._row(r) for r in rows]

    def namespaces(self) -> list[dict]:
        rows = self.db.execute("select namespace, count(*) as n, max(updated_at) as last from memories where deleted_at is null group by namespace order by namespace").fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        n = self.db.execute("select count(*) from memories where deleted_at is null").fetchone()[0]
        cand = self.db.execute("select count(*) from memories where deleted_at is null and status='candidate'").fetchone()[0]
        q = self.db.execute("select status, count(*) from jobs group by status").fetchall()
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"memories": n, "candidates": cand, "jobs": {r[0]: r[1] for r in q}, "db_bytes": size, "fts5": self.fts, "namespaces": len(self.namespaces())}

    def integrity(self) -> str:
        return self.db.execute("pragma integrity_check").fetchone()[0]

    def rebuild_fts(self) -> None:
        if self.fts:
            with self.lock:
                self.db.execute("insert into memories_fts(memories_fts) values ('rebuild')")

    # ------------------------------------------------------------- jobs
    def enqueue(self, kind: str, namespace: str, payload: dict) -> str:
        jid = str(uuid.uuid4())
        with self.lock:
            self.db.execute("insert into jobs(id, created_at, kind, namespace, payload) values (?,?,?,?,?)", (jid, now_iso(), kind, namespace, json.dumps(payload)))
        return jid

    def next_job(self) -> dict | None:
        r = self.db.execute("select * from jobs where status='queued' order by created_at limit 1").fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    def job_done(self, jid: str, model: str = "") -> None:
        with self.lock:
            self.db.execute("update jobs set status='done', done_at=?, model=? where id=?", (now_iso(), model, jid))

    def job_failed(self, jid: str, error: str, give_up: bool) -> None:
        with self.lock:
            self.db.execute("update jobs set status=?, attempts=attempts+1, last_error=? where id=?", ("failed" if give_up else "queued", error[:300], jid))

    def job_attempts(self, jid: str) -> int:
        r = self.db.execute("select attempts from jobs where id=?", (jid,)).fetchone()
        return int(r[0]) if r else 0

    def queue_depth(self) -> dict:
        r = self.db.execute("select count(*), min(created_at) from jobs where status='queued'").fetchone()
        return {"queued": r[0], "oldest": r[1]}

    # ------------------------------------------------------------- export
    def export(self, out_dir: str) -> dict:
        os.makedirs(out_dir, exist_ok=True)
        rows = [self._row(r) for r in self.db.execute("select * from memories where deleted_at is null order by namespace, created_at").fetchall()]
        with open(os.path.join(out_dir, "memories.jsonl"), "w", encoding="utf-8") as fh:
            for d in rows:
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        by_ns: dict[str, list[dict]] = {}
        for d in rows:
            by_ns.setdefault(d["namespace"], []).append(d)
        for ns, items in by_ns.items():
            name = ns.replace("/", "__") + ".md"
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
                fh.write(f"# {ns}\n\n")
                for d in items:
                    fh.write(f"## {d['title']}\n\n{d['type']} · {d['status']} · confidence {d['confidence']:.2f} · {d['updated_at']} · {d['id']}\n\n{d['content']}\n\n")
        return {"records": len(rows), "namespaces": len(by_ns), "dir": out_dir}


def fts_query(text: str) -> str:
    """Plain words become an OR-joined FTS5 query; quotes and operators from the caller are escaped."""
    words = [w.strip('"').replace('"', "") for w in text.split()]
    words = [w for w in words if w]
    if not words:
        return '""'
    return " OR ".join(f'"{w}"' for w in words[:24])
