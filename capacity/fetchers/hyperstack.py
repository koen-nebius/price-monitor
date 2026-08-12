"""
Hyperstack (NexGen Cloud) stock fetcher — the single best capacity endpoint
of any GPU cloud: GET https://infrahub-api.nexgencloud.com/v1/core/stocks
returns, per region (CANADA-1, NORWAY-1, US-1) and GPU model, a conservative
available-count string ("0" / "10+" / "100+") plus planned_7/30/100_days
restock forecasts. Real-time as GPUs deploy/free (their docs).

Auth: `api_key` header (exact name verified in the official Python SDK) from
a FREE Hyperstack account — console.hyperstack.cloud → API keys. Set the
HYPERSTACK_API_KEY repo secret to activate; skips cleanly until then.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

API = "https://infrahub-api.nexgencloud.com/v1/core/stocks"
SOURCE_URL = "https://docs.hyperstack.cloud/docs/hardware/gpu-stock-information/"

# Hyperstack model-name fragment → our model ("H100-80G-PCIe" style names)
_MODEL_FRAGMENTS = [
    ("GB300", "GB300"), ("GB200", "GB200"), ("B300", "B300"), ("B200", "B200"),
    ("H200", "H200"), ("H100", "H100"), ("L40S", "L40S"), ("L40", "L40S"),
    ("RTX-PRO-6000", "RTX6000"), ("RTX PRO 6000", "RTX6000"),
]


def _match(model_name: str):
    up = model_name.upper()
    for frag, model in _MODEL_FRAGMENTS:
        if frag in up:
            return model
    return None


def _parse_count(available: str):
    """'0' → 0, '10+' → 10, '100+' → 100 (conservative lower bounds)."""
    m = re.match(r"(\d+)", str(available or "").strip())
    return int(m.group(1)) if m else None


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    api_key = os.environ.get("HYPERSTACK_API_KEY")
    if not api_key:
        logger.warning("Hyperstack: HYPERSTACK_API_KEY not set — skipping "
                       "(free key: console.hyperstack.cloud → API keys)")
        return []

    try:
        from fetchers._http import http_get
        data = json.loads(http_get(API, headers={
            "api_key": api_key, "Accept": "application/json",
        }, timeout=30))
    except Exception as e:
        logger.error(f"Hyperstack stock fetch failed: {e}")
        return []

    stocks = data.get("stocks", [])
    records: List[AvailabilityRecord] = []
    per_model_best: dict = {}

    for stock in stocks:
        region = stock.get("region", "unknown")
        for m in stock.get("models", []):
            gpu_model = _match(m.get("model", ""))
            if not gpu_model:
                continue
            count = _parse_count(m.get("available"))
            if count is None:
                continue
            if count == 0:
                state, detail = "sold_out", "0 in stock"
            elif count < 10:
                state, detail = "limited", f"~{count} available"
            else:
                state, detail = "available", f"{m.get('available')} available"
            planned = m.get("planned_7_days") or m.get("planned_30_days")
            if planned:
                detail += f", +{planned} planned"
            records.append(AvailabilityRecord(
                provider="hyperstack", gpu_model=gpu_model, region=region,
                consumption_type="on_demand", state=state,
                metric_type="stock_level", metric_value=float(count),
                detail=detail, instance_type=m.get("model", ""),
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))
            cur = per_model_best.get(gpu_model, -1)
            per_model_best[gpu_model] = max(cur, count)

    # Global row per model for the matrix
    for gpu_model, best in per_model_best.items():
        n_regions = sum(1 for r in records
                        if r.gpu_model == gpu_model and r.state != "sold_out"
                        and r.region != "global")
        state = "sold_out" if best == 0 else ("limited" if best < 10 else "available")
        records.append(AvailabilityRecord(
            provider="hyperstack", gpu_model=gpu_model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="stock_level", metric_value=float(best),
            detail=f"stock in {n_regions} region(s), best {best}+" if best else "0 in all regions",
            fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
        ))

    logger.info(f"Hyperstack stock: {len(records)} records")
    return records
