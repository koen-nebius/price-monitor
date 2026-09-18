"""Shadeform offers retained by configuration, region and rental type.

The documented /instances/types response carries nested ``configuration``;
legacy flattened payloads are also accepted. On-demand hourly_price is USD
cents per instance. Each spot availability entry has its own USD instance-hour
price. Availability is an aggregator observation, not direct-provider stock.

The existing permission/key gate is unchanged: systematic retrieval runs only
when SHADEFORM_API_KEY is present and sends that key on every API request.
Source: https://docs.shadeform.ai/api-reference/instances/instances-types
"""
import hashlib
import json
import logging
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

API_URL = "https://api.shadeform.ai/v1/instances/types"
DIRECTORY_URL = "https://shadeform.com/directory/clouds/{cloud}"
DATA_SOURCE = "aggregator"
PARSER_VERSION = "aggregator-offers-1"
SKIP_CLOUDS = {"excesssupply"}  # Shadeform resale pool, not a distinct provider
GPU_MAP = {
    "H100": "H100", "H200": "H200", "B200": "B200", "B300": "B300",
    "GB200": "GB200", "GB300": "GB300", "L40S": "L40S", "RTXPro6000": "RTX6000",
}
_CONFIG_FIELDS = ("gpu_type", "num_gpus", "vcpus", "memory_in_gb", "storage_in_gb",
                  "interconnect", "nvlink", "vram_per_gpu_in_gb", "gpu_manufacturer")


def _number(value, *, positive=False, integer=False):
    """Parse explicit numeric fields without coercing booleans or truncating GPUs."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    if integer and not number.is_integer():
        return None
    return int(number) if integer else number


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _gpu_family(variant):
    normalized = re.sub(r"[^A-Z0-9]", "", variant.upper())
    aliases = {re.sub(r"[^A-Z0-9]", "", key.upper()): gpu for key, gpu in GPU_MAP.items()}
    # Retain variants inside the tracked family without merging their offers.
    aliases.update({"H100NVL": "H100", "H100PCIE": "H100", "H100SXM": "H100",
                    "H100SXM5": "H100", "H10080G": "H100", "H10080GB": "H100",
                    "H200NVL": "H200", "H200SXM": "H200", "H200SXM5": "H200"})
    return aliases.get(normalized)


def _form_factor(interconnect: str, variant: str = "") -> str:
    if "NVL" in variant.upper():
        return "NVL"
    ic = _text(interconnect).lower()
    if ic.startswith("sxm"):
        return "SXM"
    if ic.startswith("pcie"):
        return "PCIe"
    return "unknown"


def parse(items: List[dict], now: str) -> List[PriceRecord]:
    """Keep each priced SKU/count/region/rental offer, including unavailable ones.

    Native region identifiers are used, not display labels (different regions
    can share one display label). No region is invented for spot-only items.
    Without usable region entries, a published on-demand tariff is retained as
    a catalogue reference with unknown region and unknown availability.
    """
    records, seen = [], set()
    if not isinstance(items, list):
        return records
    for item in items:
        if not isinstance(item, dict):
            continue
        cloud = _text(item.get("cloud")).lower()
        if not cloud or cloud in SKIP_CLOUDS:
            continue
        config = {key: item.get(key) for key in _CONFIG_FIELDS}
        if isinstance(item.get("configuration"), dict):
            config.update(item["configuration"])
        variant = _text(config.get("gpu_type"))
        gpu = _gpu_family(variant)
        count = _number(config.get("num_gpus"), positive=True, integer=True)
        if not gpu or count is None:
            continue
        shade_sku = _text(item.get("shade_instance_type"))
        cloud_sku = _text(item.get("cloud_instance_type"))
        sku = shade_sku or cloud_sku or f"{variant}x{count}"
        ram = _number(config.get("memory_in_gb"))
        storage = _number(config.get("storage_in_gb"))
        vcpus = _number(config.get("vcpus"), integer=True)
        form_factor = _form_factor(config.get("interconnect"), variant)
        interconnect = "NVLink" if config.get("nvlink") is True else "unknown"
        cents = _number(item.get("hourly_price"), positive=True)
        raw_availability = item.get("availability")
        availability = [entry for entry in raw_availability if isinstance(entry, dict)] \
            if isinstance(raw_availability, list) else []
        entries = [entry for entry in availability
                   if _text(entry.get("rental_type")).lower() in {"on_demand", "spot"}]
        # A missing rental_type in an old flattened response does not establish
        # that a regional offer is on-demand. Keep its tariff as unscoped only.
        if not entries and cents is not None:
            entries = [{"rental_type": "on_demand"}]
        for entry in entries:
            rental_type = _text(entry.get("rental_type")).lower()
            instance_price = (_number(entry.get("hourly_price"), positive=True)
                              if rental_type == "spot" else cents / 100 if cents is not None else None)
            if instance_price is None:
                continue
            native_region = _text(entry.get("region"))
            region = native_region or "unknown"
            available = entry.get("available") if type(entry.get("available")) is bool else None
            if not native_region:
                available = None
            identity = {
                "cloud": cloud, "shade_sku": shade_sku, "cloud_sku": cloud_sku,
                "gpu_variant": variant, "gpu_count": count, "region": region,
                "rental_type": rental_type, "vcpus": vcpus, "ram_gb": ram,
                "storage_gb": storage, "form_factor": form_factor, "interconnect": interconnect,
                "vram_per_gpu_in_gb": _number(config.get("vram_per_gpu_in_gb")),
                "deployment_type": _text(item.get("deployment_type")),
            }
            offer_id = "shadeform:" + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            record = PriceRecord(
                provider=f"sf_{cloud}", gpu_model=gpu, gpu_count=count, instance_type=sku,
                region=region, consumption_type=rental_type,
                price_per_hour_usd=instance_price, price_per_gpu_hour_usd=instance_price / count,
                vcpu=vcpus, ram_gb=ram, storage_gb=storage,
                fetched_at=now, source_observed_at=now,
                source_url=DIRECTORY_URL.format(cloud=urllib.parse.quote(cloud, safe="")),
                data_source=DATA_SOURCE, source_feed="shadeform", parser_version=PARSER_VERSION,
                offer_id=offer_id, available=available, gpu_variant=variant,
                interconnect=interconnect, form_factor=form_factor, node_gpus=count,
                price_basis="aggregator_offer" if native_region else "aggregator_catalogue",
            )
            # Drop only byte-equivalent observations. Conflicting prices/stock
            # for one identity remain visible to downstream reconciliation.
            duplicate = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
            if duplicate not in seen:
                records.append(record)
                seen.add(duplicate)
    return records


def fetch(regions: Optional[List[str]] = None) -> List[PriceRecord]:
    api_key = os.environ.get("SHADEFORM_API_KEY")
    if not api_key:
        logger.warning("SHADEFORM_API_KEY not set — Shadeform fetch skipped (ToS s.4.2 requires "
                       "permission/key for systematic retrieval; ask support@shadeform.ai)")
        return []
    now = datetime.now(timezone.utc).isoformat()
    req = urllib.request.Request(API_URL, headers={
        "X-API-KEY": api_key,
        "User-Agent": "nebius-price-monitor/1.0 (koen@nebius.com)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    items = data if isinstance(data, list) else data.get("instance_types", []) if isinstance(data, dict) else []
    records = parse(items, now)
    clouds = sorted({record.provider for record in records})
    logger.info("Shadeform: %d offers from %d instance types across %d clouds",
                len(records), len(items) if isinstance(items, list) else 0, len(clouds))
    return records
