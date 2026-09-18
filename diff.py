"""
Compute price changes between two snapshots and format outputs.
"""
import csv
from html import escape
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from schema import PriceRecord, DiffEntry
from price_corrections import known_restatement, correct_record
from history import load_comparison_history
from report_freshness import publication_records, committed_reference_fresh
from config import provider_tier, provider_tag, ALERT_THRESHOLD_PCT, PROVIDER_TIERS

# Direct-fetcher serverless/managed platforms (Modal, Baseten). They sit in the
# managed_inference tier — excluded from raw-compute peer math — but their
# deliberate list-price moves ARE market signal, so the move surfaces admit them
# explicitly (2026-09-02 Koen ask). Aggregator-sourced cp_* inference platforms
# stay excluded from moves (stale relays, not primary sources).
DIRECT_PLATFORM_PROVIDERS = ("modal", "baseten")

# Tag → Confluence status-lozenge color for the market-sweep Tag column.
_TAG_COLORS = {"peer": "blue", "hyperscaler": "purple",
               "pricefighter": "grey", "platform": "yellow"}


def _tag_cell(provider: str) -> str:
    if provider == "nebius":
        return "<td>—</td>"
    tag = provider_tag(provider)
    color = _TAG_COLORS.get(tag, "grey")
    return f'<td><span data-type="status" data-color="{color}">{tag}</span></td>'


def _prov_display(p: str) -> str:
    """Module-level provider display name (same rules as the nested _pname
    helpers used by the Slack renderers)."""
    if p == "massedcompute":
        return "Massed Compute (account catalogue)"
    _KEEP_UPPER = {"aws", "gcp", "gpu", "gmi", "ai"}
    name = p.replace("cp_", "").replace("-", " ")
    return " ".join(w.upper() if w.lower() in _KEEP_UPPER else w.title()
                    for w in name.split())
from comparability import (enrich_comparability, is_cluster_class,
                           LOCAL_STORAGE_BUNDLED, LOCAL_STORAGE_VERIFIED,
                           local_storage_info, is_public_benchmark_eligible,
                           is_qualified_catalogue_reference, QUALIFIED_CATALOGUE_BASES)

INTEL_CSV = Path(__file__).parent / "store" / "intel.csv"
HISTORY_CSV = Path(__file__).parent / "store" / "history.csv"
# Rolling won-side benchmark: anonymized aggregates of OUR signed reserve deals
# (lo/median/hi $/GPU-hr per model, rolling 30d), refreshed weekly from the DWH by
# a local scheduled task (the pipeline itself has no DWH access — intel.csv
# pattern). Counterweight to field intel's structural negative skew: losses get
# logged by AEs, wins don't; this is the win side, from contracts, not from posts.
RESERVE_WINS_CSV = Path(__file__).parent / "store" / "reserve_wins.csv"
RESERVE_WINS_STALE_DAYS = 14

CHANGE_THRESHOLD = 0.001   # 0.1% — ignore floating-point noise in diff detection

# NB RTX6000 (RTX PRO 6000) is intentionally NOT here: it is an inference/PAYG card
# whose competitors are inference platforms (Vast, fal, Beam, …), not the training-
# cluster peers/hyperscalers these sections compare against. It gets its own market
# line via _format_rtx_callout. It IS tracked in NEBIUS_GPUS / GPU_MODELS / fetchers.
GPU_ORDER = ["H100", "H200", "B200", "B300", "GB200", "GB300", "L40S"]

# Revenue-driver GPUs that the SUMMARY headline focuses on, in priority order. The
# detailed thread + Confluence still cover all of GPU_ORDER, but the exec-facing summary
# is weighted to where the revenue and the competitive opportunity actually are (Hopper +
# Blackwell data-center GPUs). L40S, GB200 and RTX are minor on revenue, so they stay in
# the thread and don't drag the headline (e.g. L40S near-parity was widening the gap range
# to "2%"). Adjust this list to re-weight the headline.
SUMMARY_GPUS = ["H100", "H200", "B200", "B300", "GB300"]

# GPUs that exist ONLY as field intel (no public list price anywhere): rendered
# in the field-intel/committed sections but never in list-price tables.
# VR = Vera Rubin — no provider publishes rental pricing as of 2026-08-11.
FIELD_ONLY_GPUS = ["VR"]

# A recorded competitive loss drives the "lost a deal" action (summary headline, thread
# flag, recommended action) for this many days after the loss date, then DECAYS: the red
# "lost deal" badge stays in the Confluence decision-trigger table (with its date) until
# the row leaves the 90d intel window, but it stops claiming action surfaces — a month-old
# loss that has already been reviewed must not nag as if it were fresh news. Losses with
# no parseable date never decay (conservative: keep nagging rather than silently drop).
LOSS_FLAG_DECAY_DAYS = 30
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
    "massedcompute":  "Massed Compute (account catalogue)",
    "aws":            "AWS",
    "gcp":            "GCP",
    "azure":          "Azure",
    "nebius":         "Nebius",
    "coreweave":      "CoreWeave",
    "lambda":         "Lambda",
    "crusoe":         "Crusoe",
    "together":       "Together AI",
    "oracle":         "Oracle",
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
    return (r.provider, r.gpu_model, r.instance_type, r.region, r.consumption_type,
            r.source_feed, r.offer_id)


# Reversion damper (2026-07-27): a "price move" whose new level was already seen
# within this window is an oscillation back to a recent level, not market news.
# Trigger case: Scaleway B300 flip-flopped $8.5x <-> exactly $9.0075 six times in
# six weeks (fixed EUR list price; the aggregator's FX conversion intermittently
# snaps to a fixed fallback rate and back) and headlined the exec digest as
# "raised +5%" / "cut -5%" each time. Typed as change_type="reversion" so every
# market-move surface + the thread gate exclude it (same pattern as parser
# restatements); disclosed via a note line instead.
REVERSION_LOOKBACK_DAYS = 7
REVERSION_MATCH_TOL = 0.015   # ≤1.5% = "same level" (FX wobble is <1%/day;
                              # real moves are ≥3% by ALERT_THRESHOLD_PCT)


def _recent_price_levels(days: int = REVERSION_LOOKBACK_DAYS) -> Dict[tuple, list]:
    """
    {(provider, gpu_model, consumption_type): [recent daily cheapest prices]} from
    history.csv, excluding today (today's value is the change being judged).
    """
    out: Dict[tuple, list] = {}
    if not HISTORY_CSV.exists():
        return out
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    today = date.today().isoformat()
    with open(HISTORY_CSV, newline="") as f:
        for r in load_comparison_history(HISTORY_CSV):
            d = r.get("snapshot_date", "")
            if not (cutoff <= d < today):
                continue
            try:
                px = float(r["price_per_gpu_hour_usd"])
            except (ValueError, TypeError):
                continue
            out.setdefault((r["provider"], r["gpu_model"],
                            r["consumption_type"]), []).append(px)
    return out


def compute_diff(old: List[PriceRecord], new: List[PriceRecord]) -> List[DiffEntry]:
    old_map: Dict[tuple, PriceRecord] = {record_key(r): r for r in old}
    new_map: Dict[tuple, PriceRecord] = {record_key(r): r for r in new}
    recent = _recent_price_levels()

    diffs: List[DiffEntry] = []

    for key, new_rec in new_map.items():
        if key in old_map:
            old_rec = old_map[key]
            old_corrected = correct_record(old_rec)
            new_corrected = correct_record(new_rec)
            old_p = old_corrected.record.price_per_gpu_hour_usd
            new_p = new_corrected.record.price_per_gpu_hour_usd
            restatement = (known_restatement(old_rec, new_rec) or
                           not old_corrected.comparison_eligible or not new_corrected.comparison_eligible)
            if restatement:
                # Preserve the visible source correction in the ledger, without
                # treating a different product as an observed price change.
                old_p, new_p = old_rec.price_per_gpu_hour_usd, new_rec.price_per_gpu_hour_usd
            if old_p > 0 and abs(new_p - old_p) / old_p > CHANGE_THRESHOLD:
                # A price produced by DIFFERENT parser code is a methodology
                # restatement, not a market move: broadcasting it as a
                # competitor price change is false intel (e.g. the 2026-07-14
                # AWS upfront-amortization fix repriced every AWS reserved
                # record ~+90% — AWS changed nothing). Restatements are kept in
                # the diff (typed) so surfaces can disclose them, but every
                # market-move consumer filters on change_type=="price_change".
                old_pv = getattr(old_rec, "parser_version", "") or ""
                new_pv = getattr(new_rec, "parser_version", "") or ""
                change = "restatement" if (old_pv != new_pv or restatement) else "price_change"
                if change == "price_change":
                    try:
                        old_time = datetime.fromisoformat(old_rec.fetched_at.replace("Z", "+00:00"))
                        new_time = datetime.fromisoformat(new_rec.fetched_at.replace("Z", "+00:00"))
                        if abs((new_time - old_time).total_seconds()) > 48 * 3600:
                            change = "coverage_reference_change"
                    except (TypeError, ValueError):
                        # Missing observation dates cannot establish a daily move.
                        change = "coverage_reference_change"
                if (is_qualified_catalogue_reference(old_rec)
                        or is_qualified_catalogue_reference(new_rec)):
                    # A restricted/unknown catalogue tariff (or transition from
                    # one) is not an ordinary purchasable-offer price movement.
                    change = "catalog_reference_change"
                if change == "price_change":
                    # Reversion check: is the "new" price just a level this series
                    # already sat at within the lookback window? (History grain is
                    # cheapest per provider/gpu/ct — good enough: oscillating
                    # sources flip the whole series, not one region.)
                    levels = recent.get((new_rec.provider, new_rec.gpu_model,
                                         new_rec.consumption_type), [])
                    # (new_p can never match old_p here: the change gate already
                    # requires ≥CHANGE_THRESHOLD movement, far above the 1.5% tol.)
                    if any(l > 0 and abs(new_p - l) / l <= REVERSION_MATCH_TOL
                           for l in levels):
                        change = "reversion"
                if change == "price_change" and (
                        new_rec.source_type == "aggregator" or new_rec.source_feed in {"computeprices", "shadeform"}):
                    change = "aggregator_update"
                diffs.append(DiffEntry(
                    provider=new_rec.provider,
                    gpu_model=new_rec.gpu_model,
                    region=new_rec.region,
                    consumption_type=new_rec.consumption_type,
                    instance_type=new_rec.instance_type,
                    change_type=change,
                    old_price=old_p,
                    new_price=new_p,
                    delta_pct=(new_p - old_p) / old_p * 100,
                    source_feed=new_rec.source_feed, offer_id=new_rec.offer_id,
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
                source_feed=new_rec.source_feed, offer_id=new_rec.offer_id,
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
                source_feed=old_rec.source_feed, offer_id=old_rec.offer_id,
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
        and is_public_benchmark_eligible(r)
        and (tiers is None or provider_tier(r.provider) in tiers)
        and (not cluster_only or is_cluster_class(r))
    ]
    return min(candidates, key=lambda r: r.price_per_gpu_hour_usd) if candidates else None


def _representative_spot_floor(records: List[PriceRecord], gpu: str,
                               tiers: Optional[List[str]] = None):
    """
    Representative cheapest spot price for a GPU: each provider's MEDIAN across its
    regional price points, then the cheapest provider's median. NB the grain is
    REGIONS, not availability zones — each hyperscaler spot record is that
    region's floor (AWS: latest-per-AZ min; the public S3 feed and Azure/GCP
    sources are region-grain by construction). Avoids a transient single-region
    outlier (e.g. AWS H200 spot dipping to $0.79 in one region while the others
    sit at $2.0–2.2) masquerading as "the spot floor".
    Returns (provider, median_price, n_points) or None.
    """
    from collections import defaultdict as _dd
    # Per-provider on-demand baseline for this GPU, to filter PHANTOM spot floors:
    # for newest-gen GPUs (B200/B300) hyperscalers publish a spot price but have no
    # real spot capacity, so it sits ~80%+ below their own on-demand — far past any
    # genuine spot discount (~60-70%). Comparing Nebius preemptible to that produces a
    # misleading "+54% above AWS" against capacity nobody can actually get.
    MAX_SPOT_DISCOUNT = 0.80
    records = [r for r in records if is_public_benchmark_eligible(r)]
    od_by_prov: Dict[str, float] = {}
    for r in records:
        if r.gpu_model == gpu and r.consumption_type == "on_demand":
            if r.provider not in od_by_prov or r.price_per_gpu_hour_usd < od_by_prov[r.provider]:
                od_by_prov[r.provider] = r.price_per_gpu_hour_usd
    by_prov = _dd(list)
    for r in records:
        if (r.gpu_model == gpu and r.consumption_type in INTERRUPTIBLE_CTS
                and (tiers is None or provider_tier(r.provider) in tiers)):
            od = od_by_prov.get(r.provider)
            if od and r.price_per_gpu_hour_usd < od * (1 - MAX_SPOT_DISCOUNT):
                continue  # phantom: spot > 75% below this provider's own on-demand
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


def _is_cluster_peer(r) -> bool:
    """
    True if a peer record is an 8×SXM (or larger) cluster offering — the like-for-like
    basis for comparing against Nebius's cluster price. Single-GPU / Ethernet entry SKUs
    (e.g. GMI 1×H200 $2.60, Voltage 1×H100 $1.99 Ethernet) are NOT cluster-comparable and
    must not drag down the peer median. Interconnect is often "unknown" in aggregator data,
    so we gate on form factor + node size, not interconnect.
    """
    if (r.form_factor or "").upper() != "SXM":
        return False
    if r.parser_version in {"aggregator-offers-1", "direct-offers-1"}:
        return (r.gpu_count or 0) >= 8
    return (r.gpu_count or 0) >= 8 or (getattr(r, "node_gpus", 0) or 0) >= 8


def _position_for_tier(records, gpu, cts, label, cluster_only=False):
    """
    Compute Nebius position vs raw_gpu_cloud peers for a set of consumption types.
    `cts` is a set of consumption_type strings treated as the same tier.
    cluster_only=True restricts peers to 8×SXM cluster SKUs (like-for-like with Nebius's
    cluster price), so single-GPU entry SKUs don't make Nebius look artificially premium.
    """
    nebius_candidates = [r for r in records
                         if r.gpu_model == gpu and r.consumption_type in cts
                         and r.provider == "nebius" and is_public_benchmark_eligible(r)]
    nebius_rec = min(nebius_candidates, key=lambda r: r.price_per_gpu_hour_usd) \
        if nebius_candidates else None

    peers = [r for r in records
             if r.gpu_model == gpu and r.consumption_type in cts
             and provider_tier(r.provider) in ("raw_gpu_cloud", "enterprise_gpu_cloud")
             and r.provider in PROVIDER_TIERS.get("enterprise_gpu_cloud", [])
             and r.provider != "nebius"
             and is_public_benchmark_eligible(r)]
    if cluster_only:
        # Prefer cluster-class (8×SXM) peers; fall back to all peers only when none
        # exist for this GPU — e.g. L40S is PCIe everywhere, so PCIe-to-PCIe is the
        # genuine like-for-like, and B300 has no SXM-tagged peer in the aggregator yet.
        cluster_peers = [r for r in peers if _is_cluster_peer(r)]
        if cluster_peers:
            peers = cluster_peers

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
        "cheapest_peer_source": cheapest_peer.source_feed if cheapest_peer else "",
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
        od = _position_for_tier(records, gpu, {"on_demand"}, "on_demand", cluster_only=True)
        if od:
            rows.append(od)
        intr = _position_for_tier(records, gpu, INTERRUPTIBLE_CTS, "interruptible")
        if intr:
            rows.append(intr)
    return rows


# ---------------------------------------------------------------------------
# Committed pricing callout helper
# ---------------------------------------------------------------------------

def _committed_freshness():
    """Fail closed when the manually verified reference has expired."""
    return committed_reference_fresh()


def _best_aws_h100_field_deal():
    """
    Cheapest negotiated AWS H100 committed deal (term <= 36mo) from #price-intelligence.
    Returns (price, term_months, prepay_pct) or None. Shared by the committed callout,
    the TL;DR Finance line and the AWS battlecard so the same evidence backs all three
    (no hardcoded field-deal numbers on exec surfaces).
    """
    best = None
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
        if 0 < term <= 36 and (best is None or px < best[0]):
            best = (px, term, int(float(r.get("prepay_pct", "0") or 0)))
    return best


def _format_committed_callout(records: List[PriceRecord]) -> str:
    """
    Build the committed-tier summary line for the Slack message.
    Shows Nebius committed vs AWS committed for H100 — the key strategic comparison.
    Omitted entirely when the Nebius committed list is stale (see _committed_freshness).
    """
    # Stale list -> omit from exec output (main.py logs the internal staleness warning).
    fresh, vdate, days_old = _committed_freshness()
    if not fresh:
        return ""
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
    best_field = _best_aws_h100_field_deal()
    if best_field and neb_1yr and best_field[0] < neb_1yr:
        px, term, prepay = best_field
        parts.append(f"⚠ Field intel: AWS {term}mo deal seen at ${px:.2f} "
                     f"({prepay}% prepay) — below Nebius; list comparisons understate "
                     f"AWS's negotiated floor")

    if not parts:
        return ""

    header = "*Committed pricing (H100 benchmark, $/GPU-hr):*"
    footer = f"\n_Nebius committed list verified {vdate}._" if vdate else ""
    return header + "\n" + "\n".join(f"• {p}" for p in parts) + footer


# ---------------------------------------------------------------------------
# Slack message — executive brief
# ---------------------------------------------------------------------------

def _field_committed_by_gpu(days: int = 90):
    """{GPU: [(price, term_months, provider, prepay), ...]} of committed field deals."""
    from collections import defaultdict as _dd
    by_gpu = _dd(list)
    for r in _load_intel(days=days):
        try:
            term = int(float(r.get("term_months", "0") or 0))
            px = float(r["price_per_gpu_hour_usd"])
        except (ValueError, KeyError, TypeError):
            continue
        if term <= 0 or px <= 0:
            continue   # committed deals only
        by_gpu[(r.get("gpu_model") or "").upper()].append(
            (px, term, r.get("provider_name") or r.get("provider_type") or "?",
             r.get("prepay_pct") or "?"))
    return by_gpu


POSITION_HISTORY_CSV = Path(__file__).parent / "store" / "position_history.csv"


def record_position_history(records: List[PriceRecord]) -> None:
    """
    Persist today's computed position gaps (per SUMMARY GPU: on-demand gap vs
    cluster-peer median and vs cheapest comparable hyperscaler) so the Monday
    anchor can report week-over-week movement EXACTLY consistent with what was
    published — reconstructing peer medians from history.csv can't reproduce
    the live cluster-class filter (node_gpus isn't persisted there). Called by
    main.py each run; same-day reruns overwrite (upsert on date+gpu+basis).
    """
    today = date.today().isoformat()
    rows = []
    if POSITION_HISTORY_CSV.exists():
        with open(POSITION_HISTORY_CSV, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != today]
    for row in compute_position(records):
        if (row["tier_label"] != "on_demand" or not row["nebius_price"]
                or row["gpu"] not in SUMMARY_GPUS):
            continue
        if row.get("median_peer") and row["total_peers"] >= 2:
            pct = (row["nebius_price"] - row["median_peer"]) / row["median_peer"] * 100
            rows.append({"date": today, "gpu": row["gpu"], "basis": "peer_median",
                         "gap_pct": f"{pct:.2f}"})
        hyp = _best_comparable(records, row["gpu"], "on_demand", tiers=["hyperscaler"])
        if hyp:
            pct = (row["nebius_price"] - hyp.price_per_gpu_hour_usd) \
                / hyp.price_per_gpu_hour_usd * 100
            rows.append({"date": today, "gpu": row["gpu"], "basis": "hyperscaler",
                         "gap_pct": f"{pct:.2f}"})
    with open(POSITION_HISTORY_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "gpu", "basis", "gap_pct"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["date"], r["gpu"], r["basis"])))


def _peer_gap_wow(gpu: str, current_pct: float):
    """
    Week-over-week movement of the peer-median gap in percentage points:
    current gap minus the recorded gap 5-9 days ago (closest to 7). None when
    no recorded point exists in that window (young file, provider outages).
    """
    if not POSITION_HISTORY_CSV.exists():
        return None
    today = date.today()
    best = None   # (|days_from_7|, gap_pct)
    with open(POSITION_HISTORY_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r["date"] < "2026-09-18" or r["gpu"] != gpu or r["basis"] != "peer_median":
                continue
            try:
                age = (today - date.fromisoformat(r["date"])).days
                gap = float(r["gap_pct"])
            except (ValueError, TypeError):
                continue
            if 5 <= age <= 9 and (best is None or abs(age - 7) < best[0]):
                best = (abs(age - 7), gap)
    if best is None:
        return None
    return current_pct - best[1]


def _range_stability_weeks(current_range: str) -> int:
    """
    How many whole weeks the rounded hyperscaler-gap range string (e.g. "43–49")
    has been exactly what it is today, per history.csv. Used to tag the position
    line with "(unchanged Nw)" — the range is computed against hyperscaler LIST
    prices, which are sticky for quarters, so it can sit still for months and the
    stillness itself is the signal. Returns 0 when history is missing/short.
    """
    if not HISTORY_CSV.exists():
        return 0
    neb: Dict[str, Dict[str, float]] = {}
    hyp: Dict[str, Dict[str, list]] = {}
    with open(HISTORY_CSV, newline="") as f:
        for r in load_comparison_history(HISTORY_CSV):
            if r["consumption_type"] != "on_demand":
                continue
            d, g = r["snapshot_date"], r["gpu_model"]
            try:
                px = float(r["price_per_gpu_hour_usd"])
            except (ValueError, TypeError):
                continue
            if r["provider"] == "nebius":
                neb.setdefault(d, {})[g] = px
            elif (r["provider"] in ("aws", "gcp", "azure", "oracle")
                  and r.get("form_factor") == "SXM"):
                hyp.setdefault(d, {}).setdefault(g, []).append(px)
    since = None
    for d in sorted(neb, reverse=True):
        day_gaps = []
        for g in SUMMARY_GPUS:
            if g in neb[d] and hyp.get(d, {}).get(g):
                h = min(hyp[d][g])
                day_gaps.append((h - neb[d][g]) / h * 100)
        if not day_gaps:
            continue
        rng = f"{min(day_gaps):.0f}–{max(day_gaps):.0f}"
        if rng != current_range:
            break
        since = d
    if since is None:
        return 0
    return (date.today() - date.fromisoformat(since)).days // 7


def _build_takeaway(records: List[PriceRecord], include_pressure: bool = True,
                    label: str = "Bottom line") -> str:
    """
    One-line exec bottom-line, leading the digest: where Nebius sits on on-demand
    (vs hyperscalers + cluster peers) and where the real pressure is (committed deals
    competitors are actually quoting). Synthesises the position so the CEO/CRO/sales
    reader gets the 'so what' before the tables.
    """
    if not records:
        return ""
    # On-demand vs hyperscalers (cluster-class), as a range — revenue-driver GPUs only,
    # so a minor card like L40S near AWS parity doesn't drag the headline range to "2%".
    gaps = []
    for gpu in SUMMARY_GPUS:
        neb = next((r for r in records if r.provider == "nebius"
                    and r.gpu_model == gpu and r.consumption_type == "on_demand"), None)
        hyp = _best_comparable(records, gpu, "on_demand", tiers=["hyperscaler"])
        if neb and hyp:
            gaps.append((hyp.price_per_gpu_hour_usd - neb.price_per_gpu_hour_usd)
                        / hyp.price_per_gpu_hour_usd * 100)
    # On-demand vs cluster-peer median (revenue-driver GPUs only). GPUs with a
    # single public cluster-peer list (B300: only Scaleway publishes) get a named
    # vs-peer clause instead of a fake one-provider "median" (2026-08-11, Koen:
    # B300 belongs in the position line). GB300 stays out by construction: no
    # Nebius on-demand exists (no-PAYG decision) — its story is the committed
    # contested-ground clause below.
    peer_below, single_peer = [], []
    for row in compute_position(records):
        if (row["tier_label"] != "on_demand" or not row["nebius_price"]
                or row["gpu"] not in SUMMARY_GPUS):
            continue
        if row.get("median_peer") and row["total_peers"] >= 2:
            pct = (row["nebius_price"] - row["median_peer"]) / row["median_peer"] * 100
            if pct <= -3:
                peer_below.append(row["gpu"])
        elif row["total_peers"] == 1 and row.get("cheapest_peers_detail"):
            pname, ppx = row["cheapest_peers_detail"][0]
            pct = (row["nebius_price"] - ppx) / ppx * 100
            if pct <= -3:
                single_peer.append(f"{row['gpu']} {pct:.0f}% vs "
                                   f"{_provider_display(pname)} (sole public cluster-peer list)")
    od = None
    if gaps:
        lo, hi = min(gaps), max(gaps)
        od = f"on-demand sits {lo:.0f}–{hi:.0f}% below hyperscalers"
        # A range that hasn't moved in weeks is stability, not news — say so
        # explicitly instead of restating it daily as if fresh (2026-08-11).
        wk = _range_stability_weeks(f"{lo:.0f}–{hi:.0f}")
        if wk >= 3:
            od += f" (unchanged {wk}w)"
        if peer_below:
            od += f" and below cluster-peer median ({', '.join(peer_below[:3])})"
        if single_peer:
            od += f"; {'; '.join(single_peer[:2])}"
        rtx = _rtx_market_stats(records)
        if rtx:
            rvs = (rtx["neb_od"] - rtx["median"]) / rtx["median"] * 100
            od += (f". RTX PRO 6000 {rvs:+.0f}% vs RTX market median "
                   f"({rtx['n_comp']} providers)")
    # Committed pressure: the hottest GPU where a competitor deal undercuts Nebius committed.
    # Skipped when the action flag already carries this point, so the same committed-deal
    # SKU is not stated twice in one post.
    pressure = None
    if include_pressure:
        by_gpu = _field_committed_by_gpu(90)
        for g in ("VR", "GB300", "B300", "B200", "H200", "H100"):
            if not by_gpu.get(g):
                continue
            # Cheapest field row that has a COMPARABLE Nebius term bucket — not the
            # cheapest row outright. (Until 2026-08-03 a single cheapest row with no
            # comparable term, e.g. a 5yr GB300 quote, silently skipped the whole
            # GPU and under-reported its committed pressure for weeks.)
            for px, term, _prov, _pp in sorted(by_gpu[g], key=lambda x: x[0]):
                cts, _lbl = _term_bucket_cts(term)
                neb = _cheapest(records, "nebius", g, cts) if cts else None
                if neb and px < neb:
                    d = (px - neb) / neb * 100
                    pressure = (f"the contested ground is committed deals — competitors quoting "
                                f"{g} from ${px:.2f} vs our ${neb:.2f} ({d:+.0f}%)")
                    break
            if pressure:
                break
    clauses = [c for c in (od, pressure) if c]
    if not clauses:
        return ""
    return f"\n*{label}:* Nebius " + "; ".join(clauses) + "."


def _format_field_committed_callout(records: List[PriceRecord]) -> str:
    """
    Slack text version of the neocloud committed field-intel table: the lowest committed
    deal seen per GPU in #price-intelligence vs Nebius committed at the same term. This is
    where the 2026 competition actually happens (B300/GB300), and peers don't publish it.
    """
    by_gpu = _field_committed_by_gpu(90)
    present = [g for g in GPU_ORDER + FIELD_ONLY_GPUS if by_gpu.get(g)]
    if not present:
        return ""
    lines = ["\n*Committed — negotiated competitor deals (field intel, #price-intelligence, 90d):*"]
    for g in present:
        deals = by_gpu[g]
        px, term, prov, prepay = min(deals, key=lambda x: x[0])
        cts, term_label = _term_bucket_cts(term)
        neb = _cheapest(records, "nebius", g, cts) if cts else None
        prepay_str = f"{prepay}%" if str(prepay).isdigit() else str(prepay)
        if neb:
            d = (px - neb) / neb * 100
            vs = f"{d:+.0f}% vs Nebius ${neb:.2f}"
        else:
            # STORM sales/exec fix (2026-07-07): don't leave the hottest SKUs
            # (GB200/GB300) uncompared just because the headline deal's term has
            # no Nebius tier — also show the lowest deal at a term we DO offer.
            vs = "no Nebius committed at this term"
            comparable = []
            for px2, term2, prov2, _pp2 in deals:
                cts2, lbl2 = _term_bucket_cts(term2)
                neb2 = _cheapest(records, "nebius", g, cts2) if cts2 else None
                if neb2:
                    comparable.append((px2, lbl2, prov2, neb2))
            if comparable:
                px2, lbl2, prov2, neb2 = min(comparable, key=lambda x: x[0])
                d2 = (px2 - neb2) / neb2 * 100
                vs += (f" · closest comparable: ${px2:.2f} ({lbl2}, "
                       f"{_provider_display(prov2)}) = {d2:+.0f}% vs Nebius ${neb2:.2f}")
        lines.append(f"`{g:<5}` lowest ${px:.2f} ({term_label}, {prepay_str}) via "
                     f"{_provider_display(prov)}  |  {vs}  [{len(deals)} deals]")
    lines.append("_Deal-specific, anonymized; lower confidence than published list prices. "
                 "Basis: Nebius = 512+ GPU tier at 100% upfront; most field deals are "
                 "0% prepay, so true like-for-like gaps are wider than shown. "
                 "Evidence skews negative: AEs log competitor quotes and losses, wins are "
                 "rarely posted._")
    return "\n".join(lines)


def _load_reserve_wins():
    """
    Rows of store/reserve_wins.csv plus freshness. Returns (rows, generated_date,
    stale). CSV columns: generated_date,window_days,gpu,term_bucket,deals,gpus,
    price_lo,price_med,price_hi. Aggregates only — never customer-level data
    (customer names deliberately excluded: contracts carry price-confidentiality
    clauses and this page's audience is wider than CRM deal permissions).
    """
    if not RESERVE_WINS_CSV.exists():
        return [], None, True
    try:
        with open(RESERVE_WINS_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return [], None, True
    gen = rows[0].get("generated_date") if rows else None
    try:
        stale = (date.today() - date.fromisoformat(gen)).days > RESERVE_WINS_STALE_DAYS
    except (ValueError, TypeError):
        stale = True
    return rows, gen, stale


def _reserve_list_for_bucket(gpu: str, bucket: str) -> Tuple[Optional[float], str]:
    """Nebius committed list (512+, 100% upfront) matching a term bucket."""
    from config import NEBIUS_COMMITTED_PRICES
    months = {"short<=8mo": 9, "~1yr": 12, "2yr+": 24}.get(bucket, 12)
    tier = NEBIUS_COMMITTED_PRICES.get(gpu, {}).get("above_512") or {}
    return (tier.get(months) or {}).get("100pct"), f"{months}mo"


def _format_reserve_wins_callout() -> str:
    """
    Slack thread block: OUR signed reserve deals, rolling 30d (lo/median/hi per GPU).
    The won side of the market — pairs with the loss-skewed field-intel section.
    """
    rows, gen, stale = _load_reserve_wins()
    if not rows:
        return ""
    win = rows[0].get("window_days", "30")
    lines = [f"\n*Reserve wins — our signed deals (rolling {win}d, from contracts; internal only):*"]
    for r in rows:
        try:
            lo, med, hi = (float(r["price_lo"]), float(r["price_med"]), float(r["price_hi"]))
            gpus = int(float(r["gpus"]))
        except (ValueError, KeyError):
            continue
        bucket = r.get("term_bucket", "~1yr")
        lst, lbl = _reserve_list_for_bucket(r["gpu"], bucket)
        vs = f"  |  med {med / lst - 1:+.0%} vs {lbl} list ${lst:.2f}" if lst else ""
        lines.append(f"`{r['gpu']:<5}` {bucket:<10} {r['deals']} deals, {gpus:>6,} GPUs: "
                     f"${lo:.2f} / ${med:.2f} / ${hi:.2f} (lo/med/hi){vs}")
    tail = (f"_Signed reserve deals closed in the window (CRM deal reviews; autorenewals "
            f"excluded; anonymized aggregates), refreshed {gen}")
    if stale:
        tail += f" ⚠ STALE (>{RESERVE_WINS_STALE_DAYS}d — refresh reserve_wins.csv)"
    tail += (". This is the won side; field intel above skews to losses. "
             "Internal benchmark only — never quote these rates to customers._")
    lines.append(tail)
    return "\n".join(lines)


def _build_reserve_wins_section():
    rows, generated, stale = _load_reserve_wins()
    if not rows:
        return '<p>CRM win aggregates unavailable.</p>'
    html = [f'<h3>CRM won-deal aggregates — generated {escape(generated or "unknown")}</h3>',
            '<p>Historical CRM aggregates, not independently verified contract prices. '
            'The window is anchored to the generation date; it does not roll forward with this page.</p>',
            '<p>Excluded from current comparisons: source refresh overdue.</p>' if stale else '',
            '<table><tbody><tr><th>GPU / term</th><th>Window days</th><th>Deals / GPUs</th><th>Low / median / high ($/GPU-h)</th></tr>']
    for r in rows:
        html.append('<tr>' + ''.join('<td>' + escape(str(v)) + '</td>' for v in (
            r['gpu'] + ' / ' + r['term_bucket'], r['window_days'], r['deals'] + ' / ' + r['gpus'],
            r['price_lo'] + ' / ' + r['price_med'] + ' / ' + r['price_hi'])) + '</tr>')
    html.append('</tbody></table>')
    return '\n'.join(html)


def _rtx_market_stats(records: List[PriceRecord]):
    """
    Shared data for the RTX PRO 6000 surfaces (Slack callout + Confluence section):
    Nebius OD/spot/committed vs the full RTX market. Returns None when Nebius or
    competitor RTX prices are absent.
    """
    # Account-specific catalogues remain visible as labelled observations in
    # the market sweep, but cannot silently enter the public-market statistic.
    rtx = [r for r in records if r.gpu_model == "RTX6000"
           and is_public_benchmark_eligible(r)]
    if not rtx:
        return None
    neb_od = min((r.price_per_gpu_hour_usd for r in rtx
                  if r.provider == "nebius" and r.consumption_type == "on_demand"), default=None)
    neb_sp = min((r.price_per_gpu_hour_usd for r in rtx
                  if r.provider == "nebius" and r.consumption_type in INTERRUPTIBLE_CTS), default=None)
    comp: Dict[str, float] = {}
    for r in rtx:
        if r.provider == "nebius" or r.consumption_type != "on_demand":
            continue
        if r.provider not in comp or r.price_per_gpu_hour_usd < comp[r.provider]:
            comp[r.provider] = r.price_per_gpu_hour_usd
    if neb_od is None or not comp:
        return None

    def _comm_min(is_nebius: bool):
        vals = [r.price_per_gpu_hour_usd for r in rtx
                if (r.provider == "nebius") == is_nebius
                and ("committed" in r.consumption_type or "reserved" in r.consumption_type)]
        return min(vals) if vals else None

    # Market interruptible/spot floor (Krenev 2026-08-24: the spot section is
    # hyperscaler-comparison-driven and no hyperscaler sells this card, so the
    # RTX block carries its own market-spot context instead).
    spot_obs: Dict[str, list] = {}
    for r in rtx:
        if r.provider == "nebius" or r.consumption_type not in INTERRUPTIBLE_CTS:
            continue
        spot_obs.setdefault(r.provider, []).append(r.price_per_gpu_hour_usd)
    # Same method as the main spot section: per-provider MEDIAN across its
    # regional observations (a single-region teaser must not pose as the
    # floor), then cheapest provider.
    spot_comp = {p: statistics.median(v) for p, v in spot_obs.items()}

    prices = sorted(comp.values())
    return {
        "neb_od": neb_od, "neb_sp": neb_sp,
        "median": statistics.median(prices),
        "n_comp": len(prices),
        "cheaper": sum(1 for p in prices if p < neb_od),
        "floor_prov": min(comp, key=comp.get), "floor": prices[0],
        "neb_comm": _comm_min(True), "comp_comm": _comm_min(False),
        "spot_floor": min(spot_comp.values()) if spot_comp else None,
        "spot_floor_prov": min(spot_comp, key=spot_comp.get) if spot_comp else None,
        "n_spot": len(spot_comp),
    }


def _format_rtx_callout(records: List[PriceRecord]) -> str:
    """
    RTX PRO 6000 (Blackwell 96GB inference/PAYG card) vs the broader RTX market.
    Its competitors are inference platforms, not the curated training-cluster peers, so
    we compare Nebius against the full set of providers that price RTX PRO 6000 rather
    than forcing it through the cluster-GPU peer logic. Supports the RTX demand push.
    """
    s = _rtx_market_stats(records)
    if not s:
        return ""
    vs = (s["neb_od"] - s["median"]) / s["median"] * 100
    sign = "+" if vs >= 0 else ""
    out = ["\n*RTX PRO 6000 (inference / PAYG card) — vs RTX market:*",
           f"`OD  ` Nebius ${s['neb_od']:.2f}  {sign}{vs:.0f}% vs market median ${s['median']:.2f}  "
           f"|  {s['cheaper']}/{s['n_comp']} cheaper  |  floor: {_provider_display(s['floor_prov'])} ${s['floor']:.2f}"]
    if s["neb_sp"] is not None:
        sp = f"`Spot` Nebius ${s['neb_sp']:.2f}"
        if s.get("spot_floor") is not None:
            d_sp = (s["neb_sp"] - s["spot_floor"]) / s["spot_floor"] * 100
            sp += (f"  vs market spot floor ${s['spot_floor']:.2f} "
                   f"({_provider_display(s['spot_floor_prov'])}"
                   + (f", {s['n_spot']} providers" if s["n_spot"] > 1 else "")
                   + f")  →  Nebius {d_sp:+.0f}%")
        out.append(sp)
    if s["neb_comm"] is not None:
        c = f"`Comm` Nebius committed from ${s['neb_comm']:.2f}"
        if s["comp_comm"] is not None:
            c += f"  |  market committed from ${s['comp_comm']:.2f}"
        out.append(c)
    out.append("_RTX market = inference platforms and GPU clouds, plus AWS (g7e) and "
               "Azure (NC v6), which list the card since 2026 — not training-cluster peers._")
    return "\n".join(out)


def _self_move_line(diffs: List[DiffEntry]) -> str:
    """
    Self-move attribution (STORM backlog #1, shipped 2026-07-07): when NEBIUS's own
    price changed, every position delta in the post shifts. Without this line the
    daily message reports our own repricing as market movement — worst during a
    planned rollout (e.g. the Aug-2026 regional changes). Renders one lead line:
    '*We repriced:* H200 on-demand $4.50→$4.73 (+5%) …' or '' when we didn't move.
    """
    moves = [d for d in diffs
             if d.provider == "nebius" and d.change_type == "price_change"
             and abs(d.delta_pct or 0) >= 0.5]
    if not moves:
        return ""
    groups: Dict[tuple, DiffEntry] = {}
    for d in moves:   # one entry per gpu × tier bucket (largest move wins)
        ct = "spot" if d.consumption_type in INTERRUPTIBLE_CTS else \
            d.consumption_type.replace("on_demand", "on-demand").replace("_", " ")
        k = (d.gpu_model, ct)
        if k not in groups or abs(d.delta_pct or 0) > abs(groups[k].delta_pct or 0):
            groups[k] = d
    parts = [f"{gpu} {ct} ${d.old_price:.2f}→${d.new_price:.2f} ({d.delta_pct:+.0f}%)"
             for (gpu, ct), d in sorted(groups.items())]
    return ("\n*We repriced:* " + ", ".join(parts)
            + " — position deltas below reflect our move, not the market.")


def format_slack_summary(diffs, run_date, confluence_url, records=None,
                         provider_status=None, post_thread=True, weekly=False):
    """Daily changes and material source exceptions; no standing sales guidance."""
    current, notices = publication_records(records or [], run_date)
    diffs = _publication_diffs(diffs, current)
    moves = _group_significant_moves(diffs or [])
    lines = [f"*GPU pricing · {run_date}*"]
    if moves:
        for g in moves[:2]:
            d = g['peak']
            lines.append(f"• {_prov_display(d.provider)} {d.gpu_model} {g['bucket']}: "
                         f"${d.old_price:.2f} → ${d.new_price:.2f}/GPU-h ({d.delta_pct:+.1f}%, {d.region}); "
                         f"{g['sku_count']} SKU/region observations in this group.")
        if len(moves) > 2:
            lines.append(f"{len(moves)-2} further change groups in the daily ledger.")
    else:
        lines.append(f"No observed competitor price changes of {ALERT_THRESHOLD_PCT:g}% or more in the eligible snapshot.")
    restated = {d.provider for d in (diffs or []) if d.change_type == 'restatement'}
    if restated:
        lines.append("Source corrections: " + ', '.join(_prov_display(p) for p in sorted(restated)) + "; excluded from market moves.")
    if notices:
        excluded = sorted({n.split(':', 1)[0] for n in notices})
        lines.append("Comparison exclusions: " + ', '.join(_prov_display(p) for p in excluded) + ". Reasons and input dates are on the page.")
    if weekly:
        lines.append(f"Weekly reference: {len({r.provider for r in current})} providers with eligible observations; benchmark and evidence below.")
    lines.append(f"<{confluence_url}|Pricing and change ledger> · <https://nebius.atlassian.net/wiki/pages/viewpage.action?pageId=2164457614|Capacity>")
    return '\n'.join(lines)


def _ct_bucket_label(ct: str) -> str:
    if ct in INTERRUPTIBLE_CTS:            return "spot"
    if ct in RESERVED_1YR_CTS:             return "committed/reserved 1yr"
    if ct in RESERVED_2YR_CTS:             return "committed/reserved 2yr"
    if ct in RESERVED_3YR_CTS:             return "committed/reserved 3yr"
    if "reserved" in ct or "committed" in ct: return "committed/reserved"
    return "on-demand"


def _group_significant_moves(diffs: List[DiffEntry]) -> list:
    """
    Market price moves ≥ ALERT_THRESHOLD_PCT grouped by (provider, gpu, ct-bucket,
    direction), most significant first (median |Δ%|). The ONE grouping source for
    both the Slack thread moves block and the Confluence "Price Moves (last 24h)"
    section — they must render identical numbers, or Slack references detail that
    the page can't back up (2026-07-14 external-review finding). Nebius self-moves
    are excluded here; they're attributed separately in the summary lead.
    Raw diffs carry one entry per (instance × region × ct); grouping turns "AWS
    L40S reserved -17% across 30 SKUs" into one line instead of 90 bullets.
    """
    significant = [
        d for d in diffs
        if d.change_type == "price_change"
        and d.provider != "nebius"
        and not d.provider.startswith(("cp_", "sf_"))
        and d.source_feed not in {"computeprices", "shadeform"}
        and (provider_tier(d.provider) in ("raw_gpu_cloud", "hyperscaler",
                                           "enterprise_gpu_cloud")
             or d.provider in DIRECT_PLATFORM_PROVIDERS)
        and abs(d.delta_pct or 0) >= ALERT_THRESHOLD_PCT
    ]
    from collections import defaultdict as _dd
    groups: Dict[tuple, list] = _dd(list)
    for d in significant:
        direction = "up" if (d.delta_pct or 0) > 0 else "down"
        groups[(d.provider, d.gpu_model,
                _ct_bucket_label(d.consumption_type), direction)].append(d)

    out = []
    for (prov, gpu, bucket, direction), items in groups.items():
        pcts = [d.delta_pct or 0 for d in items]
        out.append({
            "provider": prov, "gpu": gpu, "bucket": bucket, "direction": direction,
            "items": items,
            "avg_pct": statistics.mean(pcts),
            "sku_count": len(set((d.instance_type, d.region) for d in items)),
            "peak": max(items, key=lambda d: abs(d.delta_pct or 0)),
            "tier_label": ("hyperscaler" if provider_tier(prov) == "hyperscaler"
                           else "major neocloud"
                           if prov.lower() in PROVIDER_TIERS.get("enterprise_gpu_cloud", [])
                           else "platform"
                           if prov.lower() in DIRECT_PLATFORM_PROVIDERS
                           else "small provider"),
        })
    out.sort(key=lambda g: -statistics.median([abs(d.delta_pct or 0)
                                               for d in g["items"]]))
    return out


def format_slack_message(diffs, run_date, confluence_url, records=None, provider_status=None):
    """Bounded supporting reply. Unchanged reference tables remain on Confluence."""
    current, notices = publication_records(records or [], run_date)
    diffs = _publication_diffs(diffs, current)
    lines = [f"*Pricing changes and exceptions · {run_date}*"]
    for g in _group_significant_moves(diffs or [])[:6]:
        d = g['peak']
        lines.append(f"• {_prov_display(d.provider)} {d.gpu_model} {g['bucket']}: "
                     f"${d.old_price:.2f} → ${d.new_price:.2f}/GPU-h ({d.delta_pct:+.1f}%, {d.region}); "
                     f"{g['sku_count']} observed SKU/region pairs.")
    if not _group_significant_moves(diffs or []):
        lines.append(f"No eligible price changes above the {ALERT_THRESHOLD_PCT:g}% reporting threshold.")
    restated = [d for d in (diffs or []) if d.change_type == 'restatement']
    if restated:
        lines.append(f"{len(restated)} source-correction records excluded from market movement.")
    if notices:
        lines.append("*Inputs excluded from current comparisons:*")
        lines.extend('• ' + n for n in notices[:5])
        if len(notices) > 5:
            lines.append(f"{len(notices)-5} further exclusions are listed on the page.")
    rows, generated, stale = _load_reserve_wins()
    if rows and stale:
        lines.append(f"CRM win aggregates are historical (generated {generated}); they are not a current rolling window.")
    lines.append("Prices describe observed offers. Availability, contract terms and acceptance require separate evidence.")
    lines.append(f"<{confluence_url}|Full change ledger, source dates and expandable evidence>")
    return '\n'.join(lines)


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
    from intel_schema import is_valid, is_expired, scope_key
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = []
    seen = set()
    try:
        with open(INTEL_CSV, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("message_date", "") < cutoff:
                    continue
                if not is_valid(row):
                    continue   # schema guard (Phase: forced-structured-output port)
                if is_expired(row, date.today()) or row.get("quote_status") == "signed_deal":
                    continue  # signed evidence has its own ledger; not a live asking-price floor
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
                    scope_key(row),
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
    # STORM audit fix (2026-07-06): one evidence window. Headline callers (lowest
    # committed deal, action flags, decision triggers) all use 90d; this table is the
    # evidence they cite, so it must cover the same window or headline deals become
    # untraceable (e.g. a 70-day-old lost deal cited above but absent here).
    intel_rows = _load_intel(days=90)
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
        'not public rack rates. Customer names removed; competitor/deal specifics remain: '
        '<strong>internal only, do not paste externally.</strong> '
        '"vs Nebius" compares against Nebius committed pricing at the closest matching term. '
        'Evidence skews negative: AEs log competitor quotes and losses, wins are rarely '
        'posted — read this as the pressure side of the market, not the whole market.</em></p>'
    )

    has_data = False
    for gpu in GPU_ORDER + FIELD_ONLY_GPUS:
        rows = sorted(by_gpu.get(gpu, []), key=lambda r: r["message_date"], reverse=True)
        if not rows:
            continue
        has_data = True

        cap_note = (f" — showing latest 12 of {len(rows)} deals (90d window)"
                    if len(rows) > 12
                    else f" — {len(rows)} deal{'s' if len(rows) != 1 else ''} (90d window)")
        html.append(f"<h3>{gpu}{cap_note}</h3>")
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
        elif st == "catalogue_only":
            stale.append(f"{p} catalogue refreshed; no scoped numeric price")
        else:
            stale.append(f"{p} {st or 'unknown'}")
    color = "green" if len(live) == total else ("yellow" if live else "red")
    banner = (f'<span data-type="status" data-color="{color}">'
              f'{len(live)}/{total} sources live</span>')
    detail = f' — other source states: {escape(", ".join(stale))}' if stale else " — all sources live this run"
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
             and r.provider in ent and is_public_benchmark_eligible(r)]
    if not cands:
        return None
    prov = min(cands, key=lambda r: r.price_per_gpu_hour_usd).provider
    series: Dict[str, float] = {}
    try:
        with open(HISTORY_CSV, newline="") as f:
            for r in load_comparison_history(HISTORY_CSV):
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
    public list prices barely exist. Returns {price, term, label, prepay, is_loss,
    loss, loss_fresh} or None.
    is_loss = a loss/win-against-Nebius was logged in the 90d window (drives the badge).
    loss_fresh = that loss is within LOSS_FLAG_DECAY_DAYS (drives action surfaces).
    """
    def _is_loss(notes: str) -> bool:
        n = (notes or "").lower()
        return "vs ne" in n or "win vs" in n or "lost" in n or "loss" in n

    def _row_info(r):
        try:
            term = int(float(r.get("term_months", "0") or 0))
        except (ValueError, TypeError):
            term = 0
        return {
            "price": float(r["price_per_gpu_hour_usd"]), "term": term,
            "label": (r.get("provider_name") or r.get("provider_type") or "undisclosed"),
            "date": r.get("message_date", ""),
        }

    best = None
    loss = None   # the actual most-recent recorded loss, NOT the cheapest quote
    for r in _load_intel(days=90):
        if r.get("gpu_model") != gpu:
            continue
        try:
            px = float(r["price_per_gpu_hour_usd"])
        except (ValueError, KeyError, TypeError):
            continue
        if px <= 0:
            continue
        if _is_loss(r.get("notes", "")) and \
                (loss is None or r.get("message_date", "") > loss["date"]):
            loss = _row_info(r)
        if best is None or px < best["price"]:
            best = _row_info(r)
    if best is not None:
        best["is_loss"] = loss is not None
        # STORM audit fix (2026-07-06): previously the loss flag was glued to the
        # cheapest 90d quote, so "lost a deal at the field price" cited a price from a
        # different deal than the loss. Carry the real losing quote separately.
        best["loss"] = loss
        fresh = loss is not None
        if fresh and loss.get("date"):
            try:
                fresh = (date.today() - date.fromisoformat(loss["date"])).days \
                    <= LOSS_FLAG_DECAY_DAYS
            except (ValueError, TypeError):
                pass   # unparseable date -> never decays
        best["loss_fresh"] = fresh
    return best


def _recommended_action(delta_vs_median: Optional[float], near_hyperscaler: bool,
                        trend30: Optional[float], field_loss: bool = False) -> str:
    """Neutral, decision-oriented action. Lower is not assumed good; a premium is a
    valid position to hold. Phrasing prompts a decision, doesn't prescribe a cut.
    A recorded competitive LOSS is the strongest trigger and leads the action —
    but only while fresh (callers pass field_loss from loss_fresh, which decays
    after LOSS_FLAG_DECAY_DAYS; the table badge outlives it)."""
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


def _decision_trigger_rows(records: List[PriceRecord], gpus: List[str] = None):
    """
    Shared per-GPU decision-trigger inputs, used by both the Confluence HTML table
    (_build_decision_trigger_table) and the Slack text "Action flags" sections. One
    dict per GPU that has a Nebius on-demand price, carrying the position delta, the
    cheapest peer/hyperscaler, the field deal, the 30d market trend and the neutral
    recommended action. Guards everything: missing trend/field data is left None and
    the caller decides whether the row is actionable. `gpus` defaults to GPU_ORDER.
    """
    position = {row["gpu"]: row for row in compute_position(records)
                if row["tier_label"] == "on_demand"}
    rows = []
    for gpu in (gpus if gpus is not None else GPU_ORDER):
        row = position.get(gpu)
        if not row or row.get("nebius_price") is None:
            continue
        neb = row["nebius_price"]
        median = row.get("median_peer")
        delta = ((neb - median) / median * 100) if median else None

        detail = row.get("cheapest_peers_detail") or []
        floor_n, floor_p = detail[0] if detail else (None, None)

        hyp = _best_comparable(records, gpu, "on_demand", tiers=["hyperscaler"])
        near_hyp = bool(hyp) and \
            (hyp.price_per_gpu_hour_usd - neb) / hyp.price_per_gpu_hour_usd * 100 < 5

        fi = _field_intel_floor(gpu)
        _t = _market_trend(gpu, 30, records)
        t30 = _t[0] if _t else None
        t_span = _t[1] if _t else None
        action = _recommended_action(delta, near_hyp, t30,
                                     field_loss=bool(fi and fi.get("loss_fresh")))
        rows.append({
            "gpu": gpu,
            "neb": neb,
            "median": median,
            "delta": delta,
            "floor_name": floor_n,
            "floor_price": floor_p,
            "hyp": hyp,
            "near_hyp": near_hyp,
            "fi": fi,
            "trend30": t30,
            "trend_span": t_span,
            "action": action,
        })
    return rows


def _trend_text(trend30, span) -> str:
    """
    Plain-text 30d market-trend tag for Slack: '+6% / 21d', or '' when history is too
    young (mirrors the HTML _trend_cell 'building' state, but omits rather than prints
    a placeholder in the tight Slack rows).
    """
    if trend30 is None or span is None:
        return ""
    sign = "+" if trend30 >= 0 else ""
    return f"{sign}{trend30:.0f}% / {span}d"


def _is_actionable_trigger(r: dict) -> bool:
    """
    A trigger row is worth surfacing in Slack when it carries a real signal: a FRESH
    recorded competitive loss (decayed losses keep only the Confluence badge), a wide
    peer-median gap (premium to review or headroom to hold/raise), hyperscaler parity
    to watch, or a moved (>=5%) market trend. Rows that are just 'Hold; monitor' with
    nothing moving are skipped so the section stays scannable.
    """
    if r["fi"] and r["fi"].get("loss_fresh"):
        return True
    if r["delta"] is not None and (r["delta"] >= 15 or r["delta"] <= -5):
        return True
    if r["near_hyp"]:
        return True
    if r["trend30"] is not None and abs(r["trend30"]) >= 5:
        return True
    return False


def _format_action_flags_thread(records: List[PriceRecord]) -> str:
    """
    Slack thread section: per revenue-driver GPU with a real trigger, the recommended
    action + 30d market-trend direction + a lost-deal flag where present. Same inputs as
    the Confluence decision-trigger table (_decision_trigger_rows). Skips GPUs with no
    actionable trigger; returns '' when none qualify (young history, no field deals).
    """
    rows = [r for r in _decision_trigger_rows(records, SUMMARY_GPUS)
            if _is_actionable_trigger(r)]
    if not rows:
        return ""
    lines = ["\n*Action flags (revenue-driver GPUs):*"]
    for r in rows:
        trend = _trend_text(r["trend30"], r["trend_span"])
        loss = ""
        if r["fi"] and r["fi"].get("loss_fresh"):
            L = r["fi"].get("loss") or {}
            loss = f"  ⚠ lost deal (since {L['date']})" if L.get("date") else "  ⚠ lost deal"
        trend_str = f"  |  mkt {trend}" if trend else ""   # trend already carries "/ Nd"
        # The action string is shared with the HTML table (_recommended_action); normalize
        # its em-dash to a colon for the Slack rendering without touching the table output.
        action = r["action"].replace(" — ", ": ")
        lines.append(f"`{r['gpu']:<5}` {action}{trend_str}{loss}")
    lines.append("_Action = neutral pricing prompt (premium can be a deliberate hold); "
                 "see Decision Triggers table in Confluence._")
    return "\n".join(lines)


def _top_action_flag(records: List[PriceRecord]) -> str:
    """
    Single sharpest action flag for a revenue-driver GPU, for the summary headline. Picks
    the highest-priority trigger: a recorded competitive loss first, then the widest
    peer-median premium (review candidate), then a >=5% market move. Returns '' when no
    revenue-driver GPU has an actionable trigger (young history / no field deals).
    """
    rows = [r for r in _decision_trigger_rows(records, SUMMARY_GPUS)
            if _is_actionable_trigger(r)]
    if not rows:
        return ""

    def _priority(r):
        loss = 0 if (r["fi"] and r["fi"].get("loss_fresh")) else 1
        premium = r["delta"] if (r["delta"] is not None and r["delta"] >= 15) else None
        move = abs(r["trend30"]) if r["trend30"] is not None else 0
        # loss first; then largest premium-to-review; then largest market move.
        return (loss, -(premium or 0), -move)

    def _term_lbl(months):
        if not months:
            return "on-demand"
        return f"{months // 12}yr" if months % 12 == 0 else f"{months}mo"

    r = min(rows, key=_priority)
    trend = _trend_text(r["trend30"], r["trend_span"])
    if r["fi"] and r["fi"].get("loss_fresh") and r["fi"].get("loss"):
        L = r["fi"]["loss"]
        since = ""
        if r["fi"]["price"] < L["price"]:
            since = (f" Lowest quote since: ${r['fi']['price']:.2f} "
                     f"({_term_lbl(r['fi']['term'])}, {r['fi']['label']}).")
        # Flag age (STORM backlog #2): a standing flag must not read as fresh news.
        try:
            age = f", flag {max((date.today() - date.fromisoformat(L['date'])).days, 0)}d old"
        except (ValueError, TypeError):
            age = ""
        return (f"\n*Action flag:* {r['gpu']} lost a {_term_lbl(L['term'])} deal at "
                f"${L['price']:.2f} ({L['label']}, {L['date']}{age}).{since} "
                f"Review committed pricing for this SKU.")
    if r["fi"] and r["fi"].get("loss_fresh"):
        return (f"\n*Action flag:* {r['gpu']} lost a deal at the field price. "
                f"Review committed pricing for this SKU.")
    if r["delta"] is not None and r["delta"] >= 15:
        tail = f", market {trend}" if trend else ""
        return (f"\n*Action flag:* {r['gpu']} +{r['delta']:.0f}% vs peer median{tail}. "
                f"Review premium vs value.")
    if r["trend30"] is not None and r["trend30"] <= -5:
        return (f"\n*Action flag:* {r['gpu']} market softening ({trend}). "
                f"Watch peer on-demand floor.")
    if r["trend30"] is not None and r["trend30"] >= 5:
        return (f"\n*Action flag:* {r['gpu']} market firming ({trend}). "
                f"Headroom to revisit price.")
    if r["near_hyp"]:
        return (f"\n*Action flag:* {r['gpu']} near hyperscaler parity. "
                f"Watch the on-demand gap.")
    return ""


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
            loss_badge = ''
            if fi["is_loss"]:
                # Badge outlives the 30d action decay (until the loss leaves the 90d
                # intel window); the date shows reviewers how old the loss is.
                L = fi.get("loss") or {}
                lbl = f'lost deal {L["date"]}' if L.get("date") else 'lost deal'
                loss_badge = f' <span data-type="status" data-color="red">{lbl}</span>'
            field_cell = (f'${fi["price"]:.2f} <em>({_term_label(fi["term"])}, '
                          f'{_provider_display(fi["label"]) if fi["label"] not in ("undisclosed",) else fi["label"]})</em>{loss_badge}')
        else:
            field_cell = '—'

        _t = _market_trend(gpu, 30, records)
        t30 = _t[0] if _t else None
        action = _recommended_action(delta, near_hyp, t30, field_loss=bool(fi and fi.get("loss_fresh")))

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
          if is_public_benchmark_eligible(r)
          and r.provider == provider and r.gpu_model == gpu and r.consumption_type in cts]
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
    field = _best_aws_h100_field_deal()
    if field:
        fin += (f"; AWS negotiated {field[1]}mo deals seen at ${field[0]:.2f} "
                f"(field intel).")
    else:
        fin += "."
    payg = f"On-demand sits {gap_lo:.0f}–{gap_hi:.0f}% below hyperscaler SXM clusters"
    if h100_prem is not None:
        payg += f"; H100 {h100_prem:+.0f}% vs peer median (premium is a position, not a problem)"
    payg += "."
    cap = ("Nebius B300 is UK-private (sales-gated); GB200/GB300 are contact-sales. "
           "Market broadly capacity-constrained (on-demand reportedly sold out across GPU types, Apr 2026).")
    # "Watch" items are data-conditional: AWS 3yr only threatens the committed story
    # while its deepest effective rate actually undercuts Nebius's deepest tier.
    neb2_tldr = _cheapest(records, "nebius", "H100", RESERVED_2YR_CTS)
    aws3_tldr = _cheapest(records, "aws", "H100", {"reserved_3yr"})
    watches = []
    if aws3_tldr and neb2_tldr and aws3_tldr < neb2_tldr:
        watches.append("AWS 3yr all-upfront")
    if field:
        watches.append("negotiated hyperscaler deals (field intel)")
    watches.append("hyperscaler spot floors below our preemptible")
    sales = ("Strong vs hyperscaler rack rates and ~49% below Oracle B200. "
             f"Watch: {', '.join(watches)}.")

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
    # STORM sales fix (2026-07-07): the most common AE objection — a cheaper
    # single-GPU/marketplace quote — compared against the cluster-class basis.
    h_noncluster = [r for r in records
                    if r.gpu_model == "H100" and r.consumption_type == "on_demand"
                    and is_public_benchmark_eligible(r)
                    and r.provider != "nebius"
                    and provider_tier(r.provider) == "raw_gpu_cloud"
                    and not _is_cluster_peer(r)]
    cheap1x = min(h_noncluster, key=lambda r: r.price_per_gpu_hour_usd) if h_noncluster else None
    _ent = set(PROVIDER_TIERS.get("enterprise_gpu_cloud", []))
    h_cluster = [r for r in records
                 if r.gpu_model == "H100" and r.consumption_type == "on_demand"
                 and is_public_benchmark_eligible(r)
                 and r.provider != "nebius"
                 and r.provider.lower() in _ent and _is_cluster_peer(r)]
    cluster_floor = min(h_cluster, key=lambda r: r.price_per_gpu_hour_usd) if h_cluster else None
    nebh = _cheapest(records, "nebius", "H100", {"on_demand"})
    if cheap1x and cluster_floor and nebh:
        cards.append((
            '"A marketplace/neocloud quoted me way less"',
            f"Probably true and not comparable: ${cheap1x.price_per_gpu_hour_usd:.2f} "
            f"({_provider_display(cheap1x.provider)}) is a single-GPU/Ethernet SKU. Training "
            f"clusters need 8×SXM nodes on a fast fabric, where the peer floor is "
            f"${cluster_floor.price_per_gpu_hour_usd:.2f} ({_provider_display(cluster_floor.provider)}) "
            f"vs Nebius ${nebh:.2f}. Pivot the conversation to the cluster basis; ask what node "
            f"size and interconnect the quote covers.", "high"))
    if (aws3nu or aws3au) and neb2:
        parts = []
        if aws3nu:
            parts.append(f"${aws3nu:.2f} no-upfront")
        if aws3au:
            parts.append(f"${aws3au:.2f} 100%-prepaid")
        fi_h100 = _field_intel_floor("H100")
        fi_note = ""
        if fi_h100 and fi_h100.get("price"):
            fi_note = (f" Negotiated deals below list exist on both sides (e.g. AWS seen at "
                       f"${fi_h100['price']:.2f} in field intel) — if the customer has a real "
                       f"committed quote, escalate to pricing, don't argue list vs negotiated.")
        # Direction-aware: with upfront fees amortized correctly (2026-07-14) AWS's
        # deepest list rate does NOT undercut Nebius; keep the old concession copy only
        # if the data ever flips back.
        aws3_best = min(x for x in (aws3nu, aws3au) if x)
        if aws3_best < neb2:
            body = (f"True at the extreme: AWS H100 3yr is {' / '.join(parts)}. But that locks 3 years"
                    + (" and full prepayment" if aws3au else "")
                    + f". Nebius 2yr ${neb2:.2f} needs no 3rd-year lock or 100% upfront, and on-demand "
                    f"has no commitment at all. Sell flexibility, not the headline rate.")
        else:
            body = (f"Not on public list: AWS's deepest H100 discount (3yr {' / '.join(parts)}, "
                    f"effective incl. upfront) is still ABOVE Nebius 2yr ${neb2:.2f} — on a longer "
                    f"lock. If a customer claims cheaper AWS committed, it's a negotiated quote, "
                    f"not the list.")
        cards.append(('"AWS 3-year is cheaper"', body + fi_note, "high"))
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
        '<p><em><strong>How to use this page:</strong> list-price comparisons and battlecards are for '
        'positioning and objection handling. Do NOT quote to customers: field-intel deal rows '
        '(third-party negotiated deals, unverifiable externally) or the Reserve-wins rates '
        '(our internal benchmarks, not offers — quoting them sets a floor in the customer\'s head). '
        'For a real competing committed quote, escalate to the pricing team rather than matching on the spot.</em></p>',
        '<p><em><strong>Storage caveat:</strong> competitor list prices in these comparisons generally '
        f'include local NVMe scratch (e.g. CoreWeave {LOCAL_STORAGE_BUNDLED["coreweave"]["note"]}, '
        f'AWS {LOCAL_STORAGE_BUNDLED["aws"]["note"]} per 8-GPU node); Nebius bills storage separately, '
        'and H100/H200/B200 hosts have no local NVMe (B300 has an opt-in local pack ≈ +$0.24/GPU-hr). '
        'If the customer\'s TCO math includes storage, add Nebius network-storage cost before '
        'comparing — details in the local-storage comparability note above.</em></p>',
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


def _build_short_term_reserved_section(records):
    rows = [r for r in records if r.consumption_type == 'reserved_short' or r.provider == 'sfcompute']
    best = {}
    for r in rows:
        key = (record_key(r), r.gpu_count, r.gpu_count_relation, r.term_min_days,
               r.term_max_days, r.term_label, r.price_per_gpu_hour_usd)
        best[key] = r
    html = ['<h2>Short-term reservations and exchange observations</h2>',
            '<p>Published Capacity Block rates and marketplace observations are separate products. '
            'A listed price does not establish an available booking, cluster size or service guarantee. '
            'SF Compute exchange observations are not treated as interruptible spot.</p>',
            '<table><tbody><tr><th>Provider / GPU</th><th>$/GPU-h</th><th>Product / region</th><th>Quantity / duration</th><th>Observed UTC / source</th></tr>']
    for r in sorted(best.values(), key=lambda x: (x.gpu_model, x.provider, x.region, x.gpu_count, x.instance_type)):
        quantity = f'{r.gpu_count:g}' + ('+' if r.gpu_count_relation == 'minimum' else '')
        if r.gpu_count_relation == 'unknown': quantity = 'unknown'
        duration = r.term_label or (' / '.join(str(v) for v in (r.term_min_days, r.term_max_days) if v is not None) + ' days' if r.term_min_days is not None else 'duration unreported')
        html.append(f'<tr><td>{escape(_prov_display(r.provider))} {escape(r.gpu_model)}</td>'
                    f'<td>${r.price_per_gpu_hour_usd:.2f}</td><td>{escape(r.instance_type)} / {escape(r.region)}</td>'
                    f'<td>{quantity} GPUs · {escape(duration)}</td>'
                    f'<td>{escape(r.fetched_at)} · <a href="{escape(r.source_url, quote=True)}">source</a></td></tr>')
    html.append('</tbody></table>')
    if not best:
        html.append('<p>No eligible recent observations.</p>')
    return '\n'.join(html)


def format_spot_auction_page(records, run_date):
    records, notices = publication_records(records, run_date)
    enrich_comparability(records)
    html = [f'<p>Snapshot date: {escape(run_date)}. Prices in $/GPU-hour. Input dates are shown separately.</p>',
            '<h2>Interruptible spot and preemptible offers</h2>',
            '<p>These observations are interruptible. They do not establish a clearing price or a recommended auction floor.</p>',
            '<table><tbody><tr><th>GPU</th><th>Nebius preemptible</th><th>Hyperscaler regional-floor median</th></tr>']
    for gpu in GPU_ORDER:
        neb = _cheapest(records, 'nebius', gpu, INTERRUPTIBLE_CTS)
        floor = _representative_spot_floor(records, gpu, tiers=['hyperscaler'])
        n = f'${neb:.2f}' if neb else '—'
        f = f'${floor[1]:.2f} ({escape(_provider_display(floor[0]))})' if floor else '—'
        html.append(f'<tr><td>{gpu}</td><td>{n}</td><td>{f}</td></tr>')
    html.append('</tbody></table>')
    html.append(_build_short_term_reserved_section(records))
    html.append('<h2>Negotiated term deals</h2><p>Longer-term sales quotes and won deals remain in the '
                '<a href="https://nebius.atlassian.net/wiki/pages/viewpage.action?pageId=2285044257">term benchmark</a>. '
                'They are not combined with spot or reservation prices into a market floor.</p>')
    html.append(_input_notice_html(notices))
    return '\n'.join(html)


def _build_price_moves_section(diffs: List[DiffEntry]) -> str:
    """
    Confluence "Price Moves (last 24h)" — the daily change ledger. Renders the
    SAME groups as the Slack thread's moves block (shared _group_significant_moves),
    so every move the Slack summary references resolves to identical, visible
    numbers on the page (2026-07-14 external-review fix: the summary used to say
    "detail in Confluence" while the page had no such section). Always present —
    quiet days state "no significant moves" explicitly rather than omitting it.
    """
    moves = _group_significant_moves(diffs or [])
    restated = [d for d in (diffs or []) if d.change_type == "restatement"]
    restate_note = ""
    if restated:
        provs = sorted({_provider_display(d.provider) for d in restated})
        restate_note = (f'<p><em>Methodology note: {len(restated)} record(s) from '
                        f'{", ".join(provs)} were restated by a parser/methodology fix — '
                        f'their value changes vs the previous build are measurement '
                        f'corrections, not competitor moves, and are excluded from the '
                        f'list above.</em></p>')
    reverted = [d for d in (diffs or []) if d.change_type == "reversion"]
    if reverted:
        rp = sorted({f"{_provider_display(d.provider)} {d.gpu_model}" for d in reverted})
        restate_note += (f'<p><em>Jitter note: {len(reverted)} move(s) excluded as '
                         f'oscillation back to a recent level ({"; ".join(rp[:6])}) — '
                         f'a return to a recent observed level; the cause is unverified '
                         f'(e.g. EUR-priced providers relayed through an aggregator '
                         f'FX layer).</em></p>')
    html = ['<h2>Price Moves (since previous build)</h2>']
    if not moves:
        html.append(f'<p><em>No significant price moves (≥{ALERT_THRESHOLD_PCT:.0f}%) '
                    f'on tracked providers since the previous build.</em></p>')
        if restate_note:
            html.append(restate_note)
        return "\n".join(html)
    html.append('<table data-layout="default"><tbody>')
    html.append('<tr><th>Provider</th><th>GPU</th><th>Type</th>'
                '<th>Move</th><th>Peak SKU (old→new)</th><th>SKUs</th></tr>')
    for g in moves[:25]:
        best = g["peak"]
        best_pct = best.delta_pct or 0
        arrow = "▲" if g["direction"] == "up" else "▼"
        html.append(
            f'<tr><td><strong>{_provider_display(g["provider"])}</strong></td>'
            f'<td>{g["gpu"]}</td>'
            f'<td>{g["bucket"]}</td>'
            f'<td>{arrow} {g["avg_pct"]:+.1f}%'
            + (f' avg / {g["sku_count"]} SKUs' if g["sku_count"] > 1 else '')
            + '</td>'
            f'<td>${best.old_price:.2f} → ${best.new_price:.2f} '
            f'<em>({best.region}, {best_pct:+.1f}%)</em></td>'
            f'<td>{g["sku_count"]}</td></tr>'
        )
    html.append('</tbody></table>')
    if len(moves) > 25:
        html.append(f'<p><em>…and {len(moves) - 25} more provider/GPU groups '
                    f'below the display cap.</em></p>')
    html.append(f'<p><em>Moves are competitor list-price changes ≥{ALERT_THRESHOLD_PCT:.0f}% '
                'vs the previous data build (normally ~24h apart), grouped by '
                'provider/GPU/type; direction is not scored as good or bad. '
                'Nebius repricing is announced separately, never mixed into this list.</em></p>')
    if restate_note:
        html.append(restate_note)
    return "\n".join(html)


def _build_rtx_section(records: List[PriceRecord]) -> str:
    """
    Confluence twin of the Slack RTX callout (shared _rtx_market_stats), so the
    page shows every SKU Nebius sells — RTX PRO 6000 was previously visible only
    in the Monday thread. Deliberately NOT merged into the exec tables: its
    competitor set is inference platforms, not training-cluster peers.
    """
    s = _rtx_market_stats(records)
    if not s:
        return ""
    vs = (s["neb_od"] - s["median"]) / s["median"] * 100
    rows = [
        '<h2>RTX PRO 6000 — Inference / PAYG Card</h2>',
        '<p>Observed RTX PRO 6000 offers across GPU clouds, hyperscalers and inference platforms. '
        'Each provider contributes its cheapest eligible on-demand observation; this is a '
        'separate cohort from training-cluster peers, not a complete market census. '
        'Prices do not establish available capacity or equivalent service configurations.</p>',
        '<table data-layout="default"><tbody>',
        '<tr><th>Tier</th><th>Nebius</th><th>Market</th><th>Position</th></tr>',
        f'<tr><td>On-demand</td><td>${s["neb_od"]:.2f}</td>'
        f'<td>median ${s["median"]:.2f} · floor {_provider_display(s["floor_prov"])} '
        f'${s["floor"]:.2f} ({s["n_comp"]} providers)</td>'
        f'<td>{vs:+.0f}% vs median · {s["cheaper"]}/{s["n_comp"]} providers cheaper</td></tr>',
    ]
    if s["neb_sp"] is not None:
        if s.get("spot_floor") is not None:
            d_sp = (s["neb_sp"] - s["spot_floor"]) / s["spot_floor"] * 100
            mkt = (f'floor ${s["spot_floor"]:.2f} '
                   f'({_provider_display(s["spot_floor_prov"])}, {s["n_spot"]} provider'
                   f'{"s" if s["n_spot"] != 1 else ""})')
            pos = f'{d_sp:+.0f}% vs market spot floor'
        else:
            mkt, pos = '—', '—'
        rows.append(f'<tr><td>Spot / preemptible</td><td>${s["neb_sp"]:.2f}</td>'
                    f'<td>{mkt}</td><td>{pos}</td></tr>')
    if s["neb_comm"] is not None:
        mkt = f'from ${s["comp_comm"]:.2f}' if s["comp_comm"] is not None else '—'
        rows.append(f'<tr><td>Committed</td><td>from ${s["neb_comm"]:.2f}</td>'
                    f'<td>{mkt}</td><td>—</td></tr>')
    rows.append('</tbody></table>')
    return "\n".join(rows)


def format_confluence_table(records, run_date, provider_status=None, diffs=None, catalogue_report=None, quote_report=None):
    raw_records = records
    records, notices = publication_records(records, run_date)
    enrich_comparability(records)
    diffs = _publication_diffs(diffs, records)
    html = [f'<p><strong>GPU pricing · {escape(run_date)}</strong>. Observed public offers in $/GPU-hour. '
            'The report date is not the verification date of every input.</p>',
            '<p><a href="https://nebius.atlassian.net/wiki/pages/viewpage.action?pageId=2164457614">Capacity</a> · '
            '<a href="https://nebius.atlassian.net/wiki/pages/viewpage.action?pageId=2285044257">Term benchmarks</a> · '
            '<a href="https://nebius.atlassian.net/wiki/pages/viewpage.action?pageId=1970110707">Spot and short-term reservations</a></p>',
            _input_notice_html(notices), _build_price_moves_section(diffs),
            '<h2>Current on-demand benchmark</h2>',
            '<p>Each enterprise provider contributes its cheapest eligible cluster-class offer (8-GPU SXM where available). '
            'Single-card markets use a separate fallback. Provider counts describe this observed cohort, not market coverage. '
            'List prices do not prove available capacity or realized transaction prices.</p>',
            _build_executive_table(records)]
    def detail(title, body):
        return f'<div data-type="expand" data-title="{escape(title, quote=True)}">{body}</div>'
    html.append(detail('Source dates, exclusions and comparison method',
                       _run_health_line(provider_status) + _input_dates_html(records) + _build_local_storage_note()
                       + _secondary_changes_html(diffs)))
    html.append(detail('Negotiated deals and historical CRM wins',
                       _build_field_committed_section(records) + _build_reserve_wins_section()))
    html.append(detail('Short-term reservation observations', _build_short_term_reserved_section(records)))
    html.append(detail('Full provider evidence, account catalogues and managed platforms',
                       _build_peer_tables(records) + _build_qualified_catalogue_section(raw_records, provider_status)
                       + _build_platform_section(records) + _build_rtx_section(records)))
    html.append(detail('Aggregator offers by configuration, region and term',
                       _aggregator_offers_html(raw_records, run_date)))
    html.append(detail('Direct offers requiring qualification',
                       _aggregator_offers_html(raw_records, run_date, direct_references=True)))
    from coverage_report import build_price_coverage, render_coverage
    if catalogue_report is not None:
        from offer_catalogue import render_catalogue
        html.append(detail('Quote-only products and commercial plans', render_catalogue(catalogue_report)))
    if quote_report is not None:
        from quote_evidence import render_quote_report
        html.append(detail('Negotiated evidence: asking prices and signed deals', render_quote_report(quote_report)))
    html.append(detail('Coverage by competitor, GPU, region and purchase type',
                       render_coverage(build_price_coverage(raw_records, run_date, provider_status,
                                        catalogue_offers=(catalogue_report or {}).get("offers", []), quote_report=quote_report))))
    html.append(detail('Regional and term price tables', _build_hyperscaler_tables(records)))
    html.append('<p>Methodology correction, 18 September 2026: the Azure fractional-GPU and Together '
                'on-demand changes previously reported on 17 September were source corrections. '
                'Azure history is re-normalized from preserved instance prices; the affected Together '
                'on-demand history is excluded because its true historical on-demand rate is unverified. '
                'Archived raw observations are retained.</p>')
    return '\n'.join(html)


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
        '<th>Peers in median (n)</th>'
        '</tr>'
    )

    for gpu in GPU_ORDER:
        row = pos_by_gpu.get(gpu)

        nebius_td = _price_td(row["nebius_price"] if row else None)

        if row and row["cheapest_peer"]:
            source_label = {"computeprices": "ComputePrices", "shadeform": "Shadeform"}.get(
                row.get("cheapest_peer_source"), row.get("cheapest_peer_source", ""))
            source_note = f' · via {escape(source_label)}' if source_label else ''
            peer_td = (f'<td>${row["cheapest_peer"]:.2f} '
                       f'<em>({_provider_display(row["cheapest_peer_name"]) if row["cheapest_peer_name"] else ""})</em>{source_note}</td>')
        else:
            peer_td = '<td>—</td>'

        # vs median (more meaningful than vs floor for pricing decisions).
        # STORM audit fix (2026-07-06): a "median" of one provider is not a market
        # verdict — same min-n rule as the Slack thread (suppress % when n < 2).
        if row and row["nebius_price"] and row["median_peer"] and row["total_peers"] >= 2:
            pct = (row["nebius_price"] - row["median_peer"]) / row["median_peer"] * 100
            loz_color = "red" if pct > 15 else ("yellow" if pct > 0 else "green")
            sign = "+" if pct >= 0 else ""
            vs_td = (f'<td><span data-type="status" data-color="{loz_color}">'
                     f'{sign}{pct:.0f}% vs median</span></td>')
        elif row and row["median_peer"] and row["total_peers"] == 1:
            vs_td = '<td><em>1 peer only — no median verdict</em></td>'
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
        # STORM audit fix (2026-07-06): show the n the median was actually computed
        # over (cluster-class peers), not the registry count inflated by +1 for
        # Nebius — the Slack thread and this table must agree on n.
        count_td = f'<td>{row["total_peers"] if row else 0}</td>'

        rows.append(
            f'<tr><td><strong>{gpu}</strong></td>'
            f'{nebius_td}{peer_td}{vs_td}{hyp_td}{med_td}{count_td}</tr>'
        )

    rows.append('</tbody></table>')
    return "\n".join(rows)


def _build_local_storage_note() -> str:
    """
    Comparability footnote under the Executive Benchmark: whether the $/GPU-hr
    list price includes local NVMe scratch. First product attribute normalized
    on this page (the 2026-07-14 external review flagged that only form factor /
    interconnect were) — data is hand-verified static config in comparability.py,
    not scraped, so it carries its own verification date. Framing is neutral:
    states the packaging difference, no verdict. Providers we haven't checked are
    named explicitly so absence reads as "unverified", not "no bundled storage".
    """
    inc = "; ".join(f'{_provider_display(b)} {e["note"]}'
                    for b, e in LOCAL_STORAGE_BUNDLED.items() if e["included"])
    seen = set(LOCAL_STORAGE_BUNDLED)
    unverified = []
    for key in PROVIDER_TIERS["hyperscaler"] + PROVIDER_TIERS["enterprise_gpu_cloud"]:
        base = key[3:] if key.startswith("cp_") else key
        if base not in seen:
            seen.add(base)
            unverified.append(_provider_display(key))
    unv = f' Not yet verified: {", ".join(unverified)}.' if unverified else ""
    return (
        '<p><em><strong>Comparability note — local storage:</strong> most competitor list '
        f'prices above include local NVMe scratch in the $/GPU-hr rate (per 8-GPU node: {inc}). '
        f'RunPod does not ({LOCAL_STORAGE_BUNDLED["runpod"]["note"]}). '
        f'Nebius does not: {LOCAL_STORAGE_BUNDLED["nebius"]["note"]}. '
        'Like-for-like TCO comparisons should add Nebius network-storage cost to the Nebius '
        f'rate, or strip scratch from the competitor rate. Verified {LOCAL_STORAGE_VERIFIED} '
        'from provider pricing pages/docs; static config, not scraped.'
        f'{unv}</em></p>'
    )


def _term_bucket_cts(term: int):
    """Map a field-deal term (months) to the matching Nebius committed CT set."""
    if 9 <= term <= 15:
        return RESERVED_1YR_CTS, "1yr"
    if 16 <= term <= 30:
        return RESERVED_2YR_CTS, "2yr"
    if 31 <= term <= 48:
        return RESERVED_3YR_CTS, "3yr"
    return None, f"{term}mo"


def _build_field_committed_section(records):
    """Dated observations without expired reference prices or invented prepay."""
    html = ['<h3>Field price observations</h3><p>Sales-reported observations from the last 90 days; '
            'not a representative market sample or proof of customer acceptance. Terms and prepayment differ. '
            'Zero prepayment is shown only when explicitly recorded as known.</p>',
            '<table><tbody><tr><th>GPU</th><th>Provider / $ per GPU-h</th><th>Months</th><th>Prepay</th><th>Reported date / source</th><th>Notes</th></tr>']
    rows = sorted(_load_intel(days=90), key=lambda r: r.get('message_date', ''), reverse=True)
    for r in rows:
        try:
            term = float(r.get('term_months') or 0)
            price = float(r.get('price_per_gpu_hour_usd') or 0)
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        known = str(r.get('prepay_known', '')).lower() in ('1', 'true', 'yes')
        prepay = str(r.get('prepay_pct', 'unknown')) + '%' if known else 'unknown'
        source = escape(r.get('message_date', 'unknown'))
        ts = r.get('message_ts', '')
        if ts.replace('.', '').isdigit():
            source = f'<a href="https://nebius.slack.com/archives/C06PM90GV0U/p{ts.replace(".", "")}">{source}</a>'
        html.append(f'<tr><td><strong>{escape(r.get("gpu_model", ""))}</strong></td>'
                    f'<td>{escape(r.get("provider_name") or r.get("provider_type") or "undisclosed")} / ${price:.2f}</td>'
                    f'<td>{format(term, "g") if term > 0 else "unreported / no term recorded"}</td>'
                    f'<td>{escape(prepay)}</td><td>{source}</td><td>{escape(r.get("notes", ""))}</td></tr>')
    html.append('</tbody></table>')
    return '\n'.join(html)


def _build_prepay_note(records: List[PriceRecord]) -> str:
    """
    Make the hyperscaler reserved prepay structure explicit (Phase: reserved depth).
    The committed table shows the deepest (all-upfront) rate; this states the no-upfront
    rate alongside so the comparison is apples-to-apples, not just the cheapest cell.
    """
    au3 = _cheapest(records, "aws", "H100", {"reserved_3yr"})
    nu3 = _cheapest(records, "aws", "H100", {"reserved_3yr_no_upfront"})
    au1 = _cheapest(records, "aws", "H100", {"reserved_1yr"})
    nu1 = _cheapest(records, "aws", "H100", {"reserved_1yr_no_upfront_convertible"})
    parts = []
    if au3 and nu3:
        parts.append(f"3yr ${au3:.2f} all-upfront vs ${nu3:.2f} no-upfront")
    if au1 and nu1:
        parts.append(f"1yr ${au1:.2f} all-upfront vs ${nu1:.2f} no-upfront (convertible)")
    if not parts:
        return ""
    return ('<p><em><strong>Prepay structure:</strong> hyperscaler reserved cells above show the '
            'deepest (all-upfront, 100%-prepaid) rate. AWS H100: ' + '; '.join(parts) + '. '
            'No-upfront trades a higher rate for zero prepayment; GCP CUD is no-upfront by default; '
            'Nebius committed shown is its 100%-upfront enterprise tier. A like-for-like comparison '
            'should match prepay terms — the all-upfront vs no-upfront gap is ~2× on AWS 3yr.</em></p>')


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
        if r.gpu_model not in SHOW_GPUS or not is_public_benchmark_eligible(r):
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
        'Storage basis: hyperscaler rates in this table include bundled local NVMe scratch per 8-GPU node '
        f'(AWS {LOCAL_STORAGE_BUNDLED["aws"]["note"]}, Azure {LOCAL_STORAGE_BUNDLED["azure"]["note"]}, '
        f'GCP {LOCAL_STORAGE_BUNDLED["gcp"]["note"]}); Nebius bills storage separately per GiB — '
        'see the local-storage comparability note under the Executive Benchmark. '
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
    """One table per GPU, rows = raw GPU clouds + hyperscalers, sorted by price,
    each tagged (peer / pricefighter / hyperscaler). Hyperscalers included since
    2026-09-02 (Koen: "for B300 I can't see any hyperscalers" — they were confined
    to the exec table's single cheapest-hyperscaler cell). Deduplicated to
    cheapest per provider (multi-node-size providers like Lambda list one row per
    node size; we show only the cheapest to avoid clutter).
    """
    html = []
    for gpu in GPU_ORDER:
        raw_peers = [r for r in records
                     if r.gpu_model == gpu
                     and r.consumption_type == "on_demand"
                     and not is_qualified_catalogue_reference(r)
                     and provider_tier(r.provider) in ("raw_gpu_cloud", "hyperscaler")]
        # Deduplicate: keep cheapest record per provider
        best_by_prov: Dict[str, PriceRecord] = {}
        for r in raw_peers:
            if r.provider not in best_by_prov or \
                    r.price_per_gpu_hour_usd < best_by_prov[r.provider].price_per_gpu_hour_usd:
                best_by_prov[r.provider] = r
        peers = sorted(best_by_prov.values(), key=lambda r: r.price_per_gpu_hour_usd)
        if not peers:
            continue

        html.append(f'<h3>{gpu} — On-demand (all tracked providers, sorted by price)</h3>')
        html.append('<table data-layout="full-width"><tbody>')
        html.append(
            '<tr><th>Provider</th><th>Tag</th><th>$/GPU-hr</th>'
            '<th>Node size</th><th>Region</th><th>vs Nebius</th></tr>'
        )

        nebius_price = next(
            (r.price_per_gpu_hour_usd for r in peers if r.provider == "nebius"), None)

        for r in peers:
            prov_display = _prov_display(r.provider)   # keeps AWS/GCP uppercase
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
                f'{_tag_cell(r.provider)}'
                f'<td><strong>${r.price_per_gpu_hour_usd:.2f}</strong></td>'
                f'<td>{node_str}</td>'
                f'<td>{r.region}</td>'
                f'{vs_td}</tr>'
            )
        html.append('</tbody></table>')

        # Providers publishing this GPU ONLY as spot/interruptible would otherwise
        # be invisible in this sweep (e.g. CoreWeave B300 spot $4.48 with no
        # on-demand list — Koen's 2026-09-02 catch). Footnote them.
        od_provs = set(best_by_prov)
        spot_only: Dict[str, float] = {}
        for r in records:
            if (r.gpu_model == gpu
                    and r.consumption_type in INTERRUPTIBLE_CTS
                    and not is_qualified_catalogue_reference(r)
                    and r.provider not in od_provs
                    and r.provider != "nebius"
                    and provider_tier(r.provider) in ("raw_gpu_cloud", "hyperscaler")):
                if (r.provider not in spot_only
                        or r.price_per_gpu_hour_usd < spot_only[r.provider]):
                    spot_only[r.provider] = r.price_per_gpu_hour_usd
        if spot_only:
            shown = sorted(spot_only.items(), key=lambda kv: kv[1])[:6]
            names = ", ".join(f"{_prov_display(p)} ${v:.2f}" for p, v in shown)
            more = f" (+{len(spot_only) - 6} more)" if len(spot_only) > 6 else ""
            html.append(
                f'<p><em>Spot/interruptible-only public pricing for {gpu} '
                f'(no on-demand list): {names}{more}. Not comparable to the '
                f'on-demand rows above.</em></p>'
            )

    return "\n".join(html)


def _build_qualified_catalogue_section(records: List[PriceRecord],
                                       provider_status: dict = None) -> str:
    """Keep constrained public tariffs inspectable without implying buyability."""
    from comparability import is_crusoe_unscoped_reference
    refs = [r for r in records if is_qualified_catalogue_reference(r) or r.price_basis == "account_catalog"]
    if not refs:
        return ""
    refs = sorted(refs, key=lambda r: (r.provider, r.gpu_model, r.instance_type,
                                      r.consumption_type, r.region))
    html = [
        '<h2>Catalogue prices — deployment restricted or unconfirmed</h2>',
        '<p>Published tariffs with restricted deployment, unreported configuration '
        'or unknown deployment eligibility. These records are excluded '
        'from ordinary price comparisons, cheapest-provider statistics and price-move '
        'alerts. They do not establish live stock, account quota, or multi-node access. '
        'Where configuration is documented, the full instance is the minimum priced unit. '
        'A per-GPU reference with unknown configuration does not establish an instance price.</p>',
        '<table data-layout="full-width"><tbody>',
        '<tr><th>Provider / GPU</th><th>Exact SKU / tier</th>'
        '<th>Minimum priced instance</th><th>$/GPU-hr</th><th>Location</th>'
        '<th>Qualification / source</th><th>Observation / freshness</th></tr>',
    ]
    for r in refs:
        tier = CT_LABELS.get(r.consumption_type, r.consumption_type)
        label = QUALIFIED_CATALOGUE_BASES.get(r.price_basis, "Account catalogue; deployment and public eligibility unverified")
        if is_crusoe_unscoped_reference(r):
            label = 'Public per-GPU tariff; configuration and minimum order unreported'
        source = (f'<a href="{escape(r.source_url, quote=True)}">Official catalogue</a>'
                  if r.source_url else 'Source unavailable')
        location = r.region if r.region not in {'', 'unspecified'} else 'Not listed'
        try:
            observed = datetime.fromisoformat((r.fetched_at or '').replace('Z', '+00:00'))
            observed_text = (observed.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                             if observed.tzinfo is not None else 'Timezone unreported')
        except (TypeError, ValueError):
            observed_text = 'Observation time unreported'
        status = (provider_status or {}).get(r.provider, {})
        freshness = 'Refresh status unreported'
        if status.get('status') == 'live':
            freshness = 'Refreshed this run'
        elif status.get('status') in {'cache', 'cached'}:
            freshness = 'Cached; not refreshed this run'
            age = status.get('cache_age_hours')
            if type(age) in (int, float) and age >= 0:
                freshness += f' (cache age {age:g}h)'
        elif status.get('status') in {'error', 'failed'}:
            freshness = 'Fetch failed; not refreshed this run'
        unit = (f'{r.gpu_count:g} GPUs · ${r.price_per_hour_usd:.2f}/instance-hr'
                if not is_crusoe_unscoped_reference(r) else 'Configuration and minimum order unknown')
        html.append(
            f'<tr><td>{escape(_provider_display(r.provider))} / {escape(r.gpu_model)}</td>'
            f'<td>{escape(r.instance_type)}<br />{escape(tier)}</td>'
            f'<td>{unit}</td>'
            f'<td>${r.price_per_gpu_hour_usd:.4f}</td><td>{escape(location)}</td>'
            f'<td>{escape(label)}<br />{source}</td>'
            f'<td>{escape(observed_text)}<br />{escape(freshness)}</td></tr>'
        )
    html.append('</tbody></table>')
    return "\n".join(html)


# What each platform's $/GPU-hr equivalent actually buys — shown verbatim in the
# platform table's Basis column (verified 2026-09-02 from provider pages).
_PLATFORM_BASIS = {
    "modal": "per-second serverless × 3600; CPU + RAM billed ON TOP",
    "baseten": "per-minute dedicated deployment × 60; vCPU + RAM bundled",
}


def _build_platform_section(records: List[PriceRecord]) -> str:
    """Serverless / managed platforms (Modal, Baseten direct fetchers + the
    aggregator-sourced inference platforms): $/GPU-hr equivalents converted from
    per-second / per-minute billing. Rendered as awareness data, tagged platform,
    and NEVER folded into peer medians or exec positioning (2026-09-02 Koen ask:
    "add Modal and Baseten to the tracked list")."""
    plat = [r for r in records
            if provider_tier(r.provider) == "managed_inference"
            and r.consumption_type == "on_demand"
            and r.gpu_model in GPU_ORDER]
    if not plat:
        return ""
    best: Dict[tuple, PriceRecord] = {}
    for r in plat:
        k = (r.provider, r.gpu_model)
        if k not in best or r.price_per_gpu_hour_usd < best[k].price_per_gpu_hour_usd:
            best[k] = r

    neb: Dict[str, float] = {}
    for r in records:
        if (r.provider == "nebius" and r.consumption_type == "on_demand"
                and r.gpu_model not in neb):
            neb[r.gpu_model] = r.price_per_gpu_hour_usd

    html = [
        '<h2>Serverless / Managed Platforms — $/GPU-hr Equivalents</h2>',
        '<p>Platform rates converted to $/GPU-hr from per-second or per-minute '
        'billing. These bundle orchestration/autoscaling — and some bill CPU/RAM '
        'on top (see basis notes) — so they are <strong>not comparable</strong> to '
        'raw IaaS cluster rates and are excluded from all peer medians and '
            'position lines. Differences in service scope prevent these rates '
            'from establishing a raw-compute price ceiling.</p>',
        '<table data-layout="default"><tbody>',
        '<tr><th>Provider</th><th>GPU</th><th>$/GPU-hr equiv.</th>'
        '<th>vs Nebius on-demand</th><th>Basis</th></tr>',
    ]
    for (prov, gpu), r in sorted(
            best.items(),
            key=lambda kv: (GPU_ORDER.index(kv[0][1]),
                            kv[1].price_per_gpu_hour_usd)):
        prov_display = prov.replace("cp_", "").replace("-", " ").title()
        nb = neb.get(gpu)
        if nb:
            diff = (r.price_per_gpu_hour_usd - nb) / nb * 100
            sign = "+" if diff >= 0 else ""
            loz = "green" if diff > 0 else "red"
            vs_td = (f'<td><span data-type="status" data-color="{loz}">'
                     f'{sign}{diff:.0f}%</span></td>')
        else:
            vs_td = '<td>—</td>'
        basis = _PLATFORM_BASIS.get(prov, "aggregator-relayed platform rate")
        html.append(
            f'<tr><td>{prov_display}</td>'
            f'<td><strong>{gpu}</strong></td>'
            f'<td><strong>${r.price_per_gpu_hour_usd:.2f}</strong></td>'
            f'{vs_td}'
            f'<td><em>{basis}</em></td></tr>'
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
        'Regional-provider cells show the cheapest observed price in a region mapped to that geography. '
        'Term groups can contain different payment options; verify the underlying offer before comparing terms. '
        '*Nebius is a price reference: its displayed column is not evidence of availability in that geography. '
        '†Oracle is an aggregator-sourced global reference repeated across geographies, not a regional quote. '
        'Blank cells mean no eligible observation in this table, not that the provider does not serve the region. '
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


def _input_notice_html(notices):
    rows, generated, stale = _load_reserve_wins()
    items = list(notices)
    if rows and stale:
        items.append(f"CRM win aggregates generated {generated}: historical only; current comparison withheld")
    if not items:
        return '<p>All displayed quote observations meet the 48-hour freshness gate.</p>'
    return '<p><strong>Comparison limits:</strong></p><ul>' + ''.join('<li>' + escape(n) + '</li>' for n in items) + '</ul>'


def _input_dates_html(records):
    by_source = defaultdict(list)
    for r in records:
        by_source[(r.provider, r.source_feed or r.data_source)].append(r.source_observed_at or r.fetched_at)
    html = ['<table><tbody><tr><th>Input</th><th>Observation time range (UTC)</th><th>Rows</th></tr>']
    for (provider, source), dates in sorted(by_source.items()):
        html.append(f'<tr><td>{escape(provider)} / {escape(source)}</td><td>{escape(min(dates))} to {escape(max(dates))}</td><td>{len(dates)}</td></tr>')
    html.append('</tbody></table>')
    return '\n'.join(html)


def _aggregator_offers_html(records, as_of, direct_references=False):
    """Inspectable offer evidence, including stale and unavailable listings."""
    from urllib.parse import urlsplit
    from source_priority import canonicalize_provider_sources
    if direct_references:
        rows = [r for r in records if r.parser_version == "direct-offers-1" and not r.comparison_eligible]
    else:
        rows = [r for r in canonicalize_provider_sources(records)
                if r.source_type == "aggregator" or r.source_feed in {"computeprices", "shadeform"}]
    if not rows:
        return '<p>No direct offers require qualification.</p>' if direct_references else '<p>No aggregator observations in this snapshot.</p>'
    html = ['<p>Each row is a source observation. Different configurations, regions, commercial tiers and terms '
            'are retained. A provider contributes once to the peer benchmark, even when several feeds cover it. '
            'Listings are not negotiated transaction prices or proof of multi-node capacity. '
            'Stale, undated and explicitly unavailable offers remain below but are excluded from current comparisons. '
            'Payment terms are unknown unless separately documented.</p>',
            '<table data-layout="full-width"><tbody><tr><th>Provider / feed</th><th>GPU / offer</th>'
            '<th>Region / configuration</th><th>Term / tier</th><th>USD per GPU-hour</th>'
            '<th>Source stock signal</th><th>Source update / fetched (UTC)</th><th>Comparison status / source</th></tr>']
    for row in sorted(rows, key=lambda r: (r.provider, r.gpu_model, r.region,
                                          r.consumption_type, r.instance_type, r.source_feed, r.offer_id)):
        eligible, notices = publication_records([row], as_of)
        status = "Current listing; availability unverified" if row.available is None else "Current listing"
        if not eligible:
            status = "; ".join(notices)
        elif not is_public_benchmark_eligible(row):
            status = "Reference only; excluded from public benchmark"
        stock = {True: "Reported available", False: "Reported unavailable", None: "Unknown"}[row.available]
        term = (f"{row.commitment_months} months" if row.commitment_months is not None
                else row.consumption_type.replace("_", " "))
        if row.term_label:
            term += " / " + row.term_label
        if row.offer_variant:
            term += " / " + row.offer_variant
        source = ''
        if urlsplit(row.source_url).scheme in {"https", "http"}:
            source = f'<br /><a href="{escape(row.source_url, quote=True)}">Source</a>'
        html.append(
            f'<tr><td>{escape(_provider_display(row.provider))}<br />{escape(row.source_feed or row.data_source or "unknown")}</td>'
            f'<td>{escape(row.gpu_variant or row.gpu_model)}<br />{escape(row.instance_type)}</td>'
            f'<td>{escape(row.region)}<br />{row.gpu_count:g} GPU(s), {escape(row.form_factor or "unknown")} / '
            f'{escape(row.interconnect or "unknown")}</td>'
            f'<td>{escape(term)}</td><td>${row.price_per_gpu_hour_usd:.4f}</td><td>{stock}</td>'
            f'<td>{escape(row.source_observed_at or "Source date unknown")}<br />Fetched: {escape(row.fetched_at)}</td>'
            f'<td>{escape(status)}{source}</td></tr>')
    html.append('</tbody></table>')
    return '\n'.join(html)


def _publication_diffs(diffs, records):
    keys = {record_key(r) for r in records if is_public_benchmark_eligible(r)}
    return [d for d in (diffs or []) if d.change_type == 'restatement' or
            record_key(d) in keys]


def _secondary_changes_html(diffs):
    rows = [d for d in (diffs or []) if d.change_type in ('reversion', 'catalog_reference_change', 'coverage_reference_change', 'aggregator_update') or
            (d.change_type == 'price_change' and d.provider.startswith(('cp_', 'sf_')))]
    if not rows:
        return ''
    html = ['<h3>Other observed source changes</h3><p>Aggregator updates, returns to prior levels, '
            'catalogue changes and gaps in coverage are retained here. They are not verified provider repricing events.</p>',
            '<table><tbody><tr><th>Provider / GPU / region</th><th>Type</th><th>Old / new $ per GPU-h</th></tr>']
    for d in rows:
        old = format(d.old_price, '.4f') if d.old_price is not None else '—'
        new = format(d.new_price, '.4f') if d.new_price is not None else '—'
        html.append(f'<tr><td>{escape(d.provider)} / {escape(d.gpu_model)} / {escape(d.region)}</td>'
                    f'<td>{escape(d.change_type)}</td><td>{old} / {new}</td></tr>')
    return '\n'.join(html) + '</tbody></table>'
