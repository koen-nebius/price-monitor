"""
SF Compute (sfcompute.com) — GPU spot-market exchange (Phase 2.3).

SF Compute is a market exchange: buyers purchase short-term compute at a
market-clearing price, with no long-term lock-in and the ability to sell back
unused capacity. This is a distinct pricing MECHANISM from on-demand or reserved
list pricing, so it's worth tracking as a market reference.

Only H100 has a public clearing price (shown on the homepage as
"Average prices for H100 nodes $X.XX average gpu/hr"). H200/B200 are not publicly
priced; B300 is "coming this fall" as of mid-2026. Recorded as consumption_type
"spot" (variable, short-term, no commitment) and tagged with a market-exchange note.

Fragile by nature (a marketing homepage), so every parse failure returns [] — the
pipeline then simply omits SF Compute rather than erroring.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord
from fetchers._http import http_get
from fetchers._tavily import fetch_text as tavily_fetch_text

logger = logging.getLogger(__name__)

URL = "https://sfcompute.com/"


def _parse_h100(raw: str) -> float:
    """Extract the H100 clearing price from page text (raw HTML or Tavily markdown)."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"Average prices for H100 nodes\s*\$\s*([\d.]+)\s*average", text, re.IGNORECASE)
    if not m:
        m = re.search(r"Buy H100s from\s*\$\s*([\d.]+)\s*/?\s*hr", text, re.IGNORECASE)
    if m:
        try:
            v = float(m.group(1))
            if 0.3 <= v <= 6.0:   # sanity bound for an H100 $/GPU-hr
                return v
        except ValueError:
            pass
    return None


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    price = None
    try:
        html = http_get(URL, timeout=20).decode("utf-8", errors="replace")
        price = _parse_h100(html)
    except Exception as e:
        logger.warning(f"SF Compute plain fetch failed: {e}")

    if price is None:
        # Escalate to Tavily — renders the Next.js page when the static parse misses
        # (e.g. if SF Compute moves the price fully client-side, or for future B-series).
        tav = tavily_fetch_text(URL)
        if tav:
            price = _parse_h100(tav)
            if price is not None:
                logger.info("SF Compute: parsed H100 clearing price via Tavily fallback")

    if price is None:
        logger.warning("SF Compute: could not parse H100 clearing price — skipping")
        return []

    rec = PriceRecord(
        provider="sfcompute",
        gpu_model="H100",
        gpu_count=8,
        instance_type="sfcompute-h100-node",
        region="us",
        consumption_type="spot",   # variable short-term market price, no commitment
        price_per_hour_usd=price * 8,
        price_per_gpu_hour_usd=price,
        fetched_at=now,
        source_url=URL,
        data_source="web_scrape",
        interconnect="InfiniBand",
        form_factor="SXM",
        node_gpus=8,
    )
    logger.info(f"SF Compute: 1 record — H100 market-clearing ${price:.2f}/GPU-hr")
    return [rec]
