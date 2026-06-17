"""
Compute price changes between two snapshots and format outputs.
"""
import csv
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from schema import PriceRecord, DiffEntry
from config import provider_tier, ALERT_THRESHOLD_PCT, PROVIDER_TIERS
from comparability import enrich_comparability, is_cluster_class

INTEL_CSV = Path(__file__).parent / "store" / "intel.csv"
HISTORY_CSV = Path(__file__).parent / "store" / "history.csv"

CHANGE_THRESHOLD = 0.001   # 0.1% — ignore floating-point noise in diff detection

GPU_ORDER = ["H100", "H200", "B200", "B300", "GB200", "GB300", "L40S"]
CT_ORDER  = ["on_demand", "spot", "preemptible", "reserved_1yr", "reserved_3yr",
             "committed_1yr", "committed_3yr"]
CT_LABELS = {
    "on_demand":     "On-demand / PAYG",
    "spot":          "Spot / Preemptible (interruptible)",
    "preemptible":   "Spot / Preemptible (interruptible)",
    "reserved_1yr":  "Reserved 1 yr (AWS all-upfront / Azure partial-upfront; GCP Committed Use Discount)",
    "reserved_3yr":  "Reserved 3 yr (AWS all-upfront / Azure partial-upfront; GCP Committed Use Discount)",
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
    "oracle":         "Oracle",
    "sfcompute":      "SF Compute",
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
                tiers: Optional[List[str]] = None,
                cluster_only: bool = False) -> Optional[PriceRecord]:
    """
    Return the cheapest record for a given gpu/ct combination, optionally filtered
    by tier. cluster_only=True restricts to cluster-class (8×SXM HGX) SKUs so a
    single-GPU NVL/PCIe entry SKU (Azure NC40ads, Lambda 1×PCIe) cannot be the
    headline "cheapest" against a competitor's SXM training node.
    """
    candidates = [
        r for r in records
        if r.gpu_model == gpu and r.consumption_type == ct
        and (tiers is None or provider_tier(r.provider) in tiers)
        and (not cluster_only or is_cluster_class(r))
    ]
    return min(candidates, key=lambda r: r.price_per_gpu_hour_usd) if candidates else None


def _representative_spot_floor(records: List[PriceRecord], gpu: str,
                               tiers: Optional[List[str]] = None):
    """
    Representative cheapest spot price for a GPU: each provider's MEDIAN across its
    zone observations, then the cheapest provider's median. Avoids a transient
    single-zone outlier (e.g. AWS H200 spot dipping to $0.79 in one zone while the
    other zones sit at $2.0–2.2) masquerading as "the spot floor".
    Returns (provider, median_price, n_zones) or None.
    """
    from collections import defaultdict as _dd
    by_prov = _dd(list)
    for r in records:
        if (r.gpu_model == gpu and r.consumption_type in INTERRUPTIBLE_CTS
                and (tiers is None or provider_tier(r.provider) in tiers)):
            by_prov[r.provider].append(r.price_per_gpu_hour_usd)
    if not by_prov:
        return None
    prov_median = {p: statistics.median(v) for p, v in by_prov.items()}
    best = min(prov_median, key=prov_median.get)
    return best, prov_median[best], len(by_prov[best])


def _best_comparable(records: List[PriceRecord], gpu: str, ct: str,
                     tiers: Optional[List[str]] = None) -> Optional[PriceRecord]:
    """
    Cheapest like-for-like price: prefer a cluster-class (SXM) SKU so a single-GPU
    NVL/PCIe entry SKU can't be the headline; fall back to the overall cheapest only
    when no cluster SKU exists for that GPU (e.g. L40S, which is PCIe everywhere — a
    Nebius-L40S-vs-AWS-L40S comparison is then genuinely like-for-like).
    """
    return (_best_price(records, gpu, ct, tiers=tiers, cluster_only=True)
            or _best_price(records, gpu, ct, tiers=tiers))


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
    aws_1yr_au = _best("aws", "H100", {"reserved_1yr"})              # all-upfront standard RI
    aws_3yr_au = _best("aws", "H100", {"reserved_3yr"})             # all-upfront standard RI
    aws_3yr_nu = _best("aws", "H100", {"reserved_3yr_no_upfront"})  # no-upfront standard RI

    parts = []

    # 1yr — compare like terms, but label AWS's prepay structure. AWS's list 1yr RI
    # is ALL-UPFRONT; a no-prepay 1yr exists only as a pricier convertible RI. So
    # "Nebius below AWS list" is true for the all-upfront list, not for what AWS
    # actually charges negotiated accounts (see field-intel line below).
    if neb_1yr and aws_1yr_au:
        d = (neb_1yr - aws_1yr_au) / aws_1yr_au * 100
        s = "+" if d > 0 else ""
        parts.append(f"1yr: Nebius ${neb_1yr:.2f} vs AWS ${aws_1yr_au:.2f} "
                     f"(AWS list, all-upfront) → Nebius {s}{d:.0f}% vs list")
    elif neb_1yr:
        parts.append(f"1yr: Nebius ${neb_1yr:.2f}")

    if neb_2yr:
        parts.append(f"2yr: Nebius ${neb_2yr:.2f} (Nebius's deepest published H100 tier)")

    # 3yr — Nebius has no 3yr H100, so do NOT compare to Nebius on-demand (the old
    # "2.8× difference" line compared committed-vs-on-demand and headlined the
    # all-upfront extreme). Show AWS's prepay structure honestly instead.
    if aws_3yr_nu or aws_3yr_au:
        struct = " / ".join(x for x in (
            f"${aws_3yr_nu:.2f} no-upfront" if aws_3yr_nu else None,
            f"${aws_3yr_au:.2f} 100%-prepaid" if aws_3yr_au else None,
        ) if x)
        note = ""
        if aws_3yr_au and neb_2yr and aws_3yr_au < neb_2yr:
            note = (f" — AWS 3yr all-upfront undercuts Nebius's 2yr ${neb_2yr:.2f}, "
                    f"but locks 3 years + full prepayment")
        parts.append(f"AWS 3yr (no Nebius 3yr): {struct}{note}")

    # Field-intel reality check: negotiated AWS deals can sit below both list and
    # Nebius. Surfaced so sales isn't blindsided by our own "below list" framing.
    best_field = None
    for r in _load_intel(days=90):
        if r.get("gpu_model") != "H100":
            continue
        if "aws" not in (r.get("provider_name", "") + r.get("provider_type", "")).lower():
            continue
        try:
            term = int(float(r.get("term_months", "0") or 0))
            px = float(r.get("price_per_gpu_hour_usd"))
        except (ValueError, TypeError):
            continue
        if 0 < term <= 36 and (best_field is None or px < best_field[0]):
            best_field = (px, term, int(float(r.get("prepay_pct", "0") or 0)))
    if best_field and neb_1yr and best_field[0] < neb_1yr:
        px, term, prepay = best_field
        parts.append(f"⚠ Field intel: AWS {term}mo deal seen at ${px:.2f} "
                     f"({prepay}% prepay) — below Nebius; list comparisons understate "
                     f"AWS's negotiated floor")

    if not parts:
        return ""

    header = "*Committed pricing (H100 benchmark, $/GPU-hr):*"
    return header + "\n" + "\n".join(f"• {p}" for p in parts)


# ---------------------------------------------------------------------------
# Slack message — executive brief
# ---------------------------------------------------------------------------

def format_slack_summary(diffs: List[DiffEntry], run_date: str,
                         confluence_url: str, records: List[PriceRecord] = None,
                         provider_status: dict = None) -> str:
    """
    Short headline digest posted to the channel. Full tables go to a thread
    reply (format_slack_message). Three parts: today's signal (most important
    competitor move), Nebius position in one line each for on-demand and
    watch items, link.
    """
    def _pname(p: str) -> str:
        _KEEP_UPPER = {"aws", "gcp", "gpu", "gmi", "ai"}
        name = p.replace("cp_", "").replace("-", " ")
        return " ".join(w.upper() if w.lower() in _KEEP_UPPER else w.title()
                        for w in name.split())

    lines = [f"*GPU Pricing Daily — {run_date}*"]

    # ── Today's signal: most important grouped price move ────────────────────
    significant = [
        d for d in diffs
        if d.change_type == "price_change"
        and provider_tier(d.provider) in ("raw_gpu_cloud", "hyperscaler",
                                          "enterprise_gpu_cloud")
        and abs(d.delta_pct or 0) >= ALERT_THRESHOLD_PCT
    ]
    if significant:
        from collections import defaultdict as _dd
        groups: Dict[tuple, list] = _dd(list)
        for d in significant:
            interruptible = d.consumption_type in INTERRUPTIBLE_CTS
            direction = "up" if (d.delta_pct or 0) > 0 else "down"
            groups[(d.provider, d.gpu_model, d.consumption_type,
                    interruptible, direction)].append(d)

        # Signal weighting: a CoreWeave or AWS repricing is a market signal; a
        # small provider with a few thousand GPUs moving price is not. Tier-2
        # (hyperscaler + enterprise neocloud) moves outrank small-provider moves
        # regardless of magnitude; spot jitter ranks below list-price changes.
        def _high_signal(prov: str) -> bool:
            # Direct membership test — provider_tier() returns raw_gpu_cloud for
            # providers that appear in both lists (coreweave, lambda, crusoe, ...)
            return (provider_tier(prov) == "hyperscaler"
                    or prov.lower() in PROVIDER_TIERS.get("enterprise_gpu_cloud", []))

        def _signal_rank(kv):
            (prov, gpu, ct, interruptible, _dir), items = kv
            pcts = [abs(d.delta_pct or 0) for d in items]
            return (not _high_signal(prov), interruptible, -statistics.median(pcts))

        ranked = sorted(groups.items(), key=_signal_rank)
        (prov, gpu, ct, interruptible, direction), items = ranked[0]
        pcts = [d.delta_pct or 0 for d in items]
        avg = statistics.mean(pcts)
        verb = "raised" if avg > 0 else "cut"
        best = max(items, key=lambda d: abs(d.delta_pct or 0))
        ct_label = "spot" if interruptible else \
            ct.replace("on_demand", "on-demand").replace("_", " ").replace("reserved 1yr", "reserved")
        sku_note = f" across {len(items)} SKUs" if len(items) > 1 else ""
        if _high_signal(prov):
            lines.append(
                f"\n*Today's signal:* {_pname(prov)} {verb} {gpu} {ct_label} "
                f"{avg:+.0f}%{sku_note} (${best.old_price:.2f}→${best.new_price:.2f})"
            )
        else:
            # Only small-provider moves today — say so instead of promoting one
            lines.append(
                f"\n*Today's signal:* no moves from hyperscalers or major neoclouds — "
                f"largest small-provider move: {_pname(prov)} {gpu} {ct_label} {avg:+.0f}%"
            )
        rest = ranked[1:]
        n_major = sum(1 for (p, *_), _ in rest if _high_signal(p))
        n_small = len(rest) - n_major
        rest_parts = []
        if n_major:
            rest_parts.append(f"{n_major} more major-provider move{'s' if n_major > 1 else ''}")
        if n_small:
            rest_parts.append(f"{n_small} small-provider move{'s' if n_small > 1 else ''}")
        if rest_parts:
            lines[-1] += f" · {' + '.join(rest_parts)} in thread"
    else:
        lines.append("\n*Today's signal:* no significant price moves "
                     f"(≥{ALERT_THRESHOLD_PCT:.0f}%) on tracked providers")

    # ── Position: one line for hyperscalers, one for peers ──────────────────
    if records:
        enrich_comparability(records)  # ensure form_factor tags for cluster-class filtering
        # vs hyperscalers (on-demand) — compare like-for-like cluster SKUs only
        gaps = []
        for gpu in GPU_ORDER:
            neb = next((r for r in records if r.provider == "nebius"
                        and r.gpu_model == gpu and r.consumption_type == "on_demand"), None)
            hyp = _best_comparable(records, gpu, "on_demand", tiers=["hyperscaler"])
            if neb and hyp:
                pct = (hyp.price_per_gpu_hour_usd - neb.price_per_gpu_hour_usd) \
                      / hyp.price_per_gpu_hour_usd * 100
                gaps.append((gpu, pct, hyp))
        # Neutral framing: report where Nebius prices sit, no better/worse language.
        # Lower is not inherently good — premium pricing can reflect product strength;
        # this digest informs pricing decisions in both directions.
        if gaps:
            lo, hi = min(p for _, p, _ in gaps), max(p for _, p, _ in gaps)
            lines.append(f"\n*Position:* Nebius on-demand sits {lo:.0f}–{hi:.0f}% "
                         f"below hyperscaler rack rates")

        # vs peer median (on-demand)
        position = compute_position(records)
        above, at_med, below = [], [], []
        for row in position:
            if row["tier_label"] != "on_demand" or row["nebius_price"] is None:
                continue
            if row["total_peers"] < 2 or row["median_peer"] is None:
                continue
            pct = (row["nebius_price"] - row["median_peer"]) / row["median_peer"] * 100
            if pct >= 3:
                above.append(f"{row['gpu']} +{pct:.0f}%")
            elif pct <= -3:
                below.append(f"{row['gpu']} {pct:.0f}%")
            else:
                at_med.append(row["gpu"])
        peer_parts = []
        if above:
            peer_parts.append(f"premium to peer median on {', '.join(above)}")
        if below:
            peer_parts.append(f"below median on {', '.join(below)}")
        if at_med:
            peer_parts.append(f"at median on {', '.join(at_med)}")
        if peer_parts:
            lines.append("Vs GPU clouds: " + "; ".join(peer_parts))

        # Reference points worth knowing when setting price — neutral observations
        notes = []
        for gpu, pct, hyp in gaps:
            if pct < 5:
                notes.append(f"{gpu} within {pct:.0f}% of {_pname(hyp.provider)} on-demand")
        neb_pre = next((r for r in sorted(records, key=lambda x: x.price_per_gpu_hour_usd)
                        if r.provider == "nebius" and r.gpu_model == "H100"
                        and r.consumption_type in INTERRUPTIBLE_CTS), None)
        hyp_floor = _representative_spot_floor(records, "H100", tiers=["hyperscaler"])
        if neb_pre and hyp_floor:
            hyp_prov, hyp_px, _ = hyp_floor
            rel = (neb_pre.price_per_gpu_hour_usd - hyp_px) / hyp_px * 100
            sign = "+" if rel > 0 else ""
            notes.append(
                f"H100 interruptible: our ${neb_pre.price_per_gpu_hour_usd:.2f} vs "
                f"{_pname(hyp_prov)} spot ${hyp_px:.2f} (median) ({sign}{rel:.0f}%)"
            )
        if notes:
            lines.append("Reference: " + " · ".join(notes))

    lines.append(f"\nFull tables in thread ↓ · <{confluence_url}|Confluence benchmark>")
    return "\n".join(lines)


def format_slack_message(diffs: List[DiffEntry], run_date: str,
                         confluence_url: str, records: List[PriceRecord] = None,
                         provider_status: dict = None) -> str:
    """
    Executive-grade Slack digest framed for CFO / Pricing PM audience.
    Neutral framing — price differences shown as plain +/-% without sentiment.

    Structure:
    1. Nebius position vs enterprise GPU cloud peers (on-demand)
    2. Committed pricing benchmark vs AWS
    3. Significant price moves (>threshold)
    4. Link to full Confluence table
    """
    # The thread reply always carries the full benchmark tables (peers,
    # hyperscalers, spot, committed) — they are reference data, not change data,
    # so we do NOT short-circuit on quiet days. The price-moves section below
    # renders "no significant moves" when nothing crossed the threshold.
    significant = [
        d for d in diffs
        if d.change_type == "price_change"
        and provider_tier(d.provider) in ("raw_gpu_cloud", "hyperscaler",
                                          "enterprise_gpu_cloud")
        and abs(d.delta_pct or 0) >= ALERT_THRESHOLD_PCT
    ]
    minor = [
        d for d in diffs
        if d.change_type == "price_change"
        and provider_tier(d.provider) in ("raw_gpu_cloud", "hyperscaler",
                                          "enterprise_gpu_cloud")
    ]

    lines = [f"*GPU Competitor Pricing — {run_date}*"]

    def _pname(p: str) -> str:
        _KEEP_UPPER = {"aws", "gcp", "gpu", "gmi", "ai"}
        name = p.replace("cp_", "").replace("-", " ")
        return " ".join(w.upper() if w.lower() in _KEEP_UPPER else w.title()
                        for w in name.split())

    if records:
        enrich_comparability(records)  # ensure form_factor tags for cluster-class filtering
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

        # ── 1b. vs hyperscaler rack rate (like-for-like SXM cluster SKUs) ─────
        hyp_rows = []
        entry_notes = []
        for gpu in GPU_ORDER:
            neb_rec = next((r for r in records if r.provider == "nebius"
                            and r.gpu_model == gpu and r.consumption_type == "on_demand"), None)
            hyp_best = _best_comparable(records, gpu, "on_demand", tiers=["hyperscaler"])
            if neb_rec and hyp_best:
                neb_px  = neb_rec.price_per_gpu_hour_usd
                hyp_px  = hyp_best.price_per_gpu_hour_usd
                cheaper_pct = (hyp_px - neb_px) / hyp_px * 100  # positive = Nebius cheaper
                hyp_rows.append((gpu, neb_px, hyp_best.provider, hyp_px, cheaper_pct))
                # Transparency: if a hyperscaler's cheapest ENTRY (non-cluster) SKU
                # undercuts its cluster price, surface it labeled — never hide it,
                # but never let it be the headline comparison either.
                entry = _best_price(records, gpu, "on_demand", tiers=["hyperscaler"])
                if entry and is_cluster_class(entry) is False and \
                   entry.price_per_gpu_hour_usd < hyp_px * 0.97:
                    entry_notes.append(
                        f"`{gpu:<5}` {_pname(entry.provider)} entry SKU "
                        f"${entry.price_per_gpu_hour_usd:.2f} ({entry.form_factor}, "
                        f"{entry.gpu_count}×, not a cluster)"
                    )

        if hyp_rows:
            lines.append("\n*vs cheapest hyperscaler on-demand (8×SXM cluster, like-for-like):*")
            for gpu, neb_px, hyp_prov, hyp_px, cheaper_pct in hyp_rows:
                flag = "  — near parity" if cheaper_pct < 5 else ""
                lines.append(
                    f"`{gpu:<5}` Nebius ${neb_px:.2f}  vs  {_pname(hyp_prov)} ${hyp_px:.2f}"
                    f"  →  Nebius {cheaper_pct:.0f}% below{flag}"
                )
            if entry_notes:
                lines.append("_Cheaper non-cluster entry SKUs (single-GPU NVL/PCIe, "
                             "not comparable to an SXM cluster):_")
                lines.extend(entry_notes)

        # ── 1c. spot/preemptible floor vs hyperscaler spot ───────────────────
        # The on-demand story flips at the spot tier: hyperscaler spot floors
        # often undercut Nebius preemptible. Show it so the digest isn't one-sided.
        spot_rows = []
        for gpu in GPU_ORDER:
            neb_candidates = [r for r in records if r.provider == "nebius"
                              and r.gpu_model == gpu
                              and r.consumption_type in INTERRUPTIBLE_CTS]
            neb_rec = min(neb_candidates, key=lambda r: r.price_per_gpu_hour_usd) \
                if neb_candidates else None
            floor = _representative_spot_floor(records, gpu, tiers=["hyperscaler"])
            if neb_rec and floor:
                spot_rows.append((gpu, neb_rec.price_per_gpu_hour_usd,
                                  floor[0], floor[1], floor[2]))
            elif neb_rec:
                spot_rows.append((gpu, neb_rec.price_per_gpu_hour_usd, None, None, None))

        if spot_rows:
            lines.append("\n*Spot / preemptible (vs cheapest hyperscaler spot, median across zones):*")
            for gpu, neb_px, hyp_prov, hyp_px, n_zones in spot_rows:
                if hyp_px is None:
                    lines.append(f"`{gpu:<5}` Nebius ${neb_px:.2f}  |  no hyperscaler spot published")
                    continue
                delta_pct = (neb_px - hyp_px) / hyp_px * 100  # positive = Nebius pricier
                pos = f"Nebius {delta_pct:.0f}% above" if delta_pct > 0 else f"Nebius {-delta_pct:.0f}% below"
                lines.append(
                    f"`{gpu:<5}` Nebius ${neb_px:.2f}  vs  {_pname(hyp_prov)} ${hyp_px:.2f}"
                    f" (median, {n_zones} zones)  →  {pos}"
                )
            lines.append("_Hyperscaler spot is interruptible, capacity not guaranteed; floors are "
                         "the cheapest provider's median across zones (single-zone dips excluded)._")

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
    # (significant and minor are already computed at the top of this function)

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
            _KEEP_UPPER = {"aws", "gcp", "gpu", "gmi", "ai"}
            name = p.replace("cp_", "").replace("-", " ")
            return " ".join(w.upper() if w.lower() in _KEEP_UPPER else w.title()
                            for w in name.split())

        lines.append(f"\n*Price moves ≥{ALERT_THRESHOLD_PCT:.0f}%:*")
        for (prov, gpu, bucket, direction), items in sorted_groups[:15]:
            arrow = "🔺" if direction == "up" else "🔻"
            pcts = [d.delta_pct or 0 for d in items]
            avg_pct = statistics.mean(pcts)
            sign = "+" if avg_pct > 0 else ""
            tier_tag = (" _(hyperscaler)_" if provider_tier(prov) == "hyperscaler"
                        else " _(major neocloud)_"
                        if prov.lower() in PROVIDER_TIERS.get("enterprise_gpu_cloud", [])
                        else " _(small provider)_")
            sku_count = len(set((d.instance_type, d.region) for d in items))
            # Most impactful SKU (largest % change)
            best = max(items, key=lambda d: abs(d.delta_pct or 0))
            best_pct = best.delta_pct or 0
            best_sign = "+" if best_pct > 0 else ""
            if sku_count > 1:
                # Show avg% as headline; call out worst-case SKU separately so the
                # two numbers are self-consistent (old→new % matches the printed %).
                # Previously "$2.42→$4.50 (+11.8%)" was confusing because the example
                # SKU was +86% but the label showed the average.
                lines.append(
                    f"{arrow} *{_display_prov(prov)}*{tier_tag} {gpu} {bucket}: "
                    f"{sign}{avg_pct:.1f}% avg across {sku_count} SKUs "
                    f"(peak: ${best.old_price:.2f}→${best.new_price:.2f} {best.region}, "
                    f"{best_sign}{best_pct:.1f}%)"
                )
            else:
                lines.append(
                    f"{arrow} *{_display_prov(prov)}*{tier_tag} {gpu} {bucket}: "
                    f"${best.old_price:.2f}→${best.new_price:.2f}/GPU-hr "
                    f"({best_sign}{best_pct:.1f}% {items[0].region})"
                )
        if len(sorted_groups) > 15:
            lines.append(f"_…and {len(sorted_groups) - 15} more provider/GPU groups_")
    else:
        # Minor changes only (below alert threshold) — note them briefly
        lines.append(f"\n_No significant price moves today "
                     f"({len(minor)} minor changes below {ALERT_THRESHOLD_PCT:.0f}% threshold)._")

    lines.append(f"\nFull benchmark table: {confluence_url}")

    # ── Data freshness footer ─────────────────────────────────────────────────
    # Show which providers used live data vs cache/fallback, with cache age.
    # Only shown when at least one provider is non-live — clean runs stay quiet.
    if provider_status:
        non_live = {
            p: s for p, s in provider_status.items()
            if s.get("status") not in ("live",) and s.get("record_count", 0) > 0
        }
        missing = {
            p: s for p, s in provider_status.items()
            if s.get("status") == "missing"
        }
        if non_live or missing:
            parts = []
            for p, s in sorted(non_live.items()):
                status = s.get("status", "?")
                age = s.get("cache_age_hours")
                if status == "cache":
                    age_str = f" ({age:.0f}h ago)" if age is not None else ""
                    parts.append(f"{p} cached{age_str}")
                elif status == "fallback":
                    src = s.get("fallback_source", "fallback")
                    parts.append(f"{p} via {src}")
                elif status == "missing":
                    parts.append(f"{p} no data")
            for p in sorted(missing):
                if p not in non_live:
                    parts.append(f"{p} no data")
            if parts:
                lines.append(f"_Data freshness: {' · '.join(parts)}_")

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


def _load_intel(days: int = 60) -> List[Dict]:
    """
    Load recent rows from intel.csv, deduplicated (Phase 1.8). Returns [] if file
    missing or empty. Collapses near-identical quotes (the same deal logged twice
    with slightly different notes — e.g. the AWS $1.80 232-GPU deal appearing as
    both '...cluster' and '...deal') by content key: date + gpu + rounded price +
    term + prepay + provider. Keeps the first occurrence.
    """
    if not INTEL_CSV.exists():
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = []
    seen = set()
    try:
        with open(INTEL_CSV, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("message_date", "") < cutoff:
                    continue
                try:
                    px_key = round(float(row.get("price_per_gpu_hour_usd", "")), 2)
                except (ValueError, TypeError):
                    px_key = row.get("price_per_gpu_hour_usd", "")
                key = (
                    row.get("message_date", ""),
                    (row.get("gpu_model", "") or "").upper(),
                    px_key,
                    str(row.get("term_months", "")),
                    str(row.get("prepay_pct", "")),
                    (row.get("provider_name", "") or "").strip().lower(),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    except Exception:
        pass
    return rows


def _build_field_intel_callout(records: List[PriceRecord]) -> str:
    """
    HTML section: recent field intel quotes from #price-intelligence vs Nebius pricing.
    Shows deal-specific quotes (committed terms, volume discounts) flagging material gaps.
    """
    intel_rows = _load_intel(days=60)
    if not intel_rows:
        return ""

    # Nebius lookup helpers
    def _neb_on_demand(gpu: str) -> Optional[float]:
        recs = [r for r in records if r.provider == "nebius"
                and r.gpu_model == gpu and r.consumption_type == "on_demand"]
        return min((r.price_per_gpu_hour_usd for r in recs), default=None)

    def _neb_committed(gpu: str, term_months: int) -> Optional[float]:
        """Best Nebius committed price at the closest available tier."""
        if term_months <= 0:
            return _neb_on_demand(gpu)
        if term_months <= 10:
            cts = {"committed_9mo"}
        elif term_months <= 15:
            cts = {"reserved_1yr", "committed_1yr"}
        elif term_months <= 21:
            cts = {"committed_18mo"}
        elif term_months <= 30:
            cts = {"committed_2yr", "reserved_2yr"}
        else:
            cts = {"reserved_3yr", "committed_3yr"}
        recs = [r for r in records if r.provider == "nebius"
                and r.gpu_model == gpu and r.consumption_type in cts]
        return min((r.price_per_gpu_hour_usd for r in recs), default=None)

    def _term_str(months: int) -> str:
        if months <= 0:   return "On-demand"
        if months == 1:   return "Monthly"
        if months < 12:   return f"{months}mo"
        yrs = months // 12
        rem = months % 12
        return f"{yrs}yr" + (f" {rem}mo" if rem else "")

    def _gap_cell(comp_px: float, neb_px: Optional[float]) -> str:
        if neb_px is None:
            return "<td>—</td>"
        gap = (comp_px - neb_px) / neb_px * 100  # negative = competitor cheaper
        label = f"{gap:+.0f}% vs Nebius ${neb_px:.2f}"
        if gap < -15:
            return f'<td><span data-type="status" data-color="red">{label}</span></td>'
        if gap < -5:
            return f'<td><span data-type="status" data-color="yellow">{label}</span></td>'
        return f"<td>{label}</td>"

    # Group by GPU, sorted by date desc
    by_gpu: Dict[str, list] = defaultdict(list)
    for row in intel_rows:
        # skip Nebius own quotes from the comparison table
        if row.get("provider_type") == "nebius":
            continue
        by_gpu[row["gpu_model"]].append(row)

    html = []
    html.append('<h2>Field Intelligence — Recent Market Quotes</h2>')
    html.append(
        '<p><em>Sourced from Nebius sales team reports in <strong>#price-intelligence</strong>. '
        'These are deal-specific quotes reflecting volume, relationship, and timing — '
        'not public rack rates. Customer names removed. '
        '"vs Nebius" compares against Nebius committed pricing at the closest matching term.</em></p>'
    )

    has_data = False
    for gpu in GPU_ORDER:
        rows = sorted(by_gpu.get(gpu, []), key=lambda r: r["message_date"], reverse=True)
        if not rows:
            continue
        has_data = True

        html.append(f"<h3>{gpu}</h3>")
        html.append(
            "<table><thead><tr>"
            "<th>Date</th><th>Provider</th><th>$/GPU-hr</th>"
            "<th>Term</th><th>Prepay</th><th>vs Nebius</th><th>Context</th>"
            "</tr></thead><tbody>"
        )

        for row in rows[:12]:
            price    = float(row["price_per_gpu_hour_usd"])
            term     = int(row.get("term_months", 0))
            prepay   = int(row.get("prepay_pct", 0))
            provider = row.get("provider_name", "Unknown")
            notes    = row.get("notes", "")
            dt       = row.get("message_date", "")
            prepay_str = f"{prepay}% upfront" if prepay > 0 else "0% / monthly"
            neb_px   = _neb_committed(gpu, term)
            html.append(
                f"<tr>"
                f"<td>{dt}</td>"
                f"<td>{provider}</td>"
                f"<td><strong>${price:.2f}</strong></td>"
                f"<td>{_term_str(term)}</td>"
                f"<td>{prepay_str}</td>"
                + _gap_cell(price, neb_px) +
                f"<td><em>{notes}</em></td>"
                f"</tr>"
            )

        html.append("</tbody></table>")

    if not has_data:
        return ""

    return "\n".join(html)


def _run_health_line(provider_status: dict) -> str:
    """
    Run-health banner (Phase 1.10): X/Y sources live, which are stale and how old.
    Honest about freshness so 'daily refreshed' isn't read as 'all live today'.
    """
    if not provider_status:
        return ""
    total = len(provider_status)
    live = [p for p, s in provider_status.items() if s.get("status") == "live"]
    stale = []
    for p, s in sorted(provider_status.items()):
        st = s.get("status")
        if st == "live":
            continue
        age = s.get("cache_age_hours")
        if st == "cache":
            stale.append(f"{p} cached {age:.0f}h" if age is not None else f"{p} cached")
        elif st == "fallback":
            stale.append(f"{p} {s.get('fallback_source', 'fallback')}")
        elif st == "missing":
            stale.append(f"{p} no data")
        elif st == "error":
            stale.append(f"{p} error")
    color = "green" if len(live) == total else ("yellow" if live else "red")
    banner = (f'<span data-type="status" data-color="{color}">'
              f'{len(live)}/{total} sources live</span>')
    detail = f' — stale: {", ".join(stale)}' if stale else " — all sources live this run"
    return f'<p><em>Data freshness: </em>{banner}<em>{detail}</em></p>'


def _market_trend(gpu: str, days: int, records: List[PriceRecord]):
    """
    Trend of today's cheapest enterprise-peer's OWN on-demand price over up to `days`.
    Returns (pct_change, span_days) or None.

    We track the current floor provider's own price path rather than min-across-peers,
    because the latter is contaminated by coverage backfill — early history captured
    fewer peers, so "cheapest peer" drops as the pipeline adds providers, not because
    the market moved. Tracking one provider's series isolates real price movement, and
    we label the actual span (history is only ~1 month old, so a true 30d isn't always
    available; <5 clean days → None so the cell reads "building").
    """
    if not HISTORY_CSV.exists():
        return None
    ent = set(PROVIDER_TIERS.get("enterprise_gpu_cloud", []))
    cands = [r for r in records if r.gpu_model == gpu and r.consumption_type == "on_demand"
             and r.provider in ent]
    if not cands:
        return None
    prov = min(cands, key=lambda r: r.price_per_gpu_hour_usd).provider
    series: Dict[str, float] = {}
    try:
        with open(HISTORY_CSV, newline="") as f:
            for r in csv.DictReader(f):
                if (r.get("provider") == prov and r.get("gpu_model") == gpu
                        and r.get("consumption_type") == "on_demand"):
                    try:
                        series[r["snapshot_date"]] = float(r["price_per_gpu_hour_usd"])
                    except (ValueError, KeyError):
                        pass
    except Exception:
        return None
    if len(series) < 2:
        return None
    dates = sorted(series)
    latest = dates[-1]
    target = date.fromisoformat(latest) - timedelta(days=days)
    prior = [d for d in dates if date.fromisoformat(d) <= target]
    base_date = prior[-1] if prior else dates[0]   # earliest available if window not full
    span = (date.fromisoformat(latest) - date.fromisoformat(base_date)).days
    if span < 5 or series[base_date] <= 0:
        return None
    return (series[latest] - series[base_date]) / series[base_date] * 100, span


def _trend_cell(gpu: str, records: List[PriceRecord]) -> str:
    """Market-trend cell labeled with the real span (history is <90d old)."""
    t = _market_trend(gpu, 30, records)
    if t is None:
        return '<td><em>building</em></td>'
    pct, span = t
    sign = "+" if pct >= 0 else ""
    color = "yellow" if abs(pct) >= 5 else "green"
    return f'<td><span data-type="status" data-color="{color}">{sign}{pct:.0f}% / {span}d</span></td>'


def _field_intel_floor(gpu: str):
    """
    Lowest real competitive deal for a GPU from #price-intelligence (intel.csv).
    This is the ground-truth signal for next-gen GPUs (B200/B300/GB200/GB300) where
    public list prices barely exist. Returns {price, term, label, prepay, is_loss} or None.
    is_loss = the quote was logged as a competitive loss/win against Nebius.
    """
    def _is_loss(notes: str) -> bool:
        n = (notes or "").lower()
        return "vs ne" in n or "win vs" in n or "lost" in n or "loss" in n

    best = None
    any_loss = False
    for r in _load_intel(days=90):
        if r.get("gpu_model") != gpu:
            continue
        try:
            px = float(r["price_per_gpu_hour_usd"])
        except (ValueError, KeyError, TypeError):
            continue
        if px <= 0:
            continue
        if _is_loss(r.get("notes", "")):
            any_loss = True
        if best is None or px < best["price"]:
            try:
                term = int(float(r.get("term_months", "0") or 0))
            except (ValueError, TypeError):
                term = 0
            best = {
                "price": px, "term": term,
                "label": (r.get("provider_name") or r.get("provider_type") or "undisclosed"),
            }
    if best is not None:
        best["is_loss"] = any_loss  # any recorded loss for this GPU, not just the cheapest quote
    return best


def _recommended_action(delta_vs_median: Optional[float], near_hyperscaler: bool,
                        trend30: Optional[float], field_loss: bool = False) -> str:
    """Neutral, decision-oriented action. Lower is not assumed good; a premium is a
    valid position to hold. Phrasing prompts a decision, doesn't prescribe a cut.
    A recorded competitive LOSS is the strongest trigger and leads the action."""
    if field_loss:
        return "Lost a deal at the field price — review committed pricing for this SKU"
    if delta_vs_median is None:
        primary = "Establish peer benchmark"
    elif delta_vs_median >= 15:
        primary = "Review premium vs value"
    elif delta_vs_median <= -5:
        primary = "Headroom to hold or raise"
    else:
        primary = "Hold; monitor"
    mods = []
    if near_hyperscaler:
        mods.append("watch hyperscaler parity")
    if trend30 is not None and trend30 <= -5:
        mods.append("market softening")
    elif trend30 is not None and trend30 >= 5:
        mods.append("market firming")
    return primary + (f" ({'; '.join(mods)})" if mods else "")


def _term_label(term: int) -> str:
    if not term:
        return "on-demand"
    if term % 12 == 0:
        return f"{term // 12}yr"
    return f"{term}mo"


def _build_decision_trigger_table(records: List[PriceRecord]) -> str:
    """
    Phase 3.2: the actionable core. One row per GPU with the competitive position,
    30d market trend, and a recommended action + owner/review/margin columns.
    Owner/review/margin are operational placeholders for the pricing team to fill;
    margin is never invented (cost data is out of this pipeline's scope).
    """
    position = {row["gpu"]: row for row in compute_position(records)
                if row["tier_label"] == "on_demand"}
    if not position:
        return ""

    html = [
        '<h2>Decision Triggers — Pricing / Finance</h2>',
        '<p>Per-GPU competitive position with a recommended action. '
        '<strong>Neutral framing:</strong> a premium to the market can be a deliberate, '
        'defensible position; these triggers prompt a pricing decision, they do not assume '
        'lower is better. Owner / review-by / margin-risk are for the pricing team to fill '
        '(margin/cost data is out of this tool\'s scope and never auto-populated).</p>',
        '<table data-layout="full-width"><tbody>',
        '<tr><th>GPU</th><th>Nebius OD</th><th>vs peer median</th><th>Cheapest peer</th>'
        '<th>Cheapest hyperscaler (SXM cluster)</th><th>Competitor field deal</th><th>Market 30d</th>'
        '<th>Recommended action</th><th>Owner</th><th>Review by</th><th>Margin risk</th></tr>',
    ]

    for gpu in GPU_ORDER:
        row = position.get(gpu)
        if not row or row.get("nebius_price") is None:
            continue
        neb = row["nebius_price"]
        median = row.get("median_peer")
        delta = ((neb - median) / median * 100) if median else None

        detail = row.get("cheapest_peers_detail") or []
        floor_n, floor_p = detail[0] if detail else (None, None)
        floor_cell = (f'${floor_p:.2f} <em>({_provider_display(floor_n)})</em>'
                      if floor_p is not None else '—')

        hyp = _best_comparable(records, gpu, "on_demand", tiers=["hyperscaler"])
        near_hyp = False
        if hyp:
            hyp_cell = f'${hyp.price_per_gpu_hour_usd:.2f} <em>({_provider_display(hyp.provider)})</em>'
            near_hyp = (hyp.price_per_gpu_hour_usd - neb) / hyp.price_per_gpu_hour_usd * 100 < 5
        else:
            hyp_cell = '—'

        if delta is None:
            vs_cell = '<td>—</td>'
        else:
            c = "red" if delta > 15 else ("yellow" if delta > 0 else "green")
            s = "+" if delta >= 0 else ""
            vs_cell = f'<td><span data-type="status" data-color="{c}">{s}{delta:.0f}%</span></td>'

        # Competitor field deal (real negotiated quote from #price-intelligence) —
        # the only real signal for next-gen GPUs where public list prices barely exist.
        fi = _field_intel_floor(gpu)
        if fi:
            loss_badge = (' <span data-type="status" data-color="red">lost deal</span>'
                          if fi["is_loss"] else '')
            field_cell = (f'${fi["price"]:.2f} <em>({_term_label(fi["term"])}, '
                          f'{_provider_display(fi["label"]) if fi["label"] not in ("undisclosed",) else fi["label"]})</em>{loss_badge}')
        else:
            field_cell = '—'

        _t = _market_trend(gpu, 30, records)
        t30 = _t[0] if _t else None
        action = _recommended_action(delta, near_hyp, t30, field_loss=bool(fi and fi["is_loss"]))

        html.append(
            f'<tr><td><strong>{gpu}</strong></td>'
            f'<td>${neb:.2f}</td>'
            f'{vs_cell}'
            f'<td>{floor_cell}</td>'
            f'<td>{hyp_cell}</td>'
            f'<td>{field_cell}</td>'
            f'{_trend_cell(gpu, records)}'
            f'<td>{action}</td>'
            f'<td><em>Pricing PM</em></td>'
            f'<td>—</td>'
            f'<td>—</td></tr>'
        )

    html.append('</tbody></table>')
    html.append('<p><em>Market 30d = change in the cheapest enterprise-peer on-demand '
                'price over the last 30 days (90d trend appears once ≥90 days of history '
                'accrues). "Cheapest hyperscaler" is the like-for-like 8×SXM cluster SKU. '
                '"Competitor field deal" is the lowest real negotiated quote from '
                '#price-intelligence (term shown; may be committed, not on-demand) — the '
                'primary signal for next-gen GPUs where public list prices are sparse.</em></p>')
    return "\n".join(html)


def _cheapest(records, provider, gpu, cts) -> Optional[float]:
    ps = [r.price_per_gpu_hour_usd for r in records
          if r.provider == provider and r.gpu_model == gpu and r.consumption_type in cts]
    return min(ps) if ps else None


def _build_tldr(records: List[PriceRecord]) -> str:
    """Phase 3.1: top-of-page per-stakeholder readout (computed from live data)."""
    pos = {r["gpu"]: r for r in compute_position(records) if r["tier_label"] == "on_demand"}
    gaps = []
    for gpu in GPU_ORDER:
        row = pos.get(gpu)
        neb = row["nebius_price"] if row else None
        hyp = _best_comparable(records, gpu, "on_demand", tiers=["hyperscaler"])
        if neb and hyp:
            gaps.append((hyp.price_per_gpu_hour_usd - neb) / hyp.price_per_gpu_hour_usd * 100)
    gap_lo, gap_hi = (min(gaps), max(gaps)) if gaps else (0, 0)
    h100 = pos.get("H100")
    h100_prem = (((h100["nebius_price"] - h100["median_peer"]) / h100["median_peer"] * 100)
                 if h100 and h100.get("median_peer") else None)
    neb1 = _cheapest(records, "nebius", "H100", RESERVED_1YR_CTS)
    aws1 = _cheapest(records, "aws", "H100", {"reserved_1yr"})

    fin = f"Committed: Nebius H100 1yr ${neb1:.2f}" if neb1 else "Committed: see table"
    if neb1 and aws1:
        fin += f" ({(neb1 - aws1) / aws1 * 100:+.0f}% vs AWS list, all-upfront)"
    fin += "; AWS negotiated 1yr deals seen at $1.80 (field intel)."
    payg = f"On-demand sits {gap_lo:.0f}–{gap_hi:.0f}% below hyperscaler SXM clusters"
    if h100_prem is not None:
        payg += f"; H100 {h100_prem:+.0f}% vs peer median (premium is a position, not a problem)"
    payg += "."
    cap = ("Nebius B300 is UK-private (sales-gated); GB200/GB300 are contact-sales. "
           "Market broadly capacity-constrained (on-demand reportedly sold out across GPU types, Apr 2026).")
    sales = ("Strong vs hyperscaler rack rates and ~49% below Oracle B200. Watch: AWS 3yr all-upfront "
             "and negotiated 1yr deals, plus hyperscaler spot floors below our preemptible.")

    rows = [
        '<h2>TL;DR by Stakeholder</h2>',
        '<table data-layout="full-width"><tbody>',
        '<tr><th>For</th><th>Today\'s read</th><th>Detail in</th></tr>',
        f'<tr><td><strong>Finance</strong></td><td>{fin}</td><td>Committed Pricing</td></tr>',
        f'<tr><td><strong>PAYG Product</strong></td><td>{payg}</td><td>Decision Triggers + Product Gaps</td></tr>',
        f'<tr><td><strong>Capacity</strong></td><td>{cap}</td><td>Availability</td></tr>',
        f'<tr><td><strong>Sales</strong></td><td>{sales}</td><td>Battlecards</td></tr>',
        '</tbody></table>',
    ]
    return "\n".join(rows)


def _build_payg_gap_table(records: List[PriceRecord]) -> str:
    """
    Phase 3.3: pricing-MODEL coverage — where competitors offer a consumption model
    Nebius lacks. This is a product-model overview (does provider X offer model Y),
    not a live price feed; specifics should be validated before external use.
    """
    cols = ["Model", "Nebius", "AWS", "GCP", "Azure", "CoreWeave"]
    rows = [
        ("On-demand", "Yes", "Yes", "Yes", "Yes", "Yes"),
        ("Spot / preemptible", "Yes", "Yes", "Yes", "Yes", "Yes"),
        ("Bid / max-price spot", "No", "Yes", "No", "Yes", "—"),
        ("Capacity blocks (short-term guaranteed)", "No", "Yes", "Yes (DWS)", "Yes (cap. res.)", "—"),
        ("Committed reserved (1–3yr)", "Yes (≤2–3yr)", "Yes", "Yes (CUD)", "Yes", "Yes"),
        ("Flexible savings plan (spend commit)", "No", "Yes", "No", "No", "—"),
        ("Short-term cluster (days–weeks)", "Partial", "No", "No", "No", "Yes"),
    ]
    html = [
        '<h2>PAYG Product-Model Gaps</h2>',
        '<p>Consumption models offered per provider. <strong>Nebius gaps</strong> (models '
        'competitors sell that Nebius does not) are highlighted — these are product '
        'opportunities, not price gaps. Product-model overview; validate specifics before '
        'external use. "—" = not confirmed.</p>',
        '<table data-layout="full-width"><tbody>',
        '<tr>' + "".join(f'<th>{c}</th>' for c in cols) + '</tr>',
    ]
    for model, neb, aws, gcp, az, cw in rows:
        neb_cell = (f'<td><span data-type="status" data-color="red">{neb}</span></td>'
                    if neb == "No" else f'<td>{neb}</td>')
        html.append(f'<tr><td><strong>{model}</strong></td>{neb_cell}'
                    f'<td>{aws}</td><td>{gcp}</td><td>{az}</td><td>{cw}</td></tr>')
    html.append('</tbody></table>')
    html.append('<p><em>Actionable gaps: Nebius has no bid/max-price spot, no short-term '
                'capacity-block product, and no flexible spend-commit savings plan — each is a '
                'model competitors use to capture price-sensitive or capacity-anxious demand.</em></p>')
    return "\n".join(html)


def _build_battlecards(records: List[PriceRecord]) -> str:
    """Phase 3.5: per-objection sales battlecards with reconciled numbers + talk track."""
    aws3nu = _cheapest(records, "aws", "H100", {"reserved_3yr_no_upfront"})
    aws3au = _cheapest(records, "aws", "H100", {"reserved_3yr"})
    neb2 = _cheapest(records, "nebius", "H100", RESERVED_2YR_CTS)
    azspot = _cheapest(records, "azure", "H100", {"spot"})
    nebpre = _cheapest(records, "nebius", "H100", INTERRUPTIBLE_CTS)
    nebl = _cheapest(records, "nebius", "L40S", {"on_demand"})
    awsl = _cheapest(records, "aws", "L40S", {"on_demand"})
    awsl3 = _cheapest(records, "aws", "L40S", {"reserved_3yr"})
    orab = _cheapest(records, "oracle", "B200", {"on_demand"}) or _cheapest(records, "cp_oracle", "B200", {"on_demand"})
    nebb = _cheapest(records, "nebius", "B200", {"on_demand"})

    cards = []
    if (aws3nu or aws3au) and neb2:
        parts = []
        if aws3nu:
            parts.append(f"${aws3nu:.2f} no-upfront")
        if aws3au:
            parts.append(f"${aws3au:.2f} 100%-prepaid")
        cards.append((
            '"AWS 3-year is cheaper"',
            f"True at the extreme: AWS H100 3yr is {' / '.join(parts)}. But that locks 3 years"
            + (" and full prepayment" if aws3au else "")
            + f". Nebius 2yr ${neb2:.2f} needs no 3rd-year lock or 100% upfront, and on-demand has no "
            f"commitment at all. Sell flexibility, not the headline rate.", "high"))
    if azspot and nebpre:
        cards.append((
            '"Azure spot is cheaper"',
            f"Azure H100 spot (${azspot:.2f}) is interruptible, no capacity guarantee, single best region. "
            f"Nebius preemptible is ${nebpre:.2f}; for production training the relevant comparison is our "
            f"guaranteed on-demand/committed capacity, not scavenger spot.", "high"))
    if nebl and awsl:
        c = f"Nebius L40S ${nebl:.2f} vs AWS ${awsl:.2f} on-demand — only {((awsl - nebl) / awsl * 100):.0f}% apart, our one near-parity SKU."
        if awsl3:
            c += (f" AWS L40S falls to ~${awsl3:.2f} at 3yr committed — a gap we can't match (no Nebius "
                  f"committed L40S). Acknowledge it; pivot to flexibility and bundled value.")
        cards.append(('"L40S is near AWS parity"', c, "high"))
    if orab and nebb:
        cards.append((
            '"Oracle\'s B200 undercuts us"',
            f"Not on list: Oracle B200 on-demand is ${orab:.2f} vs Nebius ${nebb:.2f} — we are "
            f"{((orab - nebb) / orab * 100):.0f}% BELOW Oracle. A lower Oracle number is a negotiated/committed "
            f"deal; ask for the term + prepay to compare like-for-like.", "med"))

    if not cards:
        return ""
    html = [
        '<h2>Sales Battlecards</h2>',
        '<p>Reconciled numbers and approved talk tracks for common objections. Customer names omitted. '
        'Neutral framing: where a competitor genuinely wins (e.g. L40S 3yr), we acknowledge and pivot.</p>',
        '<table data-layout="full-width"><tbody>',
        '<tr><th>Objection</th><th>Response (with the number)</th><th>Confidence</th></tr>',
    ]
    for obj, resp, conf in cards:
        color = {"high": "green", "med": "yellow", "low": "red"}.get(conf, "yellow")
        html.append(f'<tr><td><strong>{obj}</strong></td><td>{resp}</td>'
                    f'<td><span data-type="status" data-color="{color}">{conf}</span></td></tr>')
    html.append('</tbody></table>')
    return "\n".join(html)


def _build_availability_note(records: List[PriceRecord]) -> str:
    """
    Phase 3.4: capacity/availability signal per Nebius SKU (from verified region data).
    A low competitor price isn't actionable if capacity is unavailable. We only have
    authoritative availability for our own SKUs; competitor real-time availability has
    no public feed, so we don't assert it.
    """
    avail = [
        ("H100", "Available", "green", "eu-north1 (Finland) only"),
        ("H200", "Available", "green", "eu-north1, eu-north2, eu-west1, us-central1"),
        ("B200", "Available", "green", "us-central1, me-west1 (no EU region)"),
        ("B300", "Sales-gated", "yellow", "uk-south1 private region (existing deployments only)"),
        ("L40S", "Available", "green", "eu-north1"),
        ("GB200", "Contact sales", "yellow", "no public on-demand rate"),
        ("GB300", "Contact sales", "yellow", "no public on-demand rate"),
    ]
    html = [
        '<h2>Availability &amp; Access</h2>',
        '<p>Capacity signal by Nebius SKU (from official region data). A cheaper competitor '
        'quote is not actionable if the capacity is unavailable. Competitor real-time '
        'availability is not tracked here (no public feed).</p>',
        '<table data-layout="full-width"><tbody>',
        '<tr><th>GPU</th><th>Nebius availability</th><th>Where</th></tr>',
    ]
    for gpu, status, color, where in avail:
        html.append(f'<tr><td><strong>{gpu}</strong></td>'
                    f'<td><span data-type="status" data-color="{color}">{status}</span></td>'
                    f'<td>{where}</td></tr>')
    html.append('</tbody></table>')
    html.append('<p><em>Market context: SemiAnalysis reported on-demand GPU capacity sold out '
                'across all GPU types as of April 2026 — list prices currently reflect scarcity, '
                'so a cheaper competitor quote may not come with available capacity.</em></p>')
    return "\n".join(html)


def format_confluence_table(records: List[PriceRecord], run_date: str,
                            provider_status: dict = None) -> str:
    html = []
    if records:
        enrich_comparability(records)  # form_factor tags for cluster-class filtering

    html.append(
        f'<p><em>Last updated: {run_date}</em> — <strong>daily refreshed</strong> '
        f'(point-in-time snapshot, not real-time). '
        f'All prices in <strong>$/GPU-hr</strong>. '
        f'Source: direct provider APIs/pages + '
        f'<a href="https://computeprices.com">ComputePrices.com</a>.</p>'
    )
    rh = _run_health_line(provider_status)
    if rh:
        html.append(rh)

    # ── Section 0: TL;DR by stakeholder (Phase 3.1) ─────────────────────────
    if records:
        html.append(_build_tldr(records))

    # ── Section 1: Executive benchmark ──────────────────────────────────────
    html.append('<h2>Executive Benchmark — Nebius vs Market</h2>')
    html.append(
        '<p>On-demand prices, cheapest available per provider. '
        '<strong>Enterprise GPU cloud</strong> peers are the direct competitive set '
        '(named providers with enterprise SLAs; commodity rental marketplaces excluded). '
        'Hyperscaler column shows rack-rate list price — enterprise customers pay 40–57% less at 3yr committed. '
        'Nebius on-demand prices are uniform across regions (no US discount); availability by GPU: '
        'H100 eu-north1 only, H200 EU + us-central1, B200 us-central1 + me-west1, B300 uk-south1 (private).</p>'
    )
    html.append(_build_executive_table(records))

    # ── Section 1b: Decision triggers (actionable core, Phase 3.2) ──────────
    dt = _build_decision_trigger_table(records)
    if dt:
        html.append(dt)

    # ── Section 1c: PAYG product-model gaps (Phase 3.3) ─────────────────────
    if records:
        html.append(_build_payg_gap_table(records))

    # ── Section 1d: Availability & access (Phase 3.4) ───────────────────────
    if records:
        html.append(_build_availability_note(records))

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
    html.append(_build_capacity_block_section(records))
    html.append(_build_committed_implication(records))

    # ── Section 2b: Sales battlecards (Phase 3.5) ───────────────────────────
    if records:
        bc = _build_battlecards(records)
        if bc:
            html.append(bc)

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

    # ── Section 5: Field intelligence ───────────────────────────────────────
    intel_html = _build_field_intel_callout(records)
    if intel_html:
        html.append(intel_html)

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

        # Cheapest hyperscaler on-demand — like-for-like 8×SXM cluster SKU only
        # (excludes single-GPU NVL/PCIe entry SKUs such as Azure NC40ads).
        hyp_best = _best_comparable(records, gpu, "on_demand", tiers=["hyperscaler"])
        if hyp_best:
            # Directional badge (1.7): aggregator-sourced cells aren't provider-verified.
            badge = (' <span data-type="status" data-color="yellow">directional</span>'
                     if getattr(hyp_best, "source_type", "") == "aggregator" else '')
            hyp_td = (f'<td>${hyp_best.price_per_gpu_hour_usd:.2f} '
                      f'<em>({_provider_display(hyp_best.provider)}, {hyp_best.form_factor})</em>{badge}</td>')
        else:
            hyp_td = '<td>—</td>'

        med_td = f'<td>${row["median_peer"]:.2f}</td>' if row and row["median_peer"] else '<td>—</td>'
        count_td = f'<td>{row["total_peers"] + 1 if row else 0}</td>'  # +1 for Nebius

        rows.append(
            f'<tr><td><strong>{gpu}</strong></td>'
            f'{nebius_td}{peer_td}{vs_td}{hyp_td}{med_td}{count_td}</tr>'
        )

    rows.append('</tbody></table>')
    # Generate the peer list from the tier registry so it can't go stale (e.g. it
    # used to hardcode "Gcore", which was demoted, and omitted Together AI).
    ent_peers = ", ".join(_provider_display(p)
                          for p in PROVIDER_TIERS["enterprise_gpu_cloud"] if p != "nebius")
    rows.append(
        '<p><em>'
        f'Enterprise peers (from tier registry): {ent_peers}. '
        'Criteria: GPU-first business, meaningful owned capacity (1,000+ GPUs), enterprise SLAs, active. '
        'Excluded: Genesis Cloud (in liquidation 2025), Sesterce (broker/reseller model), '
        'Denvr Dataworks (too small). '
        'Hyperscaler column = cheapest of AWS / GCP / Azure / Oracle (on-demand list price; '
        'enterprise customers typically pay 40–57% less at 3yr committed). '
        'Nebius on-demand prices are uniform across regions (no US discount); availability by GPU: '
        'H100 eu-north1 only, H200 EU + us-central1, B200 us-central1 + me-west1, B300 uk-south1 (private). '
        'IREN: competitor named in enterprise sales calls; not yet tracked (no public pricing). '
        '⚠️ L40S pricing risk: Nebius on-demand ($1.82) is only 2% below AWS on-demand ($1.86); '
        'AWS L40S drops to ~$0.37 at 3yr committed — a 5× gap that Nebius has no committed L40S tier to counter. '
        'GB200/GB300: Nebius has committed pricing for these GPUs (see table below) but no published on-demand rate. '
        'Coverage: current-gen datacenter GPUs (H100, H200, B200, B300, L40S, GB200, GB300). '
        'A100 (prior-gen) is excluded as demand has shifted to Hopper/Blackwell; AMD MI300X/MI325X '
        'excluded as a separate ecosystem. Both can be added on request.'
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
        # cp_civo intentionally included: public 36mo B200 @ $3.79 is below Nebius $4.15
        # and gives a rare public neocloud committed benchmark for Blackwell
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
                # Check 2: cross-provider floor — ONLY as a fallback when the provider
                # has no on-demand record of its own (Check 1 can't run). Catches e.g.
                # Gcore H200 "reserved" at $19 with no Gcore on-demand to compare to.
                # Must NOT fire when the provider has its own on-demand (Check 1 already
                # validated committed < own OD): the global floor includes cheap
                # marketplace on-demand (~$1.66 H100), so 4× wrongly dropped GCP's
                # legitimate H100 1yr $6.80 while AWS/Azure survived.
                elif od is None and global_floor and committed > global_floor * 4.0:
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
        '¹ AWS: Standard reserved, all-upfront effective rate (the deepest discount, requires '
        '100% prepayment; the no-upfront 3yr rate is materially higher, e.g. H100 ~$2.97). '
        'Azure: partial-upfront capacity reservation. '
        'GCP: Committed Use Discount (no upfront, usage commitment, no capacity guarantee). '
        'Oracle: on-demand prices now sourced directly from the OCI price-list API '
        '(api). Oracle does not publish committed GPU pricing publicly. '
        'Nebius: internal pricing model effective April 23rd 2026; enterprise tier (512+ GPU, 100% upfront). '
        'Standard tier (&lt;512 GPU) ~5–10% higher; 36-month H100/H200 available on request. '
        'Peer providers (Civo, Vultr) sourced from ComputePrices.com. '
        'Civo committed rates are public list prices, not negotiated. '
        'Nebius on-demand prices are uniform across regions (no US discount); availability by GPU: '
        'H100 eu-north1 only, H200 EU + us-central1, B200 us-central1 + me-west1, B300 uk-south1 (private).'
        '</em></p>'
    )
    return "\n".join(html)


def _build_capacity_block_section(records: List[PriceRecord]) -> str:
    """
    AWS Capacity Block effective hourly prices — a separate section because
    Capacity Blocks are capacity-guaranteed, time-bounded reservations (≤6 months),
    not traditional reserved instances. Not comparable to Nebius committed pricing.
    """
    cb = {r.gpu_model: r for r in records
          if r.provider == "aws" and r.consumption_type == "capacity_block"}
    if not cb:
        return ""

    GPUS = ["H100", "H200", "B200", "B300", "GB200"]
    rows_with_data = [g for g in GPUS if g in cb]
    if not rows_with_data:
        return ""

    html = [
        '<h3>AWS Capacity Blocks — Effective Hourly Rate</h3>',
        '<p>Capacity Blocks are public, capacity-guaranteed reservations of up to 6 months. '
        'Supported instance families: P5 (H100), P5e/P5en (H200), P6-B200, P6-B300, P6e-GB200. '
        'Prices shown are the cheapest available region. '
        '<strong>These are not comparable to 3yr Reserved Instances or Nebius committed pricing</strong> — '
        'they are a separate product class. Useful as a capacity-guarantee reference for enterprise RFPs.</p>',
        '<table data-layout="full-width"><tbody>',
        '<tr><th>GPU</th><th>Instance</th><th>AWS Capacity Block ($/GPU-hr)</th>'
        '<th>vs AWS on-demand (cheapest region)</th><th>vs Nebius on-demand</th></tr>',
    ]

    # Get AWS on-demand and Nebius on-demand for comparison — use the CHEAPEST per
    # GPU (same reference the executive table uses) so a cell never disagrees with
    # the exec table for the same (provider, gpu, on_demand). Previously this took
    # whichever region iterated last (e.g. ap-northeast $8.60), contradicting the
    # exec's $6.88 (Phase 1.5).
    def _cheapest_od(provider: str) -> dict:
        out: dict = {}
        for r in records:
            if r.provider == provider and r.consumption_type == "on_demand":
                if r.gpu_model not in out or r.price_per_gpu_hour_usd < out[r.gpu_model]:
                    out[r.gpu_model] = r.price_per_gpu_hour_usd
        return out
    aws_od = _cheapest_od("aws")
    neb_od = _cheapest_od("nebius")

    for gpu in rows_with_data:
        r = cb[gpu]
        p = r.price_per_gpu_hour_usd

        aws_od_p = aws_od.get(gpu)
        neb_od_p = neb_od.get(gpu)

        vs_aws = ""
        if aws_od_p:
            pct = (p - aws_od_p) / aws_od_p * 100
            sign = "+" if pct >= 0 else ""
            color = "green" if pct < 0 else "yellow"
            vs_aws = f'<span data-type="status" data-color="{color}">{sign}{pct:.0f}% vs OD ${aws_od_p:.2f}</span>'

        vs_neb = ""
        if neb_od_p:
            pct = (p - neb_od_p) / neb_od_p * 100
            sign = "+" if pct >= 0 else ""
            color = "red" if pct > 20 else ("yellow" if pct > 0 else "green")
            vs_neb = f'<span data-type="status" data-color="{color}">{sign}{pct:.0f}% vs Nebius ${neb_od_p:.2f}</span>'

        html.append(
            f'<tr><td><strong>{gpu}</strong></td>'
            f'<td><em>{r.instance_type}</em></td>'
            f'<td><strong>${p:.3f}</strong></td>'
            f'<td>{vs_aws}</td>'
            f'<td>{vs_neb}</td></tr>'
        )
    html.append('</tbody></table>')
    html.append(
        '<p><em>Source: <a href="https://aws.amazon.com/ec2/capacityblocks/pricing/">'
        'aws.amazon.com/ec2/capacityblocks/pricing</a>. '
        'GB200 = P6e UltraServer (36-GPU node, Dallas Local Zone). '
        'B300 = P6-B300 (Oregon/N. Virginia). B200 = P6-B200 (Ohio/N. Virginia/Oregon). '
        'H200 = P5e (multiple regions). H100 = P5 (multiple regions). '
        'Prices verified June 2026.</em></p>'
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
        'AWS: standard reserved, all-upfront effective rate (deepest discount, 100% prepaid). '
        'GCP: Committed Use Discount (no upfront). '
        'Azure: partial-upfront capacity reservation. '
        '*Nebius on-demand prices are uniform across regions (no US discount); availability by GPU: '
        'H100 eu-north1 only, H200 EU + us-central1, B200 us-central1 + me-west1, B300 uk-south1 (private). '
        'Nebius committed = internal pricing model (enterprise tier, 100% upfront). '
        'CoreWeave and Lambda: US regions only currently. '
        '†Oracle on-demand prices sourced directly from the OCI price-list API; '
        'Oracle does not publish GPU committed pricing publicly. '
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
