"""Read-only Capacity Block offering searches, bounded and explicitly scoped.

Each check asks for one instance for 24 hours in the next 14 days. An empty,
complete response describes only that search, never all AWS GPU capacity.
No purchase, reservation or launch operation is called. Account describe limits
stop the run immediately; incomplete pagination cannot establish an earliest
capacity offering or a sold-out result.
"""
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import List

from capacity.schema import AvailabilityRecord, plural

logger = logging.getLogger(__name__)
SOURCE_URL = "https://aws.amazon.com/ec2/capacityblocks/"
INSTANCE_GPU_MAP = {
    "p5.48xlarge": "H100", "p5en.48xlarge": "H200",
    "p6-b200.48xlarge": "B200", "p6-b300.48xlarge": "B300",
}
# Preserve the configured monitoring scope even when access prevents a check.
CB_REGIONS = ["us-east-1", "us-east-2", "us-west-2", "eu-north-1"]
LIMITED_LEAD_DAYS = 7.0
WINDOW_DAYS = 14
CALL_SPACING_S = 4
MAX_PAGES_PER_CHECK = 3
MAX_REQUESTS = 32
MAX_CONSECUTIVE_FAILURES = 3
LAST_FETCH_HEALTH = {}

_ACCESS_ERRORS = {"UnauthorizedOperation", "AccessDenied", "AccessDeniedException",
                  "AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch", "ExpiredToken"}
_LIMIT_ERRORS = {"CapacityBlockDescribeLimitExceeded", "RequestLimitExceeded",
                 "Throttling", "ThrottlingException"}
_TRANSPORT_ERRORS = {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                     "TimeoutError", "ConnectionError", "OSError"}


def _health():
    checks = [f"{region}/{sku}" for region in CB_REGIONS for sku in INSTANCE_GPU_MAP]
    LAST_FETCH_HEALTH.clear()
    LAST_FETCH_HEALTH.update(status="pending", reason="not started", error_code=None,
                            failure_class=None,
                            planned_checks=len(checks), completed_checks=0, failed_checks=0,
                            unqueried_checks=checks, errors=[], requests_attempted=0,
                            pages_fetched=0, pagination_complete=True)
    return LAST_FETCH_HEALTH


def _date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("offering StartDate missing or invalid")
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def _record(offerings, region, sku, now_dt):
    common = dict(provider="aws", gpu_model=INSTANCE_GPU_MAP[sku], region=region,
                  consumption_type="reserved_short", metric_type="lead_time_days",
                  instance_type=sku, gpu_count=8, product_scope="capacity_block_1_instance_24h",
                  fetched_at=now_dt.isoformat(), source_url=SOURCE_URL,
                  data_source="official_api", parser_version="aws-cb-bounded-2")
    if not offerings:
        return AvailabilityRecord(**common, state="sold_out", metric_value=None,
                                  detail=f"no 1-instance 24h offering ending within next {WINDOW_DAYS}d; complete search")
    starts = [_date(row.get("StartDate")) for row in offerings]
    lead = max(0.0, (min(starts) - now_dt).total_seconds() / 86400)
    fees = []
    for row in offerings:
        try:
            fee = float(row["UpfrontFee"])
            if math.isfinite(fee) and fee >= 0 and row.get("CurrencyCode", "USD") == "USD":
                fees.append(fee)
        except (KeyError, TypeError, ValueError):
            pass
    detail = f"earliest 1-instance 24h offering in {lead:.1f}d; {plural(len(offerings), 'offering')}; complete search"
    if fees:
        detail += f"; published upfront fee from ${min(fees):,.0f}"
    return AvailabilityRecord(**common, state="available" if lead <= LIMITED_LEAD_DAYS else "limited",
                              metric_value=round(lead, 1), detail=detail)


def fetch() -> List[AvailabilityRecord]:
    health = _health()
    now_dt = datetime.now(timezone.utc)
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        health.update(reason="AWS environment credentials are not configured", error_code="missing_credentials", failure_class="credentials")
        return []
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        health.update(status="failed", reason="boto3 is not installed", error_code="missing_dependency", failure_class="dependency")
        return []

    # One wire attempt per call: SDK retries must not bypass the circuit breaker.
    config = Config(connect_timeout=5, read_timeout=15,
                    retries={"mode": "standard", "total_max_attempts": 1})
    records = []
    stop = False
    consecutive_failures = 0
    last_request = None
    for region in CB_REGIONS:
        if stop:
            break
        try:
            client = boto3.client("ec2", region_name=region, config=config)
        except Exception as exc:
            health.update(error_code=type(exc).__name__, reason="EC2 client initialization failed")
            stop = True
            break
        for sku in INSTANCE_GPU_MAP:
            if stop:
                break
            check = f"{region}/{sku}"
            if health["requests_attempted"] >= MAX_REQUESTS:
                health.update(error_code="request_budget", reason="bounded describe-request budget reached")
                stop = True
                break
            health["unqueried_checks"].remove(check)
            params = dict(InstanceType=sku, InstanceCount=1, CapacityDurationHours=24,
                          StartDateRange=now_dt + timedelta(hours=1),
                          EndDateRange=now_dt + timedelta(days=WINDOW_DAYS), MaxResults=1000)
            offerings, seen_tokens = [], set()
            complete = False
            code = None
            for page in range(MAX_PAGES_PER_CHECK):
                if health["requests_attempted"] >= MAX_REQUESTS:
                    code = "request_budget"
                    health["reason"] = "bounded describe-request budget reached"
                    health["pagination_complete"] = False
                    stop = True
                    break
                if last_request is not None:
                    time.sleep(max(0.0, CALL_SPACING_S - (time.monotonic() - last_request)))
                health["requests_attempted"] += 1
                last_request = time.monotonic()
                try:
                    response = client.describe_capacity_block_offerings(**params)
                    health["pages_fetched"] += 1
                    page_rows = response.get("CapacityBlockOfferings")
                    if not isinstance(page_rows, list) or any(not isinstance(row, dict) for row in page_rows):
                        raise ValueError("invalid CapacityBlockOfferings schema")
                    offerings.extend(page_rows)
                    token = response.get("NextToken")
                    if not token:
                        complete = True
                        break
                    if token in seen_tokens:
                        code = "pagination_loop"
                        break
                    seen_tokens.add(token)
                    params["NextToken"] = token
                except Exception as exc:
                    code = getattr(exc, "response", {}).get("Error", {}).get("Code") or type(exc).__name__
                    if code in _ACCESS_ERRORS:
                        health["reason"] = "AWS authentication or DescribeCapacityBlockOfferings access denied"
                        health["failure_class"] = "access"
                        stop = True
                    elif code in _LIMIT_ERRORS:
                        account_limit = code == "CapacityBlockDescribeLimitExceeded"
                        health["failure_class"] = "account_limit" if account_limit else "rate_limit"
                        health["reason"] = ("AWS account describe limit reached; remaining checks deferred" if account_limit
                                            else "AWS describe API throttled; remaining checks deferred")
                        stop = True
                    break
            if not complete:
                code = code or "pagination_limit"
                health["pagination_complete"] = False
            if complete:
                try:
                    record = _record(offerings, region, sku, now_dt)
                except (ValueError, TypeError, AttributeError):
                    code = "invalid_offering_schema"
                    complete = False
            if complete:
                records.append(record)
                health["completed_checks"] += 1
                consecutive_failures = 0
            else:
                health["failed_checks"] += 1
                health["errors"].append({"check": check, "code": code})
                health["error_code"] = code
                consecutive_failures = consecutive_failures + 1 if code in _TRANSPORT_ERRORS else 0
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    health["reason"] = "consecutive failed checks; remaining scope deferred"
                    stop = True
    if health["completed_checks"] == health["planned_checks"]:
        health.update(status="live", reason="all configured one-instance 24h searches completed")
    else:
        health["status"] = "partial" if records else "failed"
        if health["reason"] == "not started":
            health["reason"] = "one or more configured searches could not be established"
    logger.info("AWS Capacity Blocks: %s records; %s; %s/%s checks complete; %s unqueried",
                len(records), health["status"], health["completed_checks"],
                health["planned_checks"], len(health["unqueried_checks"]))
    return records
