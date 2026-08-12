"""
AWS EC2 Capacity Blocks — purchasable-offering lead time per GPU per region.

boto3 describe_capacity_block_offerings(InstanceType=..., InstanceCount=1,
CapacityDurationHours=24, start/end window = next 14 days) per region:
  - earliest StartDate → lead_time_days (0-1d = available, >7d = limited)
  - empty offering list over the whole window = sold_out
  - UpfrontFee is dynamically priced against remaining supply (logged in the
    detail string; fee trend is itself a scarcity signal).

IAM: needs ec2:DescribeCapacityBlockOfferings — NOT covered by the repo's
pricing-only AWS creds as of 2026-08-12. The fetcher soft-skips on
AccessDenied and starts emitting records the day the permission is added.
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import List

from capacity.schema import AvailabilityRecord, plural

logger = logging.getLogger(__name__)

SOURCE_URL = "https://aws.amazon.com/ec2/capacityblocks/"

INSTANCE_GPU_MAP = {
    "p5.48xlarge": "H100",
    "p5en.48xlarge": "H200",
    "p6-b200.48xlarge": "B200",
    "p6-b300.48xlarge": "B300",
}

# Capacity Blocks regions (docs, Aug 2026). Errors for not-offered regions are
# caught per-call, so an outdated list degrades gracefully. Kept to 4 regions:
# DescribeCapacityBlockOfferings is aggressively rate-limited
# (CapacityBlockDescribeLimitExceeded on back-to-back calls, seen 2026-08-12).
CB_REGIONS = ["us-east-1", "us-east-2", "us-west-2", "eu-north-1"]

LIMITED_LEAD_DAYS = 7.0
WINDOW_DAYS = 14
# Pacing + backoff against the describe rate limit
CALL_SPACING_S = 4
BACKOFF_S = [15, 45]


def fetch() -> List[AvailabilityRecord]:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        logger.warning("AWS capacity blocks: no AWS credentials — skipping")
        return []
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        logger.warning("AWS capacity blocks: boto3 not installed — skipping")
        return []

    records: List[AvailabilityRecord] = []
    denied = False
    error_codes: dict = {}
    # The describe API has a small account-level quota (exhausted by repeated
    # test runs on 2026-08-12). Backoff cannot refill an empty quota — after 3
    # consecutive quota errors give up for this run instead of burning ~16min.
    consecutive_quota_errors = 0

    for region in CB_REGIONS:
        if denied:
            break
        client = boto3.client("ec2", region_name=region)
        for itype, gpu_model in INSTANCE_GPU_MAP.items():
            if consecutive_quota_errors >= 3:
                break
            resp = None
            for attempt, backoff in enumerate([0] + BACKOFF_S):
                if backoff:
                    time.sleep(backoff)
                try:
                    resp = client.describe_capacity_block_offerings(
                        InstanceType=itype,
                        InstanceCount=1,
                        CapacityDurationHours=24,
                        StartDateRange=now_dt + timedelta(hours=1),
                        EndDateRange=now_dt + timedelta(days=WINDOW_DAYS),
                        MaxResults=20,
                    )
                    consecutive_quota_errors = 0
                    break
                except ClientError as e:
                    code = e.response.get("Error", {}).get("Code", "")
                    if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
                        logger.warning("AWS capacity blocks: IAM lacks "
                                       "ec2:DescribeCapacityBlockOfferings — skipping provider "
                                       "(add the permission to activate this signal)")
                        denied = True
                        break
                    if code == "CapacityBlockDescribeLimitExceeded":
                        if backoff != BACKOFF_S[-1]:
                            continue   # retry with the next backoff
                        consecutive_quota_errors += 1
                    # Region/type not supported or throttled out → summarize
                    # codes + first full message at the end (a silent all-error
                    # run looked identical to IAM-pending on 2026-08-12).
                    error_codes.setdefault(code or "unknown", []).append(
                        f"{region}/{itype}: {e.response.get('Error', {}).get('Message', '')[:120]}")
                    break
                except Exception as e:
                    logger.error(f"AWS CB {region}/{itype}: {e}")
                    break
            time.sleep(CALL_SPACING_S)
            if denied or resp is None:
                if denied:
                    break
                continue

            offerings = resp.get("CapacityBlockOfferings", [])
            if not offerings:
                records.append(AvailabilityRecord(
                    provider="aws", gpu_model=gpu_model, region=region,
                    consumption_type="reserved_short", state="sold_out",
                    metric_type="lead_time_days", metric_value=None,
                    detail=f"no 1-node 24h block available in next {WINDOW_DAYS}d",
                    instance_type=itype,
                    fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
                ))
                continue

            earliest = min(o["StartDate"] for o in offerings if o.get("StartDate"))
            lead_days = max(0.0, (earliest - now_dt).total_seconds() / 86400)
            cheapest = min((float(o.get("UpfrontFee", "0") or 0) for o in offerings), default=0)
            state = "available" if lead_days <= LIMITED_LEAD_DAYS else "limited"
            records.append(AvailabilityRecord(
                provider="aws", gpu_model=gpu_model, region=region,
                consumption_type="reserved_short", state=state,
                metric_type="lead_time_days", metric_value=round(lead_days, 1),
                detail=f"earliest 24h block in {lead_days:.1f}d, "
                       f"{plural(len(offerings), 'offering')}, upfront from ${cheapest:,.0f}",
                instance_type=itype,
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))

    if denied:
        return []
    for code, calls in error_codes.items():
        logger.warning(f"AWS CB: {len(calls)} call(s) errored with {code} — {calls[0]}")
    logger.info(f"AWS capacity blocks: {len(records)} records")
    return records
