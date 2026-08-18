"""
Render the daily capacity artifacts (STORM redesign 2026-08-12).

Contract with the readers (Pricing sign-off, PAYG product, Self-service &
fraud, GTM, Capacity/reserve):
  Slack digest  — 15-second read: trigger status, cluster-scale k/n strip,
                  ≤3 material changes with a so-what, market gauges.
  Slack thread  — 2-minute evidence: per-flagship-GPU blocks grouped by
                  signal class, full provenance-tagged change list.
  Confluence    — reference: TL;DR by stakeholder, decision triggers,
                  tightness + price join, GTM battlecard, live matrix,
                  footprint table, gauges, detail, method.

Signal classes are never visually mixed: live states get the state
vocabulary; footprints render as neutral counts; badges as claims;
marketplace/exchange as numbers. Aggregator-sourced reads carry ✱.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from capacity import insights
from capacity.config import (
    CONFLUENCE_BASE_URL, FLAGSHIP_GPUS, FOOTPRINT_ONLY_GPUS, PROVIDER_LABELS,
    SECONDARY_GPUS, SIGNAL_CLASS,
)
from capacity.insights import plural, region_label
from capacity.schema import AvailabilityRecord, CapacityDiffEntry

logger = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"

PENDING_LABELS = {
    "aws_capacity_blocks": "AWS Capacity Blocks (account needs CB service quota via AWS Support)",
    "hyperstack": "Hyperstack (free key pending)",
    "together": "Together AI (free key pending)",
    "verda": "Verda (free key pending)",
}

# Method & semantics per provider — rendered on Confluence with the class and
# today's basis so no reader has to guess what a cell means.
METHOD = {
    "lambda":       ("live",          "instance-types API: per-region launchability; empty = sold out in all regions"),
    "scaleway":     ("live",          "public availability API: available / scarce / shortage per zone per SKU"),
    "runpod":       ("live",          "GraphQL stock labels (1x and 8x cluster) + per-datacenter availability"),
    "voltage_park": ("live",          "public locations API: live rentable GPU counts per fabric"),
    "hyperstack":   ("live",          "stock API: per-region counts + restock forecast (pending free key)"),
    "verda":        ("live",          "instance-availability API per location (pending free key)"),
    "together":     ("live",          "per-region capacity headroom API (pending free key)"),
    "aws":          ("spot",          "spot advisor pools (~weekly) now; Capacity Blocks lead time once IAM lands"),
    "vast":         ("marketplace",   "commodity marketplace depth: GPUs listed + floor price, not DC inventory"),
    "sfcompute":    ("marketplace",   "exchange clearing price (short-term reserve); price level = scarcity"),
    "gmi":          ("self_reported", "pricing-page badges (provider-declared, unverifiable) — never counted"),
    "coreweave":    ("footprint",     "docs AZ matrix: where deployed, not whether in stock"),
    "crusoe":       ("footprint",     "docs zone matrix: where offered (live capacities API needs account)"),
    "gcp":          ("footprint",     "GPU zones docs page: where offered"),
    "azure":        ("footprint",     "retail price API: where priced (can overstate deployment)"),
    "nebius":       ("footprint",     "outside-in: docs region matrix + which SKUs are self-service vs sales-gated"),
    "shadeform":    ("aggregator",    "19-cloud aggregator booleans: fills gaps, marked ✱, never overrides a direct read"),
}

CLASS_LABEL = {
    "live": "live stock", "spot": "spot", "marketplace": "marketplace",
    "self_reported": "self-reported", "footprint": "footprint",
    "aggregator": "aggregator",
}


def _fresh_line(manifest: dict) -> Tuple[str, str]:
    """(short slack line, confluence html line)"""
    f = insights.freshness(manifest)
    n_act, n_live = len(f["activated"]), len(f["live"])
    bits = []
    if not f["failed"] and not f["stale"]:
        short = f"Feeds: all {n_live} live"
        color = "green"
    else:
        if f["stale"]:
            bits.append("cached today: " + ", ".join(PROVIDER_LABELS.get(p, p) for p in f["stale"]))
        if f["failed"]:
            bits.append("down: " + ", ".join(PROVIDER_LABELS.get(p, p) for p in f["failed"]))
        short = f"Feeds: {n_live}/{n_act} live ({'; '.join(bits)})"
        color = "yellow"
    pend = f" · {len(f['pending'])} awaiting access" if f["pending"] else ""
    html = (f'<span data-type="status" data-color="{color}">'
            f'{n_live}/{n_act} feeds live</span>'
            + (f"<em>{pend}</em>" if pend else ""))
    return short + pend, html


def _baseline_label(old_records: List[AvailabilityRecord]) -> str:
    ts = [r.fetched_at for r in old_records if r.fetched_at]
    if not ts:
        return "first run"
    try:
        dt = datetime.fromisoformat(max(ts))
        return "since previous run, " + dt.strftime("%d %b %H:%M UTC")
    except ValueError:
        return "since previous run"


def _mark(read: dict) -> str:
    return "✱" if read["aggregator"] else ""


def _streak(read: dict, gpu: str) -> str:
    d = insights.days_in_state(read["provider"], gpu, read["state"])
    # A streak spanning the whole history just restates the monitor's age —
    # only show streaks that are shorter than the days tracked.
    if not d or d < 2 or d >= insights.history_days():
        return ""
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(d if d < 20 else d % 10, "th")
    return f" ({d}{suffix} day)"


def _event_phrase(t: dict) -> Optional[str]:
    """One compact event phrase, no k/n restatement (the strip owns k/n —
    repeating it per bullet made the 17-Aug digest say '3/7' three times)."""
    prov = PROVIDER_LABELS.get(t["provider"], t["provider"])
    if t["new"] == "sold_out":
        return f"{prov} {t['gpu']} sold out (all regions)"
    if t["new"] == "available" and t["old"] == "sold_out":
        return f"{prov} {t['gpu']} fully restocked"
    if t["new"] == "limited" and t["old"] == "sold_out":
        return f"{prov} {t['gpu']} restocked 1x only"
    if t["new"] == "limited" and t["old"] == "available":
        return f"{prov} {t['gpu']} lost cluster-scale (1x remains)"
    if t["new"] == "available" and t["old"] == "limited":
        return f"{prov} {t['gpu']} back at cluster scale"
    return None


def _digest_movement(records: List[AvailabilityRecord],
                     old_records: List[AvailabilityRecord],
                     hard_triggers: List[dict]) -> List[str]:
    """The day's movement, each fact stated ONCE: events not already in a
    trigger line go on one 'Also moved' line; one 'Read' line carries the
    per-GPU direction + pricing so-what."""
    transitions = insights.provider_transitions(records, old_records, direct_only=True)

    # Drop events the trigger block already headlines — matched on the exact
    # "Provider GPU" pair (a loose gpu-anywhere match once suppressed an
    # unrelated RunPod H100 event because another trigger mentioned H100)
    trigger_txt = " ".join(t["text"] for t in hard_triggers)
    remaining = [t for t in transitions
                 if f"{PROVIDER_LABELS.get(t['provider'], t['provider'])} {t['gpu']}"
                 not in trigger_txt]
    phrases = [p for p in (_event_phrase(t) for t in remaining[:4]) if p]

    lines = []
    if phrases:
        suffix = " _(own API)_" if len(phrases) == 1 else " _(all own APIs)_"
        lines.append("• *Moved:* " + " · ".join(phrases) + suffix)

    # One read line: direction where cluster-share actually moved, grouped so
    # the so-what appears once (numbers live in the strip's ↓/↑ deltas)
    tighter, looser = [], []
    for gpu in FLAGSHIP_GPUS:
        new_t = insights.tightness(records, gpu)
        old_t = insights.tightness(old_records, gpu) if old_records else None
        if not new_t or not old_t:
            continue
        if new_t["k_cluster"] < old_t["k_cluster"]:
            tighter.append(gpu)
        elif new_t["k_cluster"] > old_t["k_cluster"]:
            looser.append(gpu)
    reads = []
    if tighter:
        reads.append(f"{', '.join(tighter)} tightening (supports holding price)")
    if looser:
        reads.append(f"{', '.join(looser)} loosening (watch for price pressure)")
    if reads:
        lines.append("• *So what:* " + " · ".join(reads) + ".")
    return lines


# ── Slack digest ─────────────────────────────────────────────────────────────

def render_slack(records: List[AvailabilityRecord],
                 diff: List[CapacityDiffEntry],
                 manifest: dict,
                 old_records: List[AvailabilityRecord]) -> Tuple[str, str]:
    today = datetime.now(timezone.utc).strftime("%A %d %b %Y")
    triggers = insights.evaluate_triggers(records, old_records, diff)

    lines = [f"*GPU Capacity Daily — {today}*"]
    hard = [t for t in triggers if t.get("level") != "watch"]
    watch = [t for t in triggers if t.get("level") == "watch"]
    movement = _digest_movement(records, old_records, hard)

    if hard:
        for t in hard[:3]:
            lines.append(f"*Heads-up ({t['owner']}):* {t['text']}")
    elif not movement:
        # One honest null line instead of two near-duplicates; scoped to what
        # the checks actually inspect (flagship GPUs, provider level).
        lines.append("Quiet day: no provider-level moves on flagship GPUs (own APIs).")
    for t in watch[:2]:
        lines.append(f"_Unconfirmed ({t['owner']}): {t['text']}_")
    lines.append("")

    # Cluster-scale strip WITH day-over-day movement — "3/7" without "was 4/7"
    # made the 17-Aug digest unreadable. ✱ can only sit in the denominator
    # (aggregator reads never count as cluster stock); legend on Confluence.
    strip = []
    for gpu in FLAGSHIP_GPUS:
        t = insights.tightness(records, gpu)
        if not t:
            continue
        mark = "✱" if t["any_aggregator"] else ""
        old_t = insights.tightness(old_records, gpu) if old_records else None
        delta = ""
        if old_t and old_t["k_cluster"] != t["k_cluster"]:
            delta = f" (was {old_t['k_cluster']}/{old_t['n']})"
        unit = " live sources" if not strip else ""
        strip.append(f"{gpu} at {t['k_cluster']}/{t['n']}{unit}{mark}{delta}" if not strip
                     else f"{gpu} {t['k_cluster']}/{t['n']}{mark}{delta}")
    if strip:
        lines.append("• *Competitor 8x node stock:* " + " · ".join(strip))
        if any("✱" in s for s in strip):
            lines.append("_✱ = includes a third-party read (never proof of 8x stock; "
                         "don't quote to customers)_")

    if movement:
        lines.extend(movement)

    # Gauges — H100 as the market bellwether, only real numbers
    g = insights.market_gauges(records, "H100")
    gauge_bits = []
    if g.get("sfc_clearing"):
        gauge_bits.append(f"SF Compute H100 clearing price ${g['sfc_clearing']:.2f} "
                          f"(short-term reserve market)")
    if g.get("vast_gpus"):
        floor = f", cheapest {g['vast_floor']}" if g.get("vast_floor") else ""
        gauge_bits.append(f"Vast H100 {g['vast_gpus']} GPUs listed{floor} (marketplace)")
    if gauge_bits:
        lines.append("• *Market:* " + " · ".join(gauge_bits))

    fresh_short, _ = _fresh_line(manifest)
    url = _page_url()
    tail = f"_{fresh_short}_"
    if url:
        tail += f" · <{url}|matrix · battlecard · method>"
    lines.append(tail)

    return "\n".join(lines), _render_thread(records, diff, manifest, old_records)


# ── Slack thread ─────────────────────────────────────────────────────────────

def _normalize_partial(provider: str, detail: str) -> str:
    """Plain short reason for a partial read; raw fetcher prose was truncating
    mid-caveat. '1x High' beats '1x stock High, but no 8-GPU (cluster) stock'."""
    import re as _re
    d = detail or ""
    m = _re.search(r"1x[: ]+(?:stock )?(High|Medium|Low)", d, _re.I)
    if m:
        return f"1x only, stock {m.group(1)}"
    m = _re.search(r"(\d+) regions?:", d)
    if m:
        return f"small sizes in {plural(int(m.group(1)), 'region')}, 8x sold out"
    m = _re.search(r"stock in (\d+ regions?), best (\d+)\+?", d)
    if m:
        return f"~{m.group(2)} GPUs, {m.group(1)}"
    if "no 8x" in d:
        return "no 8x nodes"
    return _short(d, 40)


def _gpu_block(records: List[AvailabilityRecord], gpu: str,
               skip_gauges: bool = False, first_aws: List[bool] = None) -> List[str]:
    t = insights.tightness(records, gpu)
    if not t:
        return []

    groups = {"available": [], "limited": [], "sold_out": []}
    for r in t["reads"]:
        streak = _streak(r, gpu).strip(" ()")   # "day 2" or ""
        groups[r["state"]].append((f"{r['label']}{_mark(r)}", streak, r["detail"]))

    if groups["available"]:
        head = " · ".join(e + (f" ({s})" if s else "") for e, s, _d in groups["available"])
    else:
        head = f"none of {t['n']} live sources"
    incl = " (incl. 8x)" if gpu in FLAGSHIP_GPUS else ""
    lines = [f"*{gpu}* in stock{incl}: {head}"]

    state_bits = []
    if groups["limited"]:
        # One paren per provider: "(1x only, stock Low, 3rd day)"
        det = " · ".join(
            f"{e} ({_normalize_partial(e, d)}{', ' + s if s else ''})"
            for e, s, d in groups["limited"])
        state_bits.append(f"Partial: {det}")
    if groups["sold_out"]:
        # State + ✱ already say everything; details restating "0" are noise
        state_bits.append("Sold out: " + " · ".join(
            e + (f" ({s})" if s else "") for e, s, _d in groups["sold_out"]))
    if state_bits:
        lines.append("• " + "; ".join(state_bits))

    # Market depth line (H100 gauges live in the digest — no repeat here)
    g = insights.market_gauges(records, gpu)
    depth_bits = []
    if not skip_gauges:
        if g.get("vast_gpus"):
            floor = f", cheapest {g['vast_floor']}" if g.get("vast_floor") else ""
            depth_bits.append(f"Vast {g['vast_gpus']} GPUs listed{floor}")
        if g.get("sfc_clearing"):
            depth_bits.append(f"SF Compute clears ${g['sfc_clearing']:.2f}")
    if g.get("aws_spot_regions"):
        note = ""
        if first_aws is not None and not first_aws[0]:
            note = " (spot advisor, ~weekly)"
            first_aws[0] = True
        depth_bits.append(f"AWS spot pools in {plural(g['aws_spot_regions'], 'region')}{note}")
    if depth_bits:
        lines.append("• Market: " + " · ".join(depth_bits))

    # Nebius is us, not "the market" — its own labeled line
    ref = insights.nebius_reference(records, gpu)
    if ref["regions"]:
        gate = " (sales-gated)" if ref["sales_gated"] else ""
        lines.append(f"• Nebius: self-service in {', '.join(ref['regions'])}{gate}")

    lines.append("")
    return lines


def _short(detail: str, limit: int = 70) -> str:
    d = (detail or "").split(";")[0].strip()
    return d if len(d) <= limit else d[:limit - 1] + "…"


def _describe_change(c: CapacityDiffEntry,
                     records: List[AvailabilityRecord] = None) -> str:
    prov = PROVIDER_LABELS.get(c.provider, c.provider)
    cls = SIGNAL_CLASS.get(c.provider, "footprint")
    tag = {"live": "live", "spot": "spot", "marketplace": "marketplace",
           "self_reported": "badge", "footprint": "footprint"}[cls]
    # A "live" tag on an aggregator-sourced row overstates trust — mark it.
    if cls == "live" and records is not None:
        for r in records:
            if (r.provider == c.provider and r.gpu_model == c.gpu_model
                    and r.region == c.region and r.instance_type == c.instance_type):
                if r.data_source == "aggregator":
                    tag = "via aggregator ✱"
                break
    where = f" {c.region}" if c.region and c.region != "global" else ""
    human = {"available": "in stock", "limited": "partial", "sold_out": "sold out",
             "not_offered": "not offered", "unknown": "unknown"}
    metric_name = {"offer_depth_gpus": "listed depth", "stock_level": "stock",
                   "regions_with_capacity": "regions with capacity",
                   "clearing_price_usd": "clearing price", "lead_time_days": "lead time"}
    if c.change_type == "state_change":
        suffix = "" if tag == "live" else f" ({tag})"
        return (f"{prov} {c.gpu_model}{where}: {human.get(c.old_state, c.old_state)} → "
                f"{human.get(c.new_state, c.new_state)}{suffix}")
    if c.change_type == "metric_move":
        name = next((v for k, v in metric_name.items() if k in (c.detail or "")), None)
        if name and c.old_value is not None and c.new_value is not None:
            capped = "+, page-capped" if "page cap" in (c.detail or "") else ""
            suffix = "" if tag == "live" else f" ({tag})"
            return (f"{prov} {c.gpu_model}{where}: {name} "
                    f"{c.old_value:g} → {c.new_value:g}{capped}{suffix}")
        return f"{prov} {c.gpu_model}{where}: {c.detail} ({tag})"
    if c.change_type == "added":
        return f"{prov} {c.gpu_model}{where}: now tracked, {human.get(c.new_state, c.new_state)} ({tag})"
    return f"{prov} {c.gpu_model}{where}: {c.detail} ({tag})"


def _render_thread(records, diff, manifest, old_records) -> str:
    today = datetime.now(timezone.utc).strftime("%A %d %b %Y")
    lines = [f"*Detail · {today}*", ""]
    first_aws = [False]

    # A "footprint-only" GPU with an actual live read auto-promotes to a full
    # block (Together was live-selling GB300 while the thread claimed "no live
    # market" — red-team 2026-08-14).
    promoted = [g for g in FOOTPRINT_ONLY_GPUS if insights.tightness(records, g)]

    for gpu in FLAGSHIP_GPUS + promoted:
        lines.extend(_gpu_block(records, gpu, skip_gauges=(gpu == "H100"),
                                first_aws=first_aws))

    # Secondary GPUs, one line each
    sec_bits = []
    for gpu in SECONDARY_GPUS:
        t = insights.tightness(records, gpu)
        if t:
            sec_bits.append(f"*{gpu}* in stock at {t['k_any']}/{t['n']} live sources "
                            f"({t['k_cluster']}/{t['n']} at 8x)")
    if sec_bits:
        lines.append(" · ".join(sec_bits))
        lines.append("")

    # Changes first (decision-relevant), then static footprint/badges.
    # Default provenance is the provider's own API; only exceptions are marked.
    baseline = _baseline_label(old_records)
    changes = [c for c in diff if c.change_type in ("state_change", "metric_move")]
    provider_level = [c for c in changes if c.region == "global"]
    n_region = len(changes) - len(provider_level)

    # One basis per provider: when a direct feed returned data today, that
    # provider's aggregator-sourced diff rows are noise (mixed bases made the
    # 17-Aug list contradict itself)
    direct_today = {r.provider for r in records
                    if r.region == "global" and r.data_source != "aggregator"}
    provider_level = [c for c in provider_level
                      if not (c.provider in direct_today
                              and _change_is_aggregator(c, records, old_records))]

    gpu_order = {g: i for i, g in enumerate(FLAGSHIP_GPUS + SECONDARY_GPUS + FOOTPRINT_ONLY_GPUS)}
    provider_level.sort(key=lambda c: (gpu_order.get(c.gpu_model, 99), c.provider))

    human = {"available": "in stock", "limited": "partial", "sold_out": "sold out"}
    change_lines = []

    # Variant merge within (provider, gpu); then cross-GPU merge of identical
    # transitions per provider ("RunPod H100 · H200 · B200: partial → sold out")
    grouped: Dict[tuple, list] = {}
    for c in provider_level:
        grouped.setdefault((c.provider, c.gpu_model, c.change_type), []).append(c)

    simple_states: Dict[tuple, list] = {}   # (provider, old, new) → [gpu]
    for (prov_key, gpu, ctype), items in grouped.items():
        if ctype == "state_change" and len(items) == 1:
            c = items[0]
            simple_states.setdefault((prov_key, c.old_state, c.new_state), []).append((gpu, c))

    emitted_simple = set()
    for (prov_key, old_s, new_s), gpu_items in simple_states.items():
        if len(gpu_items) >= 2:
            prov = PROVIDER_LABELS.get(prov_key, prov_key)
            gpus = " · ".join(g for g, _c in gpu_items)
            agg = _change_is_aggregator(gpu_items[0][1], records, old_records)
            tag = " (via aggregator ✱)" if agg else ""
            change_lines.append(f"• {prov} {gpus}: {human.get(old_s, old_s)} → "
                                f"{human.get(new_s, new_s)}{tag}")
            emitted_simple.update((prov_key, g) for g, _c in gpu_items)

    for (prov_key, gpu, ctype), items in grouped.items():
        if (prov_key, gpu) in emitted_simple:
            continue
        if ctype == "state_change" and len(items) > 1:
            prov = PROVIDER_LABELS.get(prov_key, prov_key)
            parts = " · ".join(
                f"{_variant_tag(c.instance_type, gpu)} {human.get(c.old_state, c.old_state)}"
                f" → {human.get(c.new_state, c.new_state)}" for c in items)
            agg = _change_is_aggregator(items[0], records, old_records)
            tag = " (via aggregator ✱)" if agg else ""
            change_lines.append(f"• {prov} {gpu}: {parts}{tag}")
        else:
            for c in items:
                change_lines.append(f"• {_describe_change(c, records)}")

    lines.append(f"*Changes ({baseline}; own APIs unless marked):*")
    if change_lines:
        lines.extend(change_lines[:12])
    else:
        lines.append("• none at provider level")
    if n_region:
        lines.append(f"_+{plural(n_region, 'datacenter-level change')} on Confluence_")
    lines.append("")

    # Footprint-only generations (full product names; offered ≠ in stock)
    fp_label = {"GB200": "GB200 NVL72", "GB300": "GB300 NVL72"}
    for gpu in FOOTPRINT_ONLY_GPUS:
        if gpu in promoted:
            continue
        bits = []
        for prov in ("coreweave", "gcp", "azure", "crusoe"):
            rows = [r for r in records if r.provider == prov and r.gpu_model == gpu
                    and r.region == "global"]
            if rows and rows[0].metric_value:
                unit = {"coreweave": "AZ", "gcp": "zone", "azure": "priced region",
                        "crusoe": "zone"}[prov]
                bits.append(f"{PROVIDER_LABELS[prov]} {plural(int(rows[0].metric_value), unit)}")
        if bits:
            lines.append(f"*{fp_label.get(gpu, gpu)}* footprint only "
                         f"(where it's offered, not whether in stock): " + " · ".join(bits))

    # GMI badges once, consolidated — a signal that is "never counted" does
    # not deserve a bullet per GPU
    badges = sorted((r for r in records if r.provider == "gmi" and r.region == "global"),
                    key=lambda r: r.gpu_model)
    if badges:
        bits = [f"{fp_label.get(b.gpu_model, b.gpu_model)} "
                f"{_short(b.detail.replace('badge ', '').replace(' (not yet deliverable)', ''), 60)}"
                for b in badges]
        lines.append(f"_GMI badges (self-reported, never counted): {' · '.join(bits)}_")
    return "\n".join(lines)


def _change_is_aggregator(c: CapacityDiffEntry,
                          records: List[AvailabilityRecord],
                          old_records: List[AvailabilityRecord]) -> bool:
    for pool in (records, old_records):
        for r in pool:
            if (r.provider == c.provider and r.gpu_model == c.gpu_model
                    and r.region == c.region and r.consumption_type == c.consumption_type
                    and r.instance_type == c.instance_type):
                return r.data_source == "aggregator"
    return False


def _variant_tag(instance_type: str, gpu: str) -> str:
    """Short SKU-variant label: 'NVIDIA H100 80GB HBM3' → 'HBM3',
    '... Workstation Edition' → 'Workstation Edition'."""
    import re as _re
    t = (instance_type or "")
    for junk in ("NVIDIA", gpu, "PRO 6000", "RTX", "Blackwell"):
        t = t.replace(junk, " ")
    t = _re.sub(r"\d+\s*GB", " ", t, flags=_re.I)
    t = _re.sub(r"\s+", " ", t).strip(" -_.")
    words = t.split()
    return " ".join(words[-2:]) if words else "variant"


# ── Confluence ───────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _status(color: str, text: str) -> str:
    return f'<span data-type="status" data-color="{color}">{_esc(text)}</span>'


def _read_for(t: dict) -> Tuple[str, str]:
    """(status color, one-line read) from a tightness dict."""
    if t["n"] == 0:
        return "neutral", "no live sources"
    share = t["k_cluster"] / t["n"]
    if t["k_cluster"] == 0:
        return "red", "no cluster capacity at any live source; scarcity supports premium/hold"
    if share <= 1 / 3:
        return "red", "tight at cluster scale"
    if share >= 2 / 3 and t["k_any"] == t["n"]:
        return "green", "broadly available"
    return "yellow", "mixed"


def render_confluence(records: List[AvailabilityRecord],
                      diff: List[CapacityDiffEntry],
                      manifest: dict,
                      old_records: List[AvailabilityRecord]) -> str:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    triggers = insights.evaluate_triggers(records, old_records, diff)
    join = insights.price_join(records)
    gtm = insights.gtm_claims(records, diff)
    hist_days = insights.history_days()
    _, fresh_html = _fresh_line(manifest)

    tight = {g: insights.tightness(records, g)
             for g in FLAGSHIP_GPUS + SECONDARY_GPUS + FOOTPRINT_ONLY_GPUS}
    tight = {g: t for g, t in tight.items() if t}

    h = []
    h.append(f"<p><em>Last updated: {today}</em> — <strong>daily refreshed</strong> "
             f"(point-in-time snapshot ~02:30 UTC). Companion to the "
             f"<a href=\"{CONFLUENCE_BASE_URL}/spaces/PR/pages/1831469419\">GPU Competitor "
             f"Pricing overview</a>: same peers, tracking <strong>who can actually deliver "
             f"capacity</strong>. Slack: #competitor-capacity.</p>")
    h.append(f"<p><em>Data freshness: </em>{fresh_html}</p>")

    # 1 — TL;DR by stakeholder
    h.append("<h2>TL;DR by Stakeholder</h2>")
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>For</th><th>Today's read</th><th>Detail in</th></tr>")

    strip = "; ".join(f"{g} {t['k_cluster']}/{t['n']}" for g, t in tight.items()
                      if g in FLAGSHIP_GPUS)
    trig_txt = "; ".join(t["text"] for t in triggers) if triggers else "no trigger today"
    jn = join.get("H100") or {}
    book = jn.get("cheapest_bookable")
    listed = jn.get("cheapest_listed")
    price_bit = ""
    if book:
        price_bit = (f" Cheapest bookable H100 peer: {book['provider']} ${book['price']:.2f}"
                     + (" ✱" if book.get("aggregator") else ""))
        if listed and listed.get("sold_out"):
            price_bit += (f" ({listed['provider']} lists ${listed['price']:.2f} but is sold out)")
    h.append(f"<tr><td><strong>Pricing sign-off</strong></td>"
             f"<td>Cluster-scale stock at live sources: {_esc(strip)}. {_esc(trig_txt)}."
             f"{_esc(price_bit)}</td><td>Tightness per GPU + Triggers</td></tr>")

    spill = [g for g, t in tight.items() if g in FLAGSHIP_GPUS and t["k_cluster"] <= max(1, t["n"] // 3)]
    h.append(f"<tr><td><strong>PAYG product</strong></td>"
             f"<td>Peers short on cluster capacity: {_esc(', '.join(spill) or 'none')} — "
             f"spillover demand candidates. Peers monetize scarcity via short-term guaranteed "
             f"products (AWS Capacity Blocks, Lambda 1-Click, SF Compute exchange); Nebius has "
             f"no equivalent product (see pricing page product-gap table).</td>"
             f"<td>Tightness per GPU</td></tr>")

    out_counts = {g: sum(1 for r in t["reads"] if r["state"] == "sold_out")
                  for g, t in tight.items() if g in FLAGSHIP_GPUS}
    canaries = [t["text"] for t in triggers if t["id"] == "T3"]
    h.append(f"<tr><td><strong>Self-service &amp; fraud</strong></td>"
             f"<td>Peers sold out (any size): "
             f"{_esc(', '.join(f'{g}: {c}' for g, c in out_counts.items()))}. "
             f"Displaced demand (incl. abusive) lands on whoever still sells self-service."
             f"{_esc(' ' + '; '.join(canaries) if canaries else '')}</td>"
             f"<td>Live matrix</td></tr>")

    ammo_safe = [a for a in gtm["ammo"] if a["grade"].startswith("safe")][:3]
    ammo_txt = "; ".join(f"{a['provider']} {a['gpu']}" for a in ammo_safe) or "none today"
    exp_txt = ("; expired: " + "; ".join(gtm["expired"])) if gtm["expired"] else ""
    h.append(f"<tr><td><strong>GTM</strong></td>"
             f"<td>Safe-to-quote sellouts: {_esc(ammo_txt)}{_esc(exp_txt)}. Grades in the "
             f"battlecard section — never quote badges or aggregator reads.</td>"
             f"<td>Battlecard</td></tr>")

    sfc = insights.market_gauges(records, "H100").get("sfc_clearing")
    neb_od = (join.get("H100") or {}).get("nebius_od")
    cap_bit = (f"Short-term H100 clears ${sfc:.2f} on SF Compute vs Nebius OD "
               f"${neb_od:.2f}." if sfc and neb_od else "No exchange print today.")
    scarce_8x = [g for g, t in tight.items() if g in FLAGSHIP_GPUS and t["k_cluster"] == 0]
    h.append(f"<tr><td><strong>Capacity / reserve</strong></td>"
             f"<td>{_esc(cap_bit)} GPUs with no cluster stock at any live peer: "
             f"{_esc(', '.join(scarce_8x) or 'none')} — displaced enterprise demand; "
             f"hold capacity firm for reserve asks there.</td>"
             f"<td>Market gauges</td></tr>")
    h.append("</tbody></table>")

    # 2 — Decision triggers
    h.append("<h2>Decision Triggers</h2>")
    h.append("<p><em>Thresholds are proposals until agreed in the channel. A trigger firing "
             "means: look, not act.</em></p>")
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>Trigger</th><th>Fires when</th><th>Today</th><th>Owner</th></tr>")
    trigger_defs = [
        ("T1 fleet-wide flip", "a direct live source sells out or restocks a flagship GPU fleet-wide", "Pricing"),
        ("T2 cluster-share crossing", "live sources with 8x stock cross 1/3 or 2/3 of sources", "Pricing"),
        ("T3 Nebius canary", "a GPU we list self-service is not buyable per the outside-in live read", "Self-service"),
    ]
    fired_ids = {t["id"] for t in triggers}
    for name, when, owner in trigger_defs:
        tid = name.split()[0]
        if tid in fired_ids:
            texts = "; ".join(t["text"] for t in triggers if t["id"] == tid)
            cell = _status("red", "FIRED") + f" {_esc(texts)}"
        else:
            cell = _status("green", "quiet")
        h.append(f"<tr><td>{_esc(name)}</td><td>{_esc(when)}</td><td>{cell}</td>"
                 f"<td>{_esc(owner)}</td></tr>")
    h.append("</tbody></table>")

    # 3 — Tightness per GPU + price join
    h.append("<h2>Tightness per GPU (live sources) + cheapest bookable peer</h2>")
    trend_note = (f"trend building (day {hist_days}/7)" if hist_days < 7
                  else "7-day trend active")
    h.append(f"<p><em>k/n = live sources with stock at that scale; ✱ = includes aggregator "
             f"read. Price join: a listed price at a sold-out provider is not a competing "
             f"price. Trends: {trend_note}.</em></p>")
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>GPU</th><th>Cluster-scale (8x)</th><th>Any size</th>"
             "<th>Read</th><th>Cheapest bookable peer (OD)</th>"
             "<th>Cheapest listed (if different)</th><th>Nebius OD</th>"
             "<th>Nebius self-service regions</th></tr>")
    for gpu in FLAGSHIP_GPUS + SECONDARY_GPUS + FOOTPRINT_ONLY_GPUS:
        t = tight.get(gpu)
        if not t:
            continue
        color, read = _read_for(t)
        mark = "✱" if t["any_aggregator"] else ""
        jn = join.get(gpu) or {}
        book = jn.get("cheapest_bookable")
        listed = jn.get("cheapest_listed")
        book_txt = (f"{book['provider']} ${book['price']:.2f}"
                    + (" ✱" if book and book.get("aggregator") else "")) if book else "—"
        listed_txt = "—"
        if listed and (not book or listed["price"] < book["price"]):
            flag = " (sold out)" if listed.get("sold_out") else ""
            listed_txt = f"{listed['provider']} ${listed['price']:.2f}{flag}"
        neb = jn.get("nebius_od")
        ref = insights.nebius_reference(records, gpu)
        gate = " (sales-gated)" if ref["sales_gated"] else ""
        h.append(f"<tr><td><strong>{gpu}</strong></td>"
                 f"<td>{t['k_cluster']}/{t['n']}{mark}</td>"
                 f"<td>{t['k_any']}/{t['n']}{mark}</td>"
                 f"<td>{_status(color, read)}</td>"
                 f"<td>{_esc(book_txt)}</td><td>{_esc(listed_txt)}</td>"
                 f"<td>{'$' + format(neb, '.2f') if neb else '—'}</td>"
                 f"<td>{_esc(', '.join(ref['regions']) + gate if ref['regions'] else '—')}</td></tr>")
    h.append("</tbody></table>")

    # 4 — GTM battlecard
    h.append("<h2>GTM Battlecard — who cannot deliver today</h2>")
    h.append("<p><em>Grades: <strong>safe</strong> = provider's own API, quotable in a "
             "customer call as 'as of today'; <strong>verify first</strong> = aggregator "
             "read, check before using. Badges and footprints never appear here.</em></p>")
    if gtm["ammo"]:
        h.append('<table data-layout="full-width"><tbody>')
        h.append("<tr><th>Provider</th><th>GPU</th><th>Claim</th><th>Grade</th></tr>")
        for a in gtm["ammo"]:
            grade_color = "green" if a["grade"].startswith("safe") else "yellow"
            h.append(f"<tr><td>{_esc(a['provider'])}</td><td><strong>{_esc(a['gpu'])}</strong></td>"
                     f"<td>{_esc(_short(a['detail'], 110))}</td>"
                     f"<td>{_status(grade_color, a['grade'])}</td></tr>")
        h.append("</tbody></table>")
    else:
        h.append("<p><em>No quotable sellouts today.</em></p>")
    if gtm["expired"]:
        h.append("<p><strong>Expired talk tracks (restocked):</strong> "
                 + _esc("; ".join(gtm["expired"])) + "</p>")

    # 5 — Live matrix
    h.append("<h2>Live-Stock Matrix</h2>")
    h.append("<p><em>Only providers with a live or self-reported signal. "
             "✱ = via aggregator today (direct feed pending or down). GMI is "
             "provider-declared and never counted in verdicts.</em></p>")
    live_provs = [p for p in ("lambda", "scaleway", "runpod", "voltage_park",
                              "verda", "hyperstack", "together", "gmi")
                  if any(r.provider == p for r in records)]
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>GPU</th>" + "".join(f"<th>{_esc(PROVIDER_LABELS[p])}"
             + (" (badge)" if p == "gmi" else "") + "</th>" for p in live_provs) + "</tr>")
    state_disp = {"available": ("green", "In stock"), "limited": ("yellow", "Partial"),
                  "sold_out": ("red", "Sold out"), "unknown": ("neutral", "?"),
                  "not_offered": ("neutral", "—")}
    for gpu in FLAGSHIP_GPUS + SECONDARY_GPUS:
        row = [f"<td><strong>{gpu}</strong></td>"]
        any_cell = False
        for p in live_provs:
            rows = [r for r in records if r.provider == p and r.gpu_model == gpu
                    and r.region == "global" and r.consumption_type == "on_demand"]
            if not rows:
                row.append("<td>—</td>")
                continue
            direct = [r for r in rows if r.data_source != "aggregator"]
            r0 = direct[0] if direct else rows[0]
            color, label = state_disp.get(r0.state, ("neutral", r0.state))
            mark = "✱" if r0.data_source == "aggregator" else ""
            cell = _status(color, label) + mark
            if r0.state != "not_offered":
                cell += f"<br/><em>{_esc(_short(r0.detail, 60))}</em>"
                any_cell = True
            row.append(f"<td>{cell}</td>")
        if any_cell:
            h.append("<tr>" + "".join(row) + "</tr>")
    h.append("</tbody></table>")

    # 6 — Footprint table (neutral)
    h.append("<h2>Offering Footprint — where it is sold (NOT whether in stock)</h2>")
    fp_provs = ["coreweave", "crusoe", "gcp", "azure", "nebius"]
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>GPU</th>" + "".join(f"<th>{_esc(PROVIDER_LABELS[p])}</th>"
                                          for p in fp_provs) + "</tr>")
    unit = {"coreweave": "AZ", "crusoe": "zone", "gcp": "zone",
            "azure": "priced region", "nebius": "region"}
    for gpu in FLAGSHIP_GPUS + SECONDARY_GPUS + FOOTPRINT_ONLY_GPUS:
        row, any_cell = [f"<td><strong>{gpu}</strong></td>"], False
        for p in fp_provs:
            rows = [r for r in records if r.provider == p and r.gpu_model == gpu
                    and r.region == "global" and r.data_source != "aggregator"]
            if not rows or rows[0].state == "not_offered":
                row.append("<td>—</td>")
                continue
            n = rows[0].metric_value or 0
            extra = ""
            if p == "nebius" and "sales-gated" in rows[0].detail:
                extra = " (sales-gated)"
            row.append(f"<td>{plural(int(n), unit[p])}{_esc(extra)}</td>")
            any_cell = True
        if any_cell:
            h.append("<tr>" + "".join(row) + "</tr>")
    h.append("</tbody></table>")

    # 7 — Market gauges
    h.append("<h2>Market Gauges</h2>")
    h.append('<table data-layout="default"><tbody>')
    h.append("<tr><th>GPU</th><th>SF Compute clearing (short-term reserve)</th>"
             "<th>Vast depth (commodity)</th><th>AWS spot pools</th></tr>")
    for gpu in FLAGSHIP_GPUS + SECONDARY_GPUS:
        g = insights.market_gauges(records, gpu)
        if not any(g.get(k) for k in ("sfc_clearing", "vast_gpus", "aws_spot_regions")):
            continue
        sfc = f"${g['sfc_clearing']:.2f}" if g.get("sfc_clearing") else "no trades"
        vast = (f"{g['vast_gpus']} GPUs" + (f", floor {g['vast_floor']}" if g.get("vast_floor") else "")
                ) if g.get("vast_gpus") is not None else "—"
        spot = plural(g["aws_spot_regions"], "region") if g.get("aws_spot_regions") else "—"
        h.append(f"<tr><td><strong>{gpu}</strong></td><td>{_esc(sfc)}</td>"
                 f"<td>{_esc(vast)}</td><td>{_esc(spot)}</td></tr>")
    h.append("</tbody></table>")

    # 8 — Changes
    baseline = _baseline_label(old_records)
    h.append(f"<h2>Changes ({_esc(baseline)})</h2>")
    material = [c for c in diff if c.change_type == "state_change" and c.region == "global"
                and SIGNAL_CLASS.get(c.provider) in ("live", "marketplace")]
    other = [c for c in diff if c not in material and c.change_type in ("state_change", "metric_move")]
    if material:
        h.append("<p><strong>Material (live/marketplace, provider-level):</strong></p><ul>")
        for c in material[:20]:
            h.append(f"<li>{_esc(_describe_change(c, records))}</li>")
        h.append("</ul>")
    if other:
        h.append(f"<p><strong>Other ({len(other)}):</strong></p><ul>")
        for c in other[:30]:
            h.append(f"<li>{_esc(_describe_change(c, records))}</li>")
        if len(other) > 30:
            h.append(f"<li><em>+{len(other) - 30} more in the repo store</em></li>")
        h.append("</ul>")
    if not material and not other:
        h.append("<p><em>No changes since the previous build.</em></p>")

    # 9 — Provider detail (expand macro, deduped, with SKU column)
    h.append("<h2>Provider Detail</h2>")
    h.append('<ac:structured-macro ac:name="expand">'
             '<ac:parameter ac:name="title">Full per-region, per-SKU detail (long)</ac:parameter>'
             '<ac:rich-text-body>')
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>Provider</th><th>GPU</th><th>Region</th><th>SKU</th><th>Type</th>"
             "<th>State</th><th>Signal</th><th>Class</th></tr>")
    seen = set()
    order = {p: i for i, p in enumerate(PROVIDER_LABELS)}
    for r in sorted(records, key=lambda r: (order.get(r.provider, 99), r.gpu_model, r.region)):
        if r.state == "not_offered":
            continue
        key = (r.provider, r.gpu_model, r.region, r.consumption_type, r.instance_type, r.state)
        if key in seen:
            continue
        seen.add(key)
        cls = "aggregator" if r.data_source == "aggregator" else insights.signal_class(r)
        color, label = state_disp.get(r.state, ("neutral", r.state))
        h.append(f"<tr><td>{_esc(PROVIDER_LABELS.get(r.provider, r.provider))}</td>"
                 f"<td><strong>{_esc(r.gpu_model)}</strong></td><td>{_esc(r.region)}</td>"
                 f"<td>{_esc(r.instance_type or '—')}</td>"
                 f"<td>{_esc(r.consumption_type)}</td>"
                 f"<td>{_status(color, label)}</td>"
                 f"<td><em>{_esc(_short(r.detail, 90))}</em></td>"
                 f"<td>{_esc(CLASS_LABEL.get(cls, cls))}</td></tr>")
    h.append("</tbody></table></ac:rich-text-body></ac:structured-macro>")

    # 10 — Method
    h.append("<h2>Method &amp; Signal Classes</h2>")
    f = insights.freshness(manifest)
    basis = {}
    for p, s in manifest.get("provider_status", {}).items():
        prov_key = {"gcp_zones": "gcp", "azure_regions": "azure",
                    "aws_spot_advisor": "aws", "aws_capacity_blocks": "aws"}.get(p, p)
        basis.setdefault(prov_key, s.get("status"))
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>Provider</th><th>Class</th><th>Signal &amp; semantics</th>"
             "<th>Today's basis</th></tr>")
    for prov, (cls, sem) in METHOD.items():
        b = basis.get(prov, "—")
        if prov in ("hyperstack", "verda", "together") and b == "failed":
            b = "pending key (via Shadeform ✱)"
        elif b == "failed" and prov in {p for p in PENDING_ACTIVATION}:
            b = "pending activation"
        h.append(f"<tr><td>{_esc(PROVIDER_LABELS.get(prov, prov.title()))}</td>"
                 f"<td>{_esc(CLASS_LABEL.get(cls, cls))}</td><td>{_esc(sem)}</td>"
                 f"<td>{_esc(b)}</td></tr>")
    h.append("</tbody></table>")

    h.append("<h3>What this monitor cannot see</h3>")
    h.append("<ul>"
             "<li>Provider <strong>utilization</strong>: 'sold out' means the public "
             "self-service shelf at list price is empty; capacity may be allocated to "
             "reserved/private deals.</li>"
             "<li>Hyperscaler on-demand stockouts (AWS/GCP/Azure ICE) are not externally "
             "observable; AWS is proxied via spot pools until the Capacity Blocks IAM lands.</li>"
             "<li>Marketplace depth (Vast) measures listed supply, not datacenter inventory; "
             "falling depth = demand absorbing supply OR hosts delisting.</li>"
             "<li>Self-reported badges (GMI) are marketing statements and can rot.</li>"
             "<li>Levels are weak evidence; <strong>transitions and trends</strong> are the "
             "signal. Trend layer activates at 7 days of history (day "
             f"{insights.history_days()}/7).</li>"
             "</ul>")
    h.append("<p><em>Generated by the capacity monitor (price-monitor repo, capacity/). "
             "Data issues → Koen Brörmann.</em></p>")

    return "\n".join(h)


def _page_url() -> str:
    import json as _json
    meta = STORE_DIR / "confluence_page.json"
    if meta.exists():
        try:
            return _json.loads(meta.read_text()).get("url") or ""
        except ValueError:
            pass
    return ""


def write_artifacts(records: List[AvailabilityRecord],
                    diff: List[CapacityDiffEntry],
                    manifest: dict,
                    old_records: List[AvailabilityRecord] = None) -> None:
    old_records = old_records or []
    slack_msg, slack_thread = render_slack(records, diff, manifest, old_records)
    (STORE_DIR / "slack_message.txt").write_text(slack_msg)
    (STORE_DIR / "slack_thread.txt").write_text(slack_thread)
    (STORE_DIR / "confluence_body.html").write_text(
        render_confluence(records, diff, manifest, old_records))
    logger.info("Capacity artifacts written: slack_message.txt, slack_thread.txt, "
                "confluence_body.html")
