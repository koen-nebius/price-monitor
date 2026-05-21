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
        # Strategy: collect all (lease, purchase_option, offering_class, price) tuples,
        # then emit canonical CTs preferring standard over convertible per (lease, purchase).

        res = reserved_terms.get(sku, {})
        # Map (lease, purchase_option) → {offering_class: price}
        res_options: dict = {}
        for term_key, term_data in res.items():
            term_attrs = term_data.get("termAttributes", {})
            lease = term_attrs.get("LeaseContractLength", "")
            purchase = term_attrs.get("PurchaseOption", "")
            offering = term_attrs.get("OfferingClass", "standard").lower()
            years = "1yr" if lease == "1yr" else "3yr" if lease == "3yr" else None
            if not years:
                continue
            price = _extract_hourly_price({term_key: term_data})
            if price is None:
                continue
            key = (years, purchase)
            res_options.setdefault(key, {})[offering] = price

        for (years, purchase), by_class in res_options.items():
            upfront_label = purchase.replace(" ", "_").lower()
            is_partial = "partial" in upfront_label

            # Determine canonical offering: standard preferred, convertible as fallback
            if "standard" in by_class:
                canonical_class = "standard"
            else:
                canonical_class = "convertible"

            for offering_class, price in by_class.items():
                is_canonical = (is_partial and offering_class == canonical_class)
                if is_canonical:
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


def _fetch_spot(regions: List[str], fetched_at: str) -> List[PriceRecord]:
    try:
        import boto3
    except ImportError:
        logger.info("boto3 not installed — skipping spot prices")
        return []

    records = []
    instance_types = list(_ALL_INSTANCE_TYPES.keys())

    for region in regions:
        try:
            ec2 = boto3.client("ec2", region_name=region)
            resp = ec2.describe_spot_price_history(
                InstanceTypes=instance_types,
                ProductDescriptions=["Linux/UNIX"],
                MaxResults=len(instance_types) * 3,
            )
            seen = set()
            for entry in resp.get("SpotPriceHistory", []):
                it = entry["InstanceType"]
                key = (it, region)
                if key in seen:
                    continue
                seen.add(key)
                if it not in _ALL_INSTANCE_TYPES:
                    continue
                gpu_model, spec = _ALL_INSTANCE_TYPES[it]
                price = float(entry["SpotPrice"])
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
                    source_url="https://aws.amazon.com/ec2/spot/pricing/",
                ))
        except Exception as e:
            logger.warning(f"AWS spot {region} failed: {e}")

    return records
