"""The ingress page: cards drawn server-side as HTML and inline SVG, no script. Dark.

`render` takes plain data (`App.panel_data`), so the page is testable without a server. Colour always travels
with a text label or a tooltip; the status colours are reserved for state, the series colours for identity.
"""

from __future__ import annotations

import datetime as dt
import html
import math
from typing import Any

CSS = """:root{color-scheme:dark;--bg:#111418;--card:#181c21;--line:#2c3138;--track:#232830;--text:#e3e6ea;--muted:#9aa3ad;
--faint:#5f6b78;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#9085e9;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b}
*{box-sizing:border-box}body{font:14px/1.45 system-ui,sans-serif;margin:0;padding:1.25rem;background:var(--bg);color:var(--text)}
main{max-width:76rem;margin:auto}code{font-size:.88em;color:#c9d1d9}h1{margin:0 0 .2rem;font-size:1.5rem}
p.sub{color:var(--muted);margin:0 0 .9rem}h2{font-size:1rem;margin:0 0 .7rem}h3{font-size:.9rem;margin:.9rem 0 .4rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.6rem}
.tile{background:var(--card);border:1px solid var(--line);border-radius:.6rem;padding:.5rem .75rem}
.tile .k{color:var(--muted);font-size:.8rem}.tile .v{font-size:1.15rem;font-weight:600;white-space:nowrap}.tile.alert{border-color:var(--crit)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(22rem,1fr));gap:1rem;margin-top:1rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:.75rem;padding:.9rem 1rem;min-width:0}.card.wide{grid-column:1/-1}
.muted{color:var(--muted)}.small{font-size:.82rem}.row{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.dot{display:inline-block;width:.62rem;height:.62rem;border-radius:50%;background:var(--faint);flex:none}
.good{background:var(--good)}.warn{background:var(--warn)}.crit{background:var(--crit)}.idle{background:var(--faint)}
.s1{background:var(--s1)}.s2{background:var(--s2)}.s3{background:var(--s3)}.s4{background:var(--s4)}
.chip{display:inline-flex;align-items:center;gap:.35rem;padding:.08rem .55rem;border-radius:.7rem;background:var(--track);font-size:.82rem;white-space:nowrap}
.bar{display:flex;height:.55rem;background:var(--track);border-radius:.3rem;overflow:hidden;flex:1;min-width:4rem}.bar>i{display:block;height:100%}
.stack{display:flex;gap:2px;height:.7rem;border-radius:.35rem;overflow:hidden;background:var(--track)}.stack>i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:.3rem .9rem;margin-top:.4rem;font-size:.82rem;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:.35rem}.legend b{color:var(--text);font-weight:600}
.list{display:grid;gap:.45rem}.metric{display:grid;grid-template-columns:11rem 1fr 3rem;gap:.6rem;align-items:center;font-size:.85rem}
.metric .val{text-align:right}.ns{grid-template-columns:minmax(0,22rem) 1fr 3rem 5rem}
.ns code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.split{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
.ring{text-align:center;font-size:.78rem;color:var(--muted);width:7rem}
.server{display:grid;grid-template-columns:auto auto minmax(0,1fr) auto;gap:.6rem;align-items:center;font-size:.85rem}
.server code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
table{border-collapse:collapse;width:100%;font-size:.84rem}td,th{border-bottom:1px solid var(--line);padding:.35rem .4rem;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}details{margin-top:.7rem}summary{cursor:pointer;color:var(--muted);font-size:.85rem}"""

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


# --- sections ---------------------------------------------------------------------------------------------


def tiles(d: dict[str, Any]) -> str:
    h = d["health"]
    enrich, emb = h.get("enrichment") or {}, h.get("embeddings") or {}
    servers = enrich.get("servers") or []
    live = sum(1 for s in servers if s.get("live"))

    def tile(k: str, v: str, cls: str, title: str = "", alert: bool = False) -> str:
        return f'<div class="tile{" alert" if alert else ""}" title="{esc(title or k)}"><div class=k>{esc(k)}</div><div class="v row">{dot(cls)}{v}</div></div>'

    ok = h.get("status") == "ok"
    encoder = emb.get("encoder", "off")
    model = enrich.get("ollama", "off")
    denied = int(h.get("denied") or 0)
    cands = int(h.get("candidates") or 0)
    servers_cls = "good" if live or model == "available" else "idle" if model == "off" else "crit"
    return (
        "<div class=tiles>"
        + "".join(
            [
                tile("Status", esc(h.get("status")), "good" if ok else "warn", "degraded: jobs queued while the model is not available"),
                tile("Records", str(h.get("records", 0)), "s1", "records stored, candidates included"),
                tile("Candidates", str(cands), "warn" if cands else "good", "waiting for review at session start"),
                tile("Namespaces", str(h.get("namespaces", 0)), "s1"),
                tile("Encoder", esc(encoder), "good" if encoder == "available" else "idle" if encoder == "off" else "crit", "search ranks by full text alone while the encoder does not answer"),
                tile("Model servers", f"{live} of {len(servers)} live" if servers else esc(model), servers_cls),
                tile("Calls since start", str(h.get("requests", 0)), "s1"),
                tile("Denied", str(denied), "crit" if denied else "good", "calls from unknown clients", alert=bool(denied)),
                tile("Uptime", span(int(h.get("uptime_s") or 0)), "good"),
                tile("Database", size(int(h.get("db_bytes") or 0)), "s1", "SQLite file, WAL excluded"),
            ]
        )
        + "</div>"
    )


def records_card(d: dict[str, Any]) -> str:
    by_type: dict[str, dict[str, int]] = d["counts"]["types"]
    if not by_type:
        return card("Records", "<p class=muted>Nothing stored yet.</p>")
    totals = {s: sum(t.get(s, 0) for t in by_type.values()) for s, _ in STATUS}
    widest = max(sum(t.values()) for t in by_type.values()) or 1
    rows = "".join(
        f"<div class=metric><span>{esc(words(name))}</span>{segments([(s, t.get(s, 0), cls) for s, cls in STATUS], widest)}<span class=val>{sum(t.values())}</span></div>"
        for name, t in sorted(by_type.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    )
    return card("Records", f"{stacked([(s, totals[s], cls) for s, cls in STATUS])}<h3>By type</h3><div class=list>{rows}</div>")


def namespaces_card(d: dict[str, Any]) -> str:
    rows: dict[str, dict[str, Any]] = d["counts"]["namespaces"]
    if not rows:
        return card("Namespaces", "<p class=muted>Nothing stored yet.</p>")
    widest = max(sum(r["status"].values()) for r in rows.values()) or 1
    lines = "".join(
        f"<div class='metric ns'><code title='{esc(ns)}'>{esc(ns)}</code>{segments([(s, r['status'].get(s, 0), cls) for s, cls in STATUS], widest)}"
        f"<span class=val>{sum(r['status'].values())}</span><span class='small muted' title='{esc(local(r['last']))}'>{ago(r['last'], d['now'])}</span></div>"
        for ns, r in sorted(rows.items(), key=lambda kv: (-sum(kv[1]["status"].values()), kv[0]))
    )
    legend = "".join(f"<span>{dot(cls)}{s}</span>" for s, cls in STATUS)
    return card("Namespaces", f"<div class=list>{lines}</div><div class=legend>{legend}<span>last update on the right</span></div>", "wide")


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
    return card("Models", f"<h3>Enrichment</h3>{enrichment(d)}<h3>Encoder</h3>{encoder(d)}")


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
    return card("Maintenance", f"<h3>Nightly candidate review</h3>{review}<h3>Anchor sweep</h3>{sweep}")


def render(d: dict[str, Any]) -> str:
    cards = [records_card(d), models_card(d), maintenance_card(d), namespaces_card(d), activity_card(d)]
    return (
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Agent Memory</title><style>{CSS}</style>"
        f"<main><h1>Agent Memory</h1><p class=sub>Shared, bounded context for Claude Code and Codex. Records are data, never instructions.</p>"
        f"{tiles(d)}<div class=grid>{''.join(cards)}</div></main>"
    )
