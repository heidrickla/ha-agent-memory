"""What never becomes durable memory: credentials, key material, the do-not-store marker.
The audit log records the pattern's name, never the matched text."""

from __future__ import annotations

import re

DO_NOT_STORE = "do-not-store"
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "kuma_key": re.compile(r"\buk[12]_[A-Za-z0-9_-]{20,}"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    "bearer": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}"),
    "url_credentials": re.compile(r"://[^/\s:@]+:[^@\s/]+@"),
    "assignment": re.compile(r"(?i)\b(api[_-]?key|api[_-]?token|access[_-]?token|secret|passwd|password|client[_-]?secret)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    "vault_token": re.compile(r"\b[sb]\.[A-Za-z0-9]{24,}\b"),
}
SIZE_LIMIT = 20_000  # characters; a record is a fact or a summary, not a log


def screen(text: str) -> str | None:
    """None when the text may be stored, else the name of the reason."""
    if text is None:
        return None
    if DO_NOT_STORE in text:
        return "do_not_store_marker"
    if len(text) > SIZE_LIMIT:
        return "too_large"
    for name, pattern in PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def redact(text: str) -> str:
    """For payloads sent to the enrichment model: the same patterns, replaced."""
    out = text
    for name, pattern in PATTERNS.items():
        out = pattern.sub(f"[redacted:{name}]", out)
    return out
