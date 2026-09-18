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
Upstream observation time is not supplied; retained as aggregator references only.
"""
import base64
import csv
import io
import hashlib
import math
from html import unescape
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

LAST_FETCH_HEALTH = {}
LAST_CATALOGUE_OFFERS = []
PARSER_VERSION = "direct-offers-1"

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

# ── SKU spec fields (vcpu / ram_gb), read from the payloads already fetched ─────
# API: specs.vcpus / specs.memory_gib; /instances page: the vCPUs and RAM cells;
# SkyPilot CSV: vCPUs / MemoryGiB. All three are whole-SKU totals (the 8x H100
# row reads 208 vCPUs / 1800 GiB, the 1x row 26 / 225 GiB), so no per-GPU scaling.
# ram_gb keeps the GiB figure as published (TiB is folded to GiB x1024, TB x1000).
# node_gpus=0 explicitly preserves unknown physical host size. A priced SKU or
# multi-node cluster quantity is not evidence of the full physical node size.
_RAM_RE = re.compile(r"([\d.]+)\s*(GiB|GB|TiB|TB)\b", re.I)


def _opt_int(v) -> Optional[int]:
    number = _opt_float(v)
    return int(number) if number is not None and number.is_integer() else None


def _opt_float(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _ram_gb_from_text(text: str) -> Optional[float]:
    """'1800 GiB' -> 1800.0 (GiB as published); '1.8 TiB' -> 1843.2; junk -> None."""
    m = _RAM_RE.search(text or "")
    if not m:
        return None
    n = _opt_float(m.group(1))
    if n is None:
        return None
    unit = m.group(2).lower()
    return n * (1024 if unit == "tib" else 1000 if unit == "tb" else 1)

def _source_text(raw: str) -> str:
    raw = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", raw,
                 flags=re.S | re.I)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "|", raw))).strip()


def _offer_id(*parts) -> str:
    return "lambda:" + hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:24]


def _parse_one_click_clusters(raw: str, now: str):
    """Keep each printed quantity/term tier; a plus denotes a minimum quantity."""
    text = _source_text(raw)
    pattern = (
        r"NVIDIA (?P<gpu>(?:HGX )?[A-Z]+\d{2,3}\w*)[|\s]+"
        r"(?P<term>2 weeks\s*[–-]\s*1 year|1 year\s*\+)[|\s]+"
        r"(?P<count>\d+)(?P<plus>\+)?[|\s]+(?P<price>\$\s*[\d.]+|[—–-])"
    )
    records, catalogue, seen = [], [], set()
    for match in re.finditer(pattern, text, re.I):
        gpu = _match_gpu(match.group("gpu"))
        count = int(match.group("count"))
        if not gpu or count <= 0:
            continue
        relation = "minimum" if match.group("plus") else "exact"
        term = match.group("term").strip()
        long_term = term.startswith("1 year")
        identity = (gpu, count, relation, term)
        token = match.group("price")
        if (identity, token) in seen:
            continue
        seen.add((identity, token))
        suffix = "+" if relation == "minimum" else ""
        offer_id = _offer_id("one_click_clusters", *identity)
        if long_term and not token.startswith("$"):
            catalogue.append({
                "provider": "lambda", "product_id": offer_id, "gpu_model": gpu,
                "region": "unknown", "purchase_type": "reserved_unknown",
                "price_status": "quote_required", "source_url": ONE_CLICK_URL,
                "observed_at": now, "retrieved_at": now, "gpu_count": count,
                "gpu_count_relation": relation,
                "description": f"Lambda 1-Click Clusters {gpu}, {count}{suffix} GPUs, {term}; contact sales",
                "commercial_terms": {"term_min_days": 365, "term_max_days": None,
                                     "term_label": term, "node_gpus": None,
                                     "gpu_count_relation": relation},
                "product_family": "gpu_rental",
            })
            continue
        # A changed/unknown term cannot be assigned today's short-term tariff.
        if long_term or not token.startswith("$"):
            continue
        price = _opt_float(token.replace("$", "").strip())
        if price is None or not .5 <= price <= 30:
            continue
        records.append(PriceRecord(
            provider="lambda", gpu_model=gpu, gpu_count=count,
            gpu_count_relation=relation,
            instance_type=f"1-click-cluster-{count}{suffix}x", region="unknown",
            consumption_type="reserved_short", price_per_hour_usd=price * count,
            price_per_gpu_hour_usd=price, node_gpus=0,
            term_min_days=14, term_max_days=365, term_label=term,
            fetched_at=now, source_observed_at=now, source_url=ONE_CLICK_URL,
            source_feed="lambda_1cc", data_source="web_scrape", parser_version=PARSER_VERSION,
            gpu_variant=match.group("gpu"), offer_id=offer_id,
            form_factor="unknown", interconnect="unknown",
            price_basis=("per_gpu_tier_minimum_quantity" if relation == "minimum"
                         else "per_gpu_tier_exact_quantity"),
            offer_variant=f"{count}{suffix} GPUs; {term}; physical node size unspecified",
        ))
    return records, catalogue


def _cluster_health(records, catalogue, error=""):
    expected = {("B200", 16, "exact"), ("B200", 64, "exact"),
                ("B200", 256, "minimum"), ("H100", 16, "exact"),
                ("H100", 64, "exact"), ("H100", 256, "exact")}
    observed = {(r.gpu_model, r.gpu_count, r.gpu_count_relation) for r in records}
    missing = sorted(expected - observed)
    quotes = {r["gpu_model"] for r in catalogue}
    missing_quotes = sorted({"H100", "B200"} - quotes)
    reason = []
    if missing:
        reason.append(f"Missing {len(missing)} of 6 baseline priced cluster tiers")
    if missing_quotes:
        reason.append("Quote-only 1 year+ coverage absent for " + ", ".join(missing_quotes))
    if error and (missing or missing_quotes):
        reason.append(error)
    LAST_FETCH_HEALTH.setdefault("components", {})["one_click_clusters"] = {
        "status": "live" if not reason else "partial" if records or catalogue else "failed",
        "reason": "; ".join(reason), "record_count": len(records),
        "catalogue_count": len(catalogue), "missing_priced_tiers": missing,
        "missing_quote_gpu_models": missing_quotes,
    }


def _fetch_one_click_clusters(now: str):
    """Public 1CC ladder, with rendered-text fallback and explicit partial health."""
    raw, error = "", ""
    try:
        req = urllib.request.Request(ONE_CLICK_URL, headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"})
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
    except Exception as exc:
        error = "Public 1CC retrieval failed: " + type(exc).__name__
    records, catalogue = _parse_one_click_clusters(raw, now)
    if not records:
        from fetchers._tavily import fetch_text
        rendered = fetch_text(ONE_CLICK_URL)
        if rendered:
            records, catalogue = _parse_one_click_clusters(rendered, now)
    LAST_CATALOGUE_OFFERS[:] = catalogue
    _cluster_health(records, catalogue, error)
    return records


def _one_click_safe(now: str):
    """Keep usable OD rows if the independent cluster source fails, visibly."""
    try:
        records = _fetch_one_click_clusters(now)
        if "one_click_clusters" not in LAST_FETCH_HEALTH.get("components", {}):
            _cluster_health(records, LAST_CATALOGUE_OFFERS)
        return records
    except Exception as exc:
        reason = "1CC retrieval/parser failed: " + type(exc).__name__
        logger.warning("Lambda %s", reason)
        LAST_CATALOGUE_OFFERS.clear()
        _cluster_health([], [], reason)
        return []


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    LAST_FETCH_HEALTH.clear()
    LAST_CATALOGUE_OFFERS.clear()
    now = datetime.now(timezone.utc).isoformat()
    api_key = os.environ.get("LAMBDA_API_KEY")
    records, attempts = [], []
    if api_key:
        try:
            creds = base64.b64encode(f"{api_key}:".encode()).decode()
            req = urllib.request.Request(API_URL, headers={
                "Authorization": f"Basic {creds}",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=30) as response:
                records = _parse_api_data(json.loads(response.read()), now)
            if not records:
                attempts.append("API returned no supported priced instances")
        except Exception as exc:
            attempts.append("API retrieval failed: " + type(exc).__name__)
    if not records:
        records = _scrape_pricing_page(now)
        if not records:
            attempts.append("Public instance table returned no supported priced instances")
    direct_count = len(records)
    records = _supplement_with_skypilot(records, now)
    fallback_count = len(records) - direct_count
    od_reason = ("Supplement includes undated SkyPilot references" if direct_count and fallback_count
                 else "" if direct_count else "; ".join(attempts + [
                     "Only undated SkyPilot references available" if records
                     else "No on-demand price records available"]))
    LAST_FETCH_HEALTH.setdefault("components", {})["on_demand"] = {
        "status": "partial" if direct_count and fallback_count else "live" if direct_count
                  else "fallback" if records else "failed",
        "reason": od_reason, "record_count": len(records),
        "direct_record_count": direct_count, "reference_record_count": fallback_count,
    }
    clusters = _one_click_safe(now)
    components = LAST_FETCH_HEALTH["components"]
    problems = [f"{key}: {value['reason']}" for key, value in components.items()
                if value["status"] != "live"]
    LAST_FETCH_HEALTH.update({
        "status": "live" if not problems else "fallback" if records and not direct_count and not clusters
                  else "partial" if records or clusters or LAST_CATALOGUE_OFFERS else "failed",
        "reason": "; ".join(problems),
    })
    return records + clusters


def _supplement_with_skypilot(records: List[PriceRecord], now: str) -> List[PriceRecord]:
    """Retain missing exact catalogue shapes/regions as undated references.

    The same GPU family in a direct response does not prove coverage of another
    instance shape, region or purchase type. Only exact offer matches suppress a
    fallback row; an unknown direct region does not cover named catalogue regions.
    """
    def key(record):
        return (record.gpu_model, record.gpu_count, record.instance_type,
                record.region, record.consumption_type)
    covered = {key(record) for record in records}
    return records + [row for row in _fetch_skypilot_catalog(now) if key(row) not in covered]


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

    return _parse_skypilot_catalog(content, now)


def _parse_skypilot_catalog(content: str, now: str) -> List[PriceRecord]:
    records, seen = [], set()
    for row in csv.DictReader(io.StringIO(content)):
        gpu_model = _match_gpu(row.get("AcceleratorName", ""))
        gpu_count = _opt_int(row.get("AcceleratorCount"))
        instance_type = row.get("InstanceType", "").strip()
        if not gpu_model or not gpu_count or not instance_type:
            continue
        region = row.get("Region", "").strip() or "unknown"
        vcpu = _opt_int(row.get("vCPUs"))
        ram_gb = _opt_float(row.get("MemoryGiB"))
        variant = row.get("AcceleratorName", "").strip()
        for ct, price_field in [("on_demand", "Price"), ("spot", "SpotPrice")]:
            price_total = _opt_float(row.get(price_field))
            if price_total is None:
                continue
            identity = (instance_type, region, ct, gpu_model, gpu_count, vcpu, ram_gb, variant)
            # Repeated identical observations are duplicates; contradictory
            # rates retain the same offer ID for downstream conflict handling.
            if (identity, price_total) in seen:
                continue
            seen.add((identity, price_total))
            records.append(PriceRecord(
                provider="lambda", gpu_model=gpu_model, gpu_count=gpu_count,
                instance_type=instance_type, region=region, consumption_type=ct,
                price_per_hour_usd=price_total,
                price_per_gpu_hour_usd=price_total / gpu_count,
                vcpu=vcpu, ram_gb=ram_gb, node_gpus=0,
                fetched_at=now, source_observed_at="", source_url=SKYPILOT_CSV_URL,
                data_source="aggregator", source_feed="skypilot",
                parser_version="aggregator-offers-1", offer_id=_offer_id("skypilot", *identity),
                gpu_variant=variant, form_factor=_form_factor(instance_type + " " + variant),
                interconnect="unknown", price_basis="undated_community_catalogue",
                comparison_eligible=False,
                correction_reason="SkyPilot catalogue has no upstream observation timestamp; reference only",
            ))
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


def _attribute(attributes: str, name: str) -> str:
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*([\"'])(.*?)\1", attributes, re.S | re.I)
    return unescape(match.group(2)) if match else ""


def _parse_html(html: str, now: str) -> List[PriceRecord]:
    """Use each labelled GPU-count tab, never an assumed vCPU/GPU ratio."""
    counts = {}
    for button in re.finditer(r"<button\b([^>]*)>(.*?)</button>", html, re.S | re.I):
        if _attribute(button.group(1), "role") != "tab":
            continue
        label = re.sub(r"<[^>]+>", "", button.group(2)).strip()
        quantity = re.fullmatch(r"(\d+)\s*[x×]", unescape(label), re.I)
        target = _attribute(button.group(1), "aria-controls")
        if quantity and target and int(quantity.group(1)) > 0:
            counts[target] = int(quantity.group(1))
    panels = [match for match in re.finditer(r"<div\b([^>]*)>", html, re.S | re.I)
              if _attribute(match.group(1), "role") == "tabpanel"]
    records, seen = [], set()
    for index, panel in enumerate(panels):
        count = counts.get(_attribute(panel.group(1), "id"))
        if not count:
            continue
        end = panels[index + 1].start() if index + 1 < len(panels) else len(html)
        table = re.search(r"<table\b[^>]*>.*?</table>", html[panel.end():end], re.S | re.I)
        if not table:
            continue
        for row in re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", table.group(0), re.S | re.I):
            plan = _attribute(row.group(1), "data-plan")
            model = _match_gpu(plan)
            if not model:
                continue
            cells = [unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in
                     re.findall(r"<td\b[^>]*>(.*?)</td>", row.group(2), re.S | re.I)]
            if len(cells) < 5:
                continue
            price = _opt_float(cells[4].lstrip("$").replace(",", ""))
            if price is None:
                continue
            variant = plan.removeprefix("NVIDIA ").strip()
            slug = re.sub(r"[^a-z0-9]+", "-", variant.lower()).strip("-")
            instance_type = f"lambda-{slug}-{count}x"
            vcpu, ram = _opt_int(cells[1]), _ram_gb_from_text(cells[2])
            storage = _ram_gb_from_text(cells[3])
            identity = (instance_type, count, cells[0], vcpu, ram, storage)
            if (identity, price) in seen:
                continue
            seen.add((identity, price))
            records.append(PriceRecord(
                provider="lambda", gpu_model=model, gpu_count=count,
                instance_type=instance_type, region="unknown", consumption_type="on_demand",
                price_per_hour_usd=price * count, price_per_gpu_hour_usd=price,
                vcpu=vcpu, ram_gb=ram, storage_gb=storage, node_gpus=0,
                fetched_at=now, source_observed_at=now, source_url=PRICING_URL,
                source_feed="lambda_instances", data_source="web_scrape",
                parser_version=PARSER_VERSION, offer_id=_offer_id("instances", *identity),
                gpu_variant=variant + " " + cells[0], form_factor=_form_factor(plan),
                interconnect="unknown", price_basis="published_per_gpu_instance_rate",
            ))
    return records


def _parse_api_data(data: dict, now: str) -> List[PriceRecord]:
    records = []
    instance_types = data.get("data", {})
    if isinstance(instance_types, list):
        instance_types = {str(i): x for i, x in enumerate(instance_types)}

    for name, info in instance_types.items():
        if not isinstance(info, dict):
            continue
        specs = info.get("instance_type", info)
        if isinstance(specs, dict):
            name = specs.get("name") or name

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
        price_cents = _opt_float(price_cents)
        if price_cents is None:
            continue
        price = price_cents / 100.0

        # Same response: instance_type.specs.{vcpus, memory_gib} (OpenAPI example
        # 208 / 1800 for gpu_8x_h100_sxm5) — per-instance totals, GiB as published.
        sp = specs if isinstance(specs, dict) else {}
        nested = sp.get("specs")
        sp = nested if isinstance(nested, dict) else sp
        vcpu = _opt_int(sp.get("vcpus"))
        ram_gb = _opt_float(sp.get("memory_gib"))

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
        # "unknown" when none are free) so prices never silently vanish and we don't
        # fall back to the weeks-stale SkyPilot catalog just because capacity is tight.
        regions_available = info.get("regions_with_capacity_available") or []
        region_names, seen_regions = [], set()
        for region_info in regions_available:
            rn = region_info.get("name", "") if isinstance(region_info, dict) else ""
            if not rn:
                continue
            if rn not in seen_regions:
                seen_regions.add(rn)
                region_names.append(rn)
        if not region_names:
            region_names = ["unknown"]
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
                vcpu=vcpu,
                ram_gb=ram_gb,
                fetched_at=now,
                source_url=API_URL, source_observed_at=now, source_feed="lambda_api",
                data_source="official_api", parser_version=PARSER_VERSION,
                gpu_variant=name, form_factor=_form_factor(name), interconnect="unknown",
                node_gpus=0, offer_id=_offer_id("api", name, region_name, "on_demand"),
                price_basis="api_instance_hour",
            ))
    return records


def _match_gpu(name: str) -> Optional[str]:
    name_upper = name.upper()
    # GH200 = Grace Hopper Superchip — excluded (different form factor from HGX H100)
    if "GH200" in name_upper:
        return None
    for g in sorted(NEBIUS_GPUS, key=len, reverse=True):
        if g in name_upper:
            return g
    return None


def _form_factor(text: str) -> str:
    text = text.upper()
    return "SXM" if "SXM" in text else "PCIe" if "PCIE" in text else "unknown"
