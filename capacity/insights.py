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
    if r.consumption_type == "spot":
        return "spot"
    return SIGNAL_CLASS.get(r.provider, "footprint")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


# ── Per-provider live reads ──────────────────────────────────────────────────

def live_reads(records: List[AvailabilityRecord], gpu: str) -> List[dict]:
    """One read per LIVE provider for a GPU (on-demand, global rows).

    Multi-SKU providers: most-available state across DIRECT variant rows wins
    (first-variant-wins misstated RunPod RTX6000 as fully sold out while its
    Server Edition had stock — red-team 2026-08-14). Aggregator rows are a
    fallback only, and can never prove CLUSTER stock: Shadeform booleans
    cannot see instance size, so cluster_ok requires a direct 'available'."""
    reads = []
    for provider, cls in SIGNAL_CLASS.items():
        if cls != "live" or provider == "nebius":
            continue
        rows = [r for r in records
                if r.provider == provider and r.gpu_model == gpu
                and r.consumption_type == "on_demand" and r.region == "global"
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
            "cluster_ok": row.state == "available" and not is_agg,
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
    """Sellout ammo graded by how safe it is to say in a customer call, plus
    talk tracks that EXPIRED today (restocks)."""
    ammo, expired = [], []
    for gpu in FLAGSHIP_GPUS:
        for r in live_reads(records, gpu):
            if r["state"] == "sold_out":
                grade = "verify first (aggregator)" if r["aggregator"] else "safe (provider's own API)"
                ammo.append({"gpu": gpu, "provider": r["label"], "grade": grade,
                             "detail": r["detail"]})
            elif r["state"] == "limited" and not r["aggregator"]:
                ammo.append({"gpu": gpu, "provider": r["label"],
                             "grade": "safe (provider's own API)",
                             "detail": f"no cluster-scale stock: {r['detail']}"})
    for c in diff:
        if (c.change_type == "state_change" and c.old_state == "sold_out"
                and c.new_state in ("available", "limited")
                and SIGNAL_CLASS.get(c.provider) == "live"):
            expired.append(f"{PROVIDER_LABELS.get(c.provider, c.provider)} {c.gpu_model} restocked "
                           f"({'aggregator read' if not c.instance_type else c.instance_type})")
    return {"ammo": ammo, "expired": expired}


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
    providers = {p for p, c in SIGNAL_CLASS.items() if c == "live" and p != "nebius"}
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
    """Named, owner-routed trigger conditions. Thresholds are PROPOSALS until
    the channel agrees them (stated on the Confluence page)."""
    fired = []

    # T1 — fleet-wide sellout / FULL restock at a DIRECT live source,
    # aggregated across SKU variants (a 1x-only partial restock is material
    # but not a trigger).
    for t in provider_transitions(records, old_records, direct_only=True):
        if t["new"] == "sold_out" or (t["old"] == "sold_out" and t["new"] == "available"):
            verb = "sold out" if t["new"] == "sold_out" else "restocked"
            fired.append({
                "id": "T1", "owner": "Pricing",
                "text": f"{PROVIDER_LABELS.get(t['provider'], t['provider'])} {t['gpu']} "
                        f"{verb} in all regions (own API)",
            })

    # T2 — cluster-scale in-stock share crosses 1/3 or 2/3 on a flagship GPU
    for gpu in FLAGSHIP_GPUS:
        new_t = tightness(records, gpu)
        old_t = tightness(old_records, gpu) if old_records else None
        if not new_t or not old_t or old_t["n"] == 0 or new_t["n"] == 0:
            continue
        new_share = new_t["k_cluster"] / new_t["n"]
        old_share = old_t["k_cluster"] / old_t["n"]
        for threshold in (1 / 3, 2 / 3):
            if (old_share - threshold) * (new_share - threshold) < 0:
                direction = "fell below" if new_share < old_share else "rose above"
                fired.append({
                    "id": "T2", "owner": "Pricing",
                    "text": f"{gpu} cluster-scale in-stock share {direction} "
                            f"{threshold:.0%}: now {new_t['k_cluster']}/{new_t['n']} live sources",
                })

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
            "id": "T3", "owner": "Self-service", "level": "watch",
            "text": f"NEW: Nebius {', '.join(new_canaries)} not bookable via Shadeform's "
                    f"resale view (verify in console before reacting)",
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
    activated = [p for p in status if p not in pending]
    live = [p for p, s in status.items() if s.get("status") == "live"]
    return {"live": live, "failed": failed, "stale": stale,
            "pending": pending, "activated": activated}
