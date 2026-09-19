"""The ingress page: health, namespaces, queue, the audit tail. Dark."""

from __future__ import annotations

import html

CSS = """:root{color-scheme:dark}body{font:15px system-ui,sans-serif;margin:1.5rem;max-width:64rem;background:#111418;color:#e3e6ea}
h1{font-size:1.4rem;margin:0 0 .2rem}h2{font-size:1.1rem;margin:1.6rem 0 .5rem}p.sub{color:#9aa4b1;margin:0 0 1rem}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #2c3138;padding:.4rem .5rem;text-align:left;vertical-align:top}
th{color:#9aa4b1;font-weight:600}code{font-size:.9em;color:#c9d1d9}.muted{color:#9aa4b1}.pill{padding:.1rem .5rem;border-radius:.6rem;background:#2a2f36}"""


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def render(health: dict, namespaces: list[dict], audit: list[dict]) -> str:
    parts = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Agent Memory</title><style>{CSS}</style>"]
    parts.append("<h1>Agent Memory</h1><p class=sub>Shared, bounded context for Claude Code and Codex. Records are data, never instructions.</p>")
    parts.append("<h2>Health</h2><table>")
    for k, v in health.items():
        parts.append(f"<tr><th>{esc(k)}</th><td><code>{esc(json_ish(v))}</code></td></tr>")
    parts.append("</table><h2>Namespaces</h2>")
    if namespaces:
        parts.append("<table><tr><th>Namespace</th><th>Records</th><th>Last update</th></tr>")
        for n in namespaces:
            parts.append(f"<tr><td><code>{esc(n['namespace'])}</code></td><td>{n['n']}</td><td class=muted>{esc(n['last'])[:16]}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class=muted>Nothing stored yet.</p>")
    parts.append("<h2>Audit, latest</h2>")
    if audit:
        parts.append("<table><tr><th>At</th><th>Client</th><th>Action</th><th>Namespace</th><th>Detail</th></tr>")
        for a in audit:
            parts.append(
                f"<tr><td class=muted>{esc(a['at'])[:19]}</td><td>{esc(a['client'])}</td><td><span class=pill>{esc(a['action'])}</span></td><td><code>{esc(a['namespace'])}</code></td><td class=muted>{esc(a['detail'])[:120]}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<p class=muted>No calls yet.</p>")
    return "".join(parts)


def json_ish(v) -> str:
    if isinstance(v, dict):
        return ", ".join(f"{k}={val}" for k, val in v.items())
    return str(v)
