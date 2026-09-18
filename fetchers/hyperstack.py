"""Hyperstack public per-GPU rate cards, preserving every printed variant.

The page publishes maximum resources per GPU, not exact priced VM shapes.
Reservation starting-from rates omit their term and must remain references.
"""
import hashlib
import json
import logging
from html import unescape
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord
from fetchers._tavily import fetch_text as tavily_fetch_text

logger = logging.getLogger(__name__)

PRICING_URL = "https://www.hyperstack.cloud/gpu-pricing"
SOURCE_URL  = PRICING_URL

# None = skip (no public price / contact-sales)
HYPERSTACK_GPU_MAP = {
    "H200": "H200",
    "H100": "H100",
    "B200": "B200",   # public since ~Aug 2026 ($6.00 OD / $5.10 reserved)
    "B300": "B300",   # public since Aug 2026 ($7.40 OD)
    "A100": None,
    "RTX PRO 6000": "RTX6000",
    "RTX":  None,
    "L40":  None,
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    return _scrape_pricing(now)


def _scrape_pricing(now: str) -> List[PriceRecord]:
    html = ""
    try:
        req = urllib.request.Request(
            PRICING_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Hyperstack plain scrape failed: {e}")

    records = _parse_pricing(html, now) if html else []
    if not records:
        # Escalate to Tavily — renders the JS pricing page the plain scrape can miss.
        tav = tavily_fetch_text(PRICING_URL)
        if tav:
            records = _parse_pricing(tav, now)
            if records:
                logger.info("Hyperstack: parsed pricing via Tavily fallback")
    logger.info(f"Hyperstack scrape: {len(records)} records")
    return records


_SKU = r"(?:H100|H200|B200|B300)(?:\s+(?:SXM\d*|NVLink|PCIe))?|RTX\s+Pro\s+6000\s+SE"


def _parse_pricing(raw: str, now: str) -> List[PriceRecord]:
    text = re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', ' ', raw, flags=re.S | re.I)
    text = re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ', text)).replace('|', ' ')).strip()
    offers, specs = [], {}
    # Explicit maximum allowance columns are attached to that printed SKU.
    od_pattern = rf'NVIDIA\s+({_SKU})\s+\d[\d.]*\s+(\d+)\s+(\d+)\s+\$\s*([\d.]+)'
    for m in re.finditer(od_pattern, text, re.I):
        sku = m.group(1).upper()
        profile = (int(m.group(2)), float(m.group(3)))
        specs.setdefault(sku, set()).add(profile)
        offers.append((sku, "on_demand", float(m.group(4)), profile))
    reserve_pattern = rf'NVIDIA\s+({_SKU})\s+\$\s*([\d.]+)\s+Reserve\s+here'
    for m in re.finditer(reserve_pattern, text, re.I):
        offers.append((m.group(1).upper(), "reserved_unknown", float(m.group(2)), None))
    sections = list(re.finditer(r'Spot VM Pricing', text, re.I))
    if sections:
        spot_text = re.split(r'On-Demand CPU Pricing|Storage Pricing|Frequently',
                             text[sections[-1].end():], maxsplit=1, flags=re.I)[0]
        for m in re.finditer(rf'NVIDIA\s+({_SKU})\s+\$\s*([\d.]+)', spot_text, re.I):
            offers.append((m.group(1).upper(), "spot", float(m.group(2)), None))
    records, seen = [], set()
    for sku, ct, price, profile in offers:
        model = _match_gpu(sku)
        if not model or not .5 <= price <= 20:
            continue
        if profile is None:
            same_sku = specs.get(sku, set())
            profile = next(iter(same_sku)) if len(same_sku) == 1 else (None, None)
        row = _make_record(model, ct, price, now, *profile, sku=sku)
        observation = json.dumps(row.to_dict(), sort_keys=True)
        if observation not in seen:
            seen.add(observation)
            records.append(row)
    return records


def _make_record(gpu_model: str, ct: str, price: float, now: str,
                 pcpu_per_gpu: Optional[int] = None,
                 ram_gb_per_gpu: Optional[float] = None, *, sku: str = "") -> PriceRecord:
    # The documented rate is per GPU and published CPU/RAM figures are maxima.
    # Do not invent an eight-GPU host or claim a regional deployment entitlement.
    slug = re.sub(r"[^a-z0-9]+", "-", sku.lower()).strip("-")
    factor = "SXM" if "SXM" in sku else "PCIe" if "PCIE" in sku or gpu_model == "RTX6000" else "unknown"
    identity = (sku, ct, pcpu_per_gpu, ram_gb_per_gpu)
    offer_id = "hyperstack:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    reserved = ct == "reserved_unknown"
    return PriceRecord(
        provider="hyperstack", gpu_model=gpu_model, gpu_count=1,
        instance_type=f"hyperstack-{slug}", region="unknown", consumption_type=ct,
        price_per_hour_usd=price, price_per_gpu_hour_usd=price,
        # These are per-GPU upper limits, labelled in price_basis/offer_variant.
        vcpu=pcpu_per_gpu, ram_gb=ram_gb_per_gpu, node_gpus=1,
        form_factor=factor, interconnect="NVLink" if "NVLINK" in sku else "unknown",
        fetched_at=now, source_observed_at=now, source_url=SOURCE_URL,
        data_source="web_scrape", parser_version="direct-offers-1", offer_id=offer_id,
        gpu_variant=sku, offer_variant="Per-GPU rate; CPU/RAM are maximum allowances",
        price_basis="published_starting_from_unknown_term" if reserved else "per_gpu_rate_card_max_resources",
        comparison_eligible=not reserved,
        correction_reason="Reservation starting-from tariff has no published commitment term" if reserved else "",
    )


def _match_gpu(name: str) -> Optional[str]:
    name_upper = name.upper()
    for pattern in sorted(HYPERSTACK_GPU_MAP.keys(), key=len, reverse=True):
        if pattern.upper() in name_upper:
            return HYPERSTACK_GPU_MAP[pattern]
    return None
