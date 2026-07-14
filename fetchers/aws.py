"""
AWS EC2 pricing fetcher.
Uses the public AWS Bulk Pricing JSON (no credentials required for on-demand/reserved).
Spot prices require boto3 + credentials; gracefully skips if unavailable.
"""
import json
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord
from config import AWS_REGIONS, GPU_MAP

logger = logging.getLogger(__name__)

PRICING_BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/{region}/index.json"
SOURCE_URL = "https://aws.amazon.com/ec2/pricing/on-demand/"

# Reserved term codes in AWS bulk pricing
RESERVED_TERMS = {
    "1yr_no_upfront":    ("1yr",  "No Upfront"),
    "1yr_partial":       ("1yr",  "Partial Upfront"),
    "1yr_all_upfront":   ("1yr",  "All Upfront"),
    "3yr_no_upfront":    ("3yr",  "No Upfront"),
    "3yr_partial":       ("3yr",  "Partial Upfront"),
    "3yr_all_upfront":   ("3yr",  "All Upfront"),
}

# All instance types we care about (from config)
_ALL_INSTANCE_TYPES = {
    spec["instance_type"]: (gpu_model, spec)
    for gpu_model, specs in GPU_MAP.get("aws", {}).items()
    for spec in specs
}

# AWS pricing availability notes (verified June 2026):
#   p5.48xlarge  (H100) — on_demand + reserved_1yr/3yr + capacity_block ✓
#   p5en.48xlarge (H200) — on_demand + capacity_block; reserved NOT YET PUBLISHED by AWS
#   p6-b200.48xlarge (B200) — on_demand + capacity_block; reserved NOT YET PUBLISHED
#   p6-b300.48xlarge (B300) — on_demand + capacity_block; reserved NOT YET PUBLISHED
#   p6e (GB200 UltraServer) — publicly listed + capacity_block; no on_demand pricing
#   p6e-gb300 (GB300) — publicly listed; no public numeric capacity block price found
#   Spot for H200/B200/B300 — not in the public S3 spot feed; requires
#     EC2 describe-spot-price-history API with credentials (boto3 + IAM role).
#
# Capacity Blocks are public, capacity-guaranteed, time-bounded reservations (≤6 months).
# Source: https://aws.amazon.com/ec2/capacityblocks/pricing/
# They are NOT equivalent to 1yr/3yr Reserved Instances and should not be compared
# directly to Nebius committed pricing or AWS traditional reserved pricing.
# We store the cheapest available region as the canonical price.
_CAPACITY_BLOCK_PRICES = {
    # GPU      instance_type          gpu_count  $/GPU-hr  regions (cheapest)
    "H100":  ("p5.48xlarge",           8,  3.933),  # us-east-1 / us-west-2
    "H200":  ("p5e.48xlarge",          8,  4.975),  # us-west-2 / us-east-2 (P5e)
    "B200":  ("p6-b200.48xlarge",      8, 10.296),  # us-east-2 / us-east-1 / us-west-2
    "B300":  ("p6-b300.48xlarge",      8, 11.70),   # us-east-1 / us-west-2
    "GB200": ("p6e.36xlarge",         36, 10.582),  # us-east-1 Dallas Local Zone (UltraServer)
}
CAPACITY_BLOCK_SOURCE_URL = "https://aws.amazon.com/ec2/capacityblocks/pricing/"


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    regions = regions or AWS_REGIONS
    records: List[PriceRecord] = []
    now = datetime.now(timezone.utc).isoformat()

    for region in regions:
        try:
            region_records = _fetch_region(region, now)
            records.extend(region_records)
            logger.info(f"AWS {region}: {len(region_records)} records")
        except Exception as e:
            logger.warning(f"AWS {region} failed: {e}")

    spot_records = _fetch_spot(regions, now)
    records.extend(spot_records)

    cb_records = _fetch_capacity_blocks(now)
    records.extend(cb_records)

    return records


def _fetch_capacity_blocks(fetched_at: str) -> List[PriceRecord]:
    """
    Return AWS Capacity Block effective hourly prices as PriceRecords.

    Capacity Blocks are public, capacity-guaranteed reservations (≤6 months).
    Prices are manually maintained from the public pricing page:
      https://aws.amazon.com/ec2/capacityblocks/pricing/

    IMPORTANT: These are NOT comparable to traditional reserved instances or Nebius
    committed pricing. Use a separate 'capacity_block' consumption type so they
    are never accidentally compared in the same column.
    """
    records = []
    for gpu_model, (instance_type, gpu_count, price_per_gpu) in _CAPACITY_BLOCK_PRICES.items():
        records.append(PriceRecord(
            provider="aws",
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            instance_type=instance_type,
            region="us-east-1",   # cheapest available region
            consumption_type="capacity_block",
            price_per_hour_usd=price_per_gpu * gpu_count,
            price_per_gpu_hour_usd=price_per_gpu,
            fetched_at=fetched_at,
            source_url=CAPACITY_BLOCK_SOURCE_URL,
            data_source="official_api",
        ))

    logger.info(f"AWS capacity blocks: {len(records)} records (static, from public pricing page)")
    return records


def _fetch_region(region: str, fetched_at: str) -> List[PriceRecord]:
    url = PRICING_BASE.format(region=region)
    records = []

    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        data = json.loads(raw)

    products = data.get("products", {})
    on_demand_terms = data.get("terms", {}).get("OnDemand", {})
    reserved_terms = data.get("terms", {}).get("Reserved", {})

    for sku, product in products.items():
        attrs = product.get("attributes", {})
        instance_type = attrs.get("instanceType", "")
        if instance_type not in _ALL_INSTANCE_TYPES:
            continue
        if attrs.get("tenancy") != "Shared":
            continue
        if attrs.get("operatingSystem") != "Linux":
            continue
        if attrs.get("capacitystatus") != "Used":
            continue

        gpu_model, spec = _ALL_INSTANCE_TYPES[instance_type]

        # On-demand
        od = on_demand_terms.get(sku, {})
        od_price = _extract_hourly_price(od)
        if od_price is not None:
            records.append(PriceRecord(
                provider="aws",
                gpu_model=gpu_model,
                gpu_count=spec["gpu_count"],
                instance_type=instance_type,
                region=region,
                consumption_type="on_demand",
                price_per_hour_usd=od_price,
                price_per_gpu_hour_usd=od_price / spec["gpu_count"],
                vcpu=spec.get("vcpu"),
                ram_gb=spec.get("ram_gb"),
                fetched_at=fetched_at,
                source_url=SOURCE_URL,
                data_source="official_api",
            ))

        # Reserved — two passes to handle Standard vs Convertible offering classes.
        #
        # AWS sells two offering classes:
        #   "standard"    — locked to the specific instance type; biggest discount
        #   "convertible" — can exchange for different types mid-term; smaller discount
        #
        # Canonical comparison uses "standard" when available (best discount for fixed
        # commitment, comparable to Azure reservations and GCP CUDs).
        # Some newer instance families (e.g. p5.48xlarge) only have 1yr Convertible —
        # in that case we fall back to Convertible so the 1yr column isn't blank.
        #
        # All prices are EFFECTIVE hourly rates: recurring hourly + amortized upfront
        # (fix 2026-07-14 — previously only the recurring component of partial-upfront
        # offers was read, halving published prices, and true all-upfront offers were
        # dropped entirely because their $0/hr recurring rate failed a >0 check).
        #
        # Strategy: collect all (lease, purchase_option, offering_class, effective)
        # tuples, then emit the CHEAPEST effective in the canonical offering class as
        # reserved_{years} — that is the all-upfront rate when one exists, which makes
        # the "all-upfront" labels downstream true.

        res = reserved_terms.get(sku, {})
        # Map lease years → [(purchase_label, offering_class, effective_price)]
        res_by_years: dict = {}
        for term_key, term_data in res.items():
            term_attrs = term_data.get("termAttributes", {})
            lease = term_attrs.get("LeaseContractLength", "")
            purchase = term_attrs.get("PurchaseOption", "")
            offering = term_attrs.get("OfferingClass", "standard").lower()
            years = "1yr" if lease == "1yr" else "3yr" if lease == "3yr" else None
            if not years:
                continue
            hourly, upfront = _extract_reserved_components(term_data)
            if hourly is None and upfront <= 0:
                continue
            effective = (hourly or 0.0) + upfront / _HOURS_PER_TERM[years]
            if effective <= 0:
                continue
            upfront_label = purchase.replace(" ", "_").lower()
            res_by_years.setdefault(years, []).append(
                (upfront_label, offering, effective))

        for years, opts in res_by_years.items():
            classes = {c for _, c, _ in opts}
            canonical_class = "standard" if "standard" in classes else "convertible"
            canonical = min((o for o in opts if o[1] == canonical_class),
                            key=lambda o: o[2])

            for opt in opts:
                upfront_label, offering_class, price = opt
                if opt == canonical:
                    ct = f"reserved_{years}"          # canonical — used in comparisons
                elif offering_class == "convertible":
                    ct = f"reserved_{years}_{upfront_label}_convertible"
                else:
                    ct = f"reserved_{years}_{upfront_label}"  # kept but not in comparisons

                records.append(PriceRecord(
                    provider="aws",
                    gpu_model=gpu_model,
                    gpu_count=spec["gpu_count"],
                    instance_type=instance_type,
                    region=region,
                    consumption_type=ct,
                    price_per_hour_usd=price,
                    price_per_gpu_hour_usd=price / spec["gpu_count"],
                    vcpu=spec.get("vcpu"),
                    ram_gb=spec.get("ram_gb"),
                    fetched_at=fetched_at,
                    source_url="https://aws.amazon.com/ec2/pricing/reserved-instances/pricing/",
                    data_source="official_api",
                ))

    return records


def _extract_hourly_price(terms: dict) -> Optional[float]:
    for term_data in terms.values():
        for pd in term_data.get("priceDimensions", {}).values():
            if pd.get("unit") == "Hrs":
                try:
                    price = float(pd["pricePerUnit"]["USD"])
                    if price > 0:
                        return price
                except (KeyError, ValueError):
                    pass
    return None


# Hours used to amortize Reserved Instance upfront fees into an effective hourly
# rate. 365-day convention — reproduces AWS's own calculator and vantage/
# ec2instances.info effective prices exactly (verified 2026-07-14, p5.48xlarge).
_HOURS_PER_TERM = {"1yr": 8760.0, "3yr": 26280.0}


def _extract_reserved_components(term_data: dict):
    """
    (recurring_hourly, upfront_fee) for one Reserved term. Unlike on-demand,
    hourly 0.0 is VALID here (All Upfront RIs bill $0/hr — rejecting zero was
    the bug that silently dropped every all-upfront offer), and the upfront
    priceDimension (unit='Quantity') must be read or partial/all-upfront
    effective prices understate by ~50%/100% of the true cost.
    """
    hourly = None
    upfront = 0.0
    for pd in term_data.get("priceDimensions", {}).values():
        try:
            usd = float(pd["pricePerUnit"]["USD"])
        except (KeyError, ValueError, TypeError):
            continue
        unit = pd.get("unit")
        if unit == "Hrs":
            hourly = usd
        elif unit == "Quantity":
            upfront = usd
    return hourly, upfront


SPOT_INDEX_URL = "https://spot-price.s3.amazonaws.com/spot.js"
SPOT_REGIONAL_BASE = "https://spot-price.s3.amazonaws.com"
SPOT_SOURCE_URL = "https://aws.amazon.com/ec2/spot/pricing/"

# Instance types NOT in the public S3 spot feed — need boto3 credentials
_BOTO3_SPOT_INSTANCE_TYPES = {
    it for it, (_, _) in _ALL_INSTANCE_TYPES.items()
    if any(it.startswith(p) for p in ("p5en.", "p6-b200.", "p6-b300."))
}


def _fetch_spot(regions: List[str], fetched_at: str) -> List[PriceRecord]:
    """
    Fetch AWS spot prices using the public S3 JSONP feed (no credentials required).

    The feed structure is:
      callback({ "config": { "regions": [ { "region": "us-east-1",
        "instanceTypes": [ { "type": "...", "sizes": [
          { "size": "p5.48xlarge", "valueColumns": [{"name":"linux","prices":{"USD":"57.76"}}] }
        ]}]}]}});

    Note: newer instance families (p5en/H200, p6-b200/B200, p6-b300/B300) are not yet
    in this feed — they use a separate pricing mechanism. Only p5/H100, g6e/L40S etc.
    are available here.
    """
    try:
        with urllib.request.urlopen(SPOT_INDEX_URL, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'\w+\((.*)\)\s*;?\s*$', raw, re.DOTALL)
        if not m:
            logger.warning("AWS spot: could not parse JSONP wrapper")
            return []
        feed_data = json.loads(m.group(1))
    except Exception as e:
        logger.warning(f"AWS spot fetch failed: {e}")
        return []

    region_list = feed_data.get("config", {}).get("regions", [])
    records = []
    seen: set = set()  # (region, instance_type) dedup

    for region_entry in region_list:
        region = region_entry.get("region", "")
        if region not in regions:
            continue
        for it_group in region_entry.get("instanceTypes", []):
            for size_entry in it_group.get("sizes", []):
                instance_type = size_entry.get("size", "")
                if instance_type not in _ALL_INSTANCE_TYPES:
                    continue
                key = (region, instance_type)
                if key in seen:
                    continue
                # Extract linux spot price
                price = None
                for col in size_entry.get("valueColumns", []):
                    if col.get("name", "").lower() != "linux":
                        continue
                    raw_price = col.get("prices", {}).get("USD", "")
                    try:
                        p = float(raw_price)
                        if p > 0:
                            price = p
                    except (ValueError, TypeError):
                        pass
                if price is None:
                    continue
                seen.add(key)
                gpu_model, spec = _ALL_INSTANCE_TYPES[instance_type]
                records.append(PriceRecord(
                    provider="aws",
                    gpu_model=gpu_model,
                    gpu_count=spec["gpu_count"],
                    instance_type=instance_type,
                    region=region,
                    consumption_type="spot",
                    price_per_hour_usd=price,
                    price_per_gpu_hour_usd=price / spec["gpu_count"],
                    vcpu=spec.get("vcpu"),
                    ram_gb=spec.get("ram_gb"),
                    fetched_at=fetched_at,
                    source_url=SPOT_SOURCE_URL,
                    data_source="official_api",
                ))

    logger.info(f"AWS spot: {len(records)} records via public S3 feed")

    # Supplement with boto3 for newer instance families not in the S3 feed
    boto3_records = _fetch_spot_boto3(regions, fetched_at)
    records.extend(boto3_records)

    return records


def _fetch_spot_boto3(regions: List[str], fetched_at: str) -> List[PriceRecord]:
    """
    Fetch spot prices for newer instance families (p5en/H200, p6-b200/B200, p6-b300/B300)
    using the EC2 describe-spot-price-history API.

    Requires AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars (or any boto3-supported
    credential source). Gracefully returns [] if credentials are missing or boto3 unavailable.
    """
    try:
        import boto3
        import botocore.exceptions
    except ImportError:
        logger.debug("boto3 not installed — skipping spot prices for H200/B200/B300")
        return []

    import os
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        logger.debug("AWS_ACCESS_KEY_ID not set — skipping boto3 spot fetch")
        return []

    instance_types = list(_BOTO3_SPOT_INSTANCE_TYPES)
    if not instance_types:
        return []

    records = []
    seen: set = set()

    def _query(client, types: list) -> dict:
        """
        CURRENT cheapest spot price per instance type across the region's AZs.
        describe_spot_price_history returns a time series per (AZ, instance);
        we keep only the NEWEST observation per (AZ, instance) and then take the
        min across AZs — the current regional floor. (Fix 2026-07-14: previously
        the min was taken over the whole history window across AZs AND time, so
        a stale multi-day-old low could pose as today's price — that was the
        frozen $0.79 H200 reading in June.)
        """
        paginator = client.get_paginator("describe_spot_price_history")
        pages = paginator.paginate(
            InstanceTypes=types,
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(timezone.utc),   # newest observation per AZ only
            PaginationConfig={"MaxItems": 500},
        )
        latest: dict = {}   # (instance_type, az) -> (timestamp, price)
        for page in pages:
            for entry in page.get("SpotPriceHistory", []):
                it = entry.get("InstanceType", "")
                az = entry.get("AvailabilityZone", "")
                ts = entry.get("Timestamp")
                try:
                    price = float(entry.get("SpotPrice", 0))
                except (ValueError, TypeError):
                    continue
                if price <= 0:
                    continue
                key = (it, az)
                if key not in latest or (ts and latest[key][0] and ts > latest[key][0]):
                    latest[key] = (ts, price)
        best: dict = {}
        for (it, _az), (_ts, price) in latest.items():
            if it not in best or price < best[it]:
                best[it] = price
        return best

    for region in regions:
        try:
            client = boto3.client(
                "ec2",
                region_name=region,
                aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            )
            try:
                best = _query(client, instance_types)
            except botocore.exceptions.ClientError as e:
                # One unrecognized instance type name (e.g. a GPU family not yet
                # GA in EC2) fails the WHOLE batch call. Retry per-type so the
                # valid families still return prices.
                if e.response.get("Error", {}).get("Code") != "InvalidParameterValue":
                    raise
                logger.warning(f"AWS boto3 spot {region}: batch rejected ({e}) — retrying per-type")
                best = {}
                for it in instance_types:
                    try:
                        best.update(_query(client, [it]))
                    except botocore.exceptions.ClientError as e2:
                        logger.warning(f"AWS boto3 spot {region} {it}: {e2}")

            for it, price in best.items():
                if it not in _ALL_INSTANCE_TYPES:
                    continue
                key = (region, it)
                if key in seen:
                    continue
                seen.add(key)
                gpu_model, spec = _ALL_INSTANCE_TYPES[it]
                records.append(PriceRecord(
                    provider="aws",
                    gpu_model=gpu_model,
                    gpu_count=spec["gpu_count"],
                    instance_type=it,
                    region=region,
                    consumption_type="spot",
                    price_per_hour_usd=price,
                    price_per_gpu_hour_usd=price / spec["gpu_count"],
                    vcpu=spec.get("vcpu"),
                    ram_gb=spec.get("ram_gb"),
                    fetched_at=fetched_at,
                    source_url=SPOT_SOURCE_URL,
                    data_source="official_api",
                ))
        except Exception as e:
            logger.warning(f"AWS boto3 spot {region} failed: {e}")

    if records:
        gpu_summary = {}
        for r in records:
            gpu_summary.setdefault(r.gpu_model, []).append(r.price_per_gpu_hour_usd)
        summary = ", ".join(
            f"{g} ${min(ps):.2f}-${max(ps):.2f}" for g, ps in sorted(gpu_summary.items())
        )
        logger.info(f"AWS spot (boto3): {len(records)} records — {summary}")
    else:
        logger.debug("AWS spot (boto3): 0 records")

    return records
