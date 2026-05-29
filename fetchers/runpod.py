"""
RunPod GPU pricing fetcher.
Uses RunPod's public GraphQL API — no auth required, no Cloudflare.

RunPod is a GPU cloud provider (not just a marketplace) with their own
datacenters. Their "Secure Cloud" prices are RunPod's own on-demand rates
(enterprise-grade, isolated tenancy). "secureSpotPrice" is their interruptible
(spot-equivalent) rate.

Note on margins: RunPod sets its own prices with its own margin. These are
NOT underlying provider costs — they're what RunPod charges customers.
RunPod is a named competitor in the GPU cloud market.
"""
import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.runpod.io/graphql"
SOURCE_URL = "https://www.runpod.io/gpu-cloud"

# Map RunPod displayName fragment (lower) → normalized model
GPU_NAME_MAP = {
    "b300":  "B300",
    "b200":  "B200",
    "h200":  "H200",
    "h100":  "H100",
    "l40s":  "L40S",
    "gb200": "GB200",
    "gb300": "GB300",
}

# RunPod GPU types that map to more than one bucket — skip ambiguous ones.
# GH200 is a Grace+Hopper superchip (different form factor from HGX H100),
# same reasoning as in lambda_labs.py.
SKIP_KEYWORDS = {"gh200", "a100", "a40", "rtx", "mi300", "mi355", "mig"}

_QUERY = """
{
  gpuTypes {
    id
    displayName
    memoryInGb
    securePrice
    secureSpotPrice
  }
}
"""


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        body = json.dumps({"query": _QUERY}).encode()
        req = urllib.request.Request(
            GRAPHQL_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (price-monitor/1.0)",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.error(f"RunPod GraphQL fetch failed: {e}")
        return []

    gpu_types = data.get("data", {}).get("gpuTypes", [])
    if not gpu_types:
        logger.error("RunPod: no gpuTypes in response")
        return []

    records = []
    seen: set = set()

    for item in gpu_types:
        display = item.get("displayName", "")
        display_lower = display.lower()

        # Skip GPUs we don't track or that are ambiguous
        if any(kw in display_lower for kw in SKIP_KEYWORDS):
            continue

        gpu_model = _match_gpu(display_lower)
        if gpu_model is None:
            continue

        secure_price = item.get("securePrice") or 0
        spot_price = item.get("secureSpotPrice") or 0

        for ct, price in [("on_demand", secure_price), ("spot", spot_price)]:
            if price <= 0:
                continue
            key = (gpu_model, ct, display)
            if key in seen:
                continue
            seen.add(key)

            # Sanitize displayName into a slug: "H100 SXM" → "runpod-h100-sxm"
            slug = display_lower.replace(" ", "-")
            records.append(PriceRecord(
                provider="runpod",
                gpu_model=gpu_model,
                gpu_count=1,
                instance_type=f"runpod-{slug}-{ct}",
                region="global",
                consumption_type=ct,
                price_per_hour_usd=price,
                price_per_gpu_hour_usd=price,
                fetched_at=now,
                source_url=SOURCE_URL,
                data_source="official_api",
            ))

    logger.info(f"RunPod: {len(records)} records")
    return records


def _match_gpu(name_lower: str) -> Optional[str]:
    # Check longer patterns first to avoid 'h100' matching 'h100 nvl' incorrectly
    for pattern in sorted(GPU_NAME_MAP, key=len, reverse=True):
        if pattern in name_lower:
            return GPU_NAME_MAP[pattern]
    return None
