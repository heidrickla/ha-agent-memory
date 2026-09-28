"""What never becomes durable memory: credentials, key material, the do-not-store marker.
The audit log records the pattern's name, never the matched text."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

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


def secret(text: str) -> str | None:
    """The marker or a credential pattern in the text, by name; None when there is neither."""
    if text is None:
        return None
    if DO_NOT_STORE in text:
        return "do_not_store_marker"
    for name, pattern in PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def screen(text: str) -> str | None:
    """None when the text may be stored as a field, else the name of the reason."""
    if text is None:
        return None
    if DO_NOT_STORE in text:
        return "do_not_store_marker"
    if len(text) > SIZE_LIMIT:
        return "too_large"
    return secret(text)


def masked(value: str) -> str:
    """The value, or a placeholder naming the reason when it carries a secret. For context that is
    kept either way: audit rows, error text."""
    reason = secret(value)
    return f"[screened:{reason}]" if reason else value


def redact(text: str) -> str:
    """For payloads sent to the enrichment model: the same patterns, replaced."""
    out = text
    for name, pattern in PATTERNS.items():
        out = pattern.sub(f"[redacted:{name}]", out)
    return out


def screen_value(value: object) -> str | None:
    """`screen` over a string, or over every string inside a list, tuple or dict."""
    if isinstance(value, str):
        return screen(value)
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        for item in value:
            reason = screen_value(item)
            if reason:
                return reason
    return None


def screen_fields(fields: dict) -> tuple[str, str] | None:
    """(field, reason) for the first persisted field that screens, else None."""
    for name, value in fields.items():
        reason = screen_value(value)
        if reason:
            return name, reason
    return None


# A parameter name is split into its words (`X-Amz-Signature`, `accessToken`, `session_id`) and judged
# whole: `author` and `session_count` are not credentials, `auth`, `access_token` and `session_id` are.
CREDENTIAL_NAMES = frozenset(
    {"token", "secret", "password", "passwd", "pwd", "apikey", "key", "auth", "authorization", "sig", "signature", "credential", "credentials", "session", "sessionid", "sid", "jwt", "bearer"}
)
CREDENTIAL_LAST_WORDS = frozenset({"token", "secret", "password", "passwd", "pwd", "signature", "credential", "credentials"})
CREDENTIAL_LAST_PAIRS = frozenset({"apikey", "accesskey", "secretkey", "privatekey", "authkey", "sessionid", "sessionkey"})


def param_words(name: str) -> list[str]:
    """`X-Amz-Signature` -> [x, amz, signature]; `accessToken` -> [access, token]."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]


def credential_param(name: str) -> bool:
    words = param_words(name)
    if not words:
        return False
    return "".join(words) in CREDENTIAL_NAMES or words[-1] in CREDENTIAL_LAST_WORDS or "".join(words[-2:]) in CREDENTIAL_LAST_PAIRS


def url_credentials(url: str) -> str | None:
    """Why a URL anchor carries a credential: userinfo, or a query or fragment parameter named like one."""
    try:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            return "url_credentials"
    except ValueError:
        return "url_unparseable"
    for part in (parts.query, parts.fragment):
        if any(credential_param(key) for key, _ in parse_qsl(part, keep_blank_values=True)):
            return "url_credential_parameter"
    return None
