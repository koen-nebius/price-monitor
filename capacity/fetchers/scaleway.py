"""
Scaleway capacity fetcher — PUBLIC per-zone availability API (no auth).

GET https://api.scaleway.com/instance/v1/zones/{zone}/products/servers/availability
returns a ternary enum per SKU: "available" | "scarce" | "shortage" —
provider-reported live stock state, the richest hyperscaler-style signal in
the peer set. Verified 2026-08-12: B300-SXM shortage, H100-SXM-8 available,
H100-2 scarce, L40S-8 shortage (fr-par-2).
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

ZONES = ["fr-par-1", "fr-par-2", "fr-par-3",
         "nl-ams-1", "nl-ams-2", "nl-ams-3",
         "pl-waw-1", "pl-waw-2", "pl-waw-3"]
API = "https://api.scaleway.com/instance/v1/zones/{zone}/products/servers/availability?per_page=100"
SOURCE_URL = "https://www.scaleway.com/en/pricing/gpu/"

# SKU prefix → GPU model (L4-/RENDER-/GPU3070 etc. ignored)
_SKU_GPU = [
    (re.compile(r"^GB300", re.I), "GB300"),
    (re.compile(r"^GB200", re.I), "GB200"),
    (re.compile(r"^B300", re.I), "B300"),
    (re.compile(r"^B200", re.I), "B200"),
    (re.compile(r"^H200", re.I), "H200"),
    (re.compile(r"^H100", re.I), "H100"),
    (re.compile(r"^L40S", re.I), "L40S"),
    (re.compile(r"^RTX-?PRO-?6000", re.I), "RTX6000"),
]

_STATE = {"available": "available", "scarce": "limited", "shortage": "sold_out"}
_RANK = {"available": 0, "limited": 1, "sold_out": 2}


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    from fetchers._http import http_get

    # (gpu_model, zone) → best state across SKU sizes; detail per SKU
    best: dict = {}
    zones_ok = 0
    for zone in ZONES:
        try:
            data = json.loads(http_get(API.format(zone=zone), timeout=30))
        except Exception as e:
            logger.debug(f"Scaleway {zone}: {e}")
            continue
        zones_ok += 1
        for sku, info in (data.get("servers") or {}).items():
            model = next((m for rx, m in _SKU_GPU if rx.match(sku)), None)
            if not model:
                continue
            state = _STATE.get((info.get("availability") or "").lower())
            if not state:
                continue
            key = (model, zone)
            cur = best.get(key)
            # Track the most-available state and the flagship (8x SXM) state
            is_flagship = bool(re.search(r"SXM-8|8-\d+G$|-8-", sku))
            if cur is None or _RANK[state] < _RANK[cur["state"]]:
                best.setdefault(key, {"state": state, "skus": {}})["state"] = state
            best.setdefault(key, {"state": state, "skus": {}})["skus"][sku] = (state, is_flagship)

    if zones_ok == 0:
        logger.error("Scaleway: no zone responded")
        return []

    records: List[AvailabilityRecord] = []
    per_model: dict = {}
    for (model, zone), cell in sorted(best.items()):
        flag_states = [s for s, fl in cell["skus"].values() if fl]
        detail_bits = [f"{sku}: {s}" for sku, (s, _fl) in sorted(cell["skus"].items())]
        state = cell["state"]
        # Small sizes available but flagship 8x sold out → limited overall
        if state == "available" and flag_states and all(s == "sold_out" for s in flag_states):
            state = "limited"
        per_model.setdefault(model, []).append(state)
        records.append(AvailabilityRecord(
            provider="scaleway", gpu_model=model, region=zone,
            consumption_type="on_demand", state=state,
            metric_type="stock_status_label",
            metric_value=float({"available": 2, "limited": 1, "sold_out": 0}[state]),
            detail="; ".join(detail_bits)[:160],
            fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
        ))

    for model, states in per_model.items():
        n_avail = sum(1 for s in states if s == "available")
        n_total = len(states)
        if n_avail:
            state = "available"
        elif any(s == "limited" for s in states):
            state = "limited"
        else:
            state = "sold_out"
        records.append(AvailabilityRecord(
            provider="scaleway", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="regions_with_capacity", metric_value=float(n_avail),
            detail=f"available in {n_avail}/{n_total} zone(s) with the SKU",
            fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
        ))

    logger.info(f"Scaleway: {len(records)} records from {zones_ok} zones")
    return records
