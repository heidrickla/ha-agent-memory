"""Embedding retrieval: record vectors from an OpenAI-style `/v1/embeddings` server, kept in the store.

Measured offline on the frozen eval (`tools/retrieval-eval/`) against the 150-record store: the
fusion of FTS5 and cosine over Qwen3-Embedding-0.6B vectors scored MRR 0.81 against FTS5's 0.74
(paired 95% interval +0.014 to +0.122); cosine alone scored 0.82 with an interval crossing zero.

| When | Search ranks by |
|---|---|
| the encoder answers and every record searched has a current vector | reciprocal rank fusion of FTS5 and cosine, over every live record in the scopes and reach |
| otherwise | FTS5 |

The query's vector gets `embed_timeout_s`; a miss marks the encoder down until the next probe, so
retrieval never waits on it for longer than that. The texts are the offline test's
(`embed_eval.py`, from the model card): a query carries `QUERY_PROMPT`, a record is `title\\ncontent`
with no instruction. `tests/test_embeddings.py` pins both to that script.

Standard library only: a vector is a float32 `array('f')` blob, cosine is `math.sumprod` of unit vectors.
This module imports nothing from the package, so the offline eval can load it from a checkout.
"""

from __future__ import annotations

import array
import concurrent.futures
import hashlib
import json
import logging
import math
import threading
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("agent-memory.embed")

QUERY_PROMPT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
DEFAULT_MODEL = "qwen3-emb-0.6b"
DEFAULT_TIMEOUT_S = 2.0
DOC_TIMEOUT_S = 30.0
PROBE_TIMEOUT_S = 5.0
# Far below the encoder's 32,768 tokens, which a longer text would exceed and be refused. The largest
# measured record is 378 tokens.
DOC_CHARS = 16000
# What search can return, so what gets a vector.
EMBEDDED_STATUSES = ("active", "candidate")
# A text the server will not embed; it is skipped until the model changes. Any other status ends the step.
REFUSED_CODES = (400, 413, 422)
# Reciprocal rank fusion's constant and each list's depth: the offline test's `RRF_K` and `LIMIT`.
RRF_K = 60
FUSE_DEPTH = 20


def query_text(query: str) -> str:
    return QUERY_PROMPT + query


def document_text(title: str, content: str) -> str:
    return f"{title}\n{content}"[:DOC_CHARS]


def digest(model: str, snapshot: str, text: str) -> str:
    """The offline test's cache key. An edited record, another model or another snapshot reads as stale."""
    return hashlib.sha256(f"{model}|{snapshot}|{text}".encode()).hexdigest()


def pack(vec) -> bytes:
    return array.array("f", vec).tobytes()


def unpack(blob: bytes) -> array.array:
    out = array.array("f")
    out.frombytes(blob)
    return out


def unit(vec: list[float]) -> list[float]:
    """Unit length, so a dot product is the cosine. A vector already at unit length (Qwen3-Embedding
    returns them normalised) is kept exactly as sent, as the offline test used it."""
    norm = math.sqrt(math.sumprod(vec, vec))
    if not norm or not math.isfinite(norm):
        raise ValueError("zero or non-finite vector")
    return vec if abs(norm - 1.0) <= 1e-3 else [x / norm for x in vec]


class Encoder:
    """An OpenAI-style `/v1` base URL and a model name.

    `identity` is (model, snapshot) from the last probe. The snapshot is what the server says it serves:
    the last path segment of vLLM's `root`, the Hugging Face snapshot hash. llama-swap's own listing has
    no `root`, so its passthrough to the model's server is asked next.
    """

    def __init__(self, base_url: str, model: str = DEFAULT_MODEL, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.base = base_url.strip().rstrip("/")
        self.model = model
        self.timeout = float(timeout_s)
        self.identity: tuple[str, str] | None = None
        self.up: bool | None = None  # None until the first probe
        self.error = ""
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="embed-query")

    def _get(self, url: str, timeout: float) -> dict:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)

    def _post(self, url: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    def _listings(self) -> list[str]:
        urls = [f"{self.base}/models"]
        parts = urllib.parse.urlsplit(self.base)
        if parts.path.endswith("/v1"):
            origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path[: -len("/v1")], "", ""))
            urls.append(f"{origin}/upstream/{urllib.parse.quote(self.model, safe='')}/v1/models")
        return urls

    def snapshot(self) -> str:
        last: Exception | None = None
        for url in self._listings():
            try:
                doc = self._get(url, PROBE_TIMEOUT_S)
            except (urllib.error.HTTPError, ValueError) as err:
                last = err
                continue
            except (urllib.error.URLError, OSError) as err:
                last = err
                break  # the host did not answer; the passthrough is on the same host
            data = doc.get("data") if isinstance(doc, dict) else None
            entry = next((m for m in data or [] if isinstance(m, dict) and m.get("id") == self.model), None)
            root = str((entry or {}).get("root") or "").rstrip("/")
            if root:
                return root.rsplit("/", 1)[-1]
        raise ValueError(f"no listing names a snapshot for {self.model}" + (f" ({type(last).__name__}: {last})" if last else ""))

    def probe(self) -> bool:
        try:
            snap = self.snapshot()
        except (ValueError, urllib.error.URLError, OSError) as err:
            self.up, self.error = False, f"{type(err).__name__}: {err}"
            return False
        self.identity, self.up, self.error = (self.model, snap), True, ""
        return True

    def ready(self) -> tuple[str, str] | None:
        return self.identity if self.up else None

    def embed(self, text: str, timeout: float) -> list[float]:
        doc = self._post(f"{self.base}/embeddings", {"model": self.model, "input": [text], "encoding_format": "float"}, timeout)
        try:
            vec = doc["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as err:
            raise ValueError(f"reply is not an embedding: {type(err).__name__}") from err
        if not isinstance(vec, list) or not vec or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in vec):
            raise ValueError("embedding is not a list of numbers")
        return unit([float(x) for x in vec])

    def embed_query(self, query: str) -> list[float] | None:
        """The query's vector within `timeout` seconds, or None. A miss marks the encoder down until the
        next probe, so a dead or overloaded encoder costs one search the timeout, not every search."""
        if not self.up:
            return None
        future = self._pool.submit(self.embed, query_text(query), self.timeout)
        try:
            return future.result(timeout=self.timeout)
        except Exception as err:  # any failure here means FTS answers; none may reach the caller
            self.up = False
            self.error = f"query embedding: {type(err).__name__}: {err}"
            LOG.warning("encoder missed a query (%s); search uses FTS until the next probe", self.error)
            return None


def encoder_from(options: dict) -> Encoder | None:
    url = (options.get("embed_url") or "").strip()
    if not url:
        return None
    return Encoder(url, (options.get("embed_model") or "").strip() or DEFAULT_MODEL, float(options.get("embed_timeout_s") or DEFAULT_TIMEOUT_S))


def scoped_vectors(store, identity: tuple[str, str], scopes: list[str], statuses: tuple[str, ...], types: tuple[str, ...] = (), reach: tuple[str, ...] = ()) -> dict | None:
    """Current vectors for every record FTS could return for these arguments, or None when any lacks one.

    The record set is FTS's: the same scopes then reach, statuses, types and liveness. A record without a
    current vector would be invisible to cosine, so until the worker has embedded it FTS answers instead.
    """
    spaces = [*scopes, *(n for n in reach if n not in scopes)]
    if not spaces:
        return {}
    sql = (
        f"select m.id, m.title, m.content, e.digest, e.vector from memories m left join embeddings e on e.record_id = m.id "
        f"where m.namespace in ({','.join('?' for _ in spaces)}) and {store._live(statuses)}"
    )
    params: list = [*spaces, *statuses]
    if types:
        sql += " and m.type in (" + ",".join("?" for _ in types) + ")"
        params += list(types)
    with store.lock:
        rows = store.db.execute(sql, params).fetchall()
    out = {}
    for r in rows:
        if r["vector"] is None or r["digest"] != digest(*identity, document_text(r["title"], r["content"])):
            return None
        out[r["id"]] = unpack(r["vector"])
    return out


def evidence_factor(rec: dict) -> float:
    """1.0 for a record no one has witnessed, else its earned confidence over the confidence its writer asserted.

    The full-text list already ranks on earned confidence, but the record keeps its cosine place, and one
    place is about 1.6 percent of a score, so a contradiction alone could not outweigh a closer vector."""
    if not (rec["proof_count"] or rec["refutation_count"]) or rec["confidence"] <= 0:
        return 1.0
    return rec["effective_confidence"] / rec["confidence"]


def fuse(store, fts: list[dict], qvec: list[float], vectors: dict, scopes: list[str], limit: int) -> list[dict]:
    """Reciprocal rank fusion of the FTS list and the cosine list (configuration c of the offline test), scaled by evidence.

    Each list is cut to `max(limit, FUSE_DEPTH)`; a record scores the sum of 1/(RRF_K + its position)
    over the lists holding it, times its `evidence_factor`; ties go to the higher cosine, then the lower id."""
    depth = max(limit, FUSE_DEPTH)
    cos = {rid: math.sumprod(qvec, v) for rid, v in vectors.items()}
    lists = {"full-text": [h["id"] for h in fts[:depth]], "cosine": sorted(cos, key=lambda i: (-cos[i], i))[:depth]}
    score: dict[str, float] = {}
    place: dict[str, dict[str, int]] = {}
    for name, ids in lists.items():
        for n, rid in enumerate(ids, 1):
            score[rid] = score.get(rid, 0.0) + 1.0 / (RRF_K + n)
            place.setdefault(rid, {})[name] = n
    docs = {rid: d for rid in score if (d := store.get(rid)) is not None}
    factor = {rid: evidence_factor(d) for rid, d in docs.items()}
    hits = []
    for rid in sorted(docs, key=lambda i: (-score[i] * factor[i], -cos.get(i, -1.0), i))[:limit]:
        d = docs[rid]
        where = f"scope #{scopes.index(d['namespace']) + 1}" if d["namespace"] in scopes else "reach"
        ranks = ", ".join(f"{name} #{n}" for name, n in place[rid].items())
        d["rank"] = round(score[rid] * factor[rid], 5)
        d["cosine"] = round(cos.get(rid, 0.0), 4)
        evidence = f", evidence x{factor[rid]:.2f}" if factor[rid] != 1.0 else ""
        d["why"] = f"{where} {d['namespace']}, {ranks}, cosine {cos.get(rid, 0.0):.3f}, type {d['type']}, confidence {d['effective_confidence']:.2f}{evidence}"
        hits.append(d)
    return hits


def retrieve(
    store, encoder: Encoder | None, query: str, scopes: list[str], limit: int, statuses: tuple[str, ...] = ("active",), types: tuple[str, ...] = (), reach: tuple[str, ...] = ()
) -> tuple[list[dict], str, list[dict]]:
    """(hits, ranking, fts_hits). Ranking is "rrf" or "fts". FTS always runs: it is the fallback, the
    fusion's keyword list and the demand signal's count. `store.search` sorts every match before it
    cuts, so the deeper list's first `limit` are what a search at `limit` returns."""
    deep = store.search(query, scopes, limit=max(limit, FUSE_DEPTH), statuses=statuses, types=types, reach=reach)
    fts = deep[:limit]
    if encoder is None or not query.strip():
        return fts, "fts", fts
    try:
        identity = encoder.ready()
        vectors = scoped_vectors(store, identity, scopes, statuses, types, reach) if identity else None
        qvec = encoder.embed_query(query) if vectors else None  # nothing in reach: nothing to rank
        if qvec is not None:
            return fuse(store, deep, qvec, vectors, scopes, limit), "rrf", fts
    except Exception:  # retrieval never depends on the encoder
        LOG.exception("cosine ranking failed; FTS answers")
    return fts, "fts", fts


class Indexer:
    """Keeps a current vector for every record search can return, and probes the encoder every poll.

    The first step runs at start (the backfill). A store or an edit wakes it through `store.changed`.
    A text the server refuses is skipped until the model or snapshot changes; a transport failure
    marks the encoder down and ends the step.
    """

    def __init__(self, store, encoder: Encoder, poll_s: float = 30.0, batch: int = 200):
        self.store = store
        self.encoder = encoder
        self.poll_s = poll_s
        self.batch = batch
        self.refused: dict[str, str] = {}  # digest -> reason, for the current identity
        self._identity: tuple[str, str] | None = None
        self.written = 0

    def label(self) -> str:
        model, snap = self.encoder.identity or (self.encoder.model, "")
        return f"{model}@{snap[:12]}"

    def _eligible(self) -> list:
        marks = ",".join("?" for _ in EMBEDDED_STATUSES)
        with self.store.lock:
            return self.store.db.execute(
                f"select m.id, m.title, m.content, e.digest from memories m left join embeddings e on e.record_id = m.id "
                f"where m.deleted_at is null and m.status in ({marks}) order by m.created_at, m.id",
                EMBEDDED_STATUSES,
            ).fetchall()

    def pending(self, identity: tuple[str, str]) -> list[tuple[str, str, str]]:
        """(id, text, digest) of every record whose vector is missing or stale."""
        out = []
        for r in self._eligible():
            text = document_text(r["title"], r["content"])
            d = digest(*identity, text)
            if r["digest"] != d and d not in self.refused:
                out.append((r["id"], text, d))
        return out

    def step(self) -> int:
        """One poll: probe, then embed up to `batch` missing or stale records. Returns rows written."""
        if not self.encoder.probe():
            return 0
        identity = self.encoder.identity
        if identity != self._identity:
            self.refused.clear()
            self._identity = identity
        wrote = 0
        for rid, text, d in self.pending(identity)[: self.batch]:
            try:
                vec = self.encoder.embed(text, DOC_TIMEOUT_S)
            except urllib.error.HTTPError as err:
                if err.code not in REFUSED_CODES:  # 429 and 5xx pass; the next poll retries
                    self.encoder.up, self.encoder.error = False, f"HTTP {err.code} embedding record {rid[:8]}"
                    LOG.warning("embedding stopped: %s", self.encoder.error)
                    break
                self.refused[d] = f"HTTP {err.code}"
                LOG.warning("encoder refused record %s: HTTP %s; skipped until the model changes", rid[:8], err.code)
                continue
            except (ValueError, urllib.error.URLError, OSError) as err:
                self.encoder.up, self.encoder.error = False, f"{type(err).__name__}: {err}"
                LOG.warning("embedding stopped: %s", self.encoder.error)
                break
            wrote += self.store.put_embedding(rid, d, self.label(), pack(vec))
        self.written += wrote
        if wrote:
            LOG.info("embedded %d record(s) with %s", wrote, self.label())
        return wrote

    def state(self) -> dict:
        identity = self.encoder.identity
        rows = self._eligible()
        current = sum(1 for r in rows if identity and r["digest"] == digest(*identity, document_text(r["title"], r["content"])))
        return {
            "encoder": "available" if self.encoder.up else "offline",
            "model": self.encoder.model,
            "snapshot": identity[1][:12] if identity else None,
            "records": len(rows),
            "current": current,
            "refused": len(self.refused),
            "error": self.encoder.error or None,
        }

    def loop(self, stop: threading.Event) -> None:
        backoff = self.poll_s
        while not stop.is_set():
            try:
                wrote = self.step()
            except Exception:
                LOG.exception("embedding step failed")
                wrote = 0
            if wrote >= self.batch:
                continue  # more may be waiting
            backoff = min(backoff * 2, 120.0) if self.encoder.up is False else self.poll_s
            self.store.changed.wait(backoff)
            self.store.changed.clear()
