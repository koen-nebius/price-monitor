"""
GMI Cloud capacity fetcher — pricing-page availability badges.

https://www.gmicloud.ai/pricing is server-rendered (~99KB, verified
2026-08-12): each GPU card carries "AVAILABLE NOW" or "Limited Availability"
plus "Pre order" / "Contact Sales" price cells. Provider-declared state, no
region granularity. Verified badges that day: GB200 AVAILABLE NOW, GB300
AVAILABLE NOW (Pre order), H100/H200 AVAILABLE NOW, B200 Limited.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

URL = "https://www.gmicloud.ai/pricing"
SOURCE_URL = URL

# The price cell is REQUIRED: an optional group matches empty immediately
# (lazy regex) and turned GB300's "Pre order" into plain available.
_BADGE_RE = re.compile(
    r"(AVAILABLE NOW|Limited Availability)\s+NVIDIA\s+(GB300|GB200|B300|B200|H200|H100)"
    r"[^$]{0,80}?(from \$[\d.]+|Pre order|Contact Sales)",
    re.I,
)


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        html = http_get(URL, timeout=45).decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"GMI pricing page fetch failed: {e}")
        return []

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    records: List[AvailabilityRecord] = []
    seen = set()
    for m in _BADGE_RE.finditer(text):
        badge, gpu, price_cell = m.group(1), m.group(2).upper(), (m.group(3) or "")
        if gpu in seen:
            continue
        seen.add(gpu)
        preorder = "pre order" in price_cell.lower()
        if preorder:
            state = "limited"
            detail = f"badge '{badge}' but Pre order (not yet deliverable)"
        elif badge.upper() == "AVAILABLE NOW":
            state = "available"
            detail = f"badge 'AVAILABLE NOW' ({price_cell.strip() or 'priced'})"
        else:
            state = "limited"
            detail = f"badge 'Limited Availability' ({price_cell.strip() or 'contact sales'})"
        records.append(AvailabilityRecord(
            provider="gmi", gpu_model=gpu, region="global",
            consumption_type="on_demand", state=state,
            metric_type="stock_status_label",
            metric_value={"available": 2.0, "limited": 1.0}.get(state, 0.0),
            detail=detail,
            fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
        ))

    if not records:
        logger.error("GMI: no availability badges parsed — page layout changed?")
    logger.info(f"GMI: {len(records)} records")
    return records
