"""
CoreWeave availability fetcher — docs availability matrix (raw markdown).

https://docs.coreweave.com/platform/instances/availability-matrix.md is a
server-rendered markdown pipe table: GPU instance rows × 18 General Access
AZ columns, cells "Yes" or empty. Semantics: DEPLOYED FOOTPRINT per AZ
("availability is subject to capacity"), not live stock — CoreWeave publishes
no live-stock signal anywhere (verified 2026-08-12: no public API metadata,
status page is incidents-only, pricing page has no sold-out states). Row/AZ
diffs are still high-signal for the #1 peer: a new AZ lighting up or a GPU
row appearing (e.g. Vera Rubin) is deployment intel.

Verified 2026-08-12: GB300 2 AZs, GB200 6, B300 4, B200 6, H200 6, H100 8,
RTX PRO 6000 9, L40S 1.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

URL = "https://docs.coreweave.com/platform/instances/availability-matrix.md"
SOURCE_URL = "https://docs.coreweave.com/platform/instances/availability-matrix"

# Row-label fragment → GPU model (longest first; skip L40 (bare), GH200, A100)
_LABEL_MAP = [
    ("GB300", "GB300"), ("GB200", "GB200"), ("B300", "B300"), ("B200", "B200"),
    ("H200", "H200"), ("H100", "H100"), ("RTX PRO 6000", "RTX6000"),
    ("L40S", "L40S"), ("VERA", "VR"), ("RUBIN", "VR"),
]

# 3+ AZs = broadly deployed; 1-2 = thin footprint. Not live stock either way.
_LIMITED_MAX_AZS = 2


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        text = http_get(URL, timeout=45).decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"CoreWeave matrix fetch failed: {e}")
        return []

    # Only the GPU instances table (stop at the CPU section)
    gpu_section = text.split("## GPU instances", 1)
    if len(gpu_section) < 2:
        logger.error("CoreWeave matrix: '## GPU instances' section not found")
        return []
    section = gpu_section[1].split("## CPU", 1)[0]

    lines = [l for l in section.splitlines() if l.strip().startswith("|")]
    if len(lines) < 3:
        logger.error("CoreWeave matrix: table not found")
        return []

    header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    az_names = []
    for cell in header[1:]:
        m = re.match(r"\[([^\]]+)\]", cell)
        az_names.append(m.group(1) if m else cell)

    model_azs: dict = {}
    for line in lines[2:]:   # skip header + separator
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = cells[0].upper()
        model = next((m for frag, m in _LABEL_MAP if frag in label), None)
        if not model:
            continue
        for az, cell in zip(az_names, cells[1:]):
            if cell.lower().startswith("yes"):
                model_azs.setdefault(model, set()).add(az)
        model_azs.setdefault(model, set())   # row present with zero AZs still counts

    records: List[AvailabilityRecord] = []
    for model, azs in model_azs.items():
        n = len(azs)
        if n == 0:
            state, detail = "limited", "listed in docs but deployed in 0 GA AZs (preview/withdrawn)"
        elif n <= _LIMITED_MAX_AZS:
            state = "limited"
            detail = f"deployed in only {n} of {len(az_names)} GA AZs: {', '.join(sorted(azs))} (footprint, not live stock)"
        else:
            state = "available"
            detail = f"deployed in {n} of {len(az_names)} GA AZs (footprint, not live stock)"
        records.append(AvailabilityRecord(
            provider="coreweave", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="listed_offering", metric_value=float(n),
            detail=detail,
            fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
        ))
        for az in sorted(azs):
            records.append(AvailabilityRecord(
                provider="coreweave", gpu_model=model, region=az,
                consumption_type="on_demand", state="available",
                metric_type="listed_offering", metric_value=1.0,
                detail="deployed (footprint)",
                fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
            ))

    logger.info(f"CoreWeave matrix: {len(records)} records "
                f"({len(model_azs)} GPU models, {len(az_names)} AZs)")
    return records
