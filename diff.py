"""
Compute price changes between two snapshots and format outputs.
"""
import statistics
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

from schema import PriceRecord, DiffEntry
from config import provider_tier, ALERT_THRESHOLD_PCT, PROVIDER_TIERS

CHANGE_THRESHOLD = 0.001   # 0.1% — ignore floating-point noise in diff detection

GPU_ORDER = ["H100", "H200", "B200", "B300", "GB200", "GB300", "L40S"]
CT_ORDER  = ["on_demand", "spot", "preemptible", "reserved_1yr", "reserved_3yr",
             "committed_1yr", "committed_3yr"]
CT_LABELS = {
    "on_demand":     "On-demand / PAYG",
    "spot":          "Spot / Preemptible (interruptible)",
    "preemptible":   "Spot / Preemptible (interruptible)",
    "reserved_1yr":  "Reserved 1 yr (AWS/Azure: partial-upfront; GCP: Committed Use Discount)",
    "reserved_3yr":  "Reserved 3 yr (AWS/Azure: partial-upfront; GCP: Committed Use Discount)",
    "committed_1yr": "Committed 1 yr (GCP Committed Use Discount — no upfront, usage commitment)",
    "committed_3yr": "Committed 3 yr (GCP Committed Use Discount — no upfront, usage commitment)",
}

# Canonical interruptible types — treated as the same tier for cross-provider comparison
INTERRUPTIBLE_CTS = {"spot", "preemptible"}
# Canonical reserved/committed types by bucket
# AWS: reserved_Xyr (standard, partial-upfront); Azure: reserved_Xyr; GCP: committed_Xyr (CUD)
# Nebius: committed_Xyr (100% upfront, see config.NEBIUS_COMMITTED_PRICES)
RESERVED_1YR_CTS  = {"reserved_1yr", "committed_1yr"}
RESERVED_2YR_CTS  = {"committed_2yr"}
RESERVED_3YR_CTS  = {"reserved_3yr", "committed_3yr"}
# Providers shown as named columns in the detailed Confluence table
DIRECT_PROVIDERS = ["nebius", "aws", "gcp", "azure", "coreweave", "lambda", "crusoe"]

# Canonical display names for provider codes — used throughout Confluence tables
_PROVIDER_DISPLAY: Dict[str, str] = {
    "aws":            "AWS",
    "gcp":            "GCP",
    "azure":          "Azure",
    "nebius":         "Nebius",
    "coreweave":      "CoreWeave",
    "lambda":         "Lambda",
    "crusoe":         "Crusoe",
    "cp_oracle":      "Oracle",
    "cp_hyperstack":  "Hyperstack",
    "cp_voltage":     "Voltage Park",
    "cp_gmi-cloud":   "GMI Cloud",
    "cp_scaleway":    "Scaleway",
    "cp_gcore":       "Gcore",
    "cp_genesis":     "Genesis",
    "cp_civo":        "Civo",
    "cp_paperspace":  "Paperspace",
    "cp_vultr":       "Vultr",
}

def _provider_display(p: str) -> str:
    """Canonical display name for a provider code."""
    if p in _PROVIDER_DISPLAY:
        return _PROVIDER_DISPLAY[p]
    name = p.replace("cp_", "").replace("-", " ")
    _KEEP_UPPER = {"aws", "gcp", "gpu", "gmi", "ai"}
    return " ".join(w.upper() if w.lower() in _KEEP_UPPER else w.title() for w in name.split())


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

def record_key(r: PriceRecord) -> tuple:
    return (r.provider, r.gpu_model, r.instance_type, r.region, r.consumption_type)


def compute_diff(old: List[PriceRecord], new: List[PriceRecord]) -> List[DiffEntry]:
    old_map: Dict[tuple, PriceRecord] = {record_key(r): r for r in old}
    new_map: Dict[tuple, PriceRecord] = {record_key(r): r for r in new}

    diffs: List[DiffEntry] = []

    for key, new_rec in new_map.items():
        if key in old_map:
            old_rec = old_map[key]
            old_p, new_p = old_rec.price_per_gpu_hour_usd, new_rec.price_per_gpu_hour_usd
            if old_p > 0 and abs(new_p - old_p) / old_p > CHANGE_THRESHOLD:
                diffs.append(DiffEntry(
                    provider=new_rec.provider,
                    gpu_model=new_rec.gpu_model,
                    region=new_rec.region,
                    consumption_type=new_rec.consumption_type,
                    instance_type=new_rec.instance_type,
                    change_type="price_change",
                    old_price=old_p,
                    new_price=new_p,
                    delta_pct=(new_p - old_p) / old_p * 100,
                ))
        else:
            diffs.append(DiffEntry(
                provider=new_rec.provider,
                gpu_model=new_rec.gpu_model,
                region=new_rec.region,
                consumption_type=new_rec.consumption_type,
                instance_type=new_rec.instance_type,
                change_type="added",
                new_price=new_rec.price_per_gpu_hour_usd,
            ))

    for key, old_rec in old_map.items():
        if key not in new_map:
            diffs.append(DiffEntry(
                provider=old_rec.provider,
                gpu_model=old_rec.gpu_model,
                region=old_rec.region,
                consumption_type=old_rec.consumption_type,
                instance_type=old_rec.instance_type,
                change_type="removed",
                old_price=old_rec.price_per_gpu_hour_usd,
            ))

    def sort_key(d: DiffEntry):
        order = {"price_change": 0, "added": 1, "removed": 2}
        return (order.get(d.change_type, 9), -abs(d.delta_pct or 0))

    diffs.sort(key=sort_key)
    return diffs


# ---------------------------------------------------------------------------
# Competitive position analysis
# ---------------------------------------------------------------------------

def _best_price(records: List[PriceRecord], gpu: str, ct: str,
                tiers: Optional[List[str]] = None) -> Optional[PriceRecord]:
    """Return the cheapest record for a given gpu/ct combination, optionally filtered by tier."""
    candidates = [
        r for r in records
        if r.gpu_model == gpu and r.consumption_type == ct
        and (tiers is None or provider_tier(r.provider) in tiers)
    ]
    return min(candidates, key=lambda r: r.price_per_gpu_hour_usd) if candidates else None


def _position_for_tier(records, gpu, cts, label):
    """
    Compute Nebius position vs raw_gpu_cloud peers for a set of consumption types.
    `cts` is a set of consumption_type strings treated as the same tier.
    """
    nebius_candidates = [r for r in records
                         if r.gpu_model == gpu and r.consumption_type in cts
                         and r.provider == "nebius"]
    nebius_rec = min(nebius_candidates, key=lambda r: r.price_per_gpu_hour_usd) \
        if nebius_candidates else None

    peers = [r for r in records
             if r.gpu_model == gpu and r.consumption_type in cts
             and provider_tier(r.provider) in ("raw_gpu_cloud", "enterprise_gpu_cloud")
             and r.provider in PROVIDER_TIERS.get("enterprise_gpu_cloud", [])
             and r.provider != "nebius"]

    if not peers and nebius_rec is None:
        return None

    # Deduplicate peers to cheapest per provider (avoid multi-node-size inflation)
    best_per_prov: Dict[str, PriceRecord] = {}
    for r in peers:
        if r.provider not in best_per_prov or \
                r.price_per_gpu_hour_usd < best_per_prov[r.provider].price_per_gpu_hour_usd:
            best_per_prov[r.provider] = r
    peers = list(best_per_prov.values())

    cheapest_peer = min(peers, key=lambda r: r.price_per_gpu_hour_usd) if peers else None
    peer_prices = [r.price_per_gpu_hour_usd for r in peers]
    median_peer = statistics.median(peer_prices) if peer_prices else None
    cheaper_count = sum(1 for p in peer_prices
                        if nebius_rec and p < nebius_rec.price_per_gpu_hour_usd)

    vs_cheapest_pct: Optional[float] = None
    if nebius_rec and cheapest_peer:
        vs_cheapest_pct = (nebius_rec.price_per_gpu_hour_usd
                           - cheapest_peer.price_per_gpu_hour_usd) \
                          / cheapest_peer.price_per_gpu_hour_usd * 100

    # Top 3 cheapest peers with names — shown in Slack message instead of anonymous range
    peers_sorted = sorted(peers, key=lambda r: r.price_per_gpu_hour_usd)
    cheapest_peers_detail = [
        (r.provider, r.price_per_gpu_hour_usd) for r in peers_sorted[:3]
    ]

    return {
        "gpu": gpu,
        "tier_label": label,
        "nebius_price": nebius_rec.price_per_gpu_hour_usd if nebius_rec else None,
        "cheapest_peer": cheapest_peer.price_per_gpu_hour_usd if cheapest_peer else None,
        "cheapest_peer_name": cheapest_peer.provider if cheapest_peer else None,
        "cheapest_peers_detail": cheapest_peers_detail,
        "median_peer": median_peer,
        "vs_cheapest_pct": vs_cheapest_pct,
        "peers_cheaper": cheaper_count,
        "total_peers": len(peers),
    }


def compute_position(records: List[PriceRecord]) -> List[dict]:
    """
    For each GPU, compute Nebius's competitive position vs raw_gpu_cloud peers.
    Returns one row per (GPU, pricing tier) combination.
    Tiers: on_demand and interruptible (spot/preemptible grouped).
    """
    rows = []
    for gpu in GPU_ORDER:
        od = _position_for_tier(records, gpu, {"on_demand"}, "on_demand")
        if od:
            rows.append(od)
        intr = _position_for_tier(records, gpu, INTERRUPTIBLE_CTS, "interruptible")
        if intr:
            rows.append(intr)
    return rows


# ---------------------------------------------------------------------------
# Committed pricing callout helper
# ---------------------------------------------------------------------------

def _format_committed_callout(records: List[PriceRecord]) -> str:
    """
    Build the committed-tier summary line for the Slack message.
    Shows Nebius committed vs AWS committed for H100 — the key strategic comparison.
    """
    def _best(provider, gpu, cts):
        prices = [
            r.price_per_gpu_hour_usd for r in records
            if r.provider == provider
            and r.gpu_model == gpu
            and r.consumption_type in cts
        ]
        return min(prices) if prices else None

    neb_1yr = _best("nebius", "H100", RESERVED_1YR_CTS)
    neb_2yr = _best("nebius", "H100", RESERVED_2YR_CTS)
    aws_1yr = _best("aws",    "H100", RESERVED_1YR_CTS)
    aws_3yr = _best("aws",    "H100", RESERVED_3YR_CTS)

    parts = []

    if neb_1yr and aws_1yr:
        diff_pct = (neb_1yr - aws_1yr) / aws_1yr * 100
        sign = "+" if diff_pct > 0 else ""
        parts.append(
            f"H100 12-month: Nebius ${neb_1yr:.2f} vs AWS ${aws_1yr:.2f} "
            f"({sign}{diff_pct:.0f}%)"
        )
    elif neb_1yr:
        parts.append(f"H100 12-month: Nebius ${neb_1yr:.2f} (AWS: no data)")

    if neb_2yr:
        parts.append(f"H100 24-month: Nebius ${neb_2yr:.2f}/GPU-hr")

    if aws_3yr:
        neb_od = _best("nebius", "H100", {"on_demand"})
        if neb_od:
            gap_mult = neb_od / aws_3yr
            parts.append(
                f"AWS H100 3yr: ${aws_3yr:.2f} vs Nebius on-demand ${neb_od:.2f} "
                f"({gap_mult:.1f}× difference)"
            )
        else:
            parts.append(f"AWS H100 3yr: ${aws_3yr:.2f}/GPU-hr")

    if not parts:
        return ""

    header = "*Committed pricing (H100 benchmark):*"
    return header + "\n" + "\n".join(f"• {p}" for p in parts)


# ---------------------------------------------------------------------------
# Slack message — executive brief
# ---------------------------------------------------------------------------

def format_slack_message(diffs: List[DiffEntry], run_date: str,
                         confluence_url: str, records: List[PriceRecord] = None) -> str:
    """
    Executive-grade Slack digest framed for CFO / Pricing PM audience.
    Neutral framing — price differences shown as plain +/-% without sentiment.

    Structure:
    1. Nebius position vs enterprise GPU cloud peers (on-demand)
    2. Committed pricing benchmark vs AWS
    3. Significant price moves (>threshold)
    4. Link to full Confluence table
    """
    lines = [f"*GPU Competitor Pricing — {run_date}*"]

    def _pname(p: str) -> str:
        _KEEP_UPPER = {"aws", "gcp", "azure", "gpu", "gmi"}
        name = p.replace("cp_", "").replace("-", " ")
        return " ".join(w.upper() if w.lower() in _KEEP_UPPER else w.title()
                        for w in name.split())

    if records:
        position = compute_position(records)
        od_rows = [r for r in position if r["tier_label"] == "on_demand"
                   and r["nebius_price"] is not None]  # skip GPUs with no Nebius price

        # ── 1a. vs GPU cloud peers ────────────────────────────────────────────
        if od_rows:
            lines.append("\n*vs GPU cloud peers (on-demand):*")
            for row in od_rows:
                neb   = row["nebius_price"]
                med   = row["median_peer"]
                total = row["total_peers"]
                gpu   = row["gpu"]

                peer_details = row.get("cheapest_peers_detail", [])
                floor_prov, floor_px = peer_details[0] if peer_details else (None, None)

                if total == 0:
                    lines.append(f"`{gpu:<5}` ${neb:.2f}  no peer data")
                elif total == 1:
                    # Median of 1 is meaningless — just show the single peer
                    floor_str = f"{_pname(floor_prov)} ${floor_px:.2f}" if floor_prov else "—"
                    lines.append(f"`{gpu:<5}` Nebius ${neb:.2f}  |  1 peer: {floor_str}")
                else:
                    cheaper = row["peers_cheaper"]
                    vs_med_pct = (neb - med) / med * 100
                    sign = "+" if vs_med_pct >= 0 else ""
                    floor_str = f"{_pname(floor_prov)} ${floor_px:.2f}" if floor_prov else "—"
                    lines.append(
                        f"`{gpu:<5}` Nebius ${neb:.2f}  {sign}{vs_med_pct:.0f}% vs median  "
                        f"|  {cheaper}/{total} peers cheaper  |  floor: {floor_str}"
                    )

        # ── 1b. vs hyperscaler rack rate ─────────────────────────────────────
        hyp_rows = []
        for gpu in GPU_ORDER:
            neb_rec = next((r for r in records if r.provider == "nebius"
                            and r.gpu_model == gpu and r.consumption_type == "on_demand"), None)
            hyp_best = _best_price(records, gpu, "on_demand", tiers=["hyperscaler"])
            if neb_rec and hyp_best:
                neb_px  = neb_rec.price_per_gpu_hour_usd
                hyp_px  = hyp_best.price_per_gpu_hour_usd
                cheaper_pct = (hyp_px - neb_px) / hyp_px * 100  # positive = Nebius cheaper
                hyp_rows.append((gpu, neb_px, hyp_best.provider, hyp_px, cheaper_pct))

        if hyp_rows:
            lines.append("\n*vs cheapest hyperscaler on-demand rack rate:*")
            for gpu, neb_px, hyp_prov, hyp_px, cheaper_pct in hyp_rows:
                flag = "  ⚠️ narrow gap" if cheaper_pct < 5 else ""
                lines.append(
                    f"`{gpu:<5}` Nebius ${neb_px:.2f}  vs  {_pname(hyp_prov)} ${hyp_px:.2f}"
                    f"  →  Nebius {cheaper_pct:.0f}% cheaper{flag}"
                )

        # ── 2. Committed pricing benchmark ───────────────────────────────────
        _committed = _format_committed_callout(records)
        if _committed:
            lines.append("")
            for l in _committed.split("\n"):
                lines.append(l)

    # ── Significant price changes — grouped by provider + GPU ───────────────
    # Raw diffs contain one entry per (instance_type × region × ct). Group them
    # so "AWS L40S reserved -17% across 6 instance types × 5 regions" becomes
    # one readable line, not 90 separate bullets.
    significant = [
        d for d in diffs
        if d.change_type == "price_change"
        and provider_tier(d.provider) in ("raw_gpu_cloud", "hyperscaler",
                                          "enterprise_gpu_cloud")
        and abs(d.delta_pct or 0) >= ALERT_THRESHOLD_PCT
    ]

    if significant:
        # Group: (provider, gpu_model, ct_bucket, direction) → list of deltas
        from collections import defaultdict as _dd

        def _ct_bucket(ct: str) -> str:
            if ct in INTERRUPTIBLE_CTS:            return "spot"
            if ct in RESERVED_1YR_CTS:             return "committed/reserved 1yr"
            if ct in RESERVED_2YR_CTS:             return "committed/reserved 2yr"
            if ct in RESERVED_3YR_CTS:             return "committed/reserved 3yr"
            if "reserved" in ct or "committed" in ct: return "committed/reserved"
            return "on-demand"

        groups: Dict[tuple, list] = _dd(list)
        for d in significant:
            bucket = _ct_bucket(d.consumption_type)
            direction = "up" if (d.delta_pct or 0) > 0 else "down"
            groups[(d.provider, d.gpu_model, bucket, direction)].append(d)

        # Sort groups by magnitude (median abs delta_pct) descending
        def _group_sort_key(items):
            pcts = [abs(d.delta_pct or 0) for d in items]
            return -statistics.median(pcts)

        sorted_groups = sorted(groups.items(), key=lambda kv: _group_sort_key(kv[1]))

        def _display_prov(p: str) -> str:
            """Clean provider name for display: strip cp_ prefix, title-case."""
            _KEEP_UPPER = {"aws", "gcp", "azure", "gpu", "gmi"}
            name = p.replace("cp_", "").replace("-", " ")
            return " ".join(w.upper() if w.lower() in _KEEP_UPPER else w.title()
                            for w in name.split())

        lines.append(f"\n*Price moves ≥{ALERT_THRESHOLD_PCT:.0f}%:*")
        for (prov, gpu, bucket, direction), items in sorted_groups[:15]:
            arrow = "🔺" if direction == "up" else "🔻"
            pcts = [d.delta_pct or 0 for d in items]
            avg_pct = statistics.mean(pcts)
            sign = "+" if avg_pct > 0 else ""
            tier_tag = " _(hyperscaler)_" if provider_tier(prov) == "hyperscaler" else ""
            sku_count = len(set((d.instance_type, d.region) for d in items))
            sku_note = f" across {sku_count} SKUs" if sku_count > 1 else \
                       f" ({items[0].region})"
            # Sample old→new from the most impactful SKU
            best = max(items, key=lambda d: abs(d.delta_pct or 0))
            lines.append(
                f"{arrow} *{_display_prov(prov)}*{tier_tag} {gpu} {bucket}: "
                f"${best.old_price:.2f}→${best.new_price:.2f}/GPU-hr "
                f"({sign}{avg_pct:.1f}%{sku_note})"
            )
        if len(sorted_groups) > 15:
            lines.append(f"_…and {len(sorted_groups) - 15} more provider/GPU groups_")
    else:
        # Check if there were any price changes below threshold
        minor = [d for d in diffs if d.change_type == "price_change"
                 and provider_tier(d.provider) in ("raw_gpu_cloud", "hyperscaler",
                                                    "enterprise_gpu_cloud")]
        if minor:
            lines.append(f"\n_No significant price moves today "
                         f"({len(minor)} minor changes below {ALERT_THRESHOLD_PCT:.0f}% threshold)._")
        else:
            lines.append("\n_No price changes detected on tracked providers today._")

    lines.append(f"\nFull benchmark table: {confluence_url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confluence page — executive benchmark + detailed tables
# ---------------------------------------------------------------------------

def _build_committed_implication(records: List[PriceRecord]) -> str:
    """
    Dynamic strategic implication block — uses live prices so it stays accurate
    as AWS/GCP reprice their committed tiers.
    """
    def _best(provider, gpu, cts):
        prices = [
            r.price_per_gpu_hour_usd for r in records
            if r.provider == provider and r.gpu_model == gpu
            and r.consumption_type in cts
        ]
        return min(prices) if prices else None

    neb_od  = _best("nebius", "H100", {"on_demand"})
    aws_3yr = _best("aws",    "H100", RESERVED_3YR_CTS)
    neb_1yr = _best("nebius", "H100", RESERVED_1YR_CTS)

    if not (neb_od and aws_3yr):
        return ""

    gap_mult = neb_od / aws_3yr

    if neb_1yr:
        neb_discount = int((1 - neb_1yr / neb_od) * 100)
        if neb_1yr < aws_3yr:
            neb_part = (
                f" Nebius 1yr committed (${neb_1yr:.2f}, {neb_discount}% off on-demand) "
                f"is <strong>below the AWS 3yr price</strong> — "
                f"Nebius committed beats the deepest hyperscaler discount available."
            )
        else:
            neb_vs_aws3yr = (neb_1yr - aws_3yr) / aws_3yr * 100
            neb_part = (
                f" Nebius 1yr committed (${neb_1yr:.2f}, {neb_discount}% off on-demand) "
                f"is {neb_vs_aws3yr:.0f}% above the AWS 3yr price of ${aws_3yr:.2f}."
            )
    else:
        neb_part = ""

    # AWS 1yr vs Nebius 1yr — often the more actionable sales comparison
    aws_1yr = _best("aws", "H100", RESERVED_1YR_CTS)
    aws1yr_part = ""
    if aws_1yr and neb_1yr:
        if neb_1yr < aws_1yr:
            aws1yr_part = (
                f' Nebius 1yr committed (${neb_1yr:.2f}) is also '
                f'<strong>cheaper than AWS 1yr committed (${aws_1yr:.2f})</strong> — '
                f'customers do not need a 3yr AWS lock-in to match Nebius pricing.'
            )
        else:
            diff_pct = int((neb_1yr - aws_1yr) / aws_1yr * 100)
            aws1yr_part = (
                f' Nebius 1yr committed (${neb_1yr:.2f}) is {diff_pct}% above AWS 1yr (${aws_1yr:.2f}).'
            )

    # GCP 1yr — often 2–3× above Nebius committed, strong positioning point
    gcp_1yr = _best("gcp", "H100", RESERVED_1YR_CTS)
    gcp_part = ""
    if gcp_1yr and neb_1yr:
        gcp_disc = int((gcp_1yr - neb_1yr) / gcp_1yr * 100)
        gcp_part = (
            f' GCP 1yr committed runs ${gcp_1yr:.2f} — '
            f'Nebius 1yr is <strong>{gcp_disc}% cheaper than GCP committed</strong>.'
        )

    return (
        f'<p><strong>Sales context:</strong> An enterprise customer comparing '
        f'Nebius on-demand (${neb_od:.2f}/H100 GPU-hr) to AWS 3yr committed '
        f'(${aws_3yr:.2f}) sees a <strong>{gap_mult:.1f}× price difference</strong> — '
        f'the most common H100 objection in enterprise sales.'
        f'{neb_part}{aws1yr_part}{gcp_part}</p>'
    )


def format_confluence_table(records: List[PriceRecord], run_date: str) -> str:
    html = []

    html.append(
        f'<p><em>Last updated: {run_date}</em> — auto-refreshed daily. '
        f'All prices in <strong>$/GPU-hr</strong>. '
        f'Source: direct provider APIs/pages + '
        f'<a href="https://computeprices.com">ComputePrices.com</a>.</p>'
    )

    # ── Section 1: Executive benchmark ──────────────────────────────────────
    html.append('<h2>Executive Benchmark — Nebius vs Market</h2>')
    html.append(
        '<p>On-demand prices, cheapest available per provider. '
        '<strong>Enterprise GPU cloud</strong> peers are the direct competitive set '
        '(named providers with enterprise SLAs; commodity rental marketplaces excluded). '
        'Hyperscaler column shows rack-rate list price — enterprise customers pay 40–57% less at 3yr committed. '
        'Nebius prices are EU (eu-north1); US pricing typically 5–10% lower.</p>'
    )
    html.append(_build_executive_table(records))

    # ── Section 2: Committed pricing comparison ─────────────────────────────
    html.append('<h2>Committed Pricing Comparison</h2>')
    html.append(
        '<p>Nebius launched committed pricing on <strong>April 23rd 2026</strong> '
        '(9-month to 36-month terms; 100%, 50%, and 30% upfront options). '
        'The table below compares Nebius committed tiers against hyperscaler reserved pricing. '
        'Nebius figures shown are enterprise tier (512+ GPUs), 100% upfront — the most aggressive available rate. '
        'Standard tier (&lt;512 GPU) is ~5–10% higher.</p>'
    )
    html.append(_build_committed_gap_table(records))
    html.append(_build_committed_implication(records))

    # ── Section 3: Full peer price table by GPU ──────────────────────────────
    html.append('<h2>Complete Market Sweep — On-Demand by GPU</h2>')
    html.append(
        '<p>All tracked raw GPU cloud providers sorted by price. Includes commodity spot rental '
        'marketplaces (TensorDock, Vast.ai, RunPod, etc.) alongside enterprise GPU clouds — '
        'use the Executive Benchmark table above for apples-to-apples enterprise comparisons. '
        'Managed inference platforms (fal.ai, Deep Infra, Together AI) excluded — '
        'per-token billing makes $/GPU-hr comparisons meaningless.</p>'
    )
    html.append(_build_peer_tables(records))

    # ── Section 4: Regional comparison ──────────────────────────────────────
    html.append('<h2>Regional Price Comparison — All Providers by Geography</h2>')
    html.append(
        '<p>Cheapest price per provider within each geographic region. '
        'Geo buckets aggregate across provider-specific region names so AWS us-east-1, '
        'GCP us-east4, and Azure eastus can be compared in the same row. '
        'On-demand, spot, and committed tiers shown separately.</p>'
    )
    html.append(_build_hyperscaler_tables(records))

    return "\n".join(html)


def _build_executive_table(records: List[PriceRecord]) -> str:
    """
    One row per GPU: Nebius | cheapest enterprise peer | vs median | cheapest hyperscaler | count
    Peers = enterprise_gpu_cloud tier only (excludes commodity spot marketplaces).
    """
    # Only keep on-demand rows — the dict comprehension would otherwise overwrite
    # on-demand entries with interruptible entries (same gpu key, appended last).
    position = [row for row in compute_position(records) if row["tier_label"] == "on_demand"]
    pos_by_gpu = {row["gpu"]: row for row in position}

    rows = ['<table data-layout="full-width"><tbody>']
    rows.append(
        '<tr>'
        '<th>GPU</th>'
        '<th>Nebius (on-demand)</th>'
        '<th>Cheapest enterprise peer</th>'
        '<th>vs peer median</th>'
        '<th>Cheapest hyperscaler (on-demand)</th>'
        '<th>Enterprise peer median</th>'
        '<th>Enterprise peers tracked</th>'
        '</tr>'
    )

    for gpu in GPU_ORDER:
        row = pos_by_gpu.get(gpu)

        nebius_td = _price_td(row["nebius_price"] if row else None)

        if row and row["cheapest_peer"]:
            peer_td = (f'<td>${row["cheapest_peer"]:.2f} '
                       f'<em>({_provider_display(row["cheapest_peer_name"]) if row["cheapest_peer_name"] else ""})</em></td>')
        else:
            peer_td = '<td>—</td>'

        # vs median (more meaningful than vs floor for pricing decisions)
        if row and row["nebius_price"] and row["median_peer"]:
            pct = (row["nebius_price"] - row["median_peer"]) / row["median_peer"] * 100
            loz_color = "red" if pct > 15 else ("yellow" if pct > 0 else "green")
            sign = "+" if pct >= 0 else ""
            vs_td = (f'<td><span data-type="status" data-color="{loz_color}">'
                     f'{sign}{pct:.0f}% vs median</span></td>')
        else:
            vs_td = '<td>—</td>'

        # Cheapest hyperscaler on-demand
        hyp_best = _best_price(records, gpu, "on_demand", tiers=["hyperscaler"])
        hyp_td = (f'<td>${hyp_best.price_per_gpu_hour_usd:.2f} '
                  f'<em>({_provider_display(hyp_best.provider)})</em></td>') \
                 if hyp_best else '<td>—</td>'

        med_td = f'<td>${row["median_peer"]:.2f}</td>' if row and row["median_peer"] else '<td>—</td>'
        count_td = f'<td>{row["total_peers"] + 1 if row else 0}</td>'  # +1 for Nebius

        rows.append(
            f'<tr><td><strong>{gpu}</strong></td>'
            f'{nebius_td}{peer_td}{vs_td}{hyp_td}{med_td}{count_td}</tr>'
        )

    rows.append('</tbody></table>')
    rows.append(
        '<p><em>'
        'Enterprise peers: CoreWeave, Lambda, Crusoe, Hyperstack (NexGen Cloud), '
        'Voltage Park, GMI Cloud, Scaleway, Gcore. '
        'Criteria: GPU-first business, meaningful owned capacity (1,000+ GPUs), enterprise SLAs, active. '
        'Excluded: Genesis Cloud (in liquidation 2025), Sesterce (broker/reseller model), '
        'Denvr Dataworks (too small). '
        'Hyperscaler column = cheapest of AWS / GCP / Azure / Oracle (on-demand list price; '
        'enterprise customers typically pay 40–57% less at 3yr committed). '
        'Nebius prices are from EU (eu-north1); US pricing typically 5–10% lower. '
        'IREN: competitor named in enterprise sales calls; not yet tracked (no public pricing). '
        '⚠️ L40S pricing risk: Nebius on-demand ($1.82) is only 2% below AWS on-demand ($1.86); '
        'AWS L40S drops to ~$0.37 at 3yr committed — a 5× gap that Nebius has no committed L40S tier to counter. '
        'GB200/GB300: Nebius has committed pricing for these GPUs (see table below) but no published on-demand rate.'
        '</em></p>'
    )
    return "\n".join(rows)


def _build_committed_gap_table(records: List[PriceRecord]) -> str:
    """
    Show on-demand + committed pricing across hyperscalers and Nebius.

    Columns: on-demand | 1yr | 2yr (Nebius max for H100/H200) | 3yr
    GPUs: H100, H200, B200, B300, GB300

    Nebius committed prices come from the internal pricing model
    (config.NEBIUS_COMMITTED_PRICES, effective April 23rd 2026).
    The table shows the best available Nebius price (enterprise tier, 100% upfront).
    Standard tier (<512 GPU) pricing is ~5–10% higher.
    """
    SHOW_GPUS = ["H100", "H200", "B200", "B300", "GB300"]

    # For H100/H200: hyperscalers + Nebius (committed pricing well-established)
    # For Blackwell (B200/B300/GB300): include raw_gpu_cloud peers with committed data
    # since hyperscalers don't yet publish reserved pricing for these GPUs.
    HYPERSCALER_PROVIDERS = ["aws", "gcp", "azure", "nebius"]

    # Providers to exclude from this table — defunct, consumer-focused, or not
    # relevant enterprise GPU competitors despite having committed pricing data
    COMMITTED_TABLE_EXCLUDE = {
        "cp_genesis",    # in liquidation since 2025 — prices stale and unreliable
        "cp_paperspace", # consumer ML platform (DigitalOcean), not enterprise GPU cloud
        "cp_civo",       # Kubernetes-first cloud, not a GPU compute competitor
    }

    # Column definitions: (display header, CT set)
    COLUMNS = [
        ("On-demand / PAYG",   {"on_demand"}),
        ("12-month¹",          RESERVED_1YR_CTS),
        ("24-month¹",          RESERVED_2YR_CTS),
        ("36-month¹",          RESERVED_3YR_CTS),
    ]
    CT_HDR = [c[0] for c in COLUMNS]

    # Flatten to: gpu → col_idx → provider → cheapest price
    # Include ALL providers so peer data for Blackwell is captured
    grouped: Dict[str, Dict[int, Dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict))
    for r in records:
        if r.gpu_model not in SHOW_GPUS:
            continue
        for col_idx, (_, cts) in enumerate(COLUMNS):
            if r.consumption_type not in cts:
                continue
            existing = grouped[r.gpu_model][col_idx].get(r.provider)
            if existing is None or r.price_per_gpu_hour_usd < existing:
                grouped[r.gpu_model][col_idx][r.provider] = r.price_per_gpu_hour_usd

    # Sanity filter: a committed price should be below on-demand.
    # Two checks:
    # 1. Same provider: drop if committed >= provider's own on-demand
    # 2. Cross-provider: drop if committed > cheapest on-demand for that GPU
    #    across all providers — catches bad data when provider has no on-demand record
    #    (e.g. Gcore H200 reserved at $19 when AWS H200 on-demand is $7.91).
    for gpu in list(grouped.keys()):
        od_col = grouped[gpu][0]   # col 0 = on_demand
        global_floor = min(od_col.values()) if od_col else None
        for col_idx in range(1, len(COLUMNS)):
            for prov in list(grouped[gpu][col_idx].keys()):
                committed = grouped[gpu][col_idx][prov]
                od = od_col.get(prov)
                # Check 1: provider's own on-demand
                if od and committed >= od:
                    del grouped[gpu][col_idx][prov]
                    continue
                # Check 2: cross-provider floor — catch obvious data errors
                # (e.g. Gcore H200 "reserved" at $19 when cheapest H200 on-demand is $7.91).
                # Use a generous 4x multiple to only catch clear magnitude errors,
                # not providers that are legitimately more expensive.
                if global_floor and committed > global_floor * 4.0:
                    del grouped[gpu][col_idx][prov]

    html = ['<table data-layout="full-width"><tbody>']
    html.append(
        '<tr><th>GPU / Provider</th>'
        + "".join(f'<th>{h}</th>' for h in CT_HDR)
        + '</tr>'
    )

    for gpu in SHOW_GPUS:
        if gpu not in grouped:
            continue

        # Determine which providers to show for this GPU:
        # always hyperscalers+nebius; for Blackwell also add any peer with committed pricing
        providers_with_data = {
            p for col_data in grouped[gpu].values() for p in col_data
        }
        # Hyperscalers first, then sorted peers (by cheapest committed price)
        peers_with_committed = sorted(
            [p for p in providers_with_data
             if p not in HYPERSCALER_PROVIDERS
             and p not in COMMITTED_TABLE_EXCLUDE
             and any(grouped[gpu][ci].get(p) for ci in range(1, len(COLUMNS)))],
            key=lambda p: min(
                (grouped[gpu][ci].get(p, 9999) for ci in range(1, len(COLUMNS))),
                default=9999
            )
        )
        show_providers = HYPERSCALER_PROVIDERS + peers_with_committed

        # Section header row
        html.append(
            f'<tr><td colspan="{len(COLUMNS)+1}"><strong>{gpu}</strong></td></tr>'
        )

        for prov in show_providers:
            cells = []
            has_any = False
            for col_idx in range(len(COLUMNS)):
                price = grouped[gpu][col_idx].get(prov)
                if price is not None:
                    has_any = True
                    if prov == "nebius" and col_idx > 0:
                        od = grouped[gpu][0].get("nebius")
                        if od and price < od:
                            disc = int((1 - price / od) * 100)
                            cells.append(
                                f'<td>${price:.2f} '
                                f'<span data-type="status" data-color="green">-{disc}%</span></td>'
                            )
                        else:
                            cells.append(f'<td>${price:.2f}</td>')
                    else:
                        cells.append(f'<td>${price:.2f}</td>')
                else:
                    cells.append('<td>—</td>')
            if has_any:
                display = _provider_display(prov)
                html.append(
                    f'<tr><td>{display}</td>'
                    + "".join(cells) + '</tr>'
                )

    html.append('</tbody></table>')
    html.append(
        '<p><em>'
        '¹ AWS: Standard reserved, partial-upfront (best discount for fixed commitment). '
        'H100 1yr: Convertible class only (Standard 1yr not available for p5 family). '
        'Azure: partial-upfront capacity reservation. '
        'GCP: Committed Use Discount (no upfront, usage commitment, no capacity guarantee). '
        'Oracle: on-demand prices sourced from ComputePrices.com — treat as directional estimates '
        'until verified against OCI directly. Oracle does not publish committed GPU pricing publicly. '
        'Nebius: internal pricing model effective April 23rd 2026; enterprise tier (512+ GPU, 100% upfront). '
        'Standard tier (&lt;512 GPU) ~5–10% higher; 36-month H100/H200 available on request. '
        'Peer providers (Vultr) sourced from ComputePrices.com. '
        'Nebius prices from EU (eu-north1); US pricing typically 5–10% lower.'
        '</em></p>'
    )
    return "\n".join(html)


def _build_peer_tables(records: List[PriceRecord]) -> str:
    """One table per GPU, rows = raw_gpu_cloud peers, sorted by price.
    Deduplicated to cheapest per provider (multi-node-size providers like Lambda
    list one row per node size; we show only the cheapest to avoid clutter).
    """
    html = []
    for gpu in GPU_ORDER:
        raw_peers = [r for r in records
                     if r.gpu_model == gpu
                     and r.consumption_type == "on_demand"
                     and provider_tier(r.provider) == "raw_gpu_cloud"]
        # Deduplicate: keep cheapest record per provider
        best_by_prov: Dict[str, PriceRecord] = {}
        for r in raw_peers:
            if r.provider not in best_by_prov or \
                    r.price_per_gpu_hour_usd < best_by_prov[r.provider].price_per_gpu_hour_usd:
                best_by_prov[r.provider] = r
        peers = sorted(best_by_prov.values(), key=lambda r: r.price_per_gpu_hour_usd)
        if not peers:
            continue

        html.append(f'<h3>{gpu} — On-demand (raw GPU cloud, sorted by price)</h3>')
        html.append('<table data-layout="full-width"><tbody>')
        html.append(
            '<tr><th>Provider</th><th>$/GPU-hr</th>'
            '<th>Node size</th><th>Region</th><th>vs Nebius</th></tr>'
        )

        nebius_price = next(
            (r.price_per_gpu_hour_usd for r in peers if r.provider == "nebius"), None)

        for r in peers:
            prov_display = r.provider.replace("cp_", "").replace("-", " ").title()
            if r.provider == "nebius":
                prov_display = f'<strong>Nebius ★</strong>'

            node_str = f'{r.gpu_count}× GPU'

            if nebius_price and r.provider != "nebius":
                diff = (r.price_per_gpu_hour_usd - nebius_price) / nebius_price * 100
                sign = "+" if diff >= 0 else ""
                loz = "green" if diff > 0 else "red"
                vs_td = (f'<td><span data-type="status" data-color="{loz}">'
                         f'{sign}{diff:.0f}%</span></td>')
            elif r.provider == "nebius":
                vs_td = '<td>—</td>'
            else:
                vs_td = '<td>—</td>'

            html.append(
                f'<tr><td>{prov_display}</td>'
                f'<td><strong>${r.price_per_gpu_hour_usd:.2f}</strong></td>'
                f'<td>{node_str}</td>'
                f'<td>{r.region}</td>'
                f'{vs_td}</tr>'
            )
        html.append('</tbody></table>')

    return "\n".join(html)


# ---------------------------------------------------------------------------
# Geo-bucket helpers for regional comparison tables
# ---------------------------------------------------------------------------

_GEO_BUCKETS: Dict[str, set] = {
    "US": {
        "us-east-1", "us-east-2", "us-west-2", "us-west-1", "us-west-3",
        "us-east4", "us-central1", "us-west4", "us-south1",
        "eastus", "eastus2", "westus2", "westus3",
        "us-east", "us-west", "us-south",
    },
    "Europe": {
        "eu-west-1", "eu-west-2", "eu-central-1", "eu-north-1",
        "europe-west1", "europe-west3", "europe-west4",
        "westeurope", "northeurope", "germanywestcentral", "swedencentral",
        "eu-north1", "eu-west1", "eu-central1",
    },
    "APAC": {
        "ap-northeast-1", "ap-southeast-1", "ap-east-1",
        "asia-northeast1", "asia-southeast1",
        "japaneast", "southeastasia",
    },
}
_GEO_ORDER = ["US", "Europe", "APAC"]


def _region_to_geo(region: str) -> Optional[str]:
    r = region.lower()
    for geo, regions in _GEO_BUCKETS.items():
        if r in regions:
            return geo
    return None


def _build_hyperscaler_tables(records: List[PriceRecord]) -> str:
    """
    Regional price comparison table — all direct providers grouped into
    geo buckets (US / Europe / APAC) rather than exact region rows.

    Each cell shows the cheapest price for that provider in that geography,
    making cross-provider comparison meaningful even when providers don't
    share exact region names (AWS us-east-1 ≈ GCP us-east4 ≈ Azure eastus).

    Includes AWS, GCP, Azure, CoreWeave, Lambda, Crusoe.
    Excludes Oracle (global synthetic region only) and Nebius (EU only, already
    covered in the executive table above).
    """
    html = []

    # Providers in column order for this table
    GEO_PROVIDERS = ["aws", "gcp", "azure", "coreweave", "lambda", "crusoe"]
    # Providers with only global/aggregate pricing (no region-specific data).
    # Their prices are shown in every geo row as a reference column.
    GLOBAL_COL_PROVIDERS = ["cp_oracle"]
    GLOBAL_COL_LABELS    = ["Oracle†"]

    # Only include providers with real regional data (not synthetic 'global')
    included = [r for r in records if r.provider in GEO_PROVIDERS and _region_to_geo(r.region)]

    # Build: gpu → ct → geo → provider → cheapest price
    geo_grouped: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(dict))
    )
    for r in included:
        geo = _region_to_geo(r.region)
        if not geo:
            continue
        existing = geo_grouped[r.gpu_model][r.consumption_type][geo].get(r.provider)
        if existing is None or r.price_per_gpu_hour_usd < existing:
            geo_grouped[r.gpu_model][r.consumption_type][geo][r.provider] = r.price_per_gpu_hour_usd

    # Global providers: cheapest price per gpu × ct (shown identically in all geo rows)
    global_prices: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for r in records:
        if r.provider not in GLOBAL_COL_PROVIDERS:
            continue
        existing = global_prices[r.gpu_model][r.consumption_type].get(r.provider)
        if existing is None or r.price_per_gpu_hour_usd < existing:
            global_prices[r.gpu_model][r.consumption_type][r.provider] = r.price_per_gpu_hour_usd

    # Also add Nebius committed prices to the reserved CT buckets for comparison
    nebius_committed: Dict[str, Dict[str, float]] = defaultdict(dict)
    for r in records:
        if r.provider == "nebius":
            existing = nebius_committed[r.gpu_model].get(r.consumption_type)
            if existing is None or r.price_per_gpu_hour_usd < existing:
                nebius_committed[r.gpu_model][r.consumption_type] = r.price_per_gpu_hour_usd

    # CT groups to show: on-demand, spot, 1yr reserved, 3yr reserved
    CT_GROUPS = [
        ("On-demand",           {"on_demand"}),
        ("Spot / Preemptible",  INTERRUPTIBLE_CTS),
        ("Reserved / Committed 1 yr", RESERVED_1YR_CTS),
        ("Reserved / Committed 3 yr", RESERVED_3YR_CTS),
    ]

    all_col_providers = ["nebius"] + GEO_PROVIDERS + GLOBAL_COL_PROVIDERS
    col_headers = ["Nebius*"] + [p.upper() for p in GEO_PROVIDERS] + GLOBAL_COL_LABELS

    for gpu in GPU_ORDER:
        if gpu not in geo_grouped:
            continue
        html.append(f'<h3>{gpu}</h3>')

        for ct_label, cts in CT_GROUPS:
            # Collect all data for this CT group
            # geo → provider → cheapest price across all matching CTs
            ct_data: Dict[str, Dict[str, float]] = defaultdict(dict)
            for ct in cts:
                for geo, prov_map in geo_grouped[gpu].get(ct, {}).items():
                    for prov, price in prov_map.items():
                        existing = ct_data[geo].get(prov)
                        if existing is None or price < existing:
                            ct_data[geo][prov] = price

            # Add Nebius committed prices (region-agnostic — apply to all geos)
            neb_price = None
            for ct in cts:
                p = nebius_committed[gpu].get(ct)
                if p is not None and (neb_price is None or p < neb_price):
                    neb_price = p

            # Global providers: cheapest price across all matching CTs (same in all geo rows)
            global_col_prices: Dict[str, Optional[float]] = {}
            for gp in GLOBAL_COL_PROVIDERS:
                best_gp = None
                for ct in cts:
                    p = global_prices[gpu].get(ct, {}).get(gp)
                    if p is not None and (best_gp is None or p < best_gp):
                        best_gp = p
                global_col_prices[gp] = best_gp

            # Check if any geo has data for this CT group (or global provider data)
            has_data = (any(ct_data[geo] for geo in _GEO_ORDER)
                        or neb_price is not None
                        or any(v is not None for v in global_col_prices.values()))
            if not has_data:
                continue

            html.append(f'<h4>{ct_label}</h4>')
            html.append('<table data-layout="full-width"><tbody>')
            html.append(
                '<tr><th>Geography</th>'
                + "".join(f'<th>{h}</th>' for h in col_headers)
                + '</tr>'
            )

            # Nebius on-demand price (from nebius fetcher, region eu-north1 = Europe)
            neb_od_price = next(
                (r.price_per_gpu_hour_usd for r in records
                 if r.provider == "nebius" and r.gpu_model == gpu
                 and r.consumption_type == "on_demand"),
                None
            )

            for geo in _GEO_ORDER:
                row_data = ct_data.get(geo, {})
                # Skip geos with no data at all (including global column data)
                has_row_data = (row_data or neb_price is not None
                                or any(v is not None for v in global_col_prices.values())
                                or (geo == "Europe" and neb_od_price and ct_label == "On-demand"))
                if not has_row_data:
                    continue
                cells = []
                # Nebius column (on-demand: EU only; committed: shown for all geos)
                if ct_label == "On-demand":
                    # Nebius on-demand only available in Europe (eu-north1)
                    p = neb_od_price if geo == "Europe" else None
                    cells.append(f'<td><strong>${p:.2f}</strong></td>' if p else '<td>—</td>')
                elif neb_price is not None:
                    cells.append(f'<td><strong>${neb_price:.2f}</strong></td>')
                else:
                    cells.append('<td>—</td>')

                for prov in GEO_PROVIDERS:
                    price = row_data.get(prov)
                    if price is not None:
                        cells.append(f'<td>${price:.2f}</td>')
                    else:
                        cells.append('<td>—</td>')

                # Global provider columns — same price in every geo row
                for gp in GLOBAL_COL_PROVIDERS:
                    gp_price = global_col_prices.get(gp)
                    cells.append(f'<td>${gp_price:.2f}</td>' if gp_price else '<td>—</td>')

                html.append(f'<tr><td><strong>{geo}</strong></td>{"".join(cells)}</tr>')

            html.append('</tbody></table>')

    html.append(
        '<p><em>'
        'Each cell shows the cheapest price for that provider in any region within that geography. '
        'AWS: standard reserved partial-upfront. GCP: Committed Use Discount (no upfront). '
        'Azure: partial-upfront capacity reservation. '
        '*Nebius on-demand prices are EU (eu-north1); US pricing typically 5–10% lower. '
        'Nebius committed = internal pricing model (enterprise tier, 100% upfront). '
        'CoreWeave and Lambda: US regions only currently. '
        '†Oracle prices sourced from ComputePrices.com (no region breakdown available) — '
        'treat as directional estimates; Oracle does not publish GPU committed pricing publicly. '
        'Geography buckets: US includes us-east/us-west/us-central; '
        'Europe includes eu-west/eu-central/eu-north/northeurope/westeurope; '
        'APAC includes ap-northeast/ap-southeast/asia-northeast/japaneast.'
        '</em></p>'
    )
    return "\n".join(html)


def _price_td(price: Optional[float]) -> str:
    if price is None:
        return '<td>—</td>'
    return f'<td><strong>${price:.2f}</strong></td>'
