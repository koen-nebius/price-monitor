"""
Shadeform aggregator fetcher — one GET covers ~19 partner GPU clouds with
LIVE per-region availability booleans.

GET https://api.shadeform.ai/v1/instances/types — worked UNAUTHENTICATED on
2026-08-12 (docs say X-API-KEY is required, so we send SHADEFORM_API_KEY when
present and keep working keyless while that lasts).
.instance_types[]: cloud, gpu_type, num_gpus, hourly_price (cents),
availability[] = [{region, available (bool), display_name}].

Emitted under the REAL provider keys (curated map below) with
data_source="aggregator" — the renderer prefers direct-source records when a
cell has both, so these rows fill gaps (Crusoe live stock, Voltage Park,
Scaleway, outside-in Nebius) and silently become a cross-check once a direct
fetcher exists.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

API = "https://api.shadeform.ai/v1/instances/types"
SOURCE_URL = "https://www.shadeform.ai/"

# Shadeform cloud slug → our provider key (only providers in our matrix)
CLOUD_MAP = {
    "crusoe": "crusoe",
    "hyperstack": "hyperstack",
    "lambdalabs": "lambda",
    "nebius": "nebius",
    "scaleway": "scaleway",
    "verda": "verda",
    "datacrunch": "verda",
    "voltagepark": "voltage_park",
    "voltage_park": "voltage_park",
}

_GPU_MAP = {
    "GB300": "GB300", "GB200": "GB200", "B300": "B300", "B200": "B200",
    "H200": "H200", "H100": "H100", "L40S": "L40S",
    "RTXPRO6000": "RTX6000", "RTX_PRO_6000": "RTX6000", "RTX PRO 6000": "RTX6000",
}


def _match_gpu(gpu_type: str):
    up = (gpu_type or "").upper().replace("-", "").replace("_", "").replace(" ", "")
    if "GH200" in up or "ADA" in up:
        return None
    for frag, model in _GPU_MAP.items():
        if frag.replace("_", "").replace(" ", "") in up:
            return model
    return None


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    headers = {"Accept": "application/json"}
    if os.environ.get("SHADEFORM_API_KEY"):
        headers["X-API-KEY"] = os.environ["SHADEFORM_API_KEY"]

    try:
        from fetchers._http import http_get
        data = json.loads(http_get(API, headers=headers, timeout=45))
    except Exception as e:
        logger.error(f"Shadeform fetch failed: {e}")
        return []

    # (provider, gpu) → {region: available_bool} (any instance size counts)
    cells: dict = {}
    for it in data.get("instance_types", []):
        provider = CLOUD_MAP.get((it.get("cloud") or "").lower())
        if not provider:
            continue
        gpu_model = _match_gpu(it.get("gpu_type", ""))
        if not gpu_model:
            continue
        for av in it.get("availability") or []:
            region = av.get("region") or av.get("display_name") or "unknown"
            key = (provider, gpu_model)
            cur = cells.setdefault(key, {})
            cur[region] = cur.get(region, False) or bool(av.get("available"))

    records: List[AvailabilityRecord] = []
    for (provider, gpu_model), regions in sorted(cells.items()):
        avail = sorted(r for r, ok in regions.items() if ok)
        n_avail, n_total = len(avail), len(regions)
        if n_avail == 0:
            state = "sold_out"
            detail = f"0/{n_total} region(s) available (via Shadeform)"
        elif n_avail <= 1 and n_total > 2:
            state = "limited"
            detail = f"{n_avail}/{n_total} region(s) available (via Shadeform)"
        else:
            state = "available"
            detail = f"{n_avail}/{n_total} region(s) available (via Shadeform)"
        records.append(AvailabilityRecord(
            provider=provider, gpu_model=gpu_model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="regions_with_capacity", metric_value=float(n_avail),
            detail=detail,
            fetched_at=now, source_url=SOURCE_URL, data_source="aggregator",
        ))

    logger.info(f"Shadeform: {len(records)} records across "
                f"{len({p for p, _ in cells})} mapped providers")
    return records
