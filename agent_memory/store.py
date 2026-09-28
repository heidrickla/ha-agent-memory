"""Canonical storage: SQLite in WAL mode, FTS5 over title and content, an append-only audit,
a durable job queue. Everything here works with Ollama absent.

Tokens are approximated as characters / 4 throughout; every packet reports its estimate.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
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
ANCHOR_RE = re.compile(r"^(?:entity|path|host|url):\S.{0,300}$")
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
create table if not exists evidence (
  id text primary key, record_id text not null, at text not null,
  stance text not null default 'corroborates', quote text not null,
  agent text not null default '', source_session text not null default ''
);
create index if not exists evidence_record on evidence(record_id, stance);
create table if not exists queries (
  id text primary key, at text not null, namespace text not null, client text not null,
  kind text not null default 'search', query text not null, norm text not null,
  hits integer not null default 0, top_rank real not null default 0.0
);
create index if not exists queries_ns on queries(namespace, norm);
create table if not exists anchors (
  record_id text not null, anchor text not null, last_state text not null default '',
  last_checked text, primary key (record_id, anchor)
);
create table if not exists questions (
  id text primary key, created_at text not null, updated_at text not null,
  namespace text not null, question text not null, norm text not null,
  answer text not null default '', answered_at text, model text not null default '',
  source_ids text not null default '[]', basis text not null default '',
  status text not null default 'active', asked integer not null default 0
);
create unique index if not exists questions_key on questions(namespace, norm);
"""
"""`questions` holds a standing answer per question, so reading settled knowledge is a database read.

`memory_bootstrap` returns an index and `memory_project_context` retrieves afresh every time. Neither
is a maintained answer, so a session that needs one rediscovers it. A question here is answered once
in the background and read for nothing afterwards.

`basis` is what makes that safe. It digests the id, the timestamp AND the text of every record the
answer was written from, so staleness is a string comparison with no model and no retrieval: if a
source record changed, or a new record now matches, the answer is known to be behind. `source_ids`
travels with it for the same reason evidence travels with a belief: an answer nobody can trace back
to records is an assertion.
"""
"""`queries` is where standing questions come from.

Reads were not recorded at all, so what sessions actually kept asking was thrown away. Declaring
the questions by hand would guess at the demand; measuring it is the same move as deriving
confidence from evidence rather than asserting it. The OUTCOME is stored beside the query because
hit count and top rank cannot be reconstructed afterwards, and the signal worth acting on is not
"asked often" but "asked often and answered badly": a question the store already answers well
needs no standing answer.

`evidence` exists because `confidence` was a number the writer asserted and nobody could audit.

A later session that saw the same thing again had nowhere to say so, and one that found a record
WRONG could only supersede it, which throws the history away and leaves no trace of the
disagreement. A belief is now earned instead: each row is one observation, quoted, attributed and
dated, that either corroborates or contradicts a record. `effective_confidence` is derived from
those, and a contradiction lowers it in the open rather than replacing the record silently.
"""
# Stemmed, so "restarting" finds "restart" and "checks" finds "check". A database indexed before
# stemming is rebuilt once when opened (`Store._index`).
FTS = """
create virtual table if not exists memories_fts using fts5(title, content, content='memories', content_rowid='rowid', tokenize='porter unicode61 remove_diacritics 2');
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


STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "do",
        "does",
        "did",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "why",
        "how",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "about",
        "into",
        "over",
        "and",
        "or",
        "not",
        "no",
        "yes",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "we",
        "you",
        "they",
        "he",
        "she",
        "our",
        "your",
        "their",
        "my",
        "me",
        "us",
        "any",
        "some",
        "can",
        "could",
        "should",
        "would",
        "will",
        "there",
        "here",
        "as",
        "if",
        "then",
        "than",
        "so",
        "such",
        "have",
        "has",
        "had",
        "get",
        "got",
        "need",
        "want",
        "know",
        "tell",
        "show",
        "find",
    ]
)


def normalise(text: str) -> str:
    """A question reduced to its distinguishing terms, sorted, so phrasing does not split a group.

    This narrows the gap between two phrasings; it does not close it. "Which host runs the HA VM?"
    gives "ha host runs vm" and "HA VM host?" gives "ha host vm", and the 0.6 overlap threshold in
    `question_candidates` is what makes them one question. Numbers and hyphenated names survive,
    because `prod-5` and `192.0.2.10` are exactly what the questions are about.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-._:/") else " " for c in text.lower())
    words = [w.strip("-._:/") for w in cleaned.split()]
    keep = sorted({w for w in words if w and w not in STOPWORDS and len(w) > 1})
    return " ".join(keep)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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
        self.fts = self._index(FTS, "memories_fts", "porter")

    def _index(self, ddl: str, table: str, tokenizer: str) -> bool:
        """Create the full-text index, or rebuild one made with another tokenizer. False when this
        SQLite has no FTS5, and search then falls back to LIKE."""
        try:
            row = self.db.execute("select sql from sqlite_master where type='table' and name=?", (table,)).fetchone()
            stale = row is not None and tokenizer not in row[0]
            if stale:
                self.db.execute(f"drop table {table}")
            self.db.executescript(ddl)
            if stale:
                self.db.execute(f"insert into {table}({table}) values ('rebuild')")
            return True
        except sqlite3.OperationalError:
            return False

    # ------------------------------------------------------------- helpers
    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        with self.lock:
            d["anchors"] = [a[0] for a in self.db.execute("select anchor from anchors where record_id=? order by anchor", (d["id"],))]
        d["project_scopes"] = json.loads(d.pop("project_scopes") or "[]")
        d["topics"] = json.loads(d.pop("topics") or "[]")
        d["commit"] = d.pop("commit_sha")
        d.update(self.weight(d["id"], d["confidence"]))
        return d

    def weight(self, rid: str, asserted: float) -> dict:
        """What the evidence says about one record: counts, and a confidence derived from them.

        An unwitnessed record keeps the confidence its writer asserted, so nothing already stored
        changes meaning. From the first corroboration the asserted number stops deciding: each one
        closes a third of the remaining distance to certainty, and each contradiction takes half of
        what is left, so one credible refusal outweighs two agreements. Both counts travel with the
        record, because a 0.9 standing on four witnesses and a 0.9 somebody typed are not the same
        claim and the reader has to be able to tell them apart.
        """
        with self.lock:
            rows = self.db.execute("select stance, count(*) n from evidence where record_id=? group by stance", (rid,)).fetchall()
        counts = {r["stance"]: r["n"] for r in rows}
        pro, con = counts.get("corroborates", 0), counts.get("contradicts", 0)
        if not pro and not con:
            return {"proof_count": 0, "refutation_count": 0, "effective_confidence": round(float(asserted), 4)}
        conf = 0.5
        for _ in range(pro):
            conf += (1.0 - conf) / 3.0
        for _ in range(con):
            conf /= 2.0
        return {"proof_count": pro, "refutation_count": con, "effective_confidence": round(conf, 4)}

    def corroborate(self, rid: str, quote: str, stance: str, client: str, agent: str = "", source_session: str = "") -> dict | None:
        """Attach one observation to an existing record instead of storing a near-duplicate.

        This is the operation the store was missing. Two sessions learning the same fact used to
        produce two records that then disagreed as one aged; a session finding a record wrong had to
        supersede it and erase the trail.
        """
        if stance not in ("corroborates", "contradicts"):
            raise ValueError("stance must be corroborates or contradicts")
        rec = self.get(rid)
        if rec is None:
            return None
        with self.lock:
            self.db.execute(
                "insert into evidence(id, record_id, at, stance, quote, agent, source_session) values (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), rid, now_iso(), stance, quote.strip()[:1000], agent or client, source_session),
            )
            self.db.execute("update memories set updated_at=? where id=?", (now_iso(), rid))
        self.audit(client, stance, rec["namespace"], rid, quote.strip()[:200])
        return self.get(rid)

    def evidence(self, rid: str, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select stance, quote, agent, source_session, at from evidence where record_id=? order by at desc limit ?", (rid, limit)).fetchall()
        return [dict(r) for r in rows]

    def audit(self, client: str, action: str, namespace: str = "", record_id: str = "", detail: str = "") -> None:
        with self.lock:
            self.db.execute("insert into audit(at, client, action, namespace, record_id, detail) values (?,?,?,?,?,?)", (now_iso(), client, action, namespace, record_id, detail[:500]))

    # ------------------------------------------------------------- writes
    def store(self, rec: dict, client: str) -> dict:
        # Validated before the insert: a refused store must write nothing.
        self.valid_anchors(rec.get("anchors") or [])
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
        self.set_anchors(rid, rec.get("anchors") or [])
        self.audit(client, "store", rec["namespace"], rid, rec.get("type") or "")
        return self.get(rid)

    @staticmethod
    def valid_anchors(anchors: object) -> list[str]:
        if not isinstance(anchors, (list, tuple)):
            # A client with a stale tool schema sends the list as one string; iterating it
            # validated single characters.
            raise ValueError(f"anchors must be a list of strings, got {type(anchors).__name__}")
        wanted = sorted({str(a).strip() for a in anchors})
        bad = [a for a in wanted if not ANCHOR_RE.match(a)]
        if bad:
            raise ValueError(f"anchors must be entity:, path:, host: or url: followed by a value: {bad}")
        return wanted

    def set_anchors(self, rid: str, anchors: list[str]) -> None:
        """Replace a record's anchors, keeping the check state of those that stay."""
        wanted = self.valid_anchors(anchors)
        with self.lock:
            if wanted:
                marks = ",".join("?" for _ in wanted)
                self.db.execute(f"delete from anchors where record_id=? and anchor not in ({marks})", (rid, *wanted))
            else:
                self.db.execute("delete from anchors where record_id=?", (rid,))
            for a in wanted:
                self.db.execute("insert or ignore into anchors(record_id, anchor) values (?, ?)", (rid, a))

    def anchors(self, kind: str) -> list[dict]:
        """Anchors of one kind on live active records, with their last check."""
        with self.lock:
            rows = self.db.execute(
                "select a.record_id, a.anchor, a.last_state, a.last_checked from anchors a join memories m on m.id = a.record_id "
                "where m.status='active' and m.deleted_at is null and a.anchor like ? order by a.record_id, a.anchor",
                (kind + ":%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def report_anchor(self, rid: str, anchor: str, state: str, client: str) -> dict:
        """Record one check. Evidence is filed only on a transition, so a sweep repeated every
        few hours does not pile contradictions onto one record."""
        if state not in ("ok", "missing"):
            raise ValueError("state must be ok or missing")
        with self.lock:
            row = self.db.execute("select last_state from anchors where record_id=? and anchor=?", (rid, anchor)).fetchone()
        if row is None:
            raise ValueError(f"record {rid} has no anchor {anchor}")
        before = row[0]
        with self.lock:
            self.db.execute("update anchors set last_state=?, last_checked=? where record_id=? and anchor=?", (state, now_iso(), rid, anchor))
        filed = ""
        if state == "missing" and before != "missing":
            self.corroborate(rid, f"anchor {anchor} no longer resolves", "contradicts", client)
            filed = "contradicts"
        elif state == "ok" and before == "missing":
            self.corroborate(rid, f"anchor {anchor} resolves again", "corroborates", client)
            filed = "corroborates"
        return {"record_id": rid, "anchor": anchor, "state": state, "was": before or "unchecked", "filed": filed}

    def update(self, rid: str, changes: dict, client: str) -> dict | None:
        if "anchors" in changes:
            if self.get(rid) is None:
                return None
            self.set_anchors(rid, list(changes["anchors"] or []))
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
            # Queries age out on the same clock. They are a demand signal, not a record.
            self.db.execute("delete from queries where at < ?", (cutoff,))
        return cur.rowcount

    def note_query(self, namespace: str, client: str, kind: str, query: str, hits: int, top_rank: float) -> None:
        """Record one read and how well it was answered. Never raises: a failure to measure demand
        must not fail the search that a session is actually waiting on."""
        try:
            n = normalise(query)
            if not n:
                return
            with self.lock:
                self.db.execute(
                    "insert into queries(id, at, namespace, client, kind, query, norm, hits, top_rank) values (?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), now_iso(), namespace, client, kind, query.strip()[:300], n, int(hits), float(top_rank)),
                )
        except sqlite3.Error:
            pass

    def add_question(self, namespace: str, question: str, client: str, asked: int = 0) -> dict:
        """Declare a standing question. Asking the same thing twice returns the existing one."""
        n = normalise(question)
        if not n:
            raise ValueError("a question of nothing but filler is not a question")
        ts = now_iso()
        with self.lock:
            row = self.db.execute("select * from questions where namespace=? and norm=?", (namespace, n)).fetchone()
            if row:
                if asked > row["asked"]:
                    self.db.execute("update questions set asked=?, updated_at=? where id=?", (asked, ts, row["id"]))
                return self.get_question(row["id"])
            qid = str(uuid.uuid4())
            self.db.execute(
                "insert into questions(id, created_at, updated_at, namespace, question, norm, asked) values (?,?,?,?,?,?,?)",
                (qid, ts, ts, namespace, question.strip()[:300], n, int(asked)),
            )
        self.audit(client, "question", namespace, qid, question.strip()[:200])
        return self.get_question(qid)

    def get_question(self, qid: str) -> dict | None:
        with self.lock:
            r = self.db.execute("select * from questions where id=?", (qid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["source_ids"] = json.loads(d["source_ids"] or "[]")
        d["stale"] = d["basis"] != self.basis_for(d["namespace"], d["question"])
        return d

    def find_question(self, scopes: list[str], question: str) -> dict | None:
        n = normalise(question)
        if not n or not scopes:
            return None
        marks = ",".join("?" for _ in scopes)
        with self.lock:
            rows = self.db.execute(f"select id, norm from questions where namespace in ({marks}) and status='active'", scopes).fetchall()
        key = frozenset(n.split())
        best = max(rows, key=lambda r: jaccard(frozenset(r["norm"].split()), key), default=None)
        if best is None or jaccard(frozenset(best["norm"].split()), key) < 0.6:
            return None
        return self.get_question(best["id"])

    def sources_for(self, namespace: str, question: str, limit: int = 8) -> list[dict]:
        """The records a standing answer is written from: the same search a session would run."""
        return self.search(question, [namespace], limit=limit)

    def basis_for(self, namespace: str, question: str) -> str:
        """A digest of the records that answer this question, as they stand right now.

        Comparing this to the stored `basis` is how staleness is known without a model: it moves
        when a source record is edited and when a new record starts matching.
        """
        rows = self.sources_for(namespace, question)
        # The CONTENT is digested, not just the id and the timestamp. `now_iso` has second
        # resolution, so a record edited within the same second keeps its `updated_at`, and a
        # timestamp-only basis would call the answer fresh while the material under it had changed.
        material = "|".join(f"{r['id']}:{r['updated_at']}:{r['title']}:{r['content']}" for r in sorted(rows, key=lambda r: r["id"]))
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def set_answer(self, qid: str, answer: str, model: str, source_ids: list[str], basis: str) -> dict | None:
        ts = now_iso()
        with self.lock:
            self.db.execute(
                "update questions set answer=?, answered_at=?, model=?, source_ids=?, basis=?, updated_at=? where id=?",
                (answer.strip()[:4000], ts, model, json.dumps(source_ids), basis, ts, qid),
            )
        return self.get_question(qid)

    def stale_questions(self, limit: int = 20) -> list[dict]:
        """Questions never answered, or whose sources have moved since they were."""
        with self.lock:
            rows = self.db.execute("select id from questions where status='active' order by answered_at is not null, updated_at limit ?", (limit * 4,)).fetchall()
        out = [q for q in (self.get_question(r["id"]) for r in rows) if q and q["stale"]]
        return out[:limit]

    def question_candidates(self, scopes: list[str], min_asked: int = 2, limit: int = 20) -> list[dict]:
        """Recurring questions the store answers badly, worst first, with the queries as evidence.

        Grouping is on the normalised token set, so word order and filler do not split a question;
        groups sharing most of their terms are then merged, because "which host runs the HA VM" and
        "HA VM host" are one question asked twice, and counting them separately is how a real
        demand signal gets buried under near-duplicates.

        `need` rises with how often it is asked and how poorly it came back. A question already
        answered well scores near zero however popular it is -- a standing answer would add nothing.
        """
        if not scopes:
            return []
        marks = ",".join("?" for _ in scopes)
        with self.lock:
            rows = self.db.execute(
                f"select norm, query, namespace, hits, at from queries where namespace in ({marks}) order by at desc limit 5000",
                scopes,
            ).fetchall()
        groups: dict[frozenset, dict] = {}
        for r in rows:
            key = frozenset(r["norm"].split())
            match = next((k for k in groups if jaccard(k, key) >= 0.6), None)
            g = groups.setdefault(match or key, {"terms": set(), "asked": 0, "zero": 0, "hits": 0, "days": set(), "examples": [], "namespace": r["namespace"]})
            g["terms"] |= key
            g["asked"] += 1
            g["zero"] += 1 if r["hits"] == 0 else 0
            g["hits"] += r["hits"]
            g["days"].add(r["at"][:10])
            if r["query"] not in g["examples"] and len(g["examples"]) < 4:
                g["examples"].append(r["query"])
        out = []
        for g in groups.values():
            if g["asked"] < min_asked:
                continue
            mean_hits = g["hits"] / g["asked"]
            # 1.0 when every ask came back empty, 0.0 once a few records answer it every time
            poverty = g["zero"] / g["asked"] if g["zero"] else max(0.0, 1.0 - mean_hits / 3.0)
            out.append(
                {
                    "question": " ".join(sorted(g["terms"])),
                    "namespace": g["namespace"],
                    "asked": g["asked"],
                    "distinct_days": len(g["days"]),
                    "zero_hit_asks": g["zero"],
                    "mean_hits": round(mean_hits, 2),
                    "need": round(g["asked"] * poverty, 3),
                    "examples": g["examples"],
                }
            )
        out.sort(key=lambda c: (-c["need"], -c["asked"]))
        return out[:limit]

    # ------------------------------------------------------------- reads
    def get(self, rid: str, include_deleted: bool = False) -> dict | None:
        r = self.db.execute("select * from memories where id=?", (rid,)).fetchone()
        if not r or (r["deleted_at"] and not include_deleted):
            return None
        return self._row(r)

    def _live(self, statuses: tuple[str, ...]) -> str:
        marks = ",".join("?" for _ in statuses)
        return f"deleted_at is null and status in ({marks}) and (expires_at is null or expires_at > '{now_iso()}')"

    def search(self, query: str, scopes: list[str], limit: int = 20, statuses: tuple[str, ...] = ("active",), types: tuple[str, ...] = (), reach: tuple[str, ...] = ()) -> list[dict]:
        """Ranked hits: FTS relevance times scope, type and confidence weights. Snippets, not records.

        `reach` namespaces are searched too at weight 1.0, below every scope, so adding them
        leaves the order among scope hits as it was."""
        spaces = [*scopes, *(n for n in reach if n not in scopes)]
        marks = ",".join("?" for _ in spaces)
        live = self._live(statuses)
        params: list = []
        if self.fts and query.strip():
            sql = f"select m.*, bm25(memories_fts, 2.0, 1.0) as score from memories_fts join memories m on m.rowid = memories_fts.rowid where memories_fts match ? and m.namespace in ({marks}) and {live}"
            params = [fts_query(query), *spaces, *statuses]
        else:
            like = f"%{query.strip()}%"
            sql = f"select m.*, 0.0 as score from memories m where (m.title like ? or m.content like ?) and m.namespace in ({marks}) and {live}"
            params = [like, like, *spaces, *statuses]
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
            # Rank on the EARNED confidence, so a contradicted record sinks and a witnessed one
            # rises. An unwitnessed record is unaffected: effective_confidence is then the asserted
            # number and this is the expression it always was.
            d["rank"] = round((base + 0.1) * scope_w * TYPE_WEIGHT.get(d["type"], 1.0) * (0.5 + d["effective_confidence"]), 4)
            witnessed = f", {d['proof_count']} for/{d['refutation_count']} against" if d["proof_count"] or d["refutation_count"] else " (asserted, unwitnessed)"
            where = f"scope #{scope_rank + 1}" if d["namespace"] in scopes else "reach"
            d["why"] = f"{where} {d['namespace']}, type {d['type']}, confidence {d['effective_confidence']:.2f}{witnessed}"
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

    def timeline(self, namespace: str, limit: int = 30, statuses: tuple[str, ...] = ()) -> list[dict]:
        """Newest first, every status unless `statuses` names some: the review of candidates reads
        this, and bootstrap asks for active records only."""
        sql = "select * from memories where namespace=? and deleted_at is null"
        params: list = [namespace]
        if statuses:
            sql += " and status in (" + ",".join("?" for _ in statuses) + ")"
            params += list(statuses)
        rows = self.db.execute(sql + " order by created_at desc limit ?", (*params, limit)).fetchall()
        return [self._row(r) for r in rows]

    def count_status(self, namespace: str, status: str) -> int:
        return self.db.execute("select count(*) from memories where namespace=? and status=? and deleted_at is null", (namespace, status)).fetchone()[0]

    def namespaces(self) -> list[dict]:
        rows = self.db.execute("select namespace, count(*) as n, max(updated_at) as last from memories where deleted_at is null group by namespace order by namespace").fetchall()
        return [dict(r) for r in rows]

    RENAME_PREFIX_RE = re.compile(r"(github|gitea)/[\w.-]+/")

    def rename_namespaces(self, old: str, new: str, client: str = "system") -> dict[str, int]:
        """Move every namespace under `old` (`gitea/example/`) to `new`, for an owner rename.

        Idempotent: once applied nothing carries `old`, so a rerun moves 0. `audit` keeps its
        history and gains one row. A question already under the new namespace keeps that copy.
        """
        if not (self.RENAME_PREFIX_RE.fullmatch(old) and self.RENAME_PREFIX_RE.fullmatch(new)) or old == new:
            raise ValueError(f"rename needs two distinct owner prefixes like gitea/<owner>/, got {old!r} -> {new!r}")
        n = len(old)
        moved: dict[str, int] = {}
        with self.lock:
            self.db.execute("begin")
            try:
                for table, column in (("memories", "namespace"), ("memories", "repository"), ("jobs", "namespace"), ("queries", "namespace")):
                    cur = self.db.execute(f"update {table} set {column} = ? || substr({column}, ?) where substr({column}, 1, ?) = ?", (new, n + 1, n, old))
                    moved[f"{table}.{column}"] = cur.rowcount
                cur = self.db.execute("update or ignore questions set namespace = ? || substr(namespace, ?) where substr(namespace, 1, ?) = ?", (new, n + 1, n, old))
                moved["questions.namespace"] = cur.rowcount
                cur = self.db.execute("delete from questions where substr(namespace, 1, ?) = ?", (n, old))
                moved["questions.duplicates_dropped"] = cur.rowcount
                if any(moved.values()):
                    self.db.execute(
                        "insert into audit(at, client, action, namespace, record_id, detail) values (?,?,?,?,?,?)",
                        (now_iso(), client, "rename-namespaces", new, "", json.dumps({"from": old, "to": new, **moved})),
                    )
                self.db.execute("commit")
            except Exception:
                self.db.execute("rollback")
                raise
        return moved

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
