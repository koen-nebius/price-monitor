"""
Data-shaping layer between records and artifacts (STORM redesign 2026-08-12).

Turns the day's AvailabilityRecords into the decision-oriented reads the
artifacts render: per-GPU live tightness (k/n at cluster scale), market
gauges, the price join against the sibling pricing monitor, decision
triggers, GTM claims with provenance grades, and history streaks.
"""
import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from capacity.config import (
    FLAGSHIP_GPUS, PENDING_ACTIVATION, PRICE_JOIN_PEERS, PROVIDER_LABELS,
    SIGNAL_CLASS,
)
from capacity.schema import AvailabilityRecord, CapacityDiffEntry

logger = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"
PRICING_SNAPSHOT = Path(__file__).parent.parent / "store" / "last_snapshot.json"

# Nebius public region codes → customer-language names (only confident ones).
NEBIUS_REGION_NAMES = {
    "eu-north1": "Finland", "eu-north2": "Iceland", "eu-west1": "Paris",
    "me-west1": "Israel", "us-central1": "Kansas (US)", "uk-south1": "London (UK)",
}


def region_label(code: str) -> str:
    name = NEBIUS_REGION_NAMES.get(code)
    return f"{name}" if name else code


def geo(region: str) -> str:
    """Coarse geography for GTM lines."""
    r = region.lower()
    if r.startswith(("us", "u-")) or "-us" in r:
        return "US"
    if r.startswith(("eu", "europe", "fr-", "pl-", "nl-", "uk", "norway")):
        return "EU"
    if r.startswith(("asia", "ap-", "australia", "japan")):
        return "APAC"
    if r.startswith("me-"):
        return "ME"
    if r.startswith(("ca", "canada")):
        return "CA"
    return region


def signal_class(r: AvailabilityRecord) -> str:
    # Old snapshots collapsed dedicated-inference replicas into a global GPU
    # stock signal. Unscoped caches/aggregator rows must fail closed as well.
    if r.provider == "together":
        return "inference" if is_together_inference(r) else "unverified_scope"
    if r.provider == "lambda":
        return "instance" if is_lambda_instance(r) else "unverified_scope"
    if r.provider == "scaleway":
        return "instance_stock" if is_scaleway_instance(r) else "unverified_scope"
    # The provider has two independent evidence types. Cached documentation
    # never becomes live capacity merely because credentials were later added.
    if r.provider == "crusoe":
        return "live" if is_crusoe_api_quantity(r) else "footprint"
    if r.consumption_type == "spot":
        return "spot"
    return SIGNAL_CLASS.get(r.provider, "footprint")


def is_crusoe_api_quantity(r: AvailabilityRecord) -> bool:
    """Authenticated exact-SKU quantity, not a GPU count or cluster signal."""
    return (r.provider == "crusoe" and r.metric_type == "provider_quantity"
            and r.data_source == "official_api")


def crusoe_quantity_records(records: List[AvailabilityRecord]) -> List[AvailabilityRecord]:
    """Preserve alternative shapes/slices individually; quantities may overlap."""
    return sorted((r for r in records if is_crusoe_api_quantity(r)),
                  key=lambda r: (r.gpu_model, r.instance_type, r.region, r.consumption_type))


def is_together_inference(r: AvailabilityRecord) -> bool:
    return (r.provider == "together" and r.metric_type == "inference_replicas"
            and r.data_source == "official_api"
            and getattr(r, "product_scope", "") == "dedicated_inference")


def together_inference_records(records: List[AvailabilityRecord]) -> List[AvailabilityRecord]:
    """Keep every inference configuration separate; never roll into GPU stock."""
    return sorted((r for r in records if is_together_inference(r)),
                  key=lambda r: (r.gpu_model, r.instance_type, r.region))


def is_lambda_instance(r: AvailabilityRecord) -> bool:
    """Exact on-demand VM launchability; never a cluster-stock observation."""
    expected_metric = "launchable_regions" if r.region == "global" else "instance_launchability"
    return (r.provider == "lambda" and r.data_source == "official_api"
            and getattr(r, "product_scope", "") == "on_demand_instance"
            and r.consumption_type == "on_demand" and bool(r.instance_type)
            and type(getattr(r, "gpu_count", None)) is int and r.gpu_count > 0
            and r.metric_type == expected_metric)


def lambda_instance_records(records: List[AvailabilityRecord]) -> List[AvailabilityRecord]:
    """Retain individual shapes and regions without summing overlapping offers."""
    return sorted((r for r in records if is_lambda_instance(r)),
                  key=lambda r: (r.gpu_model, r.instance_type, r.fetched_at, r.region))


def is_scaleway_instance(r: AvailabilityRecord) -> bool:
    """Exact GPU VM stock label; never provider-wide or multi-node stock."""
    return (r.provider == "scaleway" and r.data_source == "official_api"
            and r.product_scope == "gpu_instance"
            and r.consumption_type == "on_demand" and bool(r.instance_type)
            and bool(r.region) and r.region != "global"
            and type(r.gpu_count) is int and r.gpu_count > 0
            and r.metric_type == "instance_stock_status")


def scaleway_instance_records(records: List[AvailabilityRecord]) -> List[AvailabilityRecord]:
    return sorted((r for r in records if is_scaleway_instance(r)),
                  key=lambda r: (r.gpu_model, r.instance_type, r.region, r.fetched_at))


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


# ── Per-provider live reads ──────────────────────────────────────────────────

def live_reads(records: List[AvailabilityRecord], gpu: str) -> List[dict]:
    """One read per LIVE provider for a GPU (on-demand, global rows).

    Multi-SKU providers: most-available state across DIRECT variant rows wins
    (first-variant-wins misstated RunPod RTX6000 as fully sold out while its
    Server Edition had stock — red-team 2026-08-14). Aggregator rows are a
    fallback only. The legacy cluster_ok key now requires explicit eight-GPU
    evidence, not a generic 'available' label. User-facing node summaries also
    include qualified exact-instance sources via node_reads, separately from
    this fleet-style helper; neither helper establishes multi-node stock."""
    reads = []
    providers = {r.provider for r in records if signal_class(r) == "live"}
    for provider in sorted(providers):
        if provider == "nebius":
            continue
        rows = [r for r in records
                if r.provider == provider and r.gpu_model == gpu
                and r.consumption_type == "on_demand" and r.region == "global"
                and signal_class(r) == "live"
                and not is_crusoe_api_quantity(r)
                and r.state in _RANK]
        if not rows:
            continue
        direct = [r for r in rows if r.data_source != "aggregator"]
        pool = direct or rows
        row = min(pool, key=lambda r: _RANK[r.state])
        is_agg = row.data_source == "aggregator"
        reads.append({
            "provider": provider,
            "label": PROVIDER_LABELS.get(provider, provider),
            "state": row.state,
            "aggregator": is_agg,
            "cluster_ok": any(node_observation(r) == "available" for r in pool),
            "detail": row.detail,
        })
    return sorted(reads, key=lambda x: ({"available": 0, "limited": 1, "sold_out": 2}[x["state"]], x["label"]))


def tightness(records: List[AvailabilityRecord], gpu: str) -> Optional[dict]:
    reads = live_reads(records, gpu)
    if not reads:
        return None
    n = len(reads)
    return {
        "gpu": gpu,
        "reads": reads,
        "n": n,
        "k_cluster": sum(1 for r in reads if r["cluster_ok"]),
        "k_any": sum(1 for r in reads if r["state"] != "sold_out"),
        "any_aggregator": any(r["aggregator"] for r in reads),
    }


# Exact single-node evidence is deliberately separate from fleet-level reads.
# A positive instance label never establishes simultaneous multi-node capacity.
def node_observation(record: AvailabilityRecord) -> Optional[str]:
    """Return available/absent/unknown for an explicitly scoped eight-GPU node.

    None means the source is not an eight-GPU observation. Aggregate GPU counts,
    inference replicas, marketplace depth and aggregator booleans are not nodes.
    """
    r = record
    if r.data_source != "official_api" or r.consumption_type != "on_demand":
        return None
    if ((is_lambda_instance(r) or is_scaleway_instance(r)) and r.gpu_count == 8):
        return {"available": "available", "limited": "available",
                "sold_out": "absent"}.get(r.state, "unknown")
    if r.provider == "runpod" and r.region == "global" and r.metric_type == "stock_status_label":
        # The fetcher's metric is the result of its explicit gpuCount=8 query.
        # Low remains positive stock; generic 'limited' also covers 1x-only stock.
        if re.search(r"8x: (High|Medium|Low)\b", r.detail):
            return "available"
        if "no 8-GPU" in r.detail or "no Secure Cloud stock at 1x or 8x" in r.detail:
            return "absent"
        return "unknown"
    if r.provider == "verda" and r.region == "global":
        # The API enumerates deployable instance sizes. Older global rows retain
        # the size explicitly in their detail; do not infer it from GPU totals.
        size = re.search(r"largest node (\d+)x", r.detail)
        if size:
            return "available" if int(size.group(1)) == 8 else ("absent" if int(size.group(1)) < 8 else "unknown")
        if "no 8x node" in r.detail:
            return "absent"
        if r.state == "sold_out" and r.detail == "in catalog but deployable in no location":
            return "absent"
    return None


def node_reads(records: List[AvailabilityRecord], gpu: str, manifest: dict = None) -> List[dict]:
    """One provider vote for observed eight-GPU configurations, unknown separately.

    Positive means at least one observed SKU/zone has a positive node signal.
    Absent means all eligible observed configurations report absence, never a
    claim about the provider's unobserved/private fleet. Cached data is unknown
    for today's count and remains inspectable in the underlying evidence.
    """
    groups = {}
    for r in records:
        if (r.gpu_model != gpu or r.provider == "nebius" or r.consumption_type != "on_demand"
                or signal_class(r) not in {"live", "instance", "instance_stock"}):
            continue
        groups.setdefault(r.provider, []).append(r)
    out = []
    for provider, rows in sorted(groups.items()):
        direct = [r for r in rows if r.data_source == "official_api"]
        scoped = [(r, node_observation(r)) for r in direct]
        scoped = [(r, state) for r, state in scoped if state is not None]
        states = [state for _, state in scoped]
        state = ("available" if "available" in states else
                 "absent" if states and all(v == "absent" for v in states) else "unknown")
        feed = (manifest or {}).get("provider_status", {}).get(provider, {})
        if feed and feed.get("status") != "live":
            state = "unknown"
        out.append({"provider": provider, "label": PROVIDER_LABELS.get(provider, provider),
                    "status": state, "records": [r for r, _ in scoped] or direct or rows,
                    # Lambda region membership is the measured launchability,
                    # not a change in which exact SKU was checked. Verda's
                    # global row switches metric names when all sizes disappear.
                    "scope": sorted({(r.instance_type, "global" if provider == "lambda" else r.region,
                                      "eight_gpu_availability" if provider in {"lambda", "verda"} else r.metric_type)
                                     for r, _ in scoped}),
                    "basis": "official API" if scoped else "8-GPU scope unverified"})
    return out


def node_summary(records, gpu, manifest=None):
    reads = node_reads(records, gpu, manifest)
    return {"gpu": gpu, "reads": reads,
            "available": [r for r in reads if r["status"] == "available"],
            "absent": [r for r in reads if r["status"] == "absent"],
            "unknown": [r for r in reads if r["status"] == "unknown"],
            "checked": [r for r in reads if r["status"] != "unknown"]}


def node_changes(records, old_records, manifest=None):
    """Matched-provider status changes; changing coverage is reported separately."""
    changes, coverage = [], []
    for gpu in FLAGSHIP_GPUS:
        now = {r["provider"]: r for r in node_reads(records, gpu, manifest)}
        old = {r["provider"]: r for r in node_reads(old_records, gpu)}
        now_checked = {p for p, r in now.items() if r["status"] != "unknown"}
        old_checked = {p for p, r in old.items() if r["status"] != "unknown"}
        added, lost = now_checked - old_checked, old_checked - now_checked
        if added or lost:
            coverage.append({"gpu": gpu, "added": sorted(added), "lost": sorted(lost)})
        for provider in sorted(now_checked & old_checked):
            n, o = now[provider], old[provider]
            if n["scope"] != o["scope"]:
                coverage.append({"gpu": gpu, "changed_scope": [provider], "added": [], "lost": []})
            elif n["status"] != o["status"]:
                changes.append({"gpu": gpu, "provider": provider, "old": o["status"], "new": n["status"]})
    return changes, coverage


# ── Market gauges (marketplace / spot context) ───────────────────────────────

def market_gauges(records: List[AvailabilityRecord], gpu: str) -> dict:
    out = {}
    for r in records:
        if r.gpu_model != gpu:
            continue
        if r.provider == "sfcompute" and r.metric_type == "clearing_price_usd" and r.metric_value:
            out["sfc_clearing"] = r.metric_value
            out["sfc_detail"] = r.detail
        if r.provider == "vast" and r.metric_type == "offer_depth_gpus":
            import re
            out["vast_gpus"] = int(r.metric_value or 0)
            out["vast_detail"] = r.detail
            m = re.search(r"min \$([\d.]+)", r.detail or "")
            out["vast_floor"] = f"${m.group(1)}" if m else None
        if r.provider == "aws" and r.consumption_type == "spot" and r.region == "global":
            out["aws_spot_regions"] = int(r.metric_value or 0)
    return out


# ── Nebius reference + canary ────────────────────────────────────────────────

def nebius_reference(records: List[AvailabilityRecord], gpu: str) -> dict:
    """Outside-in view of our own shelf: footprint (docs) + the Shadeform live
    read, which is deliberately NOT suppressed for nebius (a live 'not
    buyable' on a GPU we list self-service is a canary, not noise)."""
    footprint = [r for r in records if r.provider == "nebius" and r.gpu_model == gpu
                 and r.region == "global" and r.data_source != "aggregator"]
    live = [r for r in records if r.provider == "nebius" and r.gpu_model == gpu
            and r.region == "global" and r.data_source == "aggregator"]
    out = {"regions": [], "sales_gated": False, "canary": None}
    if footprint:
        f = footprint[0]
        out["sales_gated"] = "sales-gated" in f.detail
        out["regions"] = [region_label(r.region) for r in records
                          if r.provider == "nebius" and r.gpu_model == gpu
                          and r.region != "global" and r.data_source != "aggregator"]
    if live and footprint and not out["sales_gated"]:
        l = live[0]
        if l.state == "sold_out":
            out["canary"] = (f"listed self-service but not buyable via Shadeform today "
                             f"({l.detail})")
    return out


# ── Price join with the sibling pricing monitor ──────────────────────────────

def price_join(records: List[AvailabilityRecord]) -> Dict[str, dict]:
    """Per GPU: Nebius OD price, cheapest listed enterprise peer, and cheapest
    BOOKABLE peer (listed price AND live capacity state != sold_out). A great
    price at a sold-out provider is not a competing price."""
    try:
        prices = json.loads(PRICING_SNAPSHOT.read_text())
    except Exception as e:
        logger.warning(f"price join unavailable: {e}")
        return {}

    listed: Dict[str, list] = {}
    nebius_od: Dict[str, float] = {}
    for p in prices:
        if p.get("consumption_type") != "on_demand":
            continue
        px = p.get("price_per_gpu_hour_usd")
        if not px:
            continue
        gpu = p["gpu_model"]
        if p["provider"] == "nebius":
            nebius_od[gpu] = min(nebius_od.get(gpu, 1e9), px)
        cap_key = PRICE_JOIN_PEERS.get(p["provider"])
        if cap_key:
            listed.setdefault(gpu, []).append((px, cap_key))

    out = {}
    for gpu, entries in listed.items():
        reads = {r["provider"]: r for r in live_reads(records, gpu)}
        entries.sort()
        cheapest_listed = entries[0]
        bookable = [(px, prov) for px, prov in entries
                    if reads.get(prov) and reads[prov]["state"] != "sold_out"]
        out[gpu] = {
            "nebius_od": nebius_od.get(gpu),
            "cheapest_listed": {"price": cheapest_listed[0],
                                "provider": PROVIDER_LABELS.get(cheapest_listed[1], cheapest_listed[1]),
                                "sold_out": bool(reads.get(cheapest_listed[1]))
                                            and reads[cheapest_listed[1]]["state"] == "sold_out"},
            "cheapest_bookable": ({"price": bookable[0][0],
                                   "provider": PROVIDER_LABELS.get(bookable[0][1], bookable[0][1]),
                                   "aggregator": reads[bookable[0][1]]["aggregator"]}
                                  if bookable else None),
        }
    return out


# ── GTM claims with provenance grades ────────────────────────────────────────

def gtm_claims(records: List[AvailabilityRecord],
               diff: List[CapacityDiffEntry]) -> dict:
    """Automatic customer-facing claims are not supported by point-in-time feeds."""
    return {"ammo": [], "expired": []}


# ── History: streaks + trend maturity ────────────────────────────────────────

def history_days() -> int:
    """Distinct days accumulated in history.csv."""
    f = STORE_DIR / "history.csv"
    if not f.exists():
        return 0
    days = set()
    with f.open() as fh:
        for row in csv.DictReader(fh):
            days.add(row.get("date"))
    return len(days)


def days_in_state(provider: str, gpu: str, current_state: str) -> Optional[int]:
    """Consecutive days (incl. today) the provider's global on-demand row has
    held the current state. None until history has ≥2 days."""
    f = STORE_DIR / "history.csv"
    if not f.exists():
        return None
    by_day = {}
    with f.open() as fh:
        for row in csv.DictReader(fh):
            if (row["provider"] == provider and row["gpu_model"] == gpu
                    and row["region"] == "global" and row["consumption_type"] == "on_demand"):
                by_day[row["date"]] = row["state"]
    if len(by_day) < 2:
        return None
    streak = 0
    for day in sorted(by_day, reverse=True):
        if by_day[day] == current_state:
            streak += 1
        else:
            break
    return streak


# ── Provider-level aggregate state (variant flips must not fire triggers) ───

_RANK = {"available": 0, "limited": 1, "sold_out": 2}


def agg_state(records: List[AvailabilityRecord], provider: str, gpu: str,
              direct_only: bool = True) -> Optional[str]:
    """Most-available state across a provider's GLOBAL on-demand rows (all SKU
    variants). direct_only skips aggregator fallbacks — an aggregator boolean
    flapping intra-day must never fire a trigger (red-team 2026-08-12)."""
    states = [r.state for r in records
              if r.provider == provider and r.gpu_model == gpu
              and r.region == "global" and r.consumption_type == "on_demand"
              and signal_class(r) == "live" and not is_crusoe_api_quantity(r)
              and r.state in _RANK
              and (not direct_only or r.data_source != "aggregator")]
    if not states:
        return None
    return min(states, key=lambda s: _RANK[s])


def provider_transitions(records: List[AvailabilityRecord],
                         old_records: List[AvailabilityRecord],
                         direct_only: bool = True) -> List[dict]:
    """Provider-level (all variants aggregated) state transitions on flagship
    GPUs since the previous build."""
    out = []
    providers = {r.provider for r in records
                 if signal_class(r) == "live" and r.provider != "nebius"}
    for gpu in FLAGSHIP_GPUS:
        for provider in sorted(providers):
            new = agg_state(records, provider, gpu, direct_only)
            old = agg_state(old_records, provider, gpu, direct_only)
            if new and old and new != old:
                out.append({"provider": provider, "gpu": gpu,
                            "old": old, "new": new})
    return out


# ── Decision triggers ────────────────────────────────────────────────────────

def evaluate_triggers(records: List[AvailabilityRecord],
                      old_records: List[AvailabilityRecord],
                      diff: List[CapacityDiffEntry]) -> List[dict]:
    """Outside-in exceptions only; no fleet-wide or pricing-action inference."""
    fired = []

    # T3 — Nebius canary: listed self-service but not bookable per the
    # aggregator. Fires on TRANSITION only — the same line every day since
    # launch is wallpaper, not signal (red-team 2026-08-14). The persistent
    # condition lives in the Confluence TL;DR instead.
    canary_now = [gpu for gpu in FLAGSHIP_GPUS
                  if nebius_reference(records, gpu).get("canary")]
    canary_before = [gpu for gpu in FLAGSHIP_GPUS
                     if old_records and nebius_reference(old_records, gpu).get("canary")]
    new_canaries = [g for g in canary_now if g not in canary_before]
    if new_canaries:
        fired.append({
            "id": "T3", "owner": "self-service", "level": "watch",
            "text": f"Nebius {', '.join(new_canaries)} not bookable in Shadeform's "
                    f"resale view, new since yesterday (not checked in our console)",
        })

    # de-dup identical texts (T1 can repeat across SKU variants)
    seen, unique = set(), []
    for f in fired:
        if f["text"] not in seen:
            seen.add(f["text"])
            unique.append(f)
    return unique


# ── Freshness ────────────────────────────────────────────────────────────────

def freshness(manifest: dict) -> dict:
    status = manifest.get("provider_status", {})
    failed = [p for p, s in status.items()
              if s.get("status") == "failed" and p not in PENDING_ACTIVATION]
    pending = [p for p, s in status.items()
               if s.get("status") == "failed" and p in PENDING_ACTIVATION]
    stale = [p for p, s in status.items() if s.get("status") == "cached"]
    paused = [p for p, s in status.items() if s.get("status") == "paused"]
    activated = [p for p in status if p not in pending and p not in paused]
    live = [p for p, s in status.items() if s.get("status") == "live"]
    return {"live": live, "failed": failed, "stale": stale,
            "pending": pending, "paused": paused, "activated": activated}
