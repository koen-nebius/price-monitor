"""
Crusoe capacity fetcher — docs VM-types-to-zones matrix (offering footprint).

https://docs.crusoecloud.com/compute/virtual-machines/overview/index.html is
server-rendered HTML (verified 2026-08-12, no JS needed) mapping each
instance type to its zones (e.g. h100-80gb-sxm-ib.8x → us-east1-a,
us-southcentral1-a, eu-iceland1-a). Footprint, not live stock.

Crusoe DOES have a quantitative live endpoint — GET /v1alpha5/capacities
returns {location, type, quantity} — but it needs an account + HMAC-signed
auth (CRUSOE_ACCESS_KEY_ID/CRUSOE_SECRET_KEY). Documented for later
activation; the docs matrix keeps Crusoe on the board until then.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord, plural

logger = logging.getLogger(__name__)

URL = "https://docs.crusoecloud.com/compute/virtual-machines/overview/index.html"
SOURCE_URL = URL

_TYPE_GPU = [
    ("gb300", "GB300"), ("gb200", "GB200"), ("b300", "B300"), ("b200", "B200"),
    ("h200", "H200"), ("h100", "H100"), ("l40s", "L40S"),
]

_ZONE_RE = re.compile(r"\b(?:us|eu|ap|me)-[a-z]+\d-[a-z]\b")
_LIMITED_MAX_ZONES = 1


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        html = http_get(URL, timeout=45).decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"Crusoe docs fetch failed: {e}")
        return []

    # Table rows: instance-type slug cell followed by a zone-list cell
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    model_zones: dict = {}
    for row in rows:
        cells = [re.sub(r"<[^>]+>", " ", c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)]
        if not cells:
            continue
        row_text = " ".join(cells).lower()
        type_match = re.search(r"\b([a-z0-9]+(?:-[a-z0-9.]+)+x?)\b", row_text)
        gpu_model = None
        for frag, model in _TYPE_GPU:
            if type_match and frag in type_match.group(1):
                gpu_model = model
                break
        if not gpu_model:
            # Fallback: fragment anywhere in a type-looking token
            for frag, model in _TYPE_GPU:
                if re.search(rf"\b{frag}-\d+gb", row_text):
                    gpu_model = model
                    break
        if not gpu_model:
            continue
        zones = set(_ZONE_RE.findall(row_text))
        if zones:
            model_zones.setdefault(gpu_model, set()).update(zones)

    if not model_zones:
        logger.error("Crusoe docs: no GPU type→zone rows parsed — layout changed?")
        return []

    records: List[AvailabilityRecord] = []
    for model, zones in sorted(model_zones.items()):
        n = len(zones)
        state = "limited" if n <= _LIMITED_MAX_ZONES else "available"
        records.append(AvailabilityRecord(
            provider="crusoe", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="listed_offering", metric_value=float(n),
            detail=f"offered in {plural(n, 'zone')}: {', '.join(sorted(zones))} "
                   f"(footprint, not live stock)",
            fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
        ))

    logger.info(f"Crusoe docs: {len(records)} records "
                f"({', '.join(f'{m}:{len(z)}z' for m, z in sorted(model_zones.items()))})")
    return records
