"""
Lambda Labs (lambda.ai) pricing fetcher.
Three-tier fetch strategy:
  1. REST API  — requires LAMBDA_API_KEY env var (most reliable, always use in CCR)
  2. Web scrape — lambda.ai/instances (blocked by Cloudflare in cloud envs)
  3. SkyPilot catalog fallback — raw GitHub CSV, no auth, no Cloudflare

CCR environment: set LAMBDA_API_KEY in the routine's environment variables for
reliable API-based pricing. Without it, the scrape fallback is used, and if
that also fails (Cloudflare), the SkyPilot catalog is used as last resort.
Get a free key at: https://cloud.lambdalabs.com/api-keys

SkyPilot catalog source: https://github.com/skypilot-org/skypilot-catalog
Data may be 1-4 weeks behind live pricing; treated as data_source="aggregator".
"""
import base64
import csv
import io
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

API_URL = "https://cloud.lambdalabs.com/api/v1/instance-types"
PRICING_URL = "https://lambda.ai/instances"
SOURCE_URL = PRICING_URL
SKYPILOT_CSV_URL = "https://raw.githubusercontent.com/skypilot-org/skypilot-catalog/master/catalogs/v8/lambda/vms.csv"

NEBIUS_GPUS = {"H100", "H200", "B200", "B300", "GB200", "GB300", "L40S"}

INSTANCE_GPU_MAP = {
    "gpu_8x_h100_sxm5":  {"gpu_model": "H100", "gpu_count": 8},
    "gpu_4x_h100_sxm5":  {"gpu_model": "H100", "gpu_count": 4},
    "gpu_2x_h100_sxm5":  {"gpu_model": "H100", "gpu_count": 2},
    "gpu_1x_h100_sxm5":  {"gpu_model": "H100", "gpu_count": 1},
    "gpu_1x_h100_pcie":  {"gpu_model": "H100", "gpu_count": 1},
    "gpu_8x_b200":       {"gpu_model": "B200",  "gpu_count": 8},
    "gpu_4x_b200":       {"gpu_model": "B200",  "gpu_count": 4},
    "gpu_2x_b200":       {"gpu_model": "B200",  "gpu_count": 2},
    "gpu_1x_b200":       {"gpu_model": "B200",  "gpu_count": 1},
    # "gpu_1x_gh200": excluded — GH200 is a Grace+Hopper superchip (96GB HBM3),
    # different form factor from HGX H100. Excluded to keep H100 bucket clean.
    "gpu_8x_l40s":       {"gpu_model": "L40S",  "gpu_count": 8},
    "gpu_1x_l40s":       {"gpu_model": "L40S",  "gpu_count": 1},
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    api_key = os.environ.get("LAMBDA_API_KEY")

    # Tier 1: REST API (most reliable)
    if api_key:
        try:
            creds = base64.b64encode(f"{api_key}:".encode()).decode()
            req = urllib.request.Request(API_URL, headers={"Authorization": f"Basic {creds}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            records = _parse_api_data(data, now)
            if records:
                logger.info(f"Lambda Labs API: {len(records)} records")
                # Supplement with SkyPilot catalog for any GPU models the API didn't return
                # (e.g. L40S which is not always listed in the API response)
                records = _supplement_with_skypilot(records, now)
                return records
        except Exception as e:
            logger.info(f"Lambda Labs API failed ({e})")

    # Tier 2: Web scrape (blocked by Cloudflare in cloud environments)
    records = _scrape_pricing_page(now)
    if records:
        # Supplement with SkyPilot catalog for GPU models missing from the scrape
        # (e.g. L40S is not listed on lambda.ai/instances as of mid-2025)
        records = _supplement_with_skypilot(records, now)
        return records

    # Tier 3: SkyPilot catalog fallback — public GitHub CSV, no Cloudflare
    logger.warning("Lambda Labs: scrape returned 0 records — trying SkyPilot catalog fallback")
    return _fetch_skypilot_catalog(now)


def _supplement_with_skypilot(records: List[PriceRecord], now: str) -> List[PriceRecord]:
    """
    Supplement existing records with SkyPilot catalog data for GPU models not yet covered.
    This handles cases like L40S, which is not listed on lambda.ai/instances but appears
    in Lambda's API (when available) and in the SkyPilot community catalog.
    Only adds records for GPU models completely absent from `records`.
    """
    covered_models = {r.gpu_model for r in records}
    missing_models = NEBIUS_GPUS - covered_models
    if not missing_models:
        return records

    catalog = _fetch_skypilot_catalog(now)
    added = [r for r in catalog if r.gpu_model in missing_models]
    if added:
        logger.info(
            f"Lambda Labs: SkyPilot supplement added {len(added)} records "
            f"for {sorted(missing_models)} not found in primary source"
        )
    return records + added


def _fetch_skypilot_catalog(now: str) -> List[PriceRecord]:
    """
    Fetch Lambda pricing from the SkyPilot community catalog on GitHub.
    CSV columns: InstanceType, AcceleratorName, AcceleratorCount, vCPUs,
                 MemoryGiB, Price, Region, GpuInfo, SpotPrice
    Price and SpotPrice are per-instance/hr; divide by AcceleratorCount for per-GPU.
    """
    try:
        req = urllib.request.Request(
            SKYPILOT_CSV_URL,
            headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.error(f"Lambda Labs SkyPilot fallback failed: {e}")
        return []

    records = []
    seen: set = set()
    reader = csv.DictReader(io.StringIO(content))

    for row in reader:
        accel = row.get("AcceleratorName", "").strip()
        gpu_model = _match_gpu(accel)
        if gpu_model is None:
            continue

        try:
            gpu_count = max(1, round(float(row.get("AcceleratorCount", "1") or 1)))
        except ValueError:
            gpu_count = 1

        instance_type = row.get("InstanceType", "").strip()
        region = row.get("Region", "us-east").strip() or "us-east"

        for ct, price_field in [("on_demand", "Price"), ("spot", "SpotPrice")]:
            raw = row.get(price_field, "").strip()
            if not raw:
                continue
            try:
                price_total = float(raw)
            except ValueError:
                continue
            if price_total <= 0:
                continue

            price_per_gpu = price_total / gpu_count

            # De-duplicate: keep cheapest per (instance_type, region, ct)
            key = (instance_type, region, ct)
            if key in seen:
                continue
            seen.add(key)

            records.append(PriceRecord(
                provider="lambda",
                gpu_model=gpu_model,
                gpu_count=gpu_count,
                instance_type=instance_type,
                region=region,
                consumption_type=ct,
                price_per_hour_usd=price_total,
                price_per_gpu_hour_usd=price_per_gpu,
                fetched_at=now,
                source_url=SOURCE_URL,
                data_source="aggregator",
            ))

    if records:
        logger.info(f"Lambda Labs SkyPilot fallback: {len(records)} records")
    else:
        logger.error("Lambda Labs SkyPilot fallback: no records parsed")
    return records


def _scrape_pricing_page(now: str) -> List[PriceRecord]:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        req = urllib.request.Request(PRICING_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.error(f"Lambda Labs scrape failed: {e}")
        return []

    records = _parse_html(html, now)
    logger.info(f"Lambda Labs scrape: {len(records)} records")
    return records


def _parse_html(html: str, now: str) -> List[PriceRecord]:
    """
    Parse lambda.ai/instances pricing tables.
    The page has multiple tables for different node sizes (8/4/2/1 GPU).
    Column header PRICE/GPU/HR confirms prices are per-GPU.
    """
    records = []
    seen = set()

    # Rows: <tr data-plan="NVIDIA H100 SXM" ...>  with td cells: VRAM | vCPU | RAM | Storage | PRICE/GPU/HR
    row_pattern = re.compile(
        r'<tr[^>]*data-plan="(NVIDIA\s+[^"]+)"[^>]*>(.*?)</tr>',
        re.DOTALL,
    )

    for m in row_pattern.finditer(html):
        plan = m.group(1).strip()
        gpu_model = _match_gpu(plan)
        if not gpu_model:
            continue

        cells = re.findall(r'<td[^>]*>(.*?)</td>', m.group(2), re.DOTALL)
        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if len(clean) < 5:
            continue

        # Columns: VRAM/GPU | vCPUs | RAM | STORAGE | PRICE/GPU/HR
        vcpus_str = clean[1]
        price_str = clean[4].lstrip("$").replace(",", "")
        try:
            vcpus = int(vcpus_str)
            price_per_gpu = float(price_str)
        except ValueError:
            continue
        if price_per_gpu <= 0:
            continue

        # ~26 vCPU per GPU on standard Lambda nodes
        gpu_count = max(1, round(vcpus / 26))

        # Build a variant slug from the plan name to distinguish SXM vs PCIe etc.
        variant = plan.upper().replace("NVIDIA ", "").replace(" ", "-")
        key = (gpu_model, gpu_count, variant)
        if key in seen:
            continue
        seen.add(key)

        # Derive a clean instance_type: lambda-h100-sxm-1x, lambda-h100-pcie-1x, etc.
        variant_slug = variant.lower()
        instance_type = f"lambda-{variant_slug}-{gpu_count}x"

        records.append(PriceRecord(
            provider="lambda",
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            instance_type=instance_type,
            region="us-east",
            consumption_type="on_demand",
            price_per_hour_usd=price_per_gpu * gpu_count,
            price_per_gpu_hour_usd=price_per_gpu,
            fetched_at=now,
            source_url=SOURCE_URL,
            data_source="web_scrape",
        ))

    return records


def _parse_api_data(data: dict, now: str) -> List[PriceRecord]:
    records = []
    instance_types = data.get("data", {})
    if isinstance(instance_types, list):
        instance_types = {str(i): x for i, x in enumerate(instance_types)}

    for name, info in instance_types.items():
        mapping = INSTANCE_GPU_MAP.get(name)
        if mapping is None:
            name_lower = name.lower()
            gpu_model = next((g for g in NEBIUS_GPUS if g.lower() in name_lower), None)
            if not gpu_model:
                continue
            count = next((int(p) for p in name_lower.split("_") if p.isdigit()), 1)
            mapping = {"gpu_model": gpu_model, "gpu_count": count}

        specs = info.get("instance_type", info)
        price_cents = info.get("price_cents_per_hour") or (specs.get("price_cents_per_hour") if isinstance(specs, dict) else 0) or 0
        if not price_cents:
            continue
        price = price_cents / 100.0

        regions_available = info.get("regions_with_capacity_available", [{"name": "us-east"}])
        seen_regions = set()
        for region_info in regions_available:
            region_name = region_info.get("name", "us-east")
            if region_name in seen_regions:
                continue
            seen_regions.add(region_name)
            records.append(PriceRecord(
                provider="lambda",
                gpu_model=mapping["gpu_model"],
                gpu_count=mapping["gpu_count"],
                instance_type=name,
                region=region_name,
                consumption_type="on_demand",
                price_per_hour_usd=price,
                price_per_gpu_hour_usd=price / mapping["gpu_count"],
                fetched_at=now,
                source_url=SOURCE_URL,
                data_source="official_api",
            ))
    return records


def _match_gpu(name: str) -> Optional[str]:
    name_upper = name.upper()
    # GH200 = Grace Hopper Superchip — excluded (different form factor from HGX H100)
    if "GH200" in name_upper:
        return None
    for g in NEBIUS_GPUS:
        if g in name_upper:
            return g
    return None
