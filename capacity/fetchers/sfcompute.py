"""
SF Compute capacity fetcher — spot-exchange clearing prices (+ availability
schedule when the token is present).

Tier 1 (keyless, always on): the homepage embeds pricesByHardwareType —
31 daily {avg, top, bottom} $/GPU-hr points for H100/H200/B200. On an
exchange, price IS the supply/demand signal: emitted as
metric_type="clearing_price_usd"; the diff flags ≥30% moves.

Tier 2 (SFCOMPUTE_TOKEN — same secret the pricing scrape uses): GET
/preview/v2/instance_skus/availability returns per-SKU (site-encoded alias,
e.g. sea-3-h100) schedules of purchasable node_count — real quantitative
depth per site.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord, plural

logger = logging.getLogger(__name__)

HOMEPAGE = "https://sfcompute.com/"
AVAIL_API = "https://api.sfcompute.com/preview/v2/instance_skus/availability"
SOURCE_URL = HOMEPAGE

_SKU_GPU_RE = re.compile(r"(h100|h200|b200|b300|gb200|gb300)", re.I)


def _ticker_records(now: str) -> List[AvailabilityRecord]:
    from fetchers._http import http_get
    html = http_get(HOMEPAGE, timeout=45).decode("utf-8", "replace")
    m = re.search(r'pricesByHardwareType\\?":(\{.*?\]\})', html)
    if not m:
        logger.warning("SF Compute: pricesByHardwareType not found on homepage")
        return []
    blob = m.group(1).replace('\\"', '"')
    try:
        prices = json.loads(blob)
    except json.JSONDecodeError:
        # Escaped-JSON variants — strip remaining backslashes conservatively
        try:
            prices = json.loads(blob.replace("\\", ""))
        except json.JSONDecodeError:
            logger.warning("SF Compute: could not parse ticker JSON")
            return []

    records = []
    for hw, series in prices.items():
        gpu_model = hw.upper()
        if gpu_model not in {"H100", "H200", "B200", "B300", "GB200", "GB300"}:
            continue
        if not isinstance(series, list) or not series:
            continue
        latest = series[-1]
        avg = latest.get("avg")
        if avg is None:
            continue
        # $0.00 (or near-zero) = no trades printed, NOT an available market.
        # Emitting it as a price fabricated a number on an exec artifact
        # (caught in the 2026-08-12 red-team pass).
        if float(avg) <= 0.05:
            records.append(AvailabilityRecord(
                provider="sfcompute", gpu_model=gpu_model, region="global",
                consumption_type="reserved_short", state="unknown",
                metric_type="clearing_price_usd", metric_value=None,
                detail="no trades printed on the exchange",
                fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
            ))
            continue
        # A real clearing price = the market is transacting = purchasable.
        records.append(AvailabilityRecord(
            provider="sfcompute", gpu_model=gpu_model, region="global",
            consumption_type="reserved_short", state="available",
            metric_type="clearing_price_usd", metric_value=round(float(avg), 2),
            detail=f"clearing avg ${avg:.2f}/GPU-hr "
                   f"(range ${latest.get('bottom', 0):.2f}-${latest.get('top', 0):.2f})",
            fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
        ))
    return records


def _availability_records(now: str, token: str) -> List[AvailabilityRecord]:
    from fetchers._http import http_get
    data = json.loads(http_get(AVAIL_API, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
    }, timeout=30))
    records = []
    for item in data.get("data", []):
        sku = item.get("instance_sku") or {}
        alias = sku.get("alias") or sku.get("id") or ""
        gm = _SKU_GPU_RE.search(alias)
        if not gm:
            continue
        gpu_model = gm.group(1).upper()
        site = alias.rsplit("-", 1)[0] if "-" in alias else alias
        schedule = item.get("schedule") or []
        max_nodes = max((s.get("node_count", 0) for s in schedule), default=0)
        if max_nodes > 0:
            records.append(AvailabilityRecord(
                provider="sfcompute", gpu_model=gpu_model, region=site,
                consumption_type="reserved_short", state="available",
                metric_type="stock_level", metric_value=float(max_nodes * 8),
                detail=f"up to {plural(max_nodes, 'node')} ({max_nodes * 8} GPUs) purchasable",
                instance_type=alias,
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))
        else:
            records.append(AvailabilityRecord(
                provider="sfcompute", gpu_model=gpu_model, region=site,
                consumption_type="reserved_short", state="sold_out",
                metric_type="stock_level", metric_value=0.0,
                detail="no published availability window",
                instance_type=alias,
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))
    return records


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    records: List[AvailabilityRecord] = []

    try:
        records.extend(_ticker_records(now))
    except Exception as e:
        logger.error(f"SF Compute ticker failed: {e}")

    token = os.environ.get("SFCOMPUTE_TOKEN")
    if token:
        try:
            records.extend(_availability_records(now, token))
        except Exception as e:
            logger.warning(f"SF Compute availability API failed (ticker still serves): {e}")

    logger.info(f"SF Compute: {len(records)} records")
    return records
