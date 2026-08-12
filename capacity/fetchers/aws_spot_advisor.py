"""
AWS Spot Instance Advisor — public no-auth JSON with per-region spot pools.

https://spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json (~1.2MB,
updated by AWS roughly weekly). Two capacity signals per GPU instance type:
  1. breadth — which regions carry a spot pool at all (spot = unsold
     on-demand capacity, so presence means spare capacity exists);
  2. pressure — the interruption-frequency bucket 'r' (4 = AWS reclaims >20%
     of the time = tightest).

Weekly cadence means day-over-day diffs are mostly quiet; treat moves as
meaningful when they happen. Verified 2026-08-12: p5 in 12 regions, p6-b200
in 3 (r=1-2), p6-b300 only us-west-2 (r=0), p5e and GB200 absent.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

URL = "https://spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json"
SOURCE_URL = "https://aws.amazon.com/ec2/spot/instance-advisor/"

INSTANCE_GPU_MAP = {
    "p5.48xlarge": "H100",
    "p5e.48xlarge": "H200",
    "p5en.48xlarge": "H200",
    "p6-b200.48xlarge": "B200",
    "p6-b300.48xlarge": "B300",
    "g6e.48xlarge": "L40S",
}

_RANGE_LABELS = {0: "<5%", 1: "5-10%", 2: "10-15%", 3: "15-20%", 4: ">20%"}


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        data = json.loads(http_get(URL, timeout=60))
    except Exception as e:
        logger.error(f"AWS spot advisor fetch failed: {e}")
        return []

    spot = data.get("spot_advisor", {})
    records: List[AvailabilityRecord] = []
    per_model_regions: dict = {}

    for region, oses in spot.items():
        linux = oses.get("Linux", {})
        for itype, gpu_model in INSTANCE_GPU_MAP.items():
            entry = linux.get(itype)
            if entry is None:
                continue
            r_idx = entry.get("r")
            label = _RANGE_LABELS.get(r_idx, "?")
            state = "limited" if r_idx == 4 else "available"
            per_model_regions.setdefault(gpu_model, set()).add(region)
            records.append(AvailabilityRecord(
                provider="aws", gpu_model=gpu_model, region=region,
                consumption_type="spot", state=state,
                metric_type="stock_status_label", metric_value=float(r_idx) if r_idx is not None else None,
                detail=f"spot pool live, interruption {label}",
                instance_type=itype,
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))

    # Global breadth row per model (the matrix cell)
    covered_models = set(INSTANCE_GPU_MAP.values())
    for gpu_model in covered_models:
        regions = per_model_regions.get(gpu_model, set())
        if regions:
            records.append(AvailabilityRecord(
                provider="aws", gpu_model=gpu_model, region="global",
                consumption_type="spot", state="available",
                metric_type="regions_with_capacity", metric_value=float(len(regions)),
                detail=f"spot pools in {len(regions)} region(s) (advisor, ~weekly refresh)",
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))
        else:
            records.append(AvailabilityRecord(
                provider="aws", gpu_model=gpu_model, region="global",
                consumption_type="spot", state="sold_out",
                metric_type="regions_with_capacity", metric_value=0.0,
                detail="no spot pool in any region (advisor, ~weekly refresh)",
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))

    logger.info(f"AWS spot advisor: {len(records)} records")
    return records
