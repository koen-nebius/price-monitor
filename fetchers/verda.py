"""
Verda (ex-DataCrunch, verda.com) fetcher — public no-auth instance-types API.

API: GET https://api.verda.com/v1/instance-types  (no auth, JSON list; prices
are strings, gpu.number_of_gpus carries the count; per-instance pricing —
divide by that row's count). Retain every instance configuration and rental
type, including non-linear prices across sizes. The endpoint supplies no
per-region price; region remains unknown. Confidential-compute variants are
retained as qualified references rather than mixed with standard instances.
"""
import hashlib
import json
import math
import logging
import urllib.request
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord

logger = logging.getLogger(__name__)

API = "https://api.verda.com/v1/instance-types"
SOURCE_URL = "https://verda.com/pricing"

# Verda `name` → our model. "RTX 6000 Ada" deliberately absent (older 48GB Ada
# card, NOT the Blackwell RTX PRO 6000 — same trap as the ComputePrices map);
# Confidential-compute variants are retained as separately qualified references.
GPU_NAME_MAP = {
    "GB300 SXM6 288GB": "GB300",
    "B300 SXM6 268GB":  "B300",
    "B200 SXM6 180GB":  "B200",
    "H200 SXM5 141GB":  "H200",
    "H100 SXM5 80GB":   "H100",
    "L40S 48GB":        "L40S",
    "RTX PRO 6000 96GB": "RTX6000",
    "RTX PRO 6000 CC 96GB": "RTX6000",
    "B200 CC SXM6 180GB": "B200",
    "B300 CC SXM6 268GB": "B300",
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        req = urllib.request.Request(API, headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.load(resp)
    except Exception as e:
        logger.error(f"Verda fetch failed: {e}")
        return []

    items = data if isinstance(data, list) else data.get("data", [])
    records = parse(items, now)
    logger.info("Verda: %d distinct price offers", len(records))
    return records


def _number(value, *, integer=False):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0 or (integer and not number.is_integer()):
        return None
    return int(number) if integer else number


def parse(items, now):
    """Keep every documented instance shape; the catalogue has no region prices."""
    records, seen = [], set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or ""
        currency = it.get("currency")
        if not isinstance(name, str) or not isinstance(currency, str):
            continue
        gpu_model = GPU_NAME_MAP.get(name)
        if not gpu_model or currency.lower() != "usd":
            continue
        def spec(parent, field, integer=False):
            obj = it.get(parent)
            return _number(obj.get(field), integer=integer) if isinstance(obj, dict) else None
        n = spec("gpu", "number_of_gpus", True)
        sku = it.get("instance_type")
        if not n or not isinstance(sku, str) or not sku.strip():
            continue
        vcpu = spec("cpu", "number_of_cores", True)
        ram_gb = spec("memory", "size_in_gigabytes")
        storage_gb = spec("storage", "size_in_gigabytes")
        cc = " CC " in name
        form_factor = "SXM" if "SXM" in name else "PCIe" if gpu_model in {"L40S", "RTX6000"} else "unknown"
        for ct, field in (("on_demand", "price_per_hour"), ("spot", "spot_price")):
            price = _number(it.get(field))
            if price is None or not .10 <= price / n <= 30:
                continue
            # Unknown is explicit: a provider-wide tariff is not a fi-01 quote.
            region = "unknown"
            identity = (it.get("id"), sku, name, n, vcpu, ram_gb, storage_gb, region, ct)
            offer_id = "verda:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
            row = PriceRecord(
                provider="verda", gpu_model=gpu_model, gpu_count=n, instance_type=sku,
                region=region, consumption_type=ct, price_per_hour_usd=price,
                price_per_gpu_hour_usd=price / n, vcpu=vcpu, ram_gb=ram_gb,
                storage_gb=storage_gb, node_gpus=n, form_factor=form_factor,
                interconnect="unknown", fetched_at=now, source_observed_at=now,
                source_url=API, data_source="official_api", parser_version="direct-offers-1",
                offer_id=offer_id, gpu_variant=name,
                offer_variant="Confidential compute" if cc else "",
                price_basis="public_instance_rate",
                comparison_eligible=not cc,
                correction_reason="Confidential-compute configuration; separate comparison required" if cc else "",
            )
            observation = json.dumps(row.to_dict(), sort_keys=True)
            if observation not in seen:
                seen.add(observation)
                records.append(row)
    return records
