"""
Azure pricing fetcher.
Uses the public Azure Retail Prices API — no credentials required.
https://prices.azure.com/api/retail/prices
"""
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord
from config import AZURE_REGIONS, GPU_MAP

logger = logging.getLogger(__name__)

API_BASE = "https://prices.azure.com/api/retail/prices"
SOURCE_URL_OD = "https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/"
SOURCE_URL_SPOT = "https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/"
API_VERSION = "2023-01-01-preview"

# All Azure instance types we care about
_ALL_AZURE_TYPES = {}
for gpu_model, specs in GPU_MAP.get("azure", {}).items():
    for spec in specs:
        _ALL_AZURE_TYPES[spec["instance_type"]] = (gpu_model, spec)

# Also include known H100/H200/L40S variants not in config but discoverable
_KNOWN_PREFIXES = [
    "Standard_ND", "Standard_NC",
]


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    regions = regions or AZURE_REGIONS
    records = []
    now = datetime.now(timezone.utc).isoformat()

    for instance_type, (gpu_model, spec) in _ALL_AZURE_TYPES.items():
        try:
            instance_records = _fetch_instance(instance_type, gpu_model, spec, regions, now)
            records.extend(instance_records)
        except Exception as e:
            logger.warning(f"Azure {instance_type} failed: {e}")

    logger.info(f"Azure: {len(records)} records total")
    return records


def _fetch_instance(
    instance_type: str,
    gpu_model: str,
    spec: dict,
    regions: List[str],
    fetched_at: str,
) -> List[PriceRecord]:
    records = []

    # Query all price types for this instance
    filter_expr = f"armSkuName eq '{instance_type}' and serviceName eq 'Virtual Machines'"
    params = {
        "api-version": API_VERSION,
        "$filter": filter_expr,
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"

    while url:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read())

        for item in data.get("Items", []):
            arm_region = item.get("armRegionName", "")
            if arm_region not in regions:
                continue

            price_type = item.get("type", "")
            sku_name = item.get("skuName", "")
            product_name = item.get("productName", "")
            retail_price = item.get("retailPrice", 0)

            if retail_price <= 0:
                continue

            # Skip Windows variants — productName contains "Windows" even when skuName doesn't
            if "windows" in product_name.lower():
                continue

            reservation_term = item.get("reservationTerm", "")
            ct = _map_consumption_type(price_type, sku_name, reservation_term)
            if ct is None:
                continue

            # Azure reservation retailPrice is the upfront total for the term, not hourly.
            # Convert to effective hourly rate.
            hourly_price = retail_price
            if reservation_term:
                term_hours = {
                    "1 Year": 8760,
                    "3 Years": 26280,
                    "5 Years": 43800,
                    "10 Years": 87600,
                }.get(reservation_term, 0)
                if term_hours > 0:
                    hourly_price = retail_price / term_hours

            records.append(PriceRecord(
                provider="azure",
                gpu_model=gpu_model,
                gpu_count=spec["gpu_count"],
                instance_type=instance_type,
                region=arm_region,
                consumption_type=ct,
                price_per_hour_usd=hourly_price,
                price_per_gpu_hour_usd=hourly_price / spec["gpu_count"],
                vcpu=spec.get("vcpu"),
                ram_gb=spec.get("ram_gb"),
                fetched_at=fetched_at,
                source_url=SOURCE_URL_OD if "spot" not in ct else SOURCE_URL_SPOT,
                data_source="official_api",
            ))

        url = data.get("NextPageLink")

    # Deduplicate: keep cheapest price per (region, consumption_type).
    # Azure returns multiple Linux SKU variants (e.g. Spot vs Low Priority both map to "spot").
    best: dict = {}
    for r in records:
        key = (r.region, r.consumption_type)
        if key not in best or r.price_per_hour_usd < best[key].price_per_hour_usd:
            best[key] = r
    return list(best.values())


def _map_consumption_type(price_type: str, sku_name: str, reservation_term: str = "") -> Optional[str]:
    t = price_type.lower()
    s = sku_name.lower()

    if "spot" in t or "spot" in s:
        return "spot"
    if "low priority" in s:
        return "spot"
    if "reservation" in t or "reserved" in t:
        # Skip 5yr and 10yr — niche tiers that distort the reserved_1yr bucket
        if reservation_term in ("5 Years", "10 Years"):
            return None
        rt = reservation_term.lower()
        if "1 year" in rt or "1yr" in rt or "1 year" in s or "1yr" in s:
            return "reserved_1yr"
        if "3 year" in rt or "3yr" in rt or "3 year" in s or "3yr" in s:
            return "reserved_3yr"
        return "reserved_1yr"  # default for unmatched reservation terms
    if "devtest" in s or "dev/test" in s:
        return None
    if "windows" in s:
        return None  # skip Windows pricing
    if t == "consumption" or t == "retail":
        return "on_demand"

    return None
