"""
Oracle Cloud Infrastructure (OCI) pricing fetcher.
Uses the public OCI price list API — no authentication required.
"""
import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

API_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=USD&pricingStrategy=PAYG"
SOURCE_URL = "https://www.oracle.com/cloud/price-list/"

# OCI GPU shapes to detect in displayName (shape name as substring → (gpu_model, gpu_count))
# None value means skip this shape
OCI_GPU_SHAPES = {
    "BM.GPU.H100.8":   ("H100",  8),
    "BM.GPU.H100.4":   ("H100",  4),
    "BM.GPU4.8":       None,   # A100, skip
    "BM.GPU.B200.8":   ("B200",  8),
    "BM.GPU.GB200.4":  ("GB200", 4),
}

# Fallback: match GPU model from displayName keywords when shape name not found
# Order matters: longer/more specific matches first
OCI_GPU_KEYWORDS = [
    ("GB300",  "GB300",  4),   # GB300.42 = 4 B300 + 2 Grace per node
    ("GB200",  "GB200",  4),
    ("B300",   "B300",   8),
    ("B200",   "B200",   8),
    ("H200",   "H200",   8),
    ("H100T",  "H100",   8),
    ("H100",   "H100",   8),
    ("L40S",   "L40S",   1),
    # Skip A100, MI300X, RTX PRO, old gen
]

# GPU display name substrings to skip (AMD, old gen, non-cloud)
OCI_SKIP_KEYWORDS = [
    "MI300X", "MI355X", "RTX PRO", "A100", "A10", "X7", "V2", "E3", "E4",
    "VMware", "Roving", "Cloud@Customer", "NVIDIA AI Enterprise",
]


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        data = json.loads(http_get(API_URL, headers={"Accept": "application/json"}, timeout=30))
    except Exception as e:
        logger.warning(f"Oracle API fetch failed: {e}")
        return []

    if not isinstance(data, dict) or "items" not in data:
        logger.warning(f"Oracle API: unexpected response structure (missing 'items')")
        return []

    records = []
    seen = set()

    for item in data["items"]:
        display_name = item.get("displayName", "")
        metric_name = item.get("metricName", "")

        # Only process GPU per hour and instance per hour metrics
        metric_lower = metric_name.lower()
        if "gpu per hour" not in metric_lower and "instance per hour" not in metric_lower:
            continue

        # Skip unwanted items
        if any(skip in display_name for skip in OCI_SKIP_KEYWORDS):
            continue

        # Try shape-based match first
        gpu_model = None
        gpu_count = None
        for shape, info in OCI_GPU_SHAPES.items():
            if shape in display_name:
                if info is None:
                    break  # explicitly skipped
                gpu_model, gpu_count = info
                break

        # Fallback: keyword match
        if gpu_model is None:
            for keyword, model, count in OCI_GPU_KEYWORDS:
                if keyword in display_name:
                    gpu_model = model
                    gpu_count = count
                    break

        if gpu_model is None:
            continue

        # Extract USD price
        price_usd = _extract_price(item)
        if price_usd is None or price_usd <= 0:
            continue

        # Compute per-GPU and per-instance prices
        metric_lower = metric_name.lower()
        if "gpu per hour" in metric_lower or metric_name == "GPU_PER_HOUR":
            price_per_gpu = price_usd
            price_per_hour = price_usd * gpu_count
        else:  # INSTANCE_PER_HOUR
            price_per_gpu = price_usd / gpu_count
            price_per_hour = price_usd

        # Use display_name as instance_type slug; deduplicate by (display_name, metric_name)
        instance_type = display_name.replace(" ", "-").lower()
        key = (display_name, metric_name)
        if key in seen:
            continue
        seen.add(key)

        records.append(PriceRecord(
            provider="oracle",
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            instance_type=instance_type,
            region="global",
            consumption_type="on_demand",
            price_per_hour_usd=price_per_hour,
            price_per_gpu_hour_usd=price_per_gpu,
            fetched_at=now,
            source_url=SOURCE_URL,
            data_source="official_api",
        ))

    logger.info(f"Oracle: {len(records)} records")
    return records


def _extract_price(item: dict) -> Optional[float]:
    """Extract USD PAYG price from currencyCodeLocalizations."""
    for loc in item.get("currencyCodeLocalizations", []):
        if loc.get("currencyCode") == "USD":
            for price_entry in loc.get("prices", []):
                if price_entry.get("model") == "PAY_AS_YOU_GO":
                    try:
                        return float(price_entry["value"])
                    except (KeyError, ValueError, TypeError):
                        pass
    return None
