"""Canonical storage: SQLite in WAL mode, FTS5 over title and content, an append-only audit,
a durable job queue. Everything here works with Ollama absent.

Tokens are approximated as characters / 4 throughout; every packet reports its estimate.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import itertools
import json
import re
import os
import sqlite3
import threading
import uuid

# The add-on and the tests import modules top-level; the offline eval imports `agent_memory.store`.
if __package__:
    from .filters import screen_fields, url_credentials
else:
    from filters import screen_fields, url_credentials

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
# Lifecycle, owned by the server. A record is created active or candidate; update moves it between those
# two; only a supersession makes it superseded and only forget makes it deleted.
STATUSES = ("active", "candidate", "superseded", "deleted")
CREATE_STATUSES = ("active", "candidate")
UPDATE_TRANSITIONS = {("candidate", "active"), ("active", "candidate")}
UPDATABLE = ("title", "content", "type", "confidence", "status", "topics", "project_scopes", "expires_at", "sensitivity", "anchors")
ANCHOR_STATES = ("ok", "missing", "unavailable")
SCHEMA = """
create table if not exists memories (
  id text primary key, created_at text not null, updated_at text not null,
  namespace text not null, project_scopes text not null default '[]', topics text not null default '[]',
  type text not null, agent text not null default '', source_session text not null default '',
  repository text not null default '', commit_sha text not null default '',
  title text not null, content text not null, confidence real not null default 0.8,
  status text not null default 'active', sensitivity text not null default 'internal',
  supersedes text, expires_at text, deleted_at text, enrichment text not null default '',
  revision integer not null default 1
);
create table if not exists receipts (
  op_id text primary key, at text not null, client text not null, operation text not null,
  digest text not null, result text not null
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
  last_checked text, unavailable_since text, primary key (record_id, anchor)
);
create table if not exists questions (
  id text primary key, created_at text not null, updated_at text not null,
  namespace text not null, question text not null, norm text not null,
  answer text not null default '', answered_at text, model text not null default '',
  source_ids text not null default '[]', basis text not null default '',
  status text not null default 'active', asked integer not null default 0
);
create unique index if not exists questions_key on questions(namespace, norm);
create table if not exists embeddings (
  record_id text primary key, digest text not null, model text not null, vector blob not null
);
"""
"""`revision` counts mutations of a record: content, anchors, evidence, status, supersession. `memory_update`
applies only at the revision the caller read, because `updated_at` has one-second resolution.

`receipts` holds the result of each operation sent with an operation id, so a replay after a lost
response returns that result instead of acting twice. Only `session_close` takes one.

`anchors.last_state` is the last definitive check (`ok` or `missing`, empty when never checked);
`unavailable_since` is set while the checker could not look and cleared by the next definitive check.
"""
"""`embeddings` holds one float32 vector per record for embedding retrieval (`embed.py`).

`digest` is sha256 of model, snapshot and the embedded text, so an edited record or a changed model
reads as stale by a string comparison and search falls back to FTS until the worker re-embeds it.
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


class Rejected(ValueError):
    """A write refused by a rule; the message says which and is safe to return."""


class Refused(Rejected):
    """A write whose field screens as a secret or carries the do-not-store marker. Names the field and the
    pattern, never the text."""

    def __init__(self, field: str, reason: str):
        super().__init__(f"{reason} in {field}")
        self.field, self.reason = field, reason


class Conflict(Exception):
    """`update` at a revision that is no longer current; carries the record as it stands."""

    def __init__(self, current: dict):
        super().__init__(f"revision conflict: the record is at revision {current['revision']}")
        self.current = current


def _strings(name: str, value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        # A client with a stale tool schema sends a list as one string; iterating it kept single characters.
        raise Rejected(f"{name} must be a list of strings, got {type(value).__name__}")
    return sorted({v.strip() for v in value if v.strip()})


def _confidence(value: object) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError) as err:
        raise Rejected("confidence must be a number from 0 to 1") from err
    if isinstance(value, bool) or not 0.0 <= c <= 1.0:
        raise Rejected("confidence must be a number from 0 to 1")
    return c


def _expiry(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        dt.datetime.fromisoformat(str(value))
    except ValueError as err:
        raise Rejected("expires_at must be an ISO 8601 date or time") from err
    return str(value)


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
        self._migrate()
        self.fts = self._index(FTS, "memories_fts", "porter")
        # Set on every write that can change a record's text; the embedding worker waits on it.
        self.changed = threading.Event()
        self._savepoints = itertools.count()

    def _migrate(self) -> None:
        """Columns added after a database was created. Existing records start at revision 1."""
        added = {"memories": ("revision", "integer not null default 1"), "anchors": ("unavailable_since", "text")}
        for table, (column, decl) in added.items():
            if column not in {r[1] for r in self.db.execute(f"pragma table_info({table})")}:
                self.db.execute(f"alter table {table} add column {column} {decl}")

    @contextlib.contextmanager
    def transaction(self):
        """One transaction under the store lock; nested use becomes a savepoint inside the outer one."""
        with self.lock:
            name = f"sp{next(self._savepoints)}"
            self.db.execute(f"savepoint {name}")
            try:
                yield
            except BaseException:
                self.db.execute(f"rollback to {name}")
                self.db.execute(f"release {name}")
                raise
            self.db.execute(f"release {name}")

    def refuse(self, client: str, namespace: str, record_id: str, fields: dict) -> None:
        """Screen every value about to be persisted, and URL anchors for credentials. A hit is audited
        by field and pattern name, never the text, and raises Refused. Call it outside a transaction,
        so the audit row survives the refusal."""
        hit = screen_fields(fields)
        anchors = fields.get("anchors") if isinstance(fields.get("anchors"), (list, tuple)) else []
        for a in anchors:
            reason = url_credentials(str(a).strip()[4:]) if str(a).strip().startswith("url:") else None
            if hit is None and reason:
                hit = ("anchors", reason)
        if hit:
            self.audit(client, "rejected", namespace, record_id, f"{hit[0]}: {hit[1]}")
            raise Refused(*hit)

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
            rows = self.db.execute("select anchor, last_state, last_checked, unavailable_since from anchors where record_id=? order by anchor", (d["id"],)).fetchall()
        d["anchors"] = [a["anchor"] for a in rows]
        if rows:
            d["anchor_states"] = [{"anchor": a["anchor"], "state": a["last_state"] or "unchecked", "checked_at": a["last_checked"], "unavailable_since": a["unavailable_since"]} for a in rows]
        missing = [a["anchor"] for a in rows if a["last_state"] == "missing"]
        if missing:
            d["missing_anchors"] = missing
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
        self.refuse(client, rec["namespace"], rid, {"quote": quote, "agent": agent, "source_session": source_session})
        with self.transaction():
            self.db.execute(
                "insert into evidence(id, record_id, at, stance, quote, agent, source_session) values (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), rid, now_iso(), stance, quote.strip()[:1000], agent or client, source_session),
            )
            self.db.execute("update memories set updated_at=?, revision=revision+1 where id=?", (now_iso(), rid))
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
        """The internal create. `status` must be active or candidate; the service boundary
        (`mcp.Memory.store_record`) always passes active, and extraction and import go through
        `store_candidate`. Everything is validated and screened before the transaction, so a refused
        store writes nothing."""
        status = rec.get("status") or "active"
        if status not in CREATE_STATUSES:
            raise Rejected(f"a record is created {' or '.join(CREATE_STATUSES)}, not {status!r}")
        typ = rec.get("type") or "repository-fact"
        if typ not in TYPES:
            raise Rejected(f"type must be one of {', '.join(TYPES)}")
        title, content = str(rec.get("title") or "").strip(), str(rec.get("content") or "").strip()
        if not title or not content:
            raise Rejected("title and content are required")
        fields = {
            "namespace": rec["namespace"],
            "project_scopes": _strings("project_scopes", rec.get("project_scopes")),
            "topics": _strings("topics", rec.get("topics")),
            "type": typ,
            "agent": str(rec.get("agent") or client),
            "source_session": str(rec.get("source_session") or ""),
            "repository": str(rec.get("repository") or ""),
            "commit": str(rec.get("commit") or ""),
            "title": title,
            "content": content,
            "sensitivity": str(rec.get("sensitivity") or "internal"),
            "supersedes": str(rec["supersedes"]) if rec.get("supersedes") else None,
            "expires_at": _expiry(rec.get("expires_at")),
            "enrichment": str(rec.get("enrichment") or ""),
            "anchors": self.valid_anchors(rec.get("anchors") or []),
        }
        self.refuse(client, str(fields["namespace"]), "", fields)
        confidence = _confidence(rec.get("confidence") if rec.get("confidence") is not None else 0.8)
        rid = rec.get("id") or str(uuid.uuid4())
        ts = now_iso()
        row = (
            rid,
            ts,
            ts,
            fields["namespace"],
            json.dumps(fields["project_scopes"]),
            json.dumps(fields["topics"]),
            typ,
            fields["agent"],
            fields["source_session"],
            fields["repository"],
            fields["commit"],
            title,
            content,
            confidence,
            status,
            fields["sensitivity"],
            fields["supersedes"],
            fields["expires_at"],
            None,
            fields["enrichment"],
        )
        with self.transaction():
            self.db.execute(
                "insert into memories(id, created_at, updated_at, namespace, project_scopes, topics, type, agent, source_session, repository, commit_sha, title, content, confidence, status, sensitivity, supersedes, expires_at, deleted_at, enrichment, revision) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                row,
            )
            if fields["supersedes"]:
                self._supersede(rid, fields["supersedes"], str(fields["namespace"]), status, ts)
            self._set_anchors(rid, fields["anchors"])
            self.audit(client, "store", str(fields["namespace"]), rid, typ)
        self.changed.set()
        return self.get(rid)

    def store_candidate(self, rec: dict, client: str) -> dict:
        """The internal operation extraction and import write through: always a candidate, never superseding."""
        if rec.get("supersedes"):
            raise Rejected("a candidate cannot supersede a record")
        return self.store({**rec, "status": "candidate"}, client)

    def _supersede(self, new_id: str, target_id: str, namespace: str, new_status: str, ts: str) -> None:
        """Retire `target_id` in favour of `new_id`, inside the caller's transaction. Refused unless the
        replacement is active, the target is a different live active record in the same namespace, and
        the target's own chain of supersessions does not lead back to the replacement."""
        if new_status != "active":
            raise Rejected("only an active record can supersede another")
        if target_id == new_id:
            raise Rejected("a record cannot supersede itself")
        target = self.db.execute("select namespace, status, deleted_at, supersedes from memories where id=?", (target_id,)).fetchone()
        if target is None or target["deleted_at"]:
            raise Rejected(f"supersedes names no live record: {target_id}")
        if target["status"] != "active":
            raise Rejected(f"supersedes names a {target['status']} record; only an active one can be superseded")
        if target["namespace"] != namespace:
            raise Rejected("supersedes names a record in another namespace")
        seen, link = {target_id}, target["supersedes"]
        while link and link not in seen:
            if link == new_id:
                raise Rejected("supersedes would form a cycle")
            seen.add(link)
            nxt = self.db.execute("select supersedes from memories where id=?", (link,)).fetchone()
            link = nxt["supersedes"] if nxt else None
        self.db.execute("update memories set status='superseded', updated_at=?, revision=revision+1 where id=? and status='active'", (ts, target_id))

    @staticmethod
    def valid_anchors(anchors: object) -> list[str]:
        if not isinstance(anchors, (list, tuple)):
            # A client with a stale tool schema sends the list as one string; iterating it
            # validated single characters.
            raise Rejected(f"anchors must be a list of strings, got {type(anchors).__name__}")
        wanted = sorted({str(a).strip() for a in anchors})
        bad = [a for a in wanted if not ANCHOR_RE.match(a)]
        if bad:
            raise Rejected(f"anchors must be entity:, path:, host: or url: followed by a value: {bad}")
        return wanted

    def _set_anchors(self, rid: str, wanted: list[str]) -> None:
        """Replace a record's anchors, keeping the check state of those that stay. Caller holds the lock."""
        if wanted:
            marks = ",".join("?" for _ in wanted)
            self.db.execute(f"delete from anchors where record_id=? and anchor not in ({marks})", (rid, *wanted))
        else:
            self.db.execute("delete from anchors where record_id=?", (rid,))
        for a in wanted:
            self.db.execute("insert or ignore into anchors(record_id, anchor) values (?, ?)", (rid, a))

    def anchors(self, kind: str) -> list[dict]:
        """Anchors of one kind on live active records, with their last check: `state` is unchecked, ok or
        missing, and `unavailable_since` is set while the checker could not look."""
        with self.lock:
            rows = self.db.execute(
                "select a.record_id, a.anchor, a.last_state, a.last_checked, a.unavailable_since from anchors a join memories m on m.id = a.record_id "
                "where m.status='active' and m.deleted_at is null and a.anchor like ? order by a.record_id, a.anchor",
                (kind + ":%",),
            ).fetchall()
        return [dict(r, state=r["last_state"] or "unchecked") for r in rows]

    def report_anchor(self, rid: str, anchor: str, state: str, client: str) -> dict:
        """Record one check. `unavailable` means the checker could not look: it keeps the last definitive
        state and files nothing. Evidence is filed only on a transition between ok and missing, so a
        sweep repeated every few hours does not pile contradictions onto one record."""
        if state not in ANCHOR_STATES:
            raise Rejected(f"state must be one of {', '.join(ANCHOR_STATES)}")
        with self.lock:
            row = self.db.execute("select last_state, unavailable_since from anchors where record_id=? and anchor=?", (rid, anchor)).fetchone()
        if row is None:
            raise Rejected(f"record {rid} has no anchor {anchor}")
        before = row["last_state"]
        with self.lock:
            if state == "unavailable":
                self.db.execute("update anchors set unavailable_since=coalesce(unavailable_since, ?) where record_id=? and anchor=?", (now_iso(), rid, anchor))
                return {"record_id": rid, "anchor": anchor, "state": state, "was": before or "unchecked", "filed": ""}
            self.db.execute("update anchors set last_state=?, last_checked=?, unavailable_since=null where record_id=? and anchor=?", (state, now_iso(), rid, anchor))
        filed = ""
        if state == "missing" and before != "missing":
            self.corroborate(rid, f"anchor {anchor} no longer resolves", "contradicts", client)
            filed = "contradicts"
        elif state == "ok" and before == "missing":
            self.corroborate(rid, f"anchor {anchor} resolves again", "corroborates", client)
            filed = "corroborates"
        return {"record_id": rid, "anchor": anchor, "state": state, "was": before or "unchecked", "filed": filed}

    def update(self, rid: str, changes: dict, client: str, revision: int) -> dict | None:
        """Apply `changes` only if the record is still at `revision`, in one transaction with its anchors
        and status. Every change advances the revision and `updated_at`. Raises Conflict with the current
        record on a mismatch; None when there is no live record."""
        unknown = sorted(set(changes) - set(UPDATABLE))
        if unknown:
            raise Rejected(f"cannot change {', '.join(unknown)}; changeable: {', '.join(UPDATABLE)}")
        if not changes:
            raise Rejected("no changes given")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise Rejected("revision must be the integer revision the record was read at")
        values: dict = {}
        for k, v in changes.items():
            if k in ("topics", "project_scopes"):
                values[k] = json.dumps(_strings(k, v))
            elif k == "anchors":
                values[k] = self.valid_anchors(v or [])
            elif k == "confidence":
                values[k] = _confidence(v)
            elif k == "expires_at":
                values[k] = _expiry(v)
            elif k == "type":
                if v not in TYPES:
                    raise Rejected(f"type must be one of {', '.join(TYPES)}")
                values[k] = v
            elif k == "status":
                if v not in STATUSES:
                    raise Rejected(f"status must be one of {', '.join(STATUSES)}")
                values[k] = v
            elif k in ("title", "content"):
                values[k] = str(v or "").strip()
                if not values[k]:
                    raise Rejected(f"{k} cannot be empty")
            else:
                values[k] = str(v or "")
        current = self.get(rid)
        if current is None:
            return None
        self.refuse(client, current["namespace"], rid, changes)
        with self.transaction():
            row = self.db.execute("select status, revision from memories where id=? and deleted_at is null", (rid,)).fetchone()
            if row is None:
                return None
            if row["revision"] != revision:
                raise Conflict(self.get(rid) or current)
            if "status" in values and values["status"] != row["status"] and (row["status"], values["status"]) not in UPDATE_TRANSITIONS:
                raise Rejected(f"status cannot move from {row['status']} to {values['status']} by update")
            sets = [f"{k}=?" for k in values if k != "anchors"] + ["updated_at=?", "revision=revision+1"]
            vals = [v for k, v in values.items() if k != "anchors"] + [now_iso(), rid, revision]
            cur = self.db.execute(f"update memories set {', '.join(sets)} where id=? and revision=? and deleted_at is null", vals)
            if cur.rowcount == 0:
                raise Conflict(self.get(rid) or current)
            if "anchors" in values:
                self._set_anchors(rid, values["anchors"])
            self.audit(client, "update", current["namespace"], rid, ",".join(sorted(values)))
        self.changed.set()
        return self.get(rid)

    def forget(self, rid: str, client: str, hard: bool = False) -> bool:
        with self.transaction():
            if hard:
                cur = self.db.execute("delete from memories where id=?", (rid,))
            else:
                cur = self.db.execute("update memories set deleted_at=?, status='deleted', revision=revision+1 where id=? and deleted_at is null", (now_iso(), rid))
            if cur.rowcount:
                self.db.execute("delete from embeddings where record_id=?", (rid,))
                self.audit(client, "forget_hard" if hard else "forget", record_id=rid)
        return bool(cur.rowcount)

    def purge(self, deleted_days: int) -> int:
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=deleted_days)).isoformat(timespec="seconds")
        with self.lock:
            cur = self.db.execute("delete from memories where deleted_at is not null and deleted_at < ?", (cutoff,))
            # A vector outlives nothing it was made from: purged, forgotten or never-live records lose theirs.
            self.db.execute("delete from embeddings where record_id not in (select id from memories where deleted_at is null)")
            # Queries age out on the same clock. They are a demand signal, not a record.
            self.db.execute("delete from queries where at < ?", (cutoff,))
            # A spooled close is replayed within days; a receipt older than the retention answers nothing.
            self.db.execute("delete from receipts where at < ?", (cutoff,))
        return cur.rowcount

    def put_embedding(self, rid: str, digest: str, model: str, vector: bytes) -> int:
        """Store one record's vector, replacing its previous one. 0 when the record is gone by now."""
        with self.lock:
            cur = self.db.execute(
                "insert or replace into embeddings(record_id, digest, model, vector) select ?, ?, ?, ? where exists (select 1 from memories where id=? and deleted_at is null)",
                (rid, digest, model, vector, rid),
            )
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
        with self.lock:
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
                    bump = ", revision = revision + 1" if table == "memories" else ""
                    cur = self.db.execute(f"update {table} set {column} = ? || substr({column}, ?){bump} where substr({column}, 1, ?) = ?", (new, n + 1, n, old))
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
    def enqueue(self, kind: str, namespace: str, payload: dict, client: str = "system") -> str:
        """Queue a job. Its payload is persisted, so it passes the same screen as a record."""
        self.refuse(client, namespace, "", {f"payload.{k}": v for k, v in payload.items()})
        jid = str(uuid.uuid4())
        with self.lock:
            self.db.execute("insert into jobs(id, created_at, kind, namespace, payload) values (?,?,?,?,?)", (jid, now_iso(), kind, namespace, json.dumps(payload)))
        return jid

    # ------------------------------------------------------------- receipts
    def receipt(self, op_id: str) -> dict | None:
        with self.lock:
            r = self.db.execute("select * from receipts where op_id=?", (op_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["result"] = json.loads(d["result"])
        return d

    def put_receipt(self, op_id: str, client: str, operation: str, digest: str, result: dict) -> None:
        with self.lock:
            self.db.execute(
                "insert into receipts(op_id, at, client, operation, digest, result) values (?,?,?,?,?,?)",
                (op_id, now_iso(), client, operation, digest, json.dumps(result)),
            )

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
