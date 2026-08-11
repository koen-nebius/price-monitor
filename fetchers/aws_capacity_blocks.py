"""
AWS EC2 Capacity Blocks for ML — published effective reservation rates.

Added 2026-08-11 (B300/GB300/VR price research): the Capacity Blocks pricing
page publishes fixed effective hourly rates per instance AND per accelerator
(e.g. p6-b300.48xlarge $112.32/hr = $14.04/GPU-hr — 21% below the p6-b300
on-demand list). Capacity Blocks are short-term reservations (1 day to ~6
months), so records carry consumption_type="reserved_short" and land in the
existing "Short-term reserved market" section next to Vast reserved and
SF Compute — NOT in the 1yr+ committed benchmark.

Source: https://aws.amazon.com/ec2/capacityblocks/pricing/ — server-rendered
HTML with the tables embedded as JSON-escaped "itemTableData" blobs (no JS
needed). Rates can change over time (AWS charges the published price at time
of reservation purchase), which is exactly why a daily scrape is useful.
"""

import json
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord

logger = logging.getLogger(__name__)

URL = "https://aws.amazon.com/ec2/capacityblocks/pricing/"

# Instance-label prefix → our GPU model. NB the GB200 NVL72 ultraserver rows
# label their GPUs "72 x B200" in the table, but u-p6e-gb200* is rack-scale
# GB200 — map by instance label, not by the GPU-cell text.
_FAMILY_GPU = [
    ("u-p6e-gb300", "GB300"),
    ("u-p6e-gb200", "GB200"),
    ("p6-b300", "B300"),
    ("p6-b200", "B200"),
    ("p5en", "H200"),
    ("p5e", "H200"),
    ("p5", "H100"),
]

_ROW_RE = re.compile(r"\$([\d,]+\.?\d*)\s*(?:USD)?\s*\(\$([\d,]+\.?\d*)\s*USD\)")
_COUNT_RE = re.compile(r"(\d+)\s*x\s*[A-Z]")


def _unescape(blob: str) -> str:
    # The regex captures the INTERIOR of a JSON string; wrapping it in quotes
    # and json.loads-ing performs the exact unescape the page author encoded
    # (hand-rolled codecs.decode broke on rows ending in escape sequences).
    return json.loads(f'"{blob}"')


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"AWS Capacity Blocks fetch failed: {e}")
        return []

    # Each pricing table object carries its rows AND its per-row instance
    # labels — capture them together (separate findall calls mispair: the
    # rowGroups pattern also matches unrelated components on the page).
    blocks = re.findall(
        r'"itemTableData":"(.*?)","dark":"[^"]*","id":"[^"]*",'
        r'"itemHeading":"[^"]*","itemTableRowGroups":"(.*?)","itemRegionProperty"',
        html)

    # best per gpu_model: cheapest per-GPU rate in a STANDARD region (GovCloud
    # carries a premium; Local Zones are fine — they're standard-priced today).
    best: dict = {}
    for tbl, grp in blocks:
        try:
            rows = json.loads(_unescape(tbl))
            labels = [g.get("label", "") for g in json.loads(_unescape(grp))]
        except Exception:
            continue
        for j, row in enumerate(rows):
            cells = {k: re.sub(r"<[^>]+>|\r|\n", "", v).strip()
                     for k, v in row.items() if isinstance(v, str)}
            text = " | ".join(cells.values())
            m = _ROW_RE.search(text)
            cm = _COUNT_RE.search(text)
            if not m or not cm:
                continue
            label = labels[j] if j < len(labels) else ""
            gpu_model = next((g for prefix, g in _FAMILY_GPU
                              if label.startswith(prefix)), None)
            if gpu_model is None:
                continue
            if "govcloud" in text.lower():
                continue
            per_inst = float(m.group(1).replace(",", ""))
            per_gpu = float(m.group(2).replace(",", ""))
            n = int(cm.group(1))
            # sanity: the page's own per-accelerator figure must tie to the
            # per-instance rate and land in a plausible band
            if not (0.5 <= per_gpu <= 30) or abs(per_inst / n - per_gpu) > 0.05:
                logger.warning(f"AWS CB: inconsistent row skipped ({label}: "
                               f"${per_inst}/{n} vs ${per_gpu})")
                continue
            region = next((v for v in cells.values()
                           if re.search(r"US |Europe|Asia|Local Zone", v)), "us")
            if gpu_model not in best or per_gpu < best[gpu_model][0]:
                best[gpu_model] = (per_gpu, per_inst, n, label, region)

    records = []
    for gpu_model, (per_gpu, per_inst, n, label, region) in best.items():
        records.append(PriceRecord(
            provider="aws",
            gpu_model=gpu_model,
            gpu_count=n,
            instance_type=f"capacity-block-{label}",
            region=region,
            consumption_type="reserved_short",
            price_per_hour_usd=per_inst,
            price_per_gpu_hour_usd=per_gpu,
            fetched_at=now,
            source_url=URL,
            data_source="official_api",
        ))
    if records:
        summary = ", ".join(f"{r.gpu_model} ${r.price_per_gpu_hour_usd:.2f}"
                            for r in sorted(records, key=lambda r: r.gpu_model))
        logger.info(f"AWS Capacity Blocks: {len(records)} records ({summary})")
    else:
        logger.warning("AWS Capacity Blocks: no rows parsed — page layout may have changed")
    return records
