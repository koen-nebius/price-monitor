"""
ComputePrices.com fetcher.
Pulls GPU pricing for providers NOT already covered by direct scrapers.
API docs: https://computeprices.com/docs/api
No auth required (60 req/hr per IP). Free API key raises to 5,000/hr.
Set COMPUTEPRICES_API_KEY env var to use a key.
"""
import json
import logging
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

API_BASE = "https://computeprices.com/api/v1/gpu-prices"
SOURCE_URL = "https://computeprices.com"

# Providers already scraped directly — skip them to avoid double-counting
SKIP_PROVIDERS = {
    "amazon aws",
    "google cloud",
    "microsoft azure",
    "coreweave",
    "lambda labs",
    "crusoe",
    "nebius",
}

# ComputePrices GPU name → our normalized model name
# Only include GPUs we track; everything else is ignored.
GPU_NAME_MAP = {
    "h100 sxm":  "H100",
    "h100 pcie": "H100",
    "h100 nvl":  "H100",
    "gh200":     "H100",   # Grace Hopper = H100 architecture
    "h200":      "H200",
    "b200":      "B200",
    "hgx b300":  "B300",
    "gb200":     "GB200",
    "gb300":     "GB300",
    "l40s":      "L40S",
}

# GPU slugs to query (one request per slug keeps responses small)
# Note: B300 uses slug "hgx-b300" on ComputePrices, not "b300"
GPU_SLUGS = ["h100", "h200", "b200", "hgx-b300", "gb200", "gb300", "l40s"]


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    api_key = os.environ.get("COMPUTEPRICES_API_KEY")

    records = []
    seen: set = set()

    for slug in GPU_SLUGS:
        try:
            slug_records = _fetch_slug(slug, api_key, now, seen)
            records.extend(slug_records)
        except Exception as e:
            logger.warning(f"ComputePrices slug={slug} failed: {e}")

    # Deduplicate: for each (provider, gpu_model, ct), keep the cheapest per-GPU price.
    # Some providers have incorrect total_hourly_usd values that scale non-linearly with
    # gpu_count (e.g. UpCloud H100), causing inflated per-GPU prices for multi-GPU nodes.
    # Keeping the minimum ensures the executive table and diff log reflect the real price.
    best: Dict[tuple, PriceRecord] = {}
    for r in records:
        key = (r.provider, r.gpu_model, r.consumption_type)
        if key not in best or r.price_per_gpu_hour_usd < best[key].price_per_gpu_hour_usd:
            best[key] = r
    records = list(best.values())

    logger.info(f"ComputePrices: {len(records)} records from {len(GPU_SLUGS)} GPU slugs")
    return records


def _fetch_slug(
    slug: str,
    api_key: Optional[str],
    now: str,
    seen: set,
) -> List[PriceRecord]:
    params: dict = {"gpu": slug}
    if api_key:
        params["api_key"] = api_key
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"

    headers = {"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    records = []
    for item in data.get("data", []):
        provider_name = item.get("provider", "")
        if provider_name.lower() in SKIP_PROVIDERS:
            continue

        gpu_label = item.get("gpu", "").lower()
        gpu_model = GPU_NAME_MAP.get(gpu_label)
        if gpu_model is None:
            continue

        # Use total_hourly_usd / gpu_count as the authoritative per-GPU price.
        # ComputePrices `price_per_hour_usd` is per-GPU for most providers but
        # some (e.g. UpCloud) return total node price, causing it to scale
        # linearly with gpu_count. total_hourly_usd / gpu_count is always correct.
        gpu_count = item.get("gpu_count") or 1
        total_usd = item.get("total_hourly_usd") or 0
        price_per_hour_usd_field = item.get("price_per_hour_usd") or 0

        if total_usd > 0:
            price_usd = total_usd / gpu_count   # true per-GPU price
        elif price_per_hour_usd_field > 0:
            price_usd = price_per_hour_usd_field
        else:
            continue

        if price_usd <= 0:
            continue
        pricing_type = item.get("pricing_type", "on_demand")
        commitment_months = item.get("commitment_months")

        ct = _map_consumption_type(pricing_type, commitment_months)
        if ct is None:
            continue

        provider_slug = item.get("provider_slug", provider_name.lower().replace(" ", "_"))
        source = item.get("source_url") or SOURCE_URL

        # Region: ComputePrices doesn't expose region per-record, use provider slug as proxy
        region = "global"

        key = (provider_slug, gpu_model, gpu_count, ct)
        if key in seen:
            # Keep cheapest when same provider/gpu/count/type appears twice
            continue
        seen.add(key)

        records.append(PriceRecord(
            provider=f"cp_{provider_slug}",   # prefix to distinguish from direct scrapers
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            instance_type=f"{provider_slug}-{gpu_model.lower()}-{gpu_count}x",
            region=region,
            consumption_type=ct,
            price_per_hour_usd=price_usd * gpu_count,
            price_per_gpu_hour_usd=price_usd,
            fetched_at=now,
            source_url=source,
        ))

    return records


def _map_consumption_type(pricing_type: str, commitment_months: Optional[int]) -> Optional[str]:
    pt = pricing_type.lower()
    if pt == "spot":
        return "spot"
    if pt == "on_demand":
        return "on_demand"
    if pt == "reserved":
        if commitment_months is None:
            return "on_demand"
        # Short-term committed capacity (<= 6mo): meaningful data but not comparable
        # to standard 1yr/2yr/3yr buckets — store separately.
        if commitment_months <= 6:
            return "committed_short_term"
        if commitment_months <= 12:
            return "reserved_1yr"    # canonical 1yr bucket
        if commitment_months <= 24:
            return "committed_2yr"   # canonical 2yr bucket (Nebius also uses this)
        if commitment_months <= 36:
            return "reserved_3yr"    # canonical 3yr bucket
        if commitment_months <= 48:
            return "committed_4yr"   # kept for reference (e.g. Vultr B200 48mo)
        return None
    return None
