"""
Nebius capacity fetcher — OUTSIDE-IN view (what a prospect sees), for the
us-vs-market row. No internal credentials by design.

Two public sources (verified 2026-08-12):
  1. https://docs.nebius.com/overview/regions — platform × region matrix
     (✓/—): the public offering footprint per GPU per region.
  2. https://nebius.com/prices — which SKUs are self-service priced vs
     "Contact us" (sales-gated = not self-service capacity).
Footprint semantics, not live stock: our own console-level availability is
internal data and out of scope for a competitor-facing monitor.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

REGIONS_URL = "https://docs.nebius.com/overview/regions"
PRICES_URL = "https://nebius.com/prices"
SOURCE_URL = REGIONS_URL

# Platform display-name fragment → GPU model (uppercased match)
_PLATFORM_MAP = [
    ("GB300", "GB300"), ("GB200", "GB200"), ("B300", "B300"), ("B200", "B200"),
    ("H200", "H200"), ("H100", "H100"), ("L40S", "L40S"), ("RTX PRO 6000", "RTX6000"),
    ("VERA", "VR"), ("RUBIN", "VR"),
]


def _parse_regions(body: str):
    """Platform×region table → {gpu_model: set(regions)}.

    docs.nebius.com (Mintlify) serves raw MARKDOWN to non-browser agents and
    HTML to browsers — normalize HTML to pipe-text first, then parse the pipe
    table either way. Region codes are the column headers (`eu-north1`, ...);
    cells are ✓ / —."""
    if "<html" in body.lower():
        body = re.sub(r"<[^>]+>", "|", body)
    lines = [l for l in body.splitlines() if "|" in l]
    regions, model_regions = [], {}
    for line in lines:
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        if not regions:
            if cells[0].lower().startswith("platform name"):
                regions = [c for c in cells[1:] if re.match(r"^[a-z]{2}-[a-z]+\d$", c)]
            continue
        if set(cells[0]) <= {"-", " "}:   # markdown separator row
            continue
        label = cells[0].upper()
        model = next((mod for frag, mod in _PLATFORM_MAP if frag in label), None)
        if not model:
            continue
        for region, cell in zip(regions, cells[1:]):
            if "✓" in cell:
                model_regions.setdefault(model, set()).add(region)
        model_regions.setdefault(model, set())
    return model_regions


def _parse_sales_gated(html: str) -> set:
    """GPU models whose on-demand cell is 'Contact us' on the pricing page."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    gated = set()
    for m in re.finditer(r"NVIDIA\s+((?:HGX|GB\d+ NVL\d+|RTX PRO \d+|L40S|H\d+|B\d+)[^|]{0,20}?)\s"
                         r"[^.]{0,120}?Contact us", text):
        label = m.group(1).upper()
        model = next((mod for frag, mod in _PLATFORM_MAP if frag in label), None)
        if model:
            gated.add(model)
    return gated


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    from fetchers._http import http_get

    try:
        regions_html = http_get(REGIONS_URL, timeout=45).decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"Nebius regions docs fetch failed: {e}")
        return []
    model_regions = _parse_regions(regions_html)
    if not model_regions:
        logger.error("Nebius: platform×region matrix not parsed — layout changed?")
        return []

    gated = set()
    try:
        gated = _parse_sales_gated(http_get(PRICES_URL, timeout=45).decode("utf-8", "replace"))
    except Exception as e:
        logger.warning(f"Nebius prices page fetch failed (footprint still serves): {e}")

    records: List[AvailabilityRecord] = []
    for model, regions in sorted(model_regions.items()):
        n = len(regions)
        sales_gated = model in gated
        # Footprint width is a PRODUCT choice, not a stock state — deriving
        # "limited" from it painted our own company with a warning emoji next
        # to peers' checkmarks (red-team 2026-08-12). State stays neutral;
        # the renderer shows Nebius as a labeled reference block.
        if n == 0:
            state, detail = "not_offered", "no region lists the platform"
        elif sales_gated:
            state = "available"
            detail = (f"sales-gated ('Contact us', no self-service price): "
                      f"{', '.join(sorted(regions))}")
        else:
            state = "available"
            detail = f"self-service: {', '.join(sorted(regions))}"
        records.append(AvailabilityRecord(
            provider="nebius", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="listed_offering", metric_value=float(n),
            detail=detail,
            fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
        ))
        for region in sorted(regions):
            records.append(AvailabilityRecord(
                provider="nebius", gpu_model=model, region=region,
                consumption_type="on_demand", state="available",
                metric_type="listed_offering", metric_value=1.0,
                detail="listed (footprint)" + (" — sales-gated" if sales_gated else ""),
                fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
            ))

    logger.info(f"Nebius (outside-in): {len(records)} records "
                f"({', '.join(f'{m}:{len(r)}r' for m, r in sorted(model_regions.items()))})")
    return records
