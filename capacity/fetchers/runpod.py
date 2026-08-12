"""
RunPod capacity fetcher — public GraphQL, no auth (verified 2026-08-12).

Two queries:
  A. gpuTypes.lowestPrice(input:{gpuCount:N, secureCloud:true}).stockStatus —
     global stock label per GPU type ("High"|"Medium"|"Low"|null). Queried at
     gpuCount=1 AND gpuCount=8: the 8x view is cluster-scale capacity (1x
     stock with no 8x stock = scraps).
  B. dataCenters.gpuAvailability — per-datacenter available bool +
     stockStatus (verified: available:false pairs with stockStatus:null =
     sold out in that DC).

Secure Cloud only (RunPod's own DCs) — Community Cloud is a reseller
marketplace, not RunPod capacity.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.runpod.io/graphql"
SOURCE_URL = "https://www.runpod.io/gpu-cloud"

GPU_NAME_MAP = {
    "gb300": "GB300", "gb200": "GB200", "b300": "B300", "b200": "B200",
    "h200": "H200", "h100": "H100", "l40s": "L40S",
    "rtx pro 6000": "RTX6000", "rtx 6000 pro": "RTX6000",
}
SKIP_KEYWORDS = {"gh200", "a100", "a40", "mi300", "mi325", "mi355", "mig",
                 "rtx 6000 ada", "rtx a6000", "4090", "5090", "3090"}

_QUERY_TYPES = """
{
  gpuTypes {
    id
    displayName
    s1: lowestPrice(input:{gpuCount:1, secureCloud:true}) { stockStatus }
    s8: lowestPrice(input:{gpuCount:8, secureCloud:true}) { stockStatus }
  }
}
"""

_QUERY_DCS = """
{
  dataCenters {
    id
    location
    gpuAvailability(input:{gpuCount:1, secureCloud:true}) {
      gpuTypeId
      displayName
      available
      stockStatus
    }
  }
}
"""

_STOCK_STATE = {"High": "available", "Medium": "available", "Low": "limited"}


def _match_gpu(display: str):
    low = display.lower()
    if any(kw in low for kw in SKIP_KEYWORDS):
        return None
    for pattern in sorted(GPU_NAME_MAP, key=len, reverse=True):
        if pattern in low:
            return GPU_NAME_MAP[pattern]
    return None


def _post(query: str) -> dict:
    from fetchers._http import http_get
    body = json.dumps({"query": query}).encode()
    return json.loads(http_get(GRAPHQL_URL, data=body,
                               headers={"Content-Type": "application/json"},
                               timeout=30))


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    records: List[AvailabilityRecord] = []

    try:
        types = _post(_QUERY_TYPES).get("data", {}).get("gpuTypes", [])
    except Exception as e:
        logger.error(f"RunPod gpuTypes query failed: {e}")
        return []

    for item in types:
        gpu_model = _match_gpu(item.get("displayName", ""))
        if not gpu_model:
            continue
        s1 = (item.get("s1") or {}).get("stockStatus")
        s8 = (item.get("s8") or {}).get("stockStatus")
        if s1 is None and s8 is None:
            state, detail = "sold_out", "no Secure Cloud stock at 1x or 8x"
        elif s8 is None:
            state = "limited"
            detail = f"1x stock {s1}, but no 8-GPU (cluster) stock"
        else:
            state = _STOCK_STATE.get(s8, "limited")
            detail = f"stock 1x: {s1 or 'none'}, 8x: {s8}"
        records.append(AvailabilityRecord(
            provider="runpod", gpu_model=gpu_model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="stock_status_label",
            metric_value={"High": 3.0, "Medium": 2.0, "Low": 1.0}.get(s8, 0.0),
            detail=detail, instance_type=item.get("id", ""),
            fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
        ))

    # Per-datacenter breakdown (best-effort; global rows above are the spine)
    try:
        dcs = _post(_QUERY_DCS).get("data", {}).get("dataCenters", [])
    except Exception as e:
        logger.warning(f"RunPod dataCenters query failed (keeping global rows): {e}")
        dcs = []

    for dc in dcs:
        dc_id = dc.get("id", "")
        for ga in dc.get("gpuAvailability") or []:
            gpu_model = _match_gpu(ga.get("displayName", "") or ga.get("gpuTypeId", ""))
            if not gpu_model:
                continue
            available = bool(ga.get("available"))
            status = ga.get("stockStatus")
            records.append(AvailabilityRecord(
                provider="runpod", gpu_model=gpu_model, region=dc_id,
                consumption_type="on_demand",
                state="available" if available else "sold_out",
                metric_type="stock_status_label",
                metric_value={"High": 3.0, "Medium": 2.0, "Low": 1.0}.get(status, 0.0),
                detail=f"DC {dc.get('location', dc_id)}: "
                       f"{'stock ' + status if status else 'sold out'}",
                instance_type=ga.get("gpuTypeId", ""),
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))

    logger.info(f"RunPod capacity: {len(records)} records")
    return records
