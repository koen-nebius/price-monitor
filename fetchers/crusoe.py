"""
Crusoe pricing fetcher.
Scrapes https://crusoe.ai/cloud/pricing/
Page structure (Webflow): GPU name in h4, price in div.pricing-rich > p
"""
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord
from config import MANUAL_PRICES

logger = logging.getLogger(__name__)

PRICING_URL = "https://crusoe.ai/cloud/pricing/"
SOURCE_URL = PRICING_URL

GPU_NAME_MAP = {
    "GB200": "GB200",
    "GB300": "GB300",
    "B200":  "B200",
    "B300":  "B300",
    "H200":  "H200",
    "H100":  "H100",
    "L40S":  "L40S",
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    scraped = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        req = urllib.request.Request(PRICING_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        scraped = _parse_html(html, now)
    except Exception as e:
        logger.error(f"Crusoe scrape failed: {e}")

    manual = _manual_records(scraped, now)
    records = scraped + manual
    logger.info(f"Crusoe: {len(scraped)} scraped + {len(manual)} manual = {len(records)} records")
    return records


def _manual_records(scraped: List[PriceRecord], now: str) -> List[PriceRecord]:
    """Inject prices from MANUAL_PRICES that aren't already covered by scraping."""
    scraped_keys = {(r.gpu_model, r.consumption_type, r.region) for r in scraped}
    records = []
    for (provider, gpu_model, ct, region), price in MANUAL_PRICES.items():
        if provider != "crusoe":
            continue
        if (gpu_model, ct, region) in scraped_keys:
            continue
        records.append(PriceRecord(
            provider="crusoe",
            gpu_model=gpu_model,
            gpu_count=1,
            instance_type=f"crusoe-{gpu_model.lower()}-manual",
            region=region,
            consumption_type=ct,
            price_per_gpu_hour_usd=price,
            price_per_hour_usd=price,
            fetched_at=now,
            source_url="manual",
        ))
    return records


def _parse_html(html: str, now: str) -> List[PriceRecord]:
    records = []
    seen = set()

    # Webflow pattern: <h4>NVIDIA H100</h4> ... <div class="pricing-rich ..."><p>$X.XX/GPU-hr</p></div>
    pattern = re.compile(
        r'<h4[^>]*>\s*(?:NVIDIA\s+)?([A-Z0-9\s]+?)\s*</h4>'
        r'(?:(?!<h4).)*?'
        r'<div class="pricing-rich[^"]*"[^>]*><p>\$([0-9.]+)/GPU-hr</p>',
        re.DOTALL,
    )

    for m in pattern.finditer(html):
        gpu_raw = m.group(1).strip()
        gpu_model = _match_gpu(gpu_raw)
        if gpu_model is None:
            continue
        try:
            price = float(m.group(2))
        except ValueError:
            continue
        if price <= 0:
            continue

        ctx = html[max(0, m.start() - 800): m.end() + 100]
        ct = _detect_ct(ctx)
        key = (gpu_model, ct)
        if key in seen:
            continue
        seen.add(key)

        records.append(PriceRecord(
            provider="crusoe",
            gpu_model=gpu_model,
            gpu_count=1,
            instance_type=f"crusoe-{gpu_model.lower()}",
            region="us-east",
            consumption_type=ct,
            price_per_hour_usd=price,
            price_per_gpu_hour_usd=price,
            fetched_at=now,
            source_url=SOURCE_URL,
        ))

    # Fallback regex
    if not records:
        for m in re.finditer(r'(H100|H200|B200|B300|GB200|GB300|L40S)[^<]{0,500}?\$([0-9.]+)/GPU-hr', html, re.IGNORECASE | re.DOTALL):
            gpu_model = _match_gpu(m.group(1))
            if not gpu_model or gpu_model in seen:
                continue
            try:
                price = float(m.group(2))
            except ValueError:
                continue
            seen.add(gpu_model)
            records.append(PriceRecord(
                provider="crusoe",
                gpu_model=gpu_model,
                gpu_count=1,
                instance_type=f"crusoe-{gpu_model.lower()}",
                region="us-east",
                consumption_type="on_demand",
                price_per_hour_usd=price,
                price_per_gpu_hour_usd=price,
                fetched_at=now,
                source_url=SOURCE_URL,
            ))

    return records


def _match_gpu(name: str) -> Optional[str]:
    name = name.strip().upper()
    for pattern, model in GPU_NAME_MAP.items():
        if pattern in name:
            return model
    return None


def _detect_ct(ctx: str) -> str:
    ctx_lower = ctx.lower()
    if "spot" in ctx_lower:
        return "spot"
    if "reserved" in ctx_lower or "commit" in ctx_lower:
        return "reserved_1yr"
    return "on_demand"
