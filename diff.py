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
    return {
        "gpu": gpu,
        "tier_label": label,
        "nebius_price": nebius_rec.price_per_gpu_hour_usd if nebius_rec else None,
        "cheapest_peer": cheapest_peer.price_per_gpu_hour_usd if cheapest_peer else None,
        "cheapest_peer_name": cheapest_peer.provider if cheapest_peer else None,
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
        winner = "⚠️ AWS cheaper" if diff_pct > 0 else "✅ Nebius cheaper"
        parts.append(
            f"H100 12-month: Nebius ${neb_1yr:.2f} vs AWS ${aws_1yr:.2f} "
            f"({sign}{diff_pct:.0f}%) {winner}"
        )
    elif neb_1yr:
        parts.append(f"H100 12-month: Nebius ${neb_1yr:.2f} (AWS: no data)")

    if neb_2yr:
        parts.append(f"H100 24-month (Nebius max): ${neb_2yr:.2f}/GPU-hr")

    if aws_3yr:
        neb_od = _best("nebius", "H100", {"on_demand"})
        if neb_od:
            parts.append(
                f"AWS H100 3yr standard: ${aws_3yr:.2f}/GPU-hr "
                f"(vs Nebius on-demand ${neb_od:.2f}; 3yr Nebius H100 = per request)"
            )
        else:
            parts.append(f"AWS H100 3yr standard: ${aws_3yr:.2f}/GPU-hr")

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

    Structure:
    1. Key signals — wins, concerns, committed pricing vs AWS
    2. On-demand position vs enterprise GPU cloud peers (not commodity floor)
    3. Significant price moves (>threshold)
    4. Link to full Confluence table
    """
    lines = [f"*GPU Competitor Pricing — {run_date}*"]

    if records:
        position = compute_position(records)
        od_rows = [r for r in position if r["tier_label"] == "on_demand"]

        # ── 1. Key signals ───────────────────────────────────────────────────
        wins    = [r for r in od_rows if r["nebius_price"] and r["cheapest_peer"]
                   and r["nebius_price"] <= r["cheapest_peer"]]
        concerns = [r for r in od_rows if r["nebius_price"] and r["median_peer"]
                    and r["nebius_price"] > r["median_peer"] * 1.15]

        signals = []
        for r in wins:
            nxt = r["cheapest_peer"]
            nxt_name = (r["cheapest_peer_name"] or "").replace("cp_", "")
            signals.append(
                f"*{r['gpu']}*: Nebius ✅ cheapest at ${r['nebius_price']:.2f} "
                f"(next: ${nxt:.2f} {nxt_name})"
            )
        for r in concerns:
            signals.append(
                f"*{r['gpu']}*: Nebius ${r['nebius_price']:.2f} — above peer median "
                f"${r['median_peer']:.2f} ⚠️"
            )

        # Committed callout as a key signal
        _committed = _format_committed_callout(records)

        if signals or _committed:
            lines.append("\n*Key signals:*")
            for s in signals:
                lines.append(f"• {s}")
            if _committed:
                # Inline the committed bullets under key signals
                for l in _committed.split("\n")[1:]:   # skip the header line
                    lines.append(l)

        # ── 2. On-demand position vs enterprise peers ────────────────────────
        if od_rows:
            lines.append("\n*On-demand vs enterprise GPU cloud peers:*")
            for row in od_rows:
                neb = row["nebius_price"]
                med = row["median_peer"]
                cheap = row["cheapest_peer"]
                cheap_name = (row["cheapest_peer_name"] or "").replace("cp_", "")
                total = row["total_peers"]
                gpu = row["gpu"]

                if neb is None:
                    lines.append(f"• *{gpu}*: Nebius — no public price")
                    continue

                if cheap and cheap < neb:
                    cheaper = row["peers_cheaper"]
                    lines.append(
                        f"• *{gpu}*: Nebius ${neb:.2f} | "
                        f"Range: ${cheap:.2f}–${max(r.price_per_gpu_hour_usd for r in records if r.gpu_model == gpu and r.consumption_type == 'on_demand' and r.provider in PROVIDER_TIERS.get('enterprise_gpu_cloud',[]) and r.provider != 'nebius'):.2f} | "
                        f"Median: ${med:.2f} | {cheaper}/{total} peers cheaper"
                        if med else f"• *{gpu}*: Nebius ${neb:.2f}"
                    )
                else:
                    lines.append(
                        f"• *{gpu}*: Nebius ${neb:.2f} ✅ cheapest | "
                        f"Next: ${cheap:.2f} ({cheap_name}) | Median: ${med:.2f}"
                        if cheap and med else f"• *{gpu}*: Nebius ${neb:.2f}"
                    )

    # ── Significant price changes ────────────────────────────────────────────
    significant = [
        d for d in diffs
        if d.change_type == "price_change"
        and provider_tier(d.provider) in ("raw_gpu_cloud", "hyperscaler")
        and abs(d.delta_pct or 0) >= ALERT_THRESHOLD_PCT
    ]

    if significant:
        lines.append(f"\n*Price moves ≥{ALERT_THRESHOLD_PCT:.0f}% on tracked providers:*")
        for d in significant:
            arrow = "🔺" if d.delta_pct > 0 else "🔻"
            sign  = "+" if d.delta_pct > 0 else ""
            tier_tag = "(hyperscaler)" if provider_tier(d.provider) == "hyperscaler" else ""
            lines.append(
                f"{arrow} *{d.provider.upper()}* {tier_tag} {d.gpu_model} "
                f"({d.region}, {d.consumption_type}): "
                f"${d.old_price:.2f} → ${d.new_price:.2f}/GPU-hr "
                f"({sign}{d.delta_pct:.1f}%)"
            )
    else:
        # Check if there were any price changes below threshold
        minor = [d for d in diffs if d.change_type == "price_change"
                 and provider_tier(d.provider) in ("raw_gpu_cloud", "hyperscaler")]
        if minor:
            lines.append(f"\n_No significant price moves today "
                         f"({len(minor)} minor changes below {ALERT_THRESHOLD_PCT:.0f}% threshold)._")
        else:
            lines.append("\n_No price changes detected on tracked providers today._")

    # ── Pipeline churn summary (adds/removes — not individual SKUs) ──────────
    adds    = [d for d in diffs if d.change_type == "added"]
    removes = [d for d in diffs if d.change_type == "removed"]
    # Pipeline churn — only surface if unusually large (>50 net changes)
    # routine catalogue refreshes from ComputePrices are not CFO-relevant
    net_churn = abs(len(adds) - len(removes))
    if net_churn > 50:
        lines.append(f"\n_Note: {net_churn} net provider SKU changes — may indicate "
                     f"a new provider added or data source change._")

    lines.append(f"\nFull benchmark table: {confluence_url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confluence page — executive benchmark + detailed tables
# ---------------------------------------------------------------------------

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

    # ── Section 2: Committed pricing gap ────────────────────────────────────
    html.append('<h2>⚠️ Committed Pricing Gap</h2>')
    html.append(
        '<p>Nebius currently has <strong>no reserved or committed pricing tier</strong>. '
        'All major hyperscalers and several GPU-cloud peers offer significant discounts for '
        '1- and 3-year commitments:</p>'
    )
    html.append(_build_committed_gap_table(records))
    html.append(
        '<p><strong>Strategic implication:</strong> An enterprise customer comparing Nebius '
        'on-demand ($2.95/H100 GPU-hr) against AWS 3yr committed (~$3.04) sees near-parity. '
        'A Nebius 1yr committed at 15% discount ($2.51) would be the clear cheapest option '
        'for H100 among hyperscalers while locking in ARR.</p>'
    )

    # ── Section 3: Full peer price table by GPU ──────────────────────────────
    html.append('<h2>Full Market Reference — On-Demand by GPU</h2>')
    html.append(
        '<p>Raw GPU Cloud peers only. Managed inference platforms (fal.ai, Deep Infra, '
        'Together AI etc.) excluded — their per-GPU-hr equivalent is not comparable to '
        'bare-metal GPU cloud.</p>'
    )
    html.append(_build_peer_tables(records))

    # ── Section 4: Hyperscaler detail ───────────────────────────────────────
    html.append('<h2>Hyperscaler Detail — All Consumption Types &amp; Regions</h2>')
    html.append(
        '<p>Includes on-demand, spot, and reserved tiers across regions. '
        'Note: reservation <em>retailPrice</em> from Azure/AWS APIs is converted to effective '
        'hourly rate (total upfront ÷ term hours).</p>'
    )
    html.append(_build_hyperscaler_tables(records))

    return "\n".join(html)


def _build_executive_table(records: List[PriceRecord]) -> str:
    """
    One row per GPU: Nebius | cheapest enterprise peer | vs median | cheapest hyperscaler | count
    Peers = enterprise_gpu_cloud tier only (excludes commodity spot marketplaces).
    """
    position = compute_position(records)
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
                       f'<em>({row["cheapest_peer_name"].replace("cp_","") if row["cheapest_peer_name"] else ""})</em></td>')
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
                  f'<em>({hyp_best.provider.upper()})</em></td>') \
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
        'Enterprise peers: CoreWeave, Lambda, Crusoe, Hyperstack, Voltage Park, '
        'Genesis Cloud, GMI Cloud, Scaleway, GCore, Sesterce, denvr dataworks. '
        'Hyperscaler column = cheapest of AWS / GCP / Azure / Oracle (on-demand list price; '
        'enterprise customers typically pay 40–57% less at 3yr committed). '
        'Commodity GPU rental marketplaces (RunPod, TensorDock, Vast.ai) and general VPS '
        'providers (DigitalOcean, Vultr) excluded — not enterprise-comparable. '
        'Nebius prices are from EU (eu-north1); US pricing typically 5–10% lower. '
        'IREN: competitor identified in sales calls; not yet tracked (no public pricing).'
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
                display = prov.replace("cp_", "").upper()
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
        'Peer providers (Genesis Cloud, Vultr, Civo) sourced from ComputePrices.com. '
        'Nebius prices from EU (eu-north1); US pricing typically 5–10% lower.'
        '</em></p>'
    )
    return "\n".join(html)


def _build_peer_tables(records: List[PriceRecord]) -> str:
    """One table per GPU, rows = raw_gpu_cloud peers, sorted by price."""
    html = []
    for gpu in GPU_ORDER:
        peers = sorted(
            [r for r in records
             if r.gpu_model == gpu
             and r.consumption_type == "on_demand"
             and provider_tier(r.provider) == "raw_gpu_cloud"],
            key=lambda r: r.price_per_gpu_hour_usd
        )
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


def _build_hyperscaler_tables(records: List[PriceRecord]) -> str:
    """Full detail table for hyperscalers: all CTs × regions.
    Includes AWS, GCP, Azure only — Oracle is in the hyperscaler tier but uses
    a synthetic 'global' region from ComputePrices.com and is excluded here to
    avoid adding empty rows to the regional breakdown.
    """
    html = []
    REGIONAL_HYPERSCALERS = {"aws", "gcp", "azure"}
    hyp_records = [r for r in records
                   if r.provider in REGIONAL_HYPERSCALERS]

    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in hyp_records:
        grouped[r.gpu_model][r.consumption_type][r.provider].append(r)

    for gpu in GPU_ORDER:
        if gpu not in grouped:
            continue
        html.append(f'<h3>{gpu}</h3>')

        for ct in CT_ORDER:
            if ct not in grouped[gpu]:
                continue
            label = CT_LABELS.get(ct, ct)
            all_regions = sorted({r.region for recs in grouped[gpu][ct].values() for r in recs})
            if not all_regions:
                continue

            html.append(f'<h4>{label}</h4>')
            html.append('<table data-layout="full-width"><tbody>')
            html.append(
                '<tr><th>Region</th>'
                + "".join(f'<th>{p.upper()}</th>' for p in DIRECT_PROVIDERS if p != "nebius")
                + '</tr>'
            )
            for region in all_regions:
                cells = []
                for p in [p for p in DIRECT_PROVIDERS if p != "nebius"]:
                    recs = [r for r in grouped[gpu][ct].get(p, []) if r.region == region]
                    if recs:
                        best = min(recs, key=lambda r: r.price_per_gpu_hour_usd)
                        cells.append(f'<td>${best.price_per_gpu_hour_usd:.3f}</td>')
                    else:
                        cells.append('<td>—</td>')
                html.append(f'<tr><td><code>{region}</code></td>{"".join(cells)}</tr>')

            html.append('</tbody></table>')

    return "\n".join(html)


def _price_td(price: Optional[float]) -> str:
    if price is None:
        return '<td>—</td>'
    return f'<td><strong>${price:.2f}</strong></td>'
