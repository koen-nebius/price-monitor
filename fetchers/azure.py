"""
Azure pricing fetcher.
Uses the public Azure Retail Prices API — no credentials required.
https://prices.azure.com/api/retail/prices
"""
import json
import logging
import math
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord
from config import AZURE_REGIONS, GPU_MAP
from fetchers._http import http_get

logger = logging.getLogger(__name__)

API_BASE = "https://prices.azure.com/api/retail/prices"
SOURCE_URL_OD = "https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/"
SOURCE_URL_SPOT = "https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/"
API_VERSION = "2023-01-01-preview"

# Microsoft Learn, accelerator quantity tables (verified 2026-09-06):
# https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nc-rtxpro6000-bse-v6-series
# NC36 is a 24-GB quarter GPU, not a whole card. The 36-vCPU/GPU
# assumption in the legacy config understates normalized RTX prices fourfold.
# Match exact documented SKUs; vCPU counts and partial names do not establish
# accelerator quantities. Fractions stay numeric in the existing PriceRecord.
_RTX_GPU_COUNTS = {
    "Standard_NC36ds_xl_RTXPRO6000BSE_v6".casefold(): 0.25,
    "Standard_NC72ds_xl_RTXPRO6000BSE_v6".casefold(): 0.5,
    "Standard_NC144ds_xl_RTXPRO6000BSE_v6".casefold(): 1,
    "Standard_NC288ds_xl_RTXPRO6000BSE_v6".casefold(): 2,
    "Standard_NC24lds_xl_RTXPRO6000BSE_v6".casefold(): 0.25,
    "Standard_NC36lds_xl_RTXPRO6000BSE_v6".casefold(): 0.25,
    "Standard_NC72lds_xl_RTXPRO6000BSE_v6".casefold(): 0.5,
    "Standard_NC144lds_xl_RTXPRO6000BSE_v6".casefold(): 1,
    "Standard_NC288lds_xl_RTXPRO6000BSE_v6".casefold(): 2,
}


def _gpu_quantity(instance_type: str, gpu_model: str, spec: dict) -> Optional[float]:
    is_rtx = gpu_model.upper().replace(" ", "") in {"RTX6000", "RTXPRO6000"}
    if is_rtx or "RTXPRO6000" in instance_type.upper():
        count = _RTX_GPU_COUNTS.get(instance_type.casefold())
        if not is_rtx or count is None:
            logger.warning("Azure %s: ambiguous RTX model/SKU; skipping GPU normalization", instance_type)
            return None
        return count

    count = spec.get("gpu_count")
    if isinstance(count, bool) or not isinstance(count, (int, float)) or not math.isfinite(count) or count <= 0:
        logger.warning("Azure %s: invalid configured GPU quantity; skipping", instance_type)
        return None
    return count


# All Azure instance types we care about
_ALL_AZURE_TYPES = {}
for gpu_model, specs in GPU_MAP.get("azure", {}).items():
    for spec in specs:
        count = _gpu_quantity(spec["instance_type"], gpu_model, spec)
        if count is not None:
            # Copy rather than mutate the shared config. Registry users and the
            # fetch path see the same corrected quantity; other providers do not.
            _ALL_AZURE_TYPES[spec["instance_type"]] = (gpu_model, {**spec, "gpu_count": count})

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
    gpu_count = _gpu_quantity(instance_type, gpu_model, spec)
    if gpu_count is None:
        return records

    # Query all price types for this instance
    filter_expr = f"armSkuName eq '{instance_type}' and serviceName eq 'Virtual Machines'"
    params = {
        "api-version": API_VERSION,
        "$filter": filter_expr,
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"

    while url:
        data = json.loads(http_get(url, timeout=30))

        for item in data.get("Items", []):
            # Normalization belongs to this exact VM, not a similar meter label.
            if item.get("armSkuName", "").casefold() != instance_type.casefold():
                logger.warning("Azure %s: missing or mismatched armSkuName; skipping price", instance_type)
                continue
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
                gpu_count=gpu_count,
                instance_type=instance_type,
                region=arm_region,
                consumption_type=ct,
                price_per_hour_usd=hourly_price,
                price_per_gpu_hour_usd=hourly_price / gpu_count,
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
        # Legacy Azure "Low Priority" meter — deprecated, and a deeper discount than
        # the current Spot meter. Keep it DISTINCT (not "spot") so it can't undercut
        # the true Spot price in the interruptible comparison (Phase 1.6). It is not
        # in INTERRUPTIBLE_CTS, so it never feeds the spot/preemptible benchmark.
        return "low_priority"
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
