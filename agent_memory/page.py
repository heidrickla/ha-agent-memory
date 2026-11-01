"""The ingress page: server-rendered overview and read-only record browser.

`render` takes plain data (`App.panel_data`), so the page is testable without a server. Colour always travels
with a text label or a tooltip; the status colours are reserved for state, the series colours for identity.
"""

from __future__ import annotations

import datetime as dt
import html
import math
from pathlib import Path
from urllib.parse import urlencode
from typing import Any

CSS = Path(__file__).with_name("page.css").read_text(encoding="utf-8")

SERIES = ("s1", "s2", "s3", "s4")
STATUS = (("active", "s1"), ("candidate", "warn"), ("superseded", "idle"))
JOB = {"done": "good", "failed": "crit", "queued": "warn", "running": "s1"}


def esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def parse(ts: Any) -> dt.datetime | None:
    """An ISO time from the store; one without a zone is UTC, as the server writes it."""
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def local(ts: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    t = parse(ts)
    return t.astimezone().strftime(fmt) if t else "-"


def ago(ts: Any, now: dt.datetime) -> str:
    t = parse(ts)
    if t is None:
        return "never"
    s = max(0.0, (now - t).total_seconds())
    for limit, unit, size in ((60, "s", 1), (3600, "min", 60), (86400, "h", 3600)):
        if s < limit:
            return f"{int(s // size)} {unit} ago"
    return f"{int(s // 86400)} d ago"


def span(seconds: int) -> str:
    d, rest = divmod(max(0, int(seconds)), 86400)
    h, m = rest // 3600, rest % 3600 // 60
    return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{m} min"


def size(n: float) -> str:
    for unit in ("bytes", "KiB", "MiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


def dot(cls: str, title: str = "") -> str:
    return f'<span class="dot {cls}" title="{esc(title)}"></span>'


def chip(cls: str, label: str, title: str = "") -> str:
    return f'<span class=chip title="{esc(title or label)}">{dot(cls)}{esc(label)}</span>'


def segments(parts: list[tuple[str, int, str]], of: int) -> str:
    """One bar holding several counts, each as a share of `of`."""
    segs = "".join(f'<i class={cls} style="width:{100 * n / of:.1f}%" title="{esc(name)}: {n}"></i>' for name, n, cls in parts if n and of)
    tip = ", ".join(f"{name} {n}" for name, n, _ in parts if n) or "none"
    return f'<div class=bar title="{esc(tip)}">{segs}</div>'


def stacked(parts: list[tuple[str, int, str]]) -> str:
    """One bar split by count, with a legend that names every part and its number."""
    if sum(n for _, n, _ in parts) == 0:
        return '<div class=stack title="none"></div><div class=legend><span>none yet</span></div>'
    segs = "".join(f'<i class={cls} style="flex:{n}" title="{esc(name)}: {n}"></i>' for name, n, cls in parts if n)
    legend = "".join(f"<span>{dot(cls)}{esc(name)} <b>{n}</b></span>" for name, n, cls in parts if n)
    return f"<div class=stack>{segs}</div><div class=legend>{legend}</div>"


def ring(part: int, whole: int, name: str) -> str:
    """A donut for one share; '-' and an empty track when there is nothing to count."""
    r, w = 26.0, 7.0
    c = 2 * math.pi * r
    pct = None if whole <= 0 else 100 * part / whole
    arc = 0.0 if pct is None else c * max(0.0, min(100.0, pct)) / 100
    text = "-" if pct is None else f"{pct:.0f}%"
    tip = f"{name}: {part} of {whole}"
    # Colours go in style: Chrome does not resolve var() in presentation attributes. No arc element at zero,
    # because a zero-length dash with round caps still paints a dot.
    arc_el = (
        "" if arc == 0 else f'<circle cx=32 cy=32 r={r} style="fill:none;stroke:var(--s1);stroke-width:{w};stroke-linecap:round" stroke-dasharray="{arc:.2f} {c:.2f}" transform="rotate(-90 32 32)"/>'
    )
    return (
        f'<div class=ring title="{esc(tip)}"><svg width=64 height=64 viewBox="0 0 64 64" role=img aria-label="{esc(tip)}">'
        f'<circle cx=32 cy=32 r={r} style="fill:none;stroke:var(--track);stroke-width:{w}"/>{arc_el}'
        f'<text x=32 y=36 text-anchor=middle style="fill:var(--text);font-size:13px;font-weight:600">{text}</text></svg>'
        f"<div>{esc(name)}</div><div>{part} of {whole}</div></div>"
    )


def card(title: str, body: str, cls: str = "", aside: str = "") -> str:
    head = f'<h2 class=row style="justify-content:space-between"><span>{esc(title)}</span>{aside}</h2>'
    return f'<section class="card {cls}">{head}{body}</section>'


def words(name: str) -> str:
    return name.replace("-", " ")


def browse_href(**filters: str | int) -> str:
    return "?" + urlencode({"view": "browse", **{k: v for k, v in filters.items() if v != ""}})


# --- sections ---------------------------------------------------------------------------------------------


def tiles(d: dict[str, Any]) -> str:
    h = d["health"]
    enrich, emb = h.get("enrichment") or {}, h.get("embeddings") or {}
    servers = enrich.get("servers") or []
    offline = sum(not s.get("live", False) for s in servers)
    status_ok = h.get("status") == "ok"
    encoder = emb.get("encoder", "off")
    vector_total = int(emb.get("records") or 0)
    vector_current = int(emb.get("current") or 0)
    coverage = f"{100 * vector_current / vector_total:.0f}%" if encoder == "available" and vector_total else "Off" if encoder == "off" else "Unavailable"
    coverage_note = f"{vector_current} of {vector_total} vectors current" if encoder == "available" and vector_total else "Search uses full text"
    status_detail = "Encoder available" if encoder == "available" else "Search using full text"
    if offline:
        status_detail += f" · {offline} of {len(servers)} model endpoints offline"
    if h.get("denied"):
        status_detail += f" · {h['denied']} denied calls"
    healthy = status_ok and not offline and encoder != "unavailable" and not h.get("denied")
    status_name = "Memory service operational" if healthy else "Memory service needs attention"
    status_cls = "good-text" if healthy else "warn-text"
    status_text = "Operational" if healthy else "Needs attention"

    def kpi(label: str, value: str, note: str, href: str = "", tone: str = "") -> str:
        tag = "a" if href else "div"
        link = f' href="{esc(href)}"' if href else ""
        return f'<{tag} class="kpi{" metric-link" if href else ""}"{link}><div class=kpi-label>{esc(label)}</div><div class="kpi-value {tone}">{esc(value)}</div><div class=kpi-note>{esc(note)}</div></{tag}>'

    return (
        f"<section class=healthstrip><div><strong>{status_name}</strong><div class=detail>{esc(status_detail)} · checked {esc(local(d['now'], '%Y-%m-%d %H:%M'))}</div></div>"
        f'<div class="state {status_cls}"><span class=tiny-dot></span>{status_text}</div></section>'
        '<section class=kpis aria-label="Key metrics">'
        + kpi("Stored records", str(h.get("records", "—")), f"{h.get('namespaces', '—')} namespaces", browse_href())
        + kpi("Review candidates", str(h.get("candidates", "—")), "Awaiting agent review", browse_href(status="candidate"), "amber")
        + kpi("Vector coverage", coverage, coverage_note, tone="blue")
        + kpi("Calls since start", str(h.get("requests", "—")), f"{h.get('denied', '—')} denied · uptime {span(int(h.get('uptime_s') or 0))}")
        + "</section>"
    )


def records_card(d: dict[str, Any]) -> str:
    by_type: dict[str, dict[str, int]] = d["counts"]["types"]
    if not by_type:
        return "<section class=card><div class=cardhead><h2>Record composition</h2></div><p class=muted>Nothing stored yet.</p></section>"
    totals = {s: sum(t.get(s, 0) for t in by_type.values()) for s, _ in STATUS}
    widest = max(sum(t.values()) for t in by_type.values()) or 1
    rows = []
    for name, counts in sorted(by_type.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:6]:
        n = sum(counts.values())
        rows.append(
            f'<a class=type-link href="{esc(browse_href(kind=name))}"><span class=label>{esc(words(name))}</span><span class=bar><i class=s1 style="width:{100 * n / widest:.1f}%"></i></span><span class=value>{n}</span></a>'
        )
    total = sum(totals.values())
    legend = "".join(f'<a href="{esc(browse_href(status=s))}">{dot(cls)}{s} <b>{totals[s]}</b></a>' for s, cls in STATUS if totals[s])
    status_bar = "".join(f'<i class={cls} style="flex:{totals[s]}"></i>' for s, cls in STATUS if totals[s])
    return (
        "<section class=card><div class=cardhead><div><h2>Record composition</h2><div class=card-sub>Stored records by status and type</div></div>"
        f"<span class=smallpill>{total} total</span></div><div class=record-total>{totals['active']} <span>active records</span></div>"
        f'<div class=stack role=img aria-label="{esc(", ".join(f"{s} {totals[s]}" for s, _ in STATUS))}">{status_bar}</div><div class=legend>{legend}</div><div class=divider></div>'
        "<h3>Most common record types</h3><div class=type-list>" + "".join(rows) + "</div></section>"
    )


def namespaces_card(d: dict[str, Any]) -> str:
    rows: dict[str, dict[str, Any]] = d["counts"]["namespaces"]
    if not rows:
        return "<section class=card><div class=cardhead><h2>Namespaces</h2></div><p class=muted>Nothing stored yet.</p></section>"
    widest = max(sum(r["status"].values()) for r in rows.values()) or 1
    lines = []
    for namespace, row in sorted(rows.items(), key=lambda kv: (-sum(kv[1]["status"].values()), kv[0]))[:5]:
        n = sum(row["status"].values())
        lines.append(
            f'<a class=namespace-link href="{esc(browse_href(namespace=namespace))}" title="{esc(namespace)}">'
            f'<span class=label>{esc(namespace)}</span><span class=bar><i class=s1 style="width:{100 * n / widest:.1f}%"></i></span>'
            f'<span class=value>{n}</span><span class=age title="{esc(local(row["last"]))}">{ago(row["last"], d["now"])}</span></a>'
        )
    return (
        "<section class=card><div class=cardhead><div><h2>Largest namespaces</h2><div class=card-sub>Recent activity and record counts</div></div>"
        f"<span class=smallpill>Top {len(lines)} of {len(rows)}</span></div><div class=namespace-list>{''.join(lines)}</div></section>"
    )


def hours_chart(d: dict[str, Any]) -> str:
    """Audit rows per hour over the last 24 hours, oldest on the left, as SVG columns."""
    now: dt.datetime = d["now"]
    counts: dict[str, int] = d["counts"]["hours"]
    slots = [now - dt.timedelta(hours=i) for i in range(23, -1, -1)]
    values = [counts.get(t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H"), 0) for t in slots]
    top = max(values) or 1
    bw, gap, h = 18, 4, 80
    cols = []
    for i, (t, n) in enumerate(zip(slots, values, strict=True)):
        x = i * (bw + gap)
        bh = 0 if n == 0 else max(2.0, h * n / top)
        hour = t.astimezone().strftime("%H:00")
        tip = f"<title>{hour}: {n} audit rows</title>"
        cols.append(f'<rect x={x} y=0 width={bw} height={h} style="fill:transparent">{tip}</rect>')
        if bh:
            cols.append(f'<rect x={x} y={h - bh:.1f} width={bw} height={bh:.1f} rx=2 style="fill:var(--s1)">{tip}</rect>')
        if i % 6 == 0 or i == 23:
            cols.append(f'<text x={x + bw / 2} y={h + 14} text-anchor=middle style="fill:var(--muted);font-size:10px">{hour[:2]}</text>')
    width = 24 * (bw + gap) - gap
    axis = f'<line x1=0 y1={h + 0.5} x2={width} y2={h + 0.5} style="stroke:var(--line)"/>'
    busiest = max(values)
    return (
        f'<svg viewBox="0 0 {width} {h + 18}" width="100%" style="max-width:48rem" role=img aria-label="audit rows per hour, last 24 hours, busiest hour {busiest}">'
        f"{axis}{''.join(cols)}</svg><div class='small muted'>audit rows per hour: writes, jobs and refusals; reads are not audited. Busiest hour {busiest}</div>"
    )


def activity_card(d: dict[str, Any]) -> str:
    c, h = d["counts"], d["health"]
    actions = sorted(c["actions"].items(), key=lambda kv: (-kv[1], kv[0]))
    top = [(name, n, "crit" if name == "denied" else SERIES[i] if i < len(SERIES) else "idle") for i, (name, n) in enumerate(actions[:6])]
    rest = sum(n for _, n in actions[6:])
    if rest:
        top.append(("other", rest, "idle"))
    clients = sorted(c["clients"].items(), key=lambda kv: (-kv[1], kv[0]))
    by_client = [(name, n, SERIES[i] if i < len(SERIES) else "idle") for i, (name, n) in enumerate(clients)]
    total = sum(c["actions"].values())
    s = c["searches"]
    configured = " ".join(chip("idle", str(n)) for n in h.get("clients") or [])
    rows = "".join(
        f"<tr><td title='{esc(local(a['at'], '%Y-%m-%d %H:%M:%S'))}'>{esc(local(a['at'], '%H:%M:%S'))}</td><td>{esc(a['client'])}</td>"
        f"<td>{chip('crit' if a['action'] == 'denied' else 'idle', str(a['action']))}</td><td><code>{esc(a['namespace'])}</code></td>"
        f"<td class=muted>{esc(str(a['detail'])[:120])}</td></tr>"
        for a in d["audit"]
    )
    table = (
        f"<details><summary>Latest audit rows ({len(d['audit'])})</summary><table><tr><th>time</th><th>client</th><th>action</th><th>namespace</th><th>detail</th></tr>{rows}</table></details>"
        if rows
        else "<p class='small muted'>No audit rows yet.</p>"
    )
    body = (
        f"<div class=split><div style='flex:1;min-width:18rem'>{hours_chart(d)}</div>{ring(s['hits'], s['total'], 'searches that found records')}</div>"
        f"<h3>By action</h3>{stacked(top)}<h3>By client</h3>{stacked(by_client)}"
        f"<p class='small muted'>Clients configured: {configured or 'none'}</p>{table}"
    )
    return card("Audit, last 24 h", body, "wide", f"<span class='small muted'>{total} rows</span>")


def models_card(d: dict[str, Any]) -> str:
    h = d["health"]
    e = h.get("enrichment") or {}
    servers = e.get("servers") or []
    live = sum(bool(server.get("live")) for server in servers)
    rows = []
    for index, server in enumerate(servers):
        active = bool(server.get("live"))
        tier = server.get("tier", "?")
        label = "Primary route" if tier == 0 else f"Fallback {index}"
        state = "Available" if active else "Offline"
        tone = "good-text" if active else "warn-text"
        rows.append(
            f'<div class=model-row><span class="state {tone}"><span class=tiny-dot></span></span><div>'
            f'<div class=model-name>{esc(label)} · tier {esc(tier)}</div><div class=model-meta title="{esc(server.get("url"))}">{esc(server.get("url"))}</div></div>'
            f'<span class="model-state {tone}">{state}</span></div>'
        )
    if not e.get("configured"):
        rows.append("<p class=muted>Enrichment is off; no model server is configured.</p>")
    jobs = h.get("jobs") or {}
    emb = h.get("embeddings") or {}
    vector_status = "Off" if emb.get("encoder") == "off" else "Available" if emb.get("encoder") == "available" else "Unavailable"
    vector_detail = f"{int(emb.get('current') or 0)} of {int(emb.get('records') or 0)} vectors current" if vector_status == "Available" else "Full-text search remains available"
    return (
        "<section class=card><div class=cardhead><div><h2>Model health</h2><div class=card-sub>Enrichment and encoder availability</div></div>"
        f"<span class=smallpill>{live} of {len(servers)} live</span></div><div class=model-list>{''.join(rows)}</div>"
        f"<div class=model-foot><div><strong>{esc(vector_status)} encoder</strong><br>{esc(vector_detail)}</div>"
        f"<div><strong>{int(jobs.get('done') or 0)} done</strong><br>{int(jobs.get('failed') or 0)} failed · {int(jobs.get('queued') or 0)} queued</div></div>"
        "<details><summary>Model configuration and diagnostics</summary>" + enrichment(d) + encoder(d) + "</details></section>"
    )


def enrichment(d: dict[str, Any]) -> str:
    h = d["health"]
    e = h.get("enrichment") or {}
    if not e.get("configured"):
        return "<p class=muted>Off: no model server configured, so session notes are not enriched.</p>"
    lines = []
    for s in e.get("servers") or []:
        tier = s.get("tier")
        tier_chip = chip("s4" if tier == 0 else "idle", f"tier {tier}", "tier 0 is asked first; tier 1 only while tier 0 has no live server")
        lines.append(
            f"<div class=server>{dot('good' if s.get('live') else 'crit', 'live' if s.get('live') else 'down')}{tier_chip}"
            f"<code title='{esc(s.get('url'))}'>{esc(s.get('url'))}</code><span class='small muted'>{s.get('in_flight', 0)} in flight</span></div>"
        )
    jobs = h.get("jobs") or {}
    job_parts = [(k, int(jobs.get(k, 0)), cls) for k, cls in JOB.items()] + [(k, int(v), "idle") for k, v in sorted(jobs.items()) if k not in JOB]
    oldest = f" · oldest queued {ago(e.get('oldest'), d['now'])}" if e.get("oldest") else ""
    return (
        f"<p class=row>{chip('s4', str(e.get('model')), 'model')}{chip('idle', str(e.get('api')), 'API')}{chip('idle', str(e.get('prompt')), 'extraction prompt')}</p>"
        f"<div class=list>{''.join(lines) or '<p class=muted>No servers listed.</p>'}</div>"
        f"<div style='margin-top:.6rem'>{stacked(job_parts)}</div>"
        f"<p class='small muted'>Jobs, all time. Since start: {e.get('processed', 0)} processed, {e.get('failed', 0)} failed, {e.get('queued', 0)} queued{oldest}.</p>"
    )


def encoder(d: dict[str, Any]) -> str:
    e = d["health"].get("embeddings") or {}
    status = e.get("encoder", "off")
    if status == "off":
        return "<p class=muted>Off: no encoder configured, so search is full-text only.</p>"
    cls = "good" if status == "available" else "crit"
    err = f"<p class=small>{chip('crit', 'error')} {esc(e['error'])}</p>" if e.get("error") else ""
    return (
        f"<div class=split>{ring(int(e.get('current') or 0), int(e.get('records') or 0), 'vectors current')}"
        f"<div class=list><span class=row>{chip(cls, str(status), 'encoder')}{chip('s4', str(e.get('model')), 'model')}</span>"
        f"<span class='small muted'>snapshot <code>{esc(e.get('snapshot'))}</code> · {e.get('refused', 0)} refused by the secret screen</span></div></div>{err}"
    )


def maintenance_card(d: dict[str, Any]) -> str:
    h, now = d["health"], d["now"]
    rv = h.get("candidate_review") or {}
    sw = h.get("anchor_sweep") or {}
    if rv.get("at"):
        forgotten = sorted((rv.get("forgotten") or {}).items())
        parts = [("kept", int(rv.get("kept") or 0), "s1")]
        parts += [(f"forgotten, {k}", int(v), "s2" if i == 0 else "s4") for i, (k, v) in enumerate(forgotten)]
        parts.append(("expired", int(rv.get("expired") or 0), "s3"))
        parts.append(("failed", int(rv.get("failed") or 0), "crit"))
        split = " ".join(chip("idle", f"{u} {n}") for u, n in (rv.get("servers") or {}).items())
        err = f" {chip('crit', 'error')} {esc(rv['error'])}" if rv.get("error") else ""
        review = (
            f"<p class='small muted' title='{esc(local(rv['at']))}'>Last run {ago(rv['at'], now)}, {rv.get('reviewed', 0)} reviewed in {rv.get('seconds', 0)} s; "
            f"next at {esc(rv.get('review_at'))}.{err}</p>{stacked(parts)}" + (f"<div class=legend>{split}</div>" if split else "")
        )
    else:
        review = f"<p class='small muted'>No run yet; next at {esc(rv.get('review_at'))}.</p>"
    if sw.get("at"):
        missing = int(sw.get("missing") or 0)
        checked, filed = sw.get("checked", 0), sw.get("filed", 0)
        sweep = (
            f"<p class='small muted' title='{esc(local(sw['at']))}'>Last sweep {ago(sw['at'], now)} against {sw.get('entities', 0)} entities.</p>"
            f"<p class=row>{chip('s1', f'{checked} anchors checked')}{chip('warn' if missing else 'good', f'{missing} missing')}{chip('idle', f'{filed} filed')}</p>"
        )
    else:
        sweep = "<p class='small muted'>No sweep yet.</p>"
    review_card = f"<div class=maint><strong>Candidate review</strong><div class=meta>Last run {ago(rv.get('at'), now)} · next at {esc(rv.get('review_at', '—'))}</div>{review}</div>"
    sweep_card = f"<div class=maint><strong>Anchor sweep</strong><div class=meta>Last run {ago(sw.get('at'), now)}</div>{sweep}</div>"
    return (
        "<section class=card><div class=cardhead><div><h2>Maintenance</h2><div class=card-sub>Recent automated checks</div></div></div><div class=maint-grid>"
        + review_card
        + sweep_card
        + "</div></section>"
    )


def unused_card(d: dict[str, Any]) -> str:
    """Active records that no search, task context or recall returned in the window."""
    u = d.get("unused")
    if not u:
        return ""
    days = int(u.get("days") or 30)
    since = parse(u.get("counting_since"))
    if not u.get("complete"):
        when = f"since {esc(local(since, '%Y-%m-%d'))}; the list starts {esc((since + dt.timedelta(days=days)).astimezone().strftime('%Y-%m-%d'))}" if since else "not started"
        return card("Unused records", f"<p class='small muted'>Returns and fetches are counted {when}.</p>")
    n, recs = int(u.get("count") or 0), u.get("records") or []
    if not n:
        return card("Unused records", f"<p class='small muted'>Every active record older than {days} days was returned at least once in that time.</p>")
    rows = "".join(
        f"<div class='metric ns'><code title='{esc(r['id'])} in {esc(r['namespace'])}'>{esc(r['id'][:8])}</code><span title='{esc(r['title'])}'>{esc(r['title'])}</span>"
        f"<span class='small muted'>{esc(words(r['type']))}</span><span class='small muted' title='created {esc(local(r['created_at']))}'>{ago(r['created_at'], d['now'])}</span></div>"
        for r in recs
    )
    more = f"<p class='small muted'>{n - len(recs)} more not listed.</p>" if n > len(recs) else ""
    return card(
        "Unused records",
        f"<p class='small muted'>{n} active record(s) older than {days} days that no search, task context or recall returned in that time, oldest first.</p><div class=list>{rows}</div>{more}",
        "wide",
    )


def connect_card(d: dict[str, Any]) -> str:
    """Each client and how it authenticates, and what an agent needs to connect. No token is shown here:
    a generated one appears once, in the add-on's log."""
    c = d.get("connect")
    if not c:
        return ""
    url = esc(c.get("url"))
    by = {"generated token": "s3", "token": "s1", "address": "idle"}
    rows = "".join(f"<div class=row>{chip(by.get(r['by'], 'idle'), esc(r['name']))}<span class='small muted'>{esc(r['by'])}</span></div>" for r in c.get("clients") or [])
    steps = (
        f"<p class=small>MCP endpoint <code>{url}</code></p>"
        "<p class='small muted'>A client listed by name alone gets a token the add-on generates at start; the token is shown once, in the add-on's log, with the command for each agent. "
        "To issue a new one, remove the client, restart, add it back and restart again.</p>"
        f"<p class=small>Claude Code: <code>claude mcp add --scope user --transport http agent-memory {url} --header 'Authorization: Bearer &lt;token&gt;'</code></p>"
        f'<p class=small>Codex, <code>~/.codex/config.toml</code>: <code>[mcp_servers.agent_memory]</code> with <code>url = "{url}"</code> and <code>bearer_token_env_var = "AGENT_MEMORY_TOKEN"</code>, the token in that variable.</p>'
    )
    return "<section class=card><details><summary>Connect an agent</summary>" + f"<div class=list>{rows or '<p class=muted>No clients configured.</p>'}</div>{steps}" + "</details></section>"


def _browser_link(browser: dict, **changes: str | int) -> str:
    filters = {key: browser[key] for key in ("query", "namespace", "status", "kind") if browser[key]}
    filters.update(changes)
    return browse_href(**filters)


def browser_view(d: dict[str, Any], browser: dict) -> str:
    selected = browser["selected"]
    empty_message = "No records stored yet." if not d["health"].get("records") else "No records match these filters. Clear the filters to see all records."
    options = []
    for value, label in (("", "All statuses"), ("active", "Active"), ("candidate", "Candidate"), ("superseded", "Superseded")):
        options.append(f'<option value="{value}"{" selected" if value == browser["status"] else ""}>{label}</option>')
    namespaces = ['<option value="">All namespaces</option>']
    for row in browser["namespaces"]:
        namespace = row["namespace"]
        namespaces.append(f'<option value="{esc(namespace)}"{" selected" if namespace == browser["namespace"] else ""}>{esc(namespace)}</option>')
    types = ['<option value="">All types</option>']
    for kind in browser["types"]:
        types.append(f'<option value="{esc(kind)}"{" selected" if kind == browser["kind"] else ""}>{esc(words(kind))}</option>')
    rows = []
    for record in browser["records"]:
        excerpt = " ".join(record["content"].split())[:180]
        active = record["id"] == selected["id"] if selected else False
        href = _browser_link(browser, page=browser["page"], id=record["id"])
        badge = "candidate" if record["status"] == "candidate" else ""
        rows.append(
            f'<a class="memory-item{" selected" if active else ""}" href="{esc(href)}"{" aria-current=page" if active else ""}>'
            f"<strong>{esc(record['title'])}</strong><span class=excerpt>{esc(excerpt)}</span>"
            f'<span class=meta><span class="badge {badge}">{esc(record["status"])}</span> · {esc(record["namespace"])} · {esc(words(record["type"]))} · updated {ago(record["updated_at"], d["now"])}</span></a>'
        )
    if selected:
        fields = (
            ("Status", selected["status"]),
            ("Type", words(selected["type"])),
            ("Namespace", selected["namespace"]),
            ("Revision", selected["revision"]),
            ("Writer", selected.get("agent") or "—"),
            ("Source session", selected.get("source_session") or "—"),
            ("Repository", selected.get("repository") or "—"),
            ("Commit", selected.get("commit") or "—"),
            ("Evidence", f"{selected.get('proof_count', 0)} for / {selected.get('refutation_count', 0)} against"),
            ("Updated", local(selected.get("updated_at"))),
        )
        metadata = "".join(f"<div><div class=meta-label>{esc(label)}</div><div class=meta-value>{esc(value)}</div></div>" for label, value in fields)
        detail = (
            f"<div class=smallpill>Record {esc(selected['id'][:8])} · read only</div><h2>{esc(selected['title'])}</h2>"
            f"<div class=body>{esc(selected['content'])}</div><div class=meta-grid>{metadata}</div>"
        )
    else:
        detail = f"<div class=empty>{empty_message}</div>"
    total = browser["total"]
    start = (browser["page"] - 1) * browser["page_size"] + 1 if browser["records"] else 0
    end = min(browser["page"] * browser["page_size"], total)
    previous = f'<a href="{esc(_browser_link(browser, page=browser["page"] - 1))}">Previous</a>' if browser["page"] > 1 else "<span></span>"
    following = f'<a href="{esc(_browser_link(browser, page=browser["page"] + 1))}">Next</a>' if end < total else "<span></span>"
    return (
        "<div class=browserhead><div><h2>Record browser</h2><p>Review content and provenance in a read-only view.</p></div>"
        f"<span class=smallpill>{total} matching</span></div>"
        "<form class=filterbar method=get><input type=hidden name=view value=browse>"
        f'<input type=search name=query value="{esc(browser["query"])}" placeholder="Search record text" aria-label="Search record text" maxlength=120>'
        f'<select name=namespace aria-label="Namespace">{"".join(namespaces)}</select>'
        f'<select name=kind aria-label="Record type">{"".join(types)}</select>'
        f'<select name=status aria-label="Status">{"".join(options)}</select><button class=btn type=submit>Apply</button></form>'
        f"<div class=scopebar><span>Showing {total} records · page {browser['page']}</span>"
        f'<span><a class=btn href="{esc(browse_href())}">Show all</a> <a class=btn href="?view=overview">Back to overview</a></span></div>'
        '<div class=browsergrid><section class="card listcard" aria-label="Memory records"><div class=listhead>Records · select to inspect</div>'
        f"<div class=memory-list>{''.join(rows) or f'<div class=empty>{empty_message}</div>'}</div></section>"
        f'<section class="card detailcard" aria-label="Selected record">{detail}</section></div>'
        f"<nav class=pager aria-label=Pagination>{previous}<span>{start}–{end} of {total}</span>{following}</nav>"
    )


def render(d: dict[str, Any], browser: dict | None = None) -> str:
    selected = "browse" if browser is not None else "overview"
    content = (
        browser_view(d, browser)
        if browser is not None
        else (
            tiles(d)
            + "<div class=overviewgrid>"
            + records_card(d)
            + models_card(d)
            + "</div>"
            + "<div class=lowergrid>"
            + maintenance_card(d)
            + namespaces_card(d)
            + "</div>"
            + unused_card(d)
            + "<div class=extras>"
            + activity_card(d)
            + connect_card(d)
            + "</div>"
        )
    )
    return (
        f'<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Agent Memory</title><style>{CSS}</style><main>"
        "<header class=pagehead><div><div class=topline>Shared context</div><h1>Agent Memory</h1>"
        "<p class=sub>Shared, bounded context for coding agents. Records are data, never instructions.</p></div>"
        '<button class=theme-toggle type=button id=theme-toggle aria-label="Toggle light and dark theme">Light mode</button></header>'
        f'<nav class=tabs aria-label="Memory views"><a class="tab{" active" if selected == "overview" else ""}" href="?view=overview"{" aria-current=page" if selected == "overview" else ""}>Overview</a>'
        f'<a class="tab{" active" if selected == "browse" else ""}" href="{esc(browse_href())}"{" aria-current=page" if selected == "browse" else ""}>Browse records</a></nav>'
        f"{content}<div class=footer>Agent Memory · read-only panel · {esc(d['health'].get('version', ''))}</div></main>"
        '<script>const root=document.documentElement,button=document.getElementById("theme-toggle");'
        'try{root.dataset.theme=localStorage.getItem("agent-memory-theme")||"dark"}catch(e){root.dataset.theme="dark"}'
        'function label(){button.textContent=root.dataset.theme==="light"?"Dark mode":"Light mode"}label();'
        'button.addEventListener("click",()=>{root.dataset.theme=root.dataset.theme==="light"?"dark":"light";'
        'try{localStorage.setItem("agent-memory-theme",root.dataset.theme)}catch(e){}label()})</script></html>'
    )
