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
    # Lambda's live API uses _sxm6-suffixed B200 ids (these, not the bare ones
    # above, are what the API actually returns as of 2026-07-14):
    "gpu_8x_b200_sxm6":  {"gpu_model": "B200",  "gpu_count": 8},
    "gpu_4x_b200_sxm6":  {"gpu_model": "B200",  "gpu_count": 4},
    "gpu_2x_b200_sxm6":  {"gpu_model": "B200",  "gpu_count": 2},
    "gpu_1x_b200_sxm6":  {"gpu_model": "B200",  "gpu_count": 1},
    # "gpu_1x_gh200": excluded — GH200 is a Grace+Hopper superchip (96GB HBM3),
    # different form factor from HGX H100. Excluded to keep H100 bucket clean.
    "gpu_8x_l40s":       {"gpu_model": "L40S",  "gpu_count": 8},
    "gpu_1x_l40s":       {"gpu_model": "L40S",  "gpu_count": 1},
}


ONE_CLICK_URL = "https://lambda.ai/pricing"

def _fetch_one_click_clusters(now: str):
    """
    Lambda 1-Click Clusters — their SHORT-TERM RESERVED product (2 weeks to
    1 year, multi-node InfiniBand clusters, priced per GPU-hr by cluster size).
    Added 2026-08-12 after Danila read the $6.16 16-GPU tier as an H100 raise:
    it's a different consumption type, priced ~50% ABOVE Lambda's own on-demand
    (guaranteed-capacity premium, same economics as AWS Capacity Blocks).
    Emits consumption_type="reserved_short" (cheapest tier per GPU, per the
    short-term-reserved section convention). Server-rendered page, plain fetch.
    """
    import re as _re
    import urllib.request as _rq
    req = _rq.Request(ONE_CLICK_URL, headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"})
    with _rq.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    text = _re.sub(r"<[^>]+>", "|", html)
    text = _re.sub(r"\s*\|+\s*", "|", text)
    pat = r"NVIDIA ((?:HGX )?[A-Z]+\d{2,3}\w*)[|\s]+2 weeks\s*[–-]\s*1 year[|\s]+(\d+)\+?[|\s]+\$\s*([\d.]+)"
    if not _re.search(pat, text):
        # Cloudflare may serve cloud IPs a JS shell — escalate to Tavily's
        # rendered-text extract (same ladder, whitespace-separated).
        from fetchers._tavily import tavily_fetch_text
        tav = tavily_fetch_text(ONE_CLICK_URL)
        if tav:
            text = tav
    best = {}
    for m in _re.finditer(pat, text):
        gpu = _match_gpu(m.group(1))
        if not gpu:
            continue
        n, px = int(m.group(2)), float(m.group(3))
        if not (0.5 <= px <= 30):
            continue
        if gpu not in best or px < best[gpu][1]:
            best[gpu] = (n, px)
    records = []
    for gpu, (n, px) in best.items():
        records.append(PriceRecord(
            provider="lambda",
            gpu_model=gpu,
            gpu_count=n,
            instance_type=f"1-click-cluster-{n}x",
            region="us",
            consumption_type="reserved_short",
            price_per_hour_usd=px * n,
            price_per_gpu_hour_usd=px,
            fetched_at=now,
            source_url=ONE_CLICK_URL,
            data_source="web_scrape",
        ))
    if records:
        logger.info("Lambda 1-Click Clusters: "
                    + ", ".join(f"{r.gpu_model} ${r.price_per_gpu_hour_usd:.2f} ({r.gpu_count}x)"
                                for r in records))
    return records


def _one_click_safe(now: str):
    """1-Click Clusters records, or [] — never let the add-on break the main fetch."""
    try:
        return _fetch_one_click_clusters(now)
    except Exception as e:
        logger.warning(f"Lambda 1-Click Clusters fetch failed: {e}")
        return []


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    api_key = os.environ.get("LAMBDA_API_KEY")

    # Tier 1: REST API (most reliable)
    if api_key:
        try:
            creds = base64.b64encode(f"{api_key}:".encode()).decode()
            # The API sits behind Cloudflare, which 1010-bans requests with urllib's
            # default User-Agent (browser-signature block) BEFORE the key is checked —
            # so the key alone is not enough. Send a real browser UA + Accept.
            req = urllib.request.Request(API_URL, headers={
                "Authorization": f"Basic {creds}",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            records = _parse_api_data(data, now)
            if records:
                logger.info(f"Lambda Labs API: {len(records)} records")
                # Supplement with SkyPilot catalog for any GPU models the API didn't return
                # (e.g. L40S which is not always listed in the API response)
                records = _supplement_with_skypilot(records, now)
                return records + _one_click_safe(now)
        except Exception as e:
            logger.info(f"Lambda Labs API failed ({e})")

    # Tier 2: Web scrape (blocked by Cloudflare in cloud environments)
    records = _scrape_pricing_page(now)
    if records:
        # Supplement with SkyPilot catalog for GPU models missing from the scrape
        # (e.g. L40S is not listed on lambda.ai/instances as of mid-2025)
        records = _supplement_with_skypilot(records, now)
        return records + _one_click_safe(now)

    # Tier 3: SkyPilot catalog fallback — public GitHub CSV, no Cloudflare
    logger.warning("Lambda Labs: scrape returned 0 records — trying SkyPilot catalog fallback")
    return _fetch_skypilot_catalog(now) + _one_click_safe(now)


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
        specs = info.get("instance_type", info)

        mapping = INSTANCE_GPU_MAP.get(name)
        if mapping is None:
            # Route through _match_gpu so GH200 is excluded — its name contains
            # "h200" as a substring, which a naive match would mislabel as H200.
            gpu_model = _match_gpu(name)
            if not gpu_model:
                continue
            # GPU count, most authoritative first (fix 2026-07-14 — Lambda's real
            # ids are suffixed like gpu_8x_b200_sxm6; the old token.isdigit() scan
            # can't parse '8x', defaulted to 1, and published the $53.52 8-GPU
            # node as a $53.52/GPU-hr price for 22 days):
            #   1. the API's own specs.gpus field,
            #   2. the 'gpu_<N>x' pattern in the id,
            #   3. a bare numeric token (legacy naming).
            count = 0
            if isinstance(specs, dict):
                try:
                    count = int((specs.get("specs") or {}).get("gpus")
                                or specs.get("gpus") or 0)
                except (ValueError, TypeError):
                    count = 0
            if count <= 0:
                m = re.search(r"gpu_(\d+)x", name.lower())
                count = int(m.group(1)) if m else \
                    next((int(p) for p in name.lower().split("_") if p.isdigit()), 0)
            if count <= 0:
                logger.warning(f"Lambda: cannot derive GPU count for '{name}' — skipping "
                               f"rather than risk publishing a node price as per-GPU")
                continue
            mapping = {"gpu_model": gpu_model, "gpu_count": count}

        price_cents = info.get("price_cents_per_hour") or (specs.get("price_cents_per_hour") if isinstance(specs, dict) else 0) or 0
        if not price_cents:
            continue
        price = price_cents / 100.0

        # Per-GPU plausibility ceiling: no current-gen GPU rents anywhere near
        # $30/GPU-hr, so a higher reading means the count is wrong (a multi-GPU
        # node price about to be published as per-GPU — the exact 22-day B200
        # incident). Skip loudly instead.
        if price / mapping["gpu_count"] > 30:
            logger.warning(f"Lambda: '{name}' implausible ${price / mapping['gpu_count']:.2f}"
                           f"/GPU-hr (count={mapping['gpu_count']}) — skipping")
            continue

        # Lambda lists only regions where the instance is CURRENTLY available, but a
        # sold-out instance still has a published price. Emit it regardless (region
        # "global" when none are free) so prices never silently vanish and we don't
        # fall back to the weeks-stale SkyPilot catalog just because capacity is tight.
        regions_available = info.get("regions_with_capacity_available") or []
        region_names, seen_regions = [], set()
        for region_info in regions_available:
            rn = region_info.get("name", "us-east")
            if rn not in seen_regions:
                seen_regions.add(rn)
                region_names.append(rn)
        if not region_names:
            region_names = ["global"]
        for region_name in region_names:
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
