"""
dstack gpuhunt public catalogs -> configuration-level source cross-check.

Source (verified 2026-09-15, source-discovery sweep): dstack publishes per-provider
price catalogs to a public-read S3 bucket, refreshed hourly by GitHub Actions
(dstackai/gpuhunt, .github/workflows/catalogs.yml, cron "5 * * * *"):

    GET {BASE}/{provider}/version              -> "YYYYMMDD-<run>"
    GET {BASE}/{provider}/{version}/catalog.zip -> <provider>.csv (14 columns)

CSV columns: instance_name, location, price (USD per INSTANCE-hour), cpu, memory,
gpu_count, gpu_name (normalized: H100, H200, B200, B300, RTXPRO6000 ...), gpu_memory,
spot (True/False), disk_size, gpu_vendor, flags, cpu_arch, provider_data.

Role: retain each catalog configuration for comparison with the same direct
offer. A shared GPU family is insufficient. Source agreement may repeat one
provider rate card and does not establish independent verification.

Terms: catalog objects are published with --acl public-read for anonymous
download (publish_catalog.sh); dstack's own client hard-codes these URLs. We
fetch each provider once per run (<= 8 requests).
"""
import csv
import io
import hashlib
import json
import logging
import math
import re
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

BASE = "https://dstack-gpu-pricing.s3.eu-west-1.amazonaws.com/v3"
UA = {"User-Agent": "nebius-price-monitor/1.0 (cross-check; koen@nebius.com)"}

# gpuhunt provider slug -> our provider key (the direct fetcher it cross-checks)
PROVIDERS = {
    "lambdalabs": "lambda",
    "runpod": "runpod",
    "verda": "verda",
    "nebius": "nebius",
    "aws": "aws",
    "azure": "azure",
    # "gcp": excluded 2026-09-15 — gpuhunt's GCP catalog prices do not reconcile with
    # the Cloud Billing on-demand SKUs (a3-highgpu-8g us-east4 $43.79/node in gpuhunt
    # vs $78.37 from the API, both non-DWS); would raise a false warning daily.
    "oci": "oracle",
}

# gpuhunt gpu_name -> our gpu_model. Unlisted names (A100, V100, RTX5090, GH200,
# RTX6000Ada, RTXPRO4500SE ...) are ignored on purpose.
GPU_MAP = {
    "H100": "H100",
    "H200": "H200",
    "B200": "B200",
    "B300": "B300",
    "GB200": "GB200",
    "GB300": "GB300",
    "L40S": "L40S",
    "RTXPRO6000": "RTX6000",
}

LAST_VERSIONS: Dict[str, str] = {}   # provider slug -> catalog version seen this run
LAST_SOURCE_TIMES: Dict[str, str] = {}  # URL -> observed object Last-Modified header
LAST_CATALOG_TIMES: Dict[str, str] = {}  # provider slug -> catalog publication time


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        LAST_SOURCE_TIMES.pop(url, None)
        value = r.headers.get("Last-Modified")
        if value:
            try:
                published = parsedate_to_datetime(value)
                if published.tzinfo is not None:
                    LAST_SOURCE_TIMES[url] = published.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        return r.read()


def load_catalog(slug: str) -> List[dict]:
    """Rows of the provider's current catalog CSV (empty list on any failure)."""
    LAST_VERSIONS.pop(slug, None)
    LAST_CATALOG_TIMES.pop(slug, None)
    try:
        version = _get(f"{BASE}/{slug}/version", 30).decode("utf-8", "replace").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", version):
            raise ValueError("invalid catalog version")
        url = f"{BASE}/{slug}/{version}/catalog.zip"
        LAST_SOURCE_TIMES.pop(url, None)
        blob = _get(url, 90)
        z = zipfile.ZipFile(io.BytesIO(blob))
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        rows = list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8")))
        LAST_VERSIONS[slug] = version
        LAST_CATALOG_TIMES[slug] = LAST_SOURCE_TIMES.get(url, "")
        return rows
    except Exception as e:                      # network, zip, csv — all non-fatal
        logger.warning("gpuhunt %s: catalog unavailable (%s)", slug, type(e).__name__)
        return []


def parse_min_on_demand(rows: List[dict], provider_key: str) -> Dict[tuple, float]:
    """Legacy descriptive minimum; never used to cross-check a different offer."""
    out: Dict[tuple, float] = {}
    for r in rows:
        gpu = GPU_MAP.get((r.get("gpu_name") or "").strip())
        if not gpu:
            continue
        if str(r.get("spot", "")).strip().lower() == "true":
            continue
        # GCP rows flagged gcp-dws-calendar-mode are Dynamic Workload Scheduler
        # (calendar-reserved, discounted) prices, not on-demand list — skip them.
        if "dws" in str(r.get("flags", "")).lower():
            continue
        # GCP a3-edgegpu-* are Google Distributed Cloud edge appliances (customer-hosted
        # hardware billed as compute SKUs), not cloud on-demand — not comparable.
        if "edgegpu" in str(r.get("instance_name", "")).lower():
            continue
        try:
            count = int(float(r.get("gpu_count") or 0))
            price = float(r.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0 or price <= 0:
            continue
        per_gpu = price / count
        if not (0.10 <= per_gpu <= 100):
            continue
        k = (provider_key, gpu)
        if k not in out or per_gpu < out[k]:
            out[k] = round(per_gpu, 4)
    return out


def parse_offers(rows, provider_key, fetched_at, source_observed_at="", source_url=""):
    """Retain SKU, region, GPU count and host metadata without pooling minima.

    Catalog object publication time is distinct from our retrieval. A missing
    header remains unknown rather than being derived from a version/date label.
    """
    def number(value):
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return result if math.isfinite(result) and result > 0 else None
    out, seen = [], set()
    for row in rows:
        gpu = GPU_MAP.get(str(row.get("gpu_name") or "").strip())
        spot = str(row.get("spot", "")).strip().lower()
        sku = str(row.get("instance_name") or "").strip()
        if not gpu or spot not in {"false", "true"} or "edgegpu" in sku.lower():
            continue
        if "dws" in str(row.get("flags", "")).lower():
            continue
        count, price = number(row.get("gpu_count")), number(row.get("price"))
        if count is None or price is None:
            continue
        per_gpu = price / count
        if not math.isfinite(per_gpu) or per_gpu <= 0:
            continue
        region = str(row.get("location") or "").strip() or "unspecified"
        ct = "spot" if spot == "true" else "on_demand"
        cpu, memory, disk = (number(row.get(field)) for field in ("cpu", "memory", "disk_size"))
        identity = (provider_key, sku, region, gpu, count, ct, cpu, memory, disk,
                    str(row.get("provider_data") or ""), str(row.get("flags") or ""))
        offer_id = "gpuhunt:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
        observation = (offer_id, price, source_observed_at)
        if observation in seen:
            continue
        seen.add(observation)
        form = "SXM" if "sxm" in sku.lower() else "PCIe" if "pcie" in sku.lower() else "NVL" if "nvl" in sku.lower() else "unknown"
        # Opaque provider metadata may distinguish commercial/host variants.
        # Preserve it conservatively; an unparsed tier cannot match a blank
        # direct tier simply because provider SKU and GPU family agree.
        raw_variant = "; ".join(f"{key}={row[key]}" for key in ("flags", "provider_data")
                                if row.get(key) not in (None, "", "{}", "[]"))
        out.append(PriceRecord(
            provider=provider_key, gpu_model=gpu, gpu_count=count, instance_type=sku,
            region=region, consumption_type=ct, price_per_hour_usd=price,
            price_per_gpu_hour_usd=per_gpu, fetched_at=fetched_at,
            source_observed_at=source_observed_at, source_url=source_url,
            data_source="aggregator", source_feed="gpuhunt", offer_id=offer_id,
            vcpu=int(cpu) if cpu is not None and cpu.is_integer() else None,
            ram_gb=memory, storage_gb=disk, form_factor=form,
            gpu_variant=str(row.get("gpu_name") or ""), offer_variant=raw_variant,
            parser_version="gpuhunt-offers-1",
        ))
    return out


def fetch_crosscheck(providers: Optional[Dict[str, str]] = None) -> List[PriceRecord]:
    """Full source observations; price_crosscheck decides comparison eligibility."""
    result = []
    now = datetime.now(timezone.utc).isoformat()
    for slug, key in (providers or PROVIDERS).items():
        rows = load_catalog(slug)
        if rows:
            result.extend(parse_offers(rows, key, now, LAST_CATALOG_TIMES.get(slug, ""),
                                       f"{BASE}/{slug}/{LAST_VERSIONS.get(slug, '')}/catalog.zip"))
    logger.info(f"gpuhunt cross-check: {len(result)} configuration observations from "
                f"{len(LAST_VERSIONS)} catalogs {sorted(set(LAST_VERSIONS.values()))}")
    return result

# ── node configuration side-channel (2026-09-16) ─────────────────────────────
# The same catalogs carry cpu, memory (GB) and disk_size (GB) per instance. Used by
# scripts/merge_node_specs.py to refresh store/node_specs.json daily (cross-check of
# the researched provider-doc entries; fallback where a provider publishes no spec).
# Prices from these rows are NOT used for the tables (see PROVIDERS above); GCP is
# included here for its configuration only.
SPEC_PROVIDERS = {
    **PROVIDERS,
    "gcp": "gcp",       # prices excluded above; configuration is fine
    # vastai, datacrunch, cudo, vultr, crusoe, digitalocean, tensordock, hotaisle: no public catalog object (HTTP 403, 2026-09-16)
}


def parse_specs(rows: List[dict], provider_key: str, slug: str = "") -> List[dict]:
    """One entry per (gpu_model, gpu_count): the cheapest non-spot instance's name, CPU
    threads, memory GB, disk (TB) and location. Same row filters as parse_min_on_demand."""
    best: Dict[tuple, dict] = {}
    for r in rows:
        gpu = GPU_MAP.get((r.get("gpu_name") or "").strip())
        if not gpu:
            continue
        if str(r.get("spot", "")).strip().lower() == "true":
            continue
        if "dws" in str(r.get("flags", "")).lower() or "edgegpu" in str(r.get("instance_name", "")).lower():
            continue
        try:
            count = int(float(r.get("gpu_count") or 0))
            price = float(r.get("price") or 0)
            cpu = float(r.get("cpu") or 0)
            mem = float(r.get("memory") or 0)
            disk = float(r.get("disk_size") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0 or price <= 0:
            continue
        per_gpu = price / count
        if not (0.10 <= per_gpu <= 100):
            continue
        k = (gpu, count)
        if k not in best or per_gpu < best[k]["price_per_gpu_hour_usd"]:
            best[k] = {"provider": provider_key, "gpu_model": gpu, "instance_type": (r.get("instance_name") or "").strip(),
                       "node_gpus": count, "vcpu": int(cpu) if cpu > 0 else None, "ram_gb": mem if mem > 0 else None,
                       "local_storage_tb": round(disk / 1000.0, 2) if disk > 0 else None,
                       "location": (r.get("location") or "").strip(), "price_per_gpu_hour_usd": round(per_gpu, 4),
                       "catalog": slug, "catalog_version": LAST_VERSIONS.get(slug, "")}
    return list(best.values())


def fetch_specs(providers: Optional[Dict[str, str]] = None) -> List[dict]:
    """Node configurations per (our provider key, gpu_model, gpu_count) from the gpuhunt catalogs."""
    out: List[dict] = []
    for slug, key in (providers or SPEC_PROVIDERS).items():
        rows = load_catalog(slug)
        if rows:
            out.extend(parse_specs(rows, key, slug))
    logger.info(f"gpuhunt specs: {len(out)} (provider, gpu, count) configurations from {len(LAST_VERSIONS)} catalogs")
    return out
