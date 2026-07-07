"""
SF Compute market FILLS — actual transacted prices for short-term/forward
GPU windows, incl. a secondary market where resold reserved contracts trade.

The only public machine-readable source of EXECUTED term prices found in the
reserve-pricing source research (2026-07-07, analysis/reserve_price_sources.md).
Requires a free bearer token (SFCOMPUTE_TOKEN env; created with `sf tokens
create` after signup — no fee). Without the token this fetcher is skipped and
the provider is not registered (see config.PROVIDERS).

API notes (verified empirically 2026-07-07):
- GET api.sfcompute.com/preview/v2/orderbook/fills
  params: requirements=gpu_type:<h100|h200|b200>, start_at/end_at (epoch secs)
- fills carry dollars_per_node_hour (NODE-hour: divide by 8 for $/GPU-hr),
  filled_at, node_count; history capped ~90d; /preview API may change.
"""
import json
import logging
import os
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord

logger = logging.getLogger(__name__)

API = "https://api.sfcompute.com/preview/v2/orderbook/fills"
SOURCE_URL = "https://sfcompute.com"
GPUS_PER_NODE = 8
WINDOW_DAYS = 14
GPU_TYPES = {"h100": "H100", "h200": "H200", "b200": "B200"}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    token = os.environ.get("SFCOMPUTE_TOKEN")
    if not token:
        logger.info("SF Compute fills: SFCOMPUTE_TOKEN not set — skipping")
        return []
    now_iso = datetime.now(timezone.utc).isoformat()
    now = int(time.time())
    start = now - WINDOW_DAYS * 86400
    records = []
    for gt, gpu in GPU_TYPES.items():
        params = urllib.parse.urlencode({
            "requirements": f"gpu_type:{gt}",
            "start_at": start,
            "end_at": now,
        })
        try:
            req = urllib.request.Request(
                f"{API}?{params}",
                headers={"Authorization": f"Bearer {token}",
                         "User-Agent": "price-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.load(resp)
        except Exception as e:
            logger.warning(f"SF Compute fills {gt}: {e}")
            continue
        fills = data.get("data") or data.get("fills") or []
        rates = []
        for f in fills:
            dpnh = f.get("dollars_per_node_hour")
            if isinstance(dpnh, (int, float)) and dpnh > 0:
                rates.append(dpnh / GPUS_PER_NODE)
        if not rates:
            continue
        med = statistics.median(rates)
        if med < 0.10:   # unit-error guard
            continue
        records.append(PriceRecord(
            provider="sfcompute",
            gpu_model=gpu,
            gpu_count=GPUS_PER_NODE,
            instance_type=f"sfc-fills-{WINDOW_DAYS}d-median-n{len(rates)}",
            region="market",
            consumption_type="reserved_short",   # transacted short-term windows
            price_per_hour_usd=med * GPUS_PER_NODE,
            price_per_gpu_hour_usd=round(med, 4),
            fetched_at=now_iso,
            source_url=SOURCE_URL,
            data_source="api",
        ))
    if records:
        logger.info(f"SF Compute fills: {len(records)} records")
    return records
