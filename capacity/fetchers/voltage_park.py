"""
Voltage Park capacity fetcher — public bare-metal locations API with LIVE
GPU COUNTS (no auth, verified 2026-08-12; both counts were 0 that day =
sold out).

GET https://cloud-api.voltagepark.com/api/v1/bare-metal/locations:
results[].gpu_count_ethernet / gpu_count_infiniband = H100 GPUs rentable
right now per fabric, plus live prices. The authenticated instant-VM
endpoint exists but adds little; this is the honest headline number.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

API = "https://cloud-api.voltagepark.com/api/v1/bare-metal/locations"
SOURCE_URL = "https://www.voltagepark.com/"

_GPU_MAP = {"h100": "H100", "h200": "H200", "b200": "B200", "b300": "B300"}
_LIMITED_MAX_GPUS = 64


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        data = json.loads(http_get(API, timeout=30))
    except Exception as e:
        logger.error(f"Voltage Park fetch failed: {e}")
        return []

    per_model: dict = {}
    for loc in data.get("results", []):
        specs = loc.get("specs_per_node") or {}
        model_raw = (specs.get("gpu_model") or "").lower()
        model = next((m for frag, m in _GPU_MAP.items() if frag in model_raw), None)
        if not model:
            continue
        eth = int(loc.get("gpu_count_ethernet") or 0)
        ib = int(loc.get("gpu_count_infiniband") or 0)
        agg = per_model.setdefault(model, {"eth": 0, "ib": 0, "locations": 0})
        agg["eth"] += eth
        agg["ib"] += ib
        agg["locations"] += 1

    records: List[AvailabilityRecord] = []
    for model, agg in per_model.items():
        total = agg["eth"] + agg["ib"]
        if total == 0:
            state, detail = "sold_out", "0 GPUs listed (Ethernet and InfiniBand)"
        elif total <= _LIMITED_MAX_GPUS:
            state = "limited"
            detail = f"{total} GPUs live ({agg['ib']} IB / {agg['eth']} Eth)"
        else:
            state = "available"
            detail = f"{total} GPUs live ({agg['ib']} IB / {agg['eth']} Eth)"
        records.append(AvailabilityRecord(
            provider="voltage_park", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="stock_level", metric_value=float(total),
            detail=detail,
            fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
        ))

    if not records:
        logger.warning("Voltage Park: no GPU locations parsed")
    logger.info(f"Voltage Park: {len(records)} records")
    return records
