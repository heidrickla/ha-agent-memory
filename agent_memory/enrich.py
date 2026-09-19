"""Phase 4: enrichment through the owner's own Ollama on the LAN, from the durable job queue.

A session_close with notes leaves an `extract` job. The worker sends the redacted notes to
the configured model and stores what comes back as `candidate` records, never `active`:
a person or an agent promotes a candidate with memory_update. Everything the worker does is
bounded and checked: the payload is cut and redacted, the reply must be JSON of the agreed
shape, each candidate passes the same secret screen as a direct store, and a job is marked
done only after its candidates are written, with the job id on each so a retry after a
crash stores nothing twice. Ollama offline means jobs wait and health reads degraded.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from filters import redact, screen
from store import TYPES, Store

LOG = logging.getLogger("agent-memory.enrich")
PROMPT_VERSION = "extract-v1"
MAX_ITEMS = 8
MAX_ATTEMPTS = 5
NOTES_LIMIT = 12000

INSTRUCTIONS = (
    "You read the notes an AI coding session left behind and pull out the facts a later session "
    "in the same repository would need. Answer with JSON only: an object with one key, memories, "
    f"holding at most {MAX_ITEMS} items. Each item has type (one of: {', '.join(TYPES)}), title (short and specific), "
    "content (one to three sentences stating the fact), confidence (a number from 0 to 1). "
    "Keep decisions, constraints, failures with their fixes, procedures and next steps. Leave out "
    "chatter, restated code, anything already obvious from the repository, and any credential."
)


class Ollama:
    def __init__(self, base_url: str, model: str, timeout_s: int = 120):
        self.base = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout_s

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/api/tags", timeout=5) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def extract(self, namespace: str, notes: str) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
            "messages": [
                {"role": "system", "content": INSTRUCTIONS},
                {"role": "user", "content": f"Repository namespace: {namespace}\n\nSession notes:\n{notes}"},
            ],
        }
        req = urllib.request.Request(f"{self.base}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            doc = json.load(resp)
        return (doc.get("message") or {}).get("content", "")


def parse_candidates(text: str) -> list[dict]:
    """The model's reply as a list of well-formed candidates; a malformed reply raises ValueError."""
    doc = json.loads(text)
    items = doc.get("memories") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ValueError("reply has no memories list")
    out = []
    for it in items[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        typ = str(it.get("type", "")).strip()
        title = str(it.get("title", "")).strip()[:120]
        content = str(it.get("content", "")).strip()[:1200]
        try:
            confidence = min(1.0, max(0.0, float(it.get("confidence", 0.6))))
        except (TypeError, ValueError):
            confidence = 0.6
        if typ not in TYPES or not title or not content:
            continue
        if screen(title) or screen(content):
            continue
        out.append({"type": typ, "title": title, "content": content, "confidence": round(confidence, 2)})
    return out


class Worker:
    def __init__(self, store: Store, options: dict, ollama: Ollama | None = None, poll_s: float = 30.0):
        self.store = store
        self.options = options
        url = (options.get("ollama_url") or "").strip()
        self.ollama = ollama if ollama is not None else (Ollama(url, options.get("ollama_model") or "qwen3.5:9b", int(options.get("ollama_timeout_s") or 120)) if url else None)
        self.poll_s = poll_s
        self.last_available: bool | None = None
        self.processed = 0
        self.failed = 0

    def state(self) -> dict:
        depth = self.store.queue_depth()
        return {
            "configured": self.ollama is not None,
            "model": self.ollama.model if self.ollama else None,
            "ollama": ("available" if self.last_available else "offline") if self.ollama else "off",
            "prompt": PROMPT_VERSION,
            "queued": depth["queued"],
            "oldest": depth["oldest"],
            "processed": self.processed,
            "failed": self.failed,
        }

    def run_job(self, job: dict) -> int:
        """One job: returns how many candidates were stored; raises on failure."""
        payload = job["payload"]
        tag = f"{PROMPT_VERSION}:{job['id']}"
        already = self.store.db.execute("select count(*) from memories where enrichment=?", (tag,)).fetchone()[0]
        if already:
            self.store.job_done(job["id"], self.ollama.model)
            return 0
        notes = redact(str(payload.get("notes", ""))[:NOTES_LIMIT])
        reply = self.ollama.extract(job["namespace"], notes)
        candidates = parse_candidates(reply)
        for c in candidates:
            self.store.store(
                {
                    **c,
                    "namespace": job["namespace"],
                    "status": "candidate",
                    "agent": f"ollama:{self.ollama.model}",
                    "source_session": payload.get("session", ""),
                    "enrichment": tag,
                    "supersedes": None,
                },
                client="enrichment",
            )
        self.store.job_done(job["id"], self.ollama.model)
        self.store.audit("enrichment", "extract", job["namespace"], payload.get("summary_id", ""), f"{len(candidates)} candidates, model {self.ollama.model}")
        return len(candidates)

    def step(self) -> bool:
        """One poll: False when there was nothing to do or Ollama is away."""
        if self.ollama is None:
            return False
        self.last_available = self.ollama.available()  # every poll, so health is current with an empty queue too
        job = self.store.next_job()
        if job is None or not self.last_available:
            return False
        try:
            n = self.run_job(job)
            self.processed += 1
            LOG.info("job %s: %d candidates for %s", job["id"][:8], n, job["namespace"])
        except (ValueError, urllib.error.URLError, OSError, TimeoutError) as err:
            attempts = self.store.job_attempts(job["id"]) + 1
            give_up = attempts >= MAX_ATTEMPTS
            self.store.job_failed(job["id"], f"{type(err).__name__}: {err}", give_up)
            self.failed += int(give_up)
            LOG.warning("job %s attempt %d failed: %s%s", job["id"][:8], attempts, err, "; giving up" if give_up else "")
            return False
        return True

    def loop(self, stop: threading.Event) -> None:
        backoff = self.poll_s
        while not stop.is_set():
            try:
                did = self.step()
            except Exception:
                LOG.exception("worker step failed")
                did = False
            away = self.ollama is not None and self.last_available is False
            backoff = min(backoff * 2, 600) if away else self.poll_s
            if stop.wait(1.0 if did else backoff):
                break
