"""
CoreWeave pricing fetcher.
Scrapes https://www.coreweave.com/gpu-cloud-pricing
Page structure: table rows with h3[data-product], instance-price/spot-price spans,
and spec cells rendered "<value> <label>" (GPU Count, VRAM, vCPUs, System RAM).
"""
import json
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

PRICING_URL = "https://www.coreweave.com/pricing"
SOURCE_URL = PRICING_URL

NEBIUS_GPUS = {"H100", "H200", "B200", "B300", "GB200", "GB300", "L40S"}

# Map product_id (from data-product attr) → (gpu_model, gpu_count)
PRODUCT_MAP = {
    "hgx-h100":           ("H100",  8),
    "hgx-h200":           ("H200",  8),
    "nvidia-b200":        ("B200",  8),
    "nvidia-b300":        ("B300",  8),
    # GB200 NVL72: CoreWeave sells in 4-GPU units at $42/hr → $10.50/GPU-hr
    # (the "NVL72" is the chip generation name; their SKU GPU count = 4)
    "nvidia-gb200-nvl72": ("GB200",  4),
    "nvidia-gb300-nvl72": ("GB300",  4),
    # GH200 = Grace Hopper Superchip (H100 GPU + Grace CPU, 96GB HBM3).
    # Different form factor and memory spec from HGX H100 — exclude to keep
    # the H100 bucket clean and avoid inflating the CoreWeave H100 price.
    # "nvidia-gh200":     ("H100",  1),  # excluded
    "nvidia-l40s":        ("L40S",  8),
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        req = urllib.request.Request(PRICING_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        records = _parse_html(html, now)
        logger.info(f"CoreWeave: {len(records)} records")
        return records
    except Exception as e:
        logger.error(f"CoreWeave scrape failed: {e}")
        return []


def _parse_html(html: str, now: str) -> List[PriceRecord]:
    records = []
    seen = set()  # tracks (gpu_model, ct) only

    row_pattern = re.compile(
        r'<h3[^>]*data-product="([^"]+)"[^>]*>.*?</h3>(.*?)'
        r'(?=<h3[^>]*data-product=|$)',
        re.DOTALL,
    )

    for row_m in row_pattern.finditer(html):
        product_id = row_m.group(1)
        block = row_m.group(2)
        text = re.sub(r'<[^>]+>', ' ', block)
        text = re.sub(r'\s+', ' ', text).strip()

        gpu_model, gpu_count = _match_product(product_id)
        if gpu_model is None:
            continue

        # Spec cells of the same row, e.g. "128 vCPUs 2,048 System RAM 8 GPU Count".
        # vCPUs are published as threads (no conversion). Header says "System RAM
        # (GB)" but CoreWeave defines GB as binary 2^30 and values are power-of-two
        # DIMM totals → GiB as published, number kept. CoreWeave GPU instances are
        # whole bare-metal nodes (8-GPU HGX host; "4^1" = 2-Superchip GB200/GB300
        # tray per footnote 1), so the row's GPU Count is also the node GPU count.
        vcpu = _spec(text, "vCPUs")
        ram_gb = _spec(text, "System RAM", float)
        node_gpus = _spec(text, "GPU Count")

        od_m = re.search(r'On-Demand Price:\s*\$([0-9.]+)', text)
        spot_m = re.search(r'Spot Price:\s*\$([0-9.]+)', text)

        # All CoreWeave records use us-central-1 (their primary region)
        region = "us-central-1"

        if od_m:
            price = float(od_m.group(1))
            key = (gpu_model, "on_demand")
            if key not in seen:
                seen.add(key)
                records.append(PriceRecord(
                    provider="coreweave",
                    gpu_model=gpu_model,
                    gpu_count=gpu_count,
                    instance_type=product_id,
                    region=region,
                    consumption_type="on_demand",
                    price_per_hour_usd=price,
                    price_per_gpu_hour_usd=price / gpu_count,
                    vcpu=vcpu,
                    ram_gb=ram_gb,
                    node_gpus=node_gpus,
                    fetched_at=now,
                    source_url=SOURCE_URL,
                    data_source="web_scrape",
                ))

        if spot_m:
            price = float(spot_m.group(1))
            key = (gpu_model, "spot")
            if key not in seen:
                seen.add(key)
                records.append(PriceRecord(
                    provider="coreweave",
                    gpu_model=gpu_model,
                    gpu_count=gpu_count,
                    instance_type=product_id,
                    region=region,
                    consumption_type="spot",
                    price_per_hour_usd=price,
                    price_per_gpu_hour_usd=price / gpu_count,
                    vcpu=vcpu,
                    ram_gb=ram_gb,
                    node_gpus=node_gpus,
                    fetched_at=now,
                    source_url=SOURCE_URL,
                    data_source="web_scrape",
                ))

    return records


def _match_product(product_id: str) -> tuple:
    pid = product_id.lower()
    # Exact match first
    if pid in PRODUCT_MAP:
        return PRODUCT_MAP[pid]
    # Prefix match for variants like nvidia-hgx-h100-80gb
    for key, val in PRODUCT_MAP.items():
        if pid.startswith(key) or key in pid:
            return val
    return None, None


def _spec(text: str, label: str, cast=int):
    """Value of a spec cell rendered '<value> <label>' in the row text, e.g.
    '144 vCPUs', '2,048 System RAM', '4^1 GPU Count' (^1 = footnote marker).
    None when the row has no such cell."""
    m = re.search(r'(\d[\d,]*(?:\.\d+)?)(?:\^\d+)?\s+' + re.escape(label), text)
    if not m:
        return None
    try:
        val = cast(float(m.group(1).replace(",", "")))
    except (ValueError, OverflowError):
        return None
    return val if val > 0 else None  # a 0/garbage cell is "unknown", not a spec
