"""
Lambda Labs capacity fetcher.

GET https://cloud.lambdalabs.com/api/v1/instance-types (Basic auth,
LAMBDA_API_KEY) — each instance type carries regions_with_capacity_available:
the list of regions where it can be launched RIGHT NOW. Empty list = sold out
fleet-wide for that instance type. This is a true live-stock signal (the same
field the pricing fetcher deliberately ignores).

Per-GPU-model aggregation: a model is available in region R if ANY of its
instance sizes is launchable there; the metric is the region count and the
detail lists regions. Sold-out sizes are reported per instance type so a
"1x available / 8x gone" split (single-GPU scraps vs cluster capacity) stays
visible — the 8x flagship SKU state is what capacity decisions care about.
"""
import base64
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

API_URL = "https://cloud.lambdalabs.com/api/v1/instance-types"
SOURCE_URL = "https://cloud.lambdalabs.com"

# Instance-id fragment → GPU model (reuses the pricing fetcher's mapping rules;
# GH200 excluded — different form factor). Lambda's "gpu_1x_rtx6000" is the
# LEGACY Quadro RTX 6000 (Turing 24GB), NOT the Blackwell RTX PRO 6000 —
# excluded so it can't pollute the RTX6000 bucket (research finding 2026-08-12).
_GPU_FRAGMENTS = [
    ("gb300", "GB300"), ("gb200", "GB200"), ("b300", "B300"), ("b200", "B200"),
    ("h200", "H200"), ("h100", "H100"), ("l40s", "L40S"),
    ("rtx_pro_6000", "RTX6000"), ("rtxpro6000", "RTX6000"),
]


def _match_gpu(instance_id: str):
    low = instance_id.lower()
    if "gh200" in low:
        return None
    for frag, model in _GPU_FRAGMENTS:
        if frag in low:
            return model
    return None


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    api_key = os.environ.get("LAMBDA_API_KEY")
    if not api_key:
        logger.warning("Lambda capacity: LAMBDA_API_KEY not set — skipping")
        return []

    creds = base64.b64encode(f"{api_key}:".encode()).decode()
    req = urllib.request.Request(API_URL, headers={
        "Authorization": f"Basic {creds}",
        # Cloudflare 1010-bans urllib's default UA before auth is checked
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.error(f"Lambda capacity fetch failed: {e}")
        return []

    instance_types = data.get("data", {})
    if isinstance(instance_types, list):
        instance_types = {str(i): x for i, x in enumerate(instance_types)}

    # gpu_model → {region: [instance types with capacity]}, and per-type states
    records: List[AvailabilityRecord] = []
    model_regions: dict = {}
    model_types: dict = {}

    for name, info in instance_types.items():
        gpu_model = _match_gpu(name)
        if not gpu_model:
            continue
        regions = [r.get("name") for r in (info.get("regions_with_capacity_available") or [])
                   if isinstance(r, dict) and r.get("name")]
        model_types.setdefault(gpu_model, {})[name] = sorted(set(regions))
        for r in regions:
            model_regions.setdefault(gpu_model, {}).setdefault(r, []).append(name)

    for gpu_model, types in model_types.items():
        regions = model_regions.get(gpu_model, {})
        n_types = len(types)
        n_avail_types = sum(1 for t, rs in types.items() if rs)
        largest = max(types, key=lambda t: _size_rank(t))
        largest_avail = bool(types[largest])

        if regions:
            state = "available"
            # Flagship (largest node) sold out while only small sizes remain =
            # scraps, not cluster capacity → limited.
            if not largest_avail:
                state = "limited"
            detail = (f"{len(regions)} region(s): {', '.join(sorted(regions))}; "
                      f"{n_avail_types}/{n_types} sizes launchable"
                      + ("" if largest_avail else f" (largest size {largest} sold out)"))
            records.append(AvailabilityRecord(
                provider="lambda", gpu_model=gpu_model, region="global",
                consumption_type="on_demand", state=state,
                metric_type="regions_with_capacity", metric_value=float(len(regions)),
                detail=detail, instance_type=largest,
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))
            for r in sorted(regions):
                records.append(AvailabilityRecord(
                    provider="lambda", gpu_model=gpu_model, region=r,
                    consumption_type="on_demand", state="available",
                    metric_type="regions_with_capacity", metric_value=1.0,
                    detail=f"launchable sizes: {', '.join(sorted(regions[r]))}",
                    instance_type=sorted(regions[r])[0],
                    fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
                ))
        else:
            records.append(AvailabilityRecord(
                provider="lambda", gpu_model=gpu_model, region="global",
                consumption_type="on_demand", state="sold_out",
                metric_type="regions_with_capacity", metric_value=0.0,
                detail=f"all {n_types} instance size(s) show no region with capacity",
                instance_type=largest,
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))

    logger.info(f"Lambda capacity: {len(records)} records "
                f"({len(model_types)} GPU models)")
    return records


def _size_rank(instance_id: str) -> int:
    import re
    m = re.search(r"gpu_(\d+)x", instance_id.lower())
    return int(m.group(1)) if m else 1
