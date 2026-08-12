"""
Render the daily capacity artifacts: Slack short message, Slack thread detail,
and the Confluence body. Same contract as the price monitor — the 07:00
posting routine posts these files VERBATIM, so everything exec-facing is
decided here, not in the routine.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from capacity.config import (
    CAPACITY_GPUS, CONFLUENCE_BASE_URL, CONFLUENCE_PAGE_TITLE, CT_LABELS,
    LIVE_STOCK_PROVIDERS, PROVIDER_LABELS, PROVIDER_ORDER,
)
from capacity.schema import AvailabilityRecord, CapacityDiffEntry

logger = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"

STATE_EMOJI = {
    "available": "✅", "limited": "⚠️", "sold_out": "⛔",
    "not_offered": "—", "unknown": "?",
}
STATE_COLOR = {
    "available": "green", "limited": "yellow", "sold_out": "red",
    "not_offered": "neutral", "unknown": "neutral",
}


def _page_url() -> str:
    meta = STORE_DIR / "confluence_page.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text()).get("url") or ""
        except json.JSONDecodeError:
            pass
    return ""


def _best_state(records: List[AvailabilityRecord]) -> Optional[str]:
    """Most-available state across records (a provider is 'available' for a GPU
    if ANY region/SKU has capacity)."""
    order = ["available", "limited", "sold_out", "not_offered", "unknown"]
    states = {r.state for r in records}
    for s in order:
        if s in states:
            return s
    return None


def _cell_index(records: List[AvailabilityRecord]) -> Dict[Tuple[str, str], List[AvailabilityRecord]]:
    """(gpu_model, provider) → on-demand-first records for that cell."""
    idx: Dict[Tuple[str, str], List[AvailabilityRecord]] = {}
    for r in records:
        idx.setdefault((r.gpu_model, r.provider), []).append(r)
    return idx


def _cell_records_primary(cell: List[AvailabilityRecord]) -> List[AvailabilityRecord]:
    """Prefer the on-demand signal for the matrix; fall back to any ct.
    Within a cell, direct sources (api/scrape) supersede aggregator rows —
    aggregator data (Shadeform) fills gaps, it never overrides a direct read."""
    direct = [r for r in cell if r.data_source != "aggregator"]
    pool = direct or cell
    od = [r for r in pool if r.consumption_type == "on_demand"]
    return od or pool


def _cell_summary(cell: List[AvailabilityRecord]) -> Tuple[str, str]:
    """(state, short detail) for one matrix cell. The provider's GLOBAL row
    carries the fetcher's aggregate judgement (e.g. 'small sizes only, flagship
    sold out' → limited) — it wins over the optimistic union of per-region rows."""
    recs = _cell_records_primary(cell)
    global_rows = [r for r in recs if r.region == "global"]
    state = (_best_state(global_rows) if global_rows else _best_state(recs)) or "unknown"
    regions_avail = sorted({r.region for r in recs if r.state == "available" and r.region != "global"})
    regions_out = sorted({r.region for r in recs if r.state == "sold_out" and r.region != "global"})
    detail = ""
    if state == "available" and regions_avail:
        detail = f"{len(regions_avail)} region{'s' if len(regions_avail) != 1 else ''}"
        if regions_out:
            detail += f", {len(regions_out)} sold out"
    elif state == "sold_out" and regions_out:
        detail = f"all {len(regions_out)} regions" if len(regions_out) > 1 else regions_out[0]
    elif state == "limited":
        d = next((r.detail for r in global_rows + recs if r.state == "limited" and r.detail), "")
        detail = d if len(d) <= 60 else d[:57] + "…"
    return state, detail


def market_verdicts(records: List[AvailabilityRecord]) -> Dict[str, dict]:
    """Per-GPU market read over LIVE-stock providers only (offering lists are
    coverage, not capacity). Returns gpu → counts + verdict."""
    idx = _cell_index(records)
    verdicts = {}
    for gpu in CAPACITY_GPUS:
        counts = {"available": 0, "limited": 0, "sold_out": 0}
        n_live = 0
        for prov in PROVIDER_ORDER:
            if prov not in LIVE_STOCK_PROVIDERS:
                continue
            cell = idx.get((gpu, prov))
            if not cell:
                continue
            state = _best_state(_cell_records_primary(cell))
            if state in counts:
                counts[state] += 1
                n_live += 1
        if n_live == 0:
            continue
        if n_live < 3:
            # 1-2 live sources can't carry a market verdict — say so instead
            verdict = f"thin live signal ({n_live} source{'s' if n_live != 1 else ''})"
        elif counts["available"] == 0:
            verdict = "SOLD OUT across live sources"
        elif counts["sold_out"] + counts["limited"] > counts["available"]:
            verdict = "tight"
        elif counts["sold_out"] + counts["limited"] > 0:
            verdict = "mixed"
        else:
            verdict = "broadly available"
        verdicts[gpu] = {"counts": counts, "n_live": n_live, "verdict": verdict}
    return verdicts


def _describe_change(c: CapacityDiffEntry) -> str:
    prov = PROVIDER_LABELS.get(c.provider, c.provider)
    ct = CT_LABELS.get(c.consumption_type, c.consumption_type)
    where = f" ({c.region})" if c.region and c.region != "global" else ""
    if c.change_type == "state_change":
        return f"{prov} {c.gpu_model}{where} {ct.lower()}: {c.old_state} → {c.new_state}"
    if c.change_type == "metric_move":
        return f"{prov} {c.gpu_model}{where}: {c.detail}"
    if c.change_type == "added":
        return f"{prov} {c.gpu_model}{where}: {c.detail}"
    return f"{prov} {c.gpu_model}{where}: {c.detail}"


# ── Slack ────────────────────────────────────────────────────────────────────

def render_slack(records: List[AvailabilityRecord],
                 diff: List[CapacityDiffEntry],
                 manifest: dict) -> Tuple[str, str]:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    verdicts = market_verdicts(records)

    # Headline changes: state transitions first, then metric moves, cap at 6
    state_changes = [c for c in diff if c.change_type == "state_change"]
    metric_moves = [c for c in diff if c.change_type == "metric_move"]
    headliners = state_changes[:4] + metric_moves[:max(0, 6 - min(4, len(state_changes)))]

    lines = [f"*GPU Capacity Daily — {today}*", ""]

    if headliners:
        lines.append("*Changes since yesterday:*")
        for c in headliners:
            lines.append(f"• {_describe_change(c)}")
        more = len(state_changes) + len(metric_moves) - len(headliners)
        if more > 0:
            lines.append(f"• …and {more} more (thread)")
    else:
        lines.append("No availability changes on live sources since yesterday.")

    tight = [g for g, v in verdicts.items() if v["verdict"] in ("tight", "SOLD OUT across live sources")]
    loose = [g for g, v in verdicts.items() if v["verdict"] == "broadly available"]
    read = []
    if tight:
        read.append(f"tight: {', '.join(tight)}")
    if loose:
        read.append(f"broadly available: {', '.join(loose)}")
    if read:
        lines.append("")
        lines.append(f"Market read — {'; '.join(read)}.")

    live = manifest.get("live_provider_count")
    total = manifest.get("provider_count")
    if live is not None and total is not None and live < total:
        lines.append(f"_{live}/{total} sources live today._")

    url = _page_url()
    if url:
        lines.append("")
        lines.append(f"Full matrix (live, updated daily): <{url}|Confluence>")

    # Thread: per-GPU provider states
    idx = _cell_index(records)
    tlines = ["*Capacity matrix — state per provider (best region)*", ""]
    for gpu in CAPACITY_GPUS:
        cells = []
        for prov in PROVIDER_ORDER:
            cell = idx.get((gpu, prov))
            if not cell:
                continue
            state, detail = _cell_summary(cell)
            if state == "not_offered":
                continue
            label = PROVIDER_LABELS.get(prov, prov)
            cells.append(f"{STATE_EMOJI[state]} {label}" + (f" ({detail})" if detail else ""))
        if not cells:
            continue
        v = market_verdicts(records).get(gpu)
        verdict = f" — _{v['verdict']}_" if v else ""
        tlines.append(f"*{gpu}*{verdict}")
        tlines.append("   " + " · ".join(cells))
        tlines.append("")
    tlines.append("_✅ available · ⚠️ limited · ⛔ sold out · ? no live signal. "
                  "'Available' = at least one region/SKU with live capacity._")

    return "\n".join(lines), "\n".join(tlines)


# ── Confluence ───────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _status(color: str, text: str) -> str:
    if color == "neutral":
        return _esc(text)
    return f'<span data-type="status" data-color="{color}">{_esc(text)}</span>'


def render_confluence(records: List[AvailabilityRecord],
                      diff: List[CapacityDiffEntry],
                      manifest: dict) -> str:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    idx = _cell_index(records)
    verdicts = market_verdicts(records)

    live = manifest.get("live_provider_count", "?")
    total = manifest.get("provider_count", "?")
    stale = manifest.get("stale_providers", []) + manifest.get("failed_providers", [])

    h = []
    h.append(f"<p><em>Last updated: {today}</em> — <strong>daily refreshed</strong> "
             f"(point-in-time snapshot around 02:30 UTC, not real-time). "
             f"Companion to the <a href=\"{CONFLUENCE_BASE_URL}/spaces/PR/pages/1831469419\">"
             f"GPU Competitor Pricing overview</a>: same providers, tracking "
             f"<strong>capacity availability</strong> instead of price.</p>")
    freshness_color = "green" if stale == [] else "yellow"
    stale_note = f" — stale/failed: {_esc(', '.join(stale))}" if stale else ""
    h.append(f"<p><em>Data freshness: </em>{_status(freshness_color, f'{live}/{total} sources live')}"
             f"<em>{stale_note}</em></p>")

    # TL;DR per GPU
    h.append("<h2>Market Read — supply per GPU</h2>")
    h.append("<p>Verdict counts only <strong>live-stock signals</strong> (providers exposing "
             "real-time capacity: Lambda, Hyperstack, Verda, Scaleway, RunPod, Vast, "
             "SF Compute, Voltage Park, AWS Capacity Blocks). A provider counts as "
             "'available' when at least one region/SKU has live capacity.</p>")
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>GPU</th><th>Market verdict</th><th>Available at</th>"
             "<th>Limited</th><th>Sold out</th><th>Notable</th></tr>")
    for gpu in CAPACITY_GPUS:
        v = verdicts.get(gpu)
        if not v:
            continue
        c = v["counts"]
        verdict_color = {"broadly available": "green", "mixed": "yellow",
                         "tight": "red", "SOLD OUT across live sources": "red"}.get(v["verdict"], "neutral")
        notable = "; ".join(_describe_change(d) for d in diff
                            if d.gpu_model == gpu and d.change_type == "state_change")[:160]
        h.append(f"<tr><td><strong>{gpu}</strong></td>"
                 f"<td>{_status(verdict_color, v['verdict'])}</td>"
                 f"<td>{c['available']}/{v['n_live']} live sources</td>"
                 f"<td>{c['limited']}</td><td>{c['sold_out']}</td>"
                 f"<td><em>{_esc(notable) or '—'}</em></td></tr>")
    h.append("</tbody></table>")

    # Matrix
    h.append("<h2>Availability Matrix — GPU × Provider</h2>")
    provs = [p for p in PROVIDER_ORDER if any((g, p) in idx for g in CAPACITY_GPUS)]
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>GPU</th>" + "".join(f"<th>{_esc(PROVIDER_LABELS.get(p, p))}</th>" for p in provs) + "</tr>")
    for gpu in CAPACITY_GPUS:
        if not any((gpu, p) in idx for p in provs):
            continue
        row = [f"<td><strong>{gpu}</strong></td>"]
        for p in provs:
            cell = idx.get((gpu, p))
            if not cell:
                row.append("<td>—</td>")
                continue
            state, detail = _cell_summary(cell)
            label = {"available": "Available", "limited": "Limited",
                     "sold_out": "Sold out", "not_offered": "—", "unknown": "?"}[state]
            txt = _status(STATE_COLOR[state], label)
            if detail:
                txt += f"<br/><em>{_esc(detail)}</em>"
            row.append(f"<td>{txt}</td>")
        h.append("<tr>" + "".join(row) + "</tr>")
    h.append("</tbody></table>")
    h.append("<p><em>Cell shows the best on-demand state across the provider's regions; "
             "detail line gives region counts. '—' = provider does not list the GPU; "
             "'?' = tracked but today's signal unreadable.</em></p>")

    # Changes
    h.append("<h2>Changes (since previous build)</h2>")
    if diff:
        h.append("<ul>")
        for c in diff[:40]:
            h.append(f"<li>{_esc(_describe_change(c))}</li>")
        h.append("</ul>")
        if len(diff) > 40:
            h.append(f"<p><em>…and {len(diff) - 40} more (see repo store).</em></p>")
    else:
        h.append("<p><em>No availability changes on tracked providers since the previous build.</em></p>")

    # Per-provider detail
    h.append("<h2>Provider Detail</h2>")
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>Provider</th><th>GPU</th><th>Region</th><th>Type</th>"
             "<th>State</th><th>Signal</th></tr>")
    for prov in provs:
        prov_records = sorted(
            [r for r in records if r.provider == prov and r.state != "not_offered"],
            key=lambda r: (CAPACITY_GPUS.index(r.gpu_model) if r.gpu_model in CAPACITY_GPUS else 99,
                           r.region, r.consumption_type))
        for r in prov_records:
            sig = r.detail or (f"{r.metric_type}={r.metric_value:g}" if r.metric_value is not None else r.metric_type)
            h.append(f"<tr><td>{_esc(PROVIDER_LABELS.get(prov, prov))}</td>"
                     f"<td><strong>{_esc(r.gpu_model)}</strong></td>"
                     f"<td>{_esc(r.region)}</td>"
                     f"<td>{_esc(CT_LABELS.get(r.consumption_type, r.consumption_type))}</td>"
                     f"<td>{_status(STATE_COLOR.get(r.state, 'neutral'), r.state.replace('_', ' '))}</td>"
                     f"<td><em>{_esc(sig)}</em></td></tr>")
    h.append("</tbody></table>")

    # Method
    h.append("<h2>Method &amp; Signal Semantics</h2>")
    h.append("<p>Each provider exposes availability differently — this table is what "
             "'available' actually means per source. Signals are point-in-time daily "
             "probes of public endpoints; they show what a <strong>new customer</strong> could "
             "get, not providers' internal utilization.</p>")
    h.append('<table data-layout="default"><tbody>')
    h.append("<tr><th>Provider</th><th>Signal</th><th>Semantics</th></tr>")
    method_rows = manifest.get("method_rows", [])
    for prov, signal, sem in method_rows:
        h.append(f"<tr><td>{_esc(prov)}</td><td>{_esc(signal)}</td><td>{_esc(sem)}</td></tr>")
    h.append("</tbody></table>")
    h.append("<p><em>Caveats: hyperscaler live capacity (AWS on-demand ICE, GCP/Azure stockouts) "
             "is not externally observable — AWS is tracked via Capacity Blocks lead time, the "
             "closest public proxy. CoreWeave/Crusoe/Together publish no live stock signal; they "
             "appear only when a signal exists. Marketplace depth (Vast) measures listed "
             "supply, not datacenter inventory. Generated by the capacity monitor "
             "(price-monitor repo, capacity/) — data quality issues → Koen Brörmann.</em></p>")

    return "\n".join(h)


def write_artifacts(records: List[AvailabilityRecord],
                    diff: List[CapacityDiffEntry],
                    manifest: dict) -> None:
    slack_msg, slack_thread = render_slack(records, diff, manifest)
    (STORE_DIR / "slack_message.txt").write_text(slack_msg)
    (STORE_DIR / "slack_thread.txt").write_text(slack_thread)
    (STORE_DIR / "confluence_body.html").write_text(render_confluence(records, diff, manifest))
    logger.info("Capacity artifacts written: slack_message.txt, slack_thread.txt, confluence_body.html")
