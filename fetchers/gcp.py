"""
GCP pricing fetcher.
Uses the Cloud Billing Catalog API (requires GCP_API_KEY env var).
SKUs are per-GPU-hour; descriptions use human-readable names.
"""
import json
import logging
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

SOURCE_URL = "https://cloud.google.com/compute/gpus/gpu-regions-zones"
COMPUTE_SERVICE_ID = "6F81-5844-456A"

# Map description fragment → (gpu_model, instance_type hint)
# Order matters: check more-specific patterns first
#
# Notes on GCP GPU naming:
#   - GCP calls their Ada Lovelace card "Nvidia L4 GPU" (24GB), not "L40S".
#     L4 and L40S are different products. We track it as "L4" to avoid conflating
#     with the 48GB L40S offered by CoreWeave, RunPod, etc.
#   - B200 SKUs are live in the billing API as "A4 Nvidia B200 (1 gpu slice)" —
#     the "(1 gpu slice)" means 1 full GPU; price is quoted per-GPU.
#   - GB200, B300, GB300 SKUs are not yet live in the billing catalog (as of June 2026).
GPU_DESC_PATTERNS = [
    ("gb300",          "GB300", "a4x-maxgpu-4g"),
    ("gb200",          "GB200", "a4x-highgpu-4g"),
    ("b300",           "B300",  "a4-b300"),
    ("b200",           "B200",  "a4-highgpu-8g"),
    ("h200",           "H200",  "a3-ultragpu-8g"),
    ("h100 80gb mega", "H100",  "a3-megagpu-8g"),
    ("h100 80gb",      "H100",  "a3-highgpu-8g"),
    ("h100",           "H100",  "a3-highgpu-8g"),
    ("nvidia l4",      "L4",    "g2-standard"),   # GCP's Ada Lovelace 24GB card (≠ L40S)
]

# GPU counts per instance type (used for price_per_hour calculation)
INSTANCE_GPU_COUNT = {
    "a3-highgpu-8g":  8,
    "a3-megagpu-8g":  8,
    "a3-ultragpu-8g": 8,
    "a4-highgpu-8g":  8,
    "a4-b300":        8,
    "a4x-highgpu-4g": 4,
    "a4x-maxgpu-4g":  4,
    "g2-standard":    1,
}

# Regions to include (empty = all regions)
PRIORITY_REGIONS = {
    "us-central1", "us-east4", "us-west1", "us-west4",
    "europe-west4", "europe-west1", "europe-west3",
    "asia-southeast1", "asia-northeast1",
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    api_key = os.environ.get("GCP_API_KEY")
    if not api_key:
        logger.warning(
            "GCP_API_KEY not set — GCP data will be missing from the report. "
            "To fix: create a free GCP API key at https://console.cloud.google.com/ "
            "(enable 'Cloud Billing API', then APIs & Services → Credentials → Create API Key). "
            "Set GCP_API_KEY=<key> in your .env and in the remote agent routine config."
        )
        return []
    return _fetch_via_api(api_key)


def _fetch_via_api(api_key: str) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    records = []
    seen = set()
    page_token = None

    while True:
        params = {"key": api_key, "pageSize": 5000}
        if page_token:
            params["pageToken"] = page_token
        url = (
            f"https://cloudbilling.googleapis.com/v1/services/{COMPUTE_SERVICE_ID}/skus"
            f"?{urllib.parse.urlencode(params)}"
        )
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            logger.error(f"GCP API error: {e}")
            break

        for sku in data.get("skus", []):
            new = _parse_sku(sku, now, seen)
            records.extend(new)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"GCP: {len(records)} records")
    return records


def _parse_sku(sku: dict, fetched_at: str, seen: set) -> List[PriceRecord]:
    desc = sku.get("description", "")
    desc_lower = desc.lower()

    # Only process Compute resource family
    if sku.get("category", {}).get("resourceFamily") != "Compute":
        return []

    # Skip RAM/CPU/license/storage SKUs — we only want GPU accelerator SKUs
    skip_terms = ["ram", "cpu", "core", "license", "storage", "local ssd",
                  "calendar mode", "dws defined", "flex"]
    # Note: "gpu slice" intentionally removed — B200 SKUs use "(1 gpu slice)" to
    # mean 1 full GPU, and we want those. Fractional MIG slices don't match our
    # GPU_DESC_PATTERNS so they're naturally excluded.
    if any(t in desc_lower for t in skip_terms):
        return []

    # Match GPU model
    gpu_model, instance_type = _match_gpu(desc_lower)
    if not gpu_model:
        return []

    # Map consumption type
    ct = _consumption_type(desc_lower)
    if ct is None:
        return []

    # Extract price
    price = _extract_price(sku)
    if price <= 0:
        return []

    gpu_count = INSTANCE_GPU_COUNT.get(instance_type, 1)
    records = []

    for region in sku.get("serviceRegions", []):
        if PRIORITY_REGIONS and region not in PRIORITY_REGIONS:
            continue
        key = (gpu_model, instance_type, ct, region)
        if key in seen:
            continue
        seen.add(key)
        records.append(PriceRecord(
            provider="gcp",
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            instance_type=instance_type,
            region=region,
            consumption_type=ct,
            price_per_hour_usd=price * gpu_count,
            price_per_gpu_hour_usd=price,
            fetched_at=fetched_at,
            source_url=SOURCE_URL,
            data_source="official_api",
        ))

    return records


def _match_gpu(desc_lower: str):
    for pattern, gpu_model, instance_type in GPU_DESC_PATTERNS:
        if pattern in desc_lower:
            return gpu_model, instance_type
    return None, None


def _consumption_type(desc_lower: str) -> Optional[str]:
    if "spot preemptible" in desc_lower or "preemptible" in desc_lower:
        return "spot"
    if "commitment v1" in desc_lower or "commit" in desc_lower:
        if "3 year" in desc_lower or "3year" in desc_lower:
            return "committed_3yr"
        return "committed_1yr"
    # Skip capacity reservation types — not regular pricing
    if "reserved" in desc_lower or "flex" in desc_lower:
        return None
    return "on_demand"


def _extract_price(sku: dict) -> float:
    tiers = (
        sku.get("pricingInfo", [{}])[0]
        .get("pricingExpression", {})
        .get("tieredRates", [{}])
    )
    if not tiers:
        return 0.0
    up = tiers[0].get("unitPrice", {})
    return int(up.get("units", 0)) + int(up.get("nanos", 0)) / 1e9
