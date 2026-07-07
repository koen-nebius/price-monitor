"""
Vast.ai RESERVED-offer fetcher — the short end of the committed price curve.

Vast reserved instances are prepaid 1-6 month commitments on a commodity
marketplace (host-set discounts up to ~50% off on-demand). Their public,
unauthenticated bundles API exposes per-offer reserved pricing, giving a
daily-scrapeable floor for SHORT-TERM committed GPU pricing. Source of this
integration: reserve-pricing source research 2026-07-07
(analysis/reserve_price_sources.md).

Basis caveats (also stated where rendered): commodity marketplace SKUs,
mixed hosts, no enterprise SLA, mostly single-node — a floor signal for
short-term reserve discussions (capacity blocks / RFC 055), NOT a
cluster-class enterprise comparable.
"""
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord

logger = logging.getLogger(__name__)

API = "https://console.vast.ai/api/v0/bundles/"
SOURCE_URL = "https://vast.ai/pricing"
# NOTE: the endpoint 403s on browser-like User-Agents but accepts curl-style
# UAs (verified 2026-07-07). Keep the UA plain.
UA = "curl/8.4.0 (price-monitor)"

GPU_NAME_MAP = {
    "H100 SXM": "H100",
    "H200": "H200",
    "H200 SXM": "H200",
    "B200": "B200",
    "B200 SXM": "B200",
    "B300": "B300",
    "RTX PRO 6000 S": "RTX6000",
    "RTX PRO 6000 WS": "RTX6000",
    # H100 NVL / PCIe variants intentionally excluded: different form factor
    # would understate the SXM floor.
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    q = {"type": "reserved", "limit": 300}
    url = API + "?q=" + urllib.parse.quote(json.dumps(q))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.load(resp)
    except Exception as e:
        logger.error(f"Vast reserved fetch failed: {e}")
        return []

    offers = data.get("offers", []) or []
    best = {}   # (gpu_model) -> (per_gpu_price, offer)
    for o in offers:
        gpu = GPU_NAME_MAP.get(o.get("gpu_name") or "")
        n = o.get("num_gpus") or 0
        dph = o.get("dph_total")
        if not gpu or not n or not dph or dph <= 0:
            continue
        per_gpu = dph / n
        if per_gpu < 0.10:      # unit-error guard
            continue
        if gpu not in best or per_gpu < best[gpu][0]:
            best[gpu] = (per_gpu, o)

    records = []
    for gpu, (per_gpu, o) in best.items():
        n = o.get("num_gpus") or 1
        records.append(PriceRecord(
            provider="vast",
            gpu_model=gpu,
            gpu_count=n,
            instance_type=f"vast-reserved-{n}x",
            region=(o.get("geolocation") or "global"),
            # Short-term prepaid commitment (1-6mo marketplace reservation):
            # deliberately NOT a committed_* ct so it never mixes into the
            # 1yr+ committed benchmark tables.
            consumption_type="reserved_short",
            price_per_hour_usd=o.get("dph_total") or per_gpu * n,
            price_per_gpu_hour_usd=round(per_gpu, 4),
            fetched_at=now,
            source_url=SOURCE_URL,
            data_source="api",
        ))
    if records:
        logger.info(f"Vast reserved: {len(records)} records (cheapest per GPU model)")
    else:
        logger.warning("Vast reserved: no matching reserved offers")
    return records
