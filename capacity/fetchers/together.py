"""
Together AI capacity fetcher — dedicated-inference instance types with
per-region capacity HEADROOM (quantitative).

GET https://api.together.ai/v2/public/inference-instance-types (Bearer auth,
free key from api.together.xyz/settings/api-keys → TOGETHER_API_KEY secret):
data[].gpuType/gpuCount/regions[].headroom {value, relation} where value is
a (capped) count of replicas that currently fit and relation is RELATION_EQ
(exact) or RELATION_GTE (at least). Skips cleanly until the key is set.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

API = "https://api.together.ai/v2/public/inference-instance-types"
SOURCE_URL = "https://docs.together.ai/"

_GPU_MAP = {
    "gb300": "GB300", "gb200": "GB200", "b300": "B300", "b200": "B200",
    "h200": "H200", "h100": "H100", "l40s": "L40S", "rtx pro 6000": "RTX6000",
}

_LIMITED_MAX_HEADROOM = 2


def _match(gpu_type: str):
    low = (gpu_type or "").lower()
    for frag in sorted(_GPU_MAP, key=len, reverse=True):
        if frag in low:
            return _GPU_MAP[frag]
    return None


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    api_key = os.environ.get("TOGETHER_API_KEY")
    if not api_key:
        logger.warning("Together: TOGETHER_API_KEY not set — skipping "
                       "(free key: api.together.xyz/settings/api-keys)")
        return []

    try:
        from fetchers._http import http_get
        data = json.loads(http_get(API, headers={
            "Authorization": f"Bearer {api_key}", "Accept": "application/json",
        }, timeout=30))
    except Exception as e:
        logger.error(f"Together fetch failed: {e}")
        return []

    records: List[AvailabilityRecord] = []
    # (gpu_model, region) → best headroom seen across instance sizes
    best: dict = {}

    for item in data.get("data", []):
        gpu_model = _match(item.get("gpuType", ""))
        if not gpu_model:
            continue
        for region in item.get("regions") or []:
            name = region.get("name") or "unknown"
            head = region.get("headroom") or {}
            value = head.get("value")
            gte = (head.get("relation") == "RELATION_GTE")
            key = (gpu_model, name)
            cur = best.get(key)
            score = (value if value is not None else -1, gte)
            if cur is None or score > cur[0:2]:
                best[key] = (value if value is not None else -1, gte,
                             item.get("gpuCount"), item.get("id", ""))

    per_model_regions: dict = {}
    for (gpu_model, region), (value, gte, gpu_count, itype) in sorted(best.items()):
        if value < 0:
            state, detail = "unknown", "region listed, no headroom reported"
        elif value == 0:
            state, detail = "sold_out", "headroom 0 replicas"
        elif value <= _LIMITED_MAX_HEADROOM and not gte:
            state, detail = "limited", f"headroom {value} replica(s)"
        else:
            state = "available"
            detail = f"headroom {'≥' if gte else ''}{value} replica(s)"
        if state == "available":
            per_model_regions.setdefault(gpu_model, set()).add(region)
        records.append(AvailabilityRecord(
            provider="together", gpu_model=gpu_model, region=region,
            consumption_type="on_demand", state=state,
            metric_type="stock_level", metric_value=float(max(value, 0)),
            detail=detail, instance_type=itype,
            fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
        ))

    # Global matrix rows
    for gpu_model in {m for m, _ in best}:
        regions = per_model_regions.get(gpu_model, set())
        n_regions_total = sum(1 for (m, _r) in best if m == gpu_model)
        if regions:
            state = "available"
            detail = f"headroom in {len(regions)}/{n_regions_total} region(s)"
        else:
            any_known = any(v[0] >= 0 for (m, _r), v in best.items() if m == gpu_model)
            state = "sold_out" if any_known else "unknown"
            detail = "no region with headroom" if any_known else "no headroom data"
        records.append(AvailabilityRecord(
            provider="together", gpu_model=gpu_model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="regions_with_capacity", metric_value=float(len(regions)),
            detail=detail,
            fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
        ))

    logger.info(f"Together capacity: {len(records)} records")
    return records
