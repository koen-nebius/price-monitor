"""
Hyperstack (NexGen Cloud) pricing fetcher.
Tries the Infrahub API first (no auth required), then falls back to web scraping.
"""
import json
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

API_URL = "https://infrahub-api.nexgencloud.com/v1/core/flavours"
PRICING_URL = "https://www.hyperstack.cloud/gpu-cloud-pricing"
SOURCE_URL = PRICING_URL

# GPU name patterns to match (in GPU name or flavour name)
# None value means skip this GPU
HYPERSTACK_GPU_MAP = {
    "H100": "H100",
    "H200": "H200",
    "B200": "B200",
    "B300": "B300",
    "GB200": "GB200",
    "GB300": "GB300",
    "L40S": "L40S",
    "A100": None,
    "A30": None,
    "RTX": None,
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()

    # Try API first
    records = _fetch_api(now)
    if records:
        return records

    # Fall back to scraping
    logger.info("Hyperstack API unavailable — trying web scrape fallback")
    return _scrape_pricing(now)


def _fetch_api(now: str) -> List[PriceRecord]:
    try:
        req = urllib.request.Request(
            API_URL,
            headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.info(f"Hyperstack API fetch failed: {e}")
        return []

    if not isinstance(data, dict) or "flavours" not in data:
        logger.info(f"Hyperstack API: unexpected response structure")
        return []

    records = []
    seen = set()

    for flavour in data["flavours"]:
        # Extract GPU info
        gpu_info = flavour.get("gpu", {})
        gpu_name = gpu_info.get("name", "")
        gpu_count = gpu_info.get("count", 1) or 1

        gpu_model = _match_gpu(gpu_name) or _match_gpu(flavour.get("name", ""))
        if gpu_model is None:
            continue

        # Extract price
        price_info = flavour.get("price", {})
        try:
            amount = float(price_info.get("amount", 0))
        except (ValueError, TypeError):
            continue
        period = price_info.get("period", "hourly")
        if period != "hourly" or amount <= 0:
            continue

        # amount is per-instance per-hour
        price_per_gpu = amount / gpu_count
        price_per_hour = amount

        name = flavour.get("name", f"hyperstack-{gpu_model.lower()}")
        key = (gpu_model, gpu_count)
        if key in seen:
            # Keep cheapest
            continue
        seen.add(key)

        records.append(PriceRecord(
            provider="hyperstack",
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            instance_type=name,
            region="global",
            consumption_type="on_demand",
            price_per_hour_usd=price_per_hour,
            price_per_gpu_hour_usd=price_per_gpu,
            fetched_at=now,
            source_url=SOURCE_URL,
            data_source="official_api",
        ))

    logger.info(f"Hyperstack API: {len(records)} records")
    return records


def _scrape_pricing(now: str) -> List[PriceRecord]:
    try:
        req = urllib.request.Request(
            PRICING_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Hyperstack scrape failed: {e}")
        return []

    records = []
    seen = set()

    # Look for $X.XX/GPU/hr pattern near GPU model names
    for m in re.finditer(
        r'(H100|H200|B200|B300|GB200|GB300|L40S)[^<]{0,400}?\$([0-9]+\.?[0-9]*)/GPU/hr',
        html, re.IGNORECASE | re.DOTALL,
    ):
        gpu_model = _match_gpu(m.group(1))
        if gpu_model is None:
            continue
        try:
            price_per_gpu = float(m.group(2))
        except ValueError:
            continue
        if price_per_gpu <= 0:
            continue

        key = gpu_model
        if key in seen:
            continue
        seen.add(key)

        records.append(PriceRecord(
            provider="hyperstack",
            gpu_model=gpu_model,
            gpu_count=1,
            instance_type=f"hyperstack-{gpu_model.lower()}",
            region="global",
            consumption_type="on_demand",
            price_per_hour_usd=price_per_gpu,
            price_per_gpu_hour_usd=price_per_gpu,
            fetched_at=now,
            source_url=SOURCE_URL,
            data_source="web_scrape",
        ))

    logger.info(f"Hyperstack scrape: {len(records)} records")
    return records


def _match_gpu(name: str) -> Optional[str]:
    """Match GPU model from name string, return None if should be skipped."""
    name_upper = name.upper()
    # Check longer patterns first to avoid partial matches
    for pattern in sorted(HYPERSTACK_GPU_MAP.keys(), key=len, reverse=True):
        if pattern.upper() in name_upper:
            return HYPERSTACK_GPU_MAP[pattern]
    return None
