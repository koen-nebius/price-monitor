"""
GCP GPU zones fetcher — public "GPU regions and zones" docs page.

https://cloud.google.com/compute/docs/gpus/gpu-regions-zones is fully
server-rendered: one HTML table, ~87 zone rows with the GPU machine series
offered per zone. Semantics: OFFERING FOOTPRINT (which zones sell the
series), not live stock — GCP exposes no public live-capacity signal at all
(no spot placement scores; spot prices reprice ~monthly). Zone additions/
removals per series are the trackable signal, plus a new-series tripwire
(A4X Max = GB300 appearing in new zones, future Vera Rubin series).

Series → GPU map is printed on the page itself (verified 2026-08-12):
A4X Max=GB300, A4X=GB200, A4=B200, A3 Ultra=H200, A3 High/Mega/Edge=H100,
G4=RTX PRO 6000. G2 (L4) and N1+T4/V100 etc. are ignored.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

URL = "https://cloud.google.com/compute/docs/gpus/gpu-regions-zones"
SOURCE_URL = URL

# Longest-first token matching within a zone's series list
_SERIES_MAP = [
    ("A4X MAX", "GB300"),
    ("A4X", "GB200"),
    ("A4", "B200"),
    ("A3 ULTRA", "H200"),
    ("A3 HIGH", "H100"),
    ("A3 MEGA", "H100"),
    ("A3 EDGE", "H100"),
    ("G4", "RTX6000"),
]

_LIMITED_MAX_ZONES = 2


def _extract_series(cell_text: str) -> set:
    models = set()
    text = cell_text.upper()
    # Mask longer tokens before shorter prefixes match (A4X MAX ⊃ A4X ⊃ A4)
    for token, model in _SERIES_MAP:
        if re.search(rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])", text):
            models.add(model)
            text = text.replace(token, " ")
    return models


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        html = http_get(URL, timeout=60).decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"GCP zones fetch failed: {e}")
        return []

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    model_zones: dict = {}
    parsed_rows = 0
    for row in rows:
        cells = [re.sub(r"<[^>]+>", " ", c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)]
        if len(cells) < 4:
            continue
        zone = cells[0].strip()
        if not re.match(r"^[a-z]+-[a-z0-9]+-[a-z]$", zone):
            continue
        parsed_rows += 1
        for model in _extract_series(cells[3]):
            model_zones.setdefault(model, set()).add(zone)

    if parsed_rows == 0:
        logger.error("GCP zones: no zone rows parsed — page layout changed?")
        return []

    records: List[AvailabilityRecord] = []
    for model, zones in sorted(model_zones.items()):
        n = len(zones)
        regions = sorted({z.rsplit("-", 1)[0] for z in zones})
        state = "limited" if n <= _LIMITED_MAX_ZONES else "available"
        records.append(AvailabilityRecord(
            provider="gcp", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="listed_offering", metric_value=float(n),
            detail=f"offered in {n} zone(s) across {len(regions)} region(s) "
                   f"(footprint, not live stock)",
            fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
        ))
        for region in regions:
            n_z = sum(1 for z in zones if z.startswith(region + "-"))
            records.append(AvailabilityRecord(
                provider="gcp", gpu_model=model, region=region,
                consumption_type="on_demand", state="available",
                metric_type="listed_offering", metric_value=float(n_z),
                detail=f"{n_z} zone(s) (footprint)",
                fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
            ))

    logger.info(f"GCP zones: {len(records)} records from {parsed_rows} zone rows "
                f"({', '.join(f'{m}:{len(z)}z' for m, z in sorted(model_zones.items()))})")
    return records
