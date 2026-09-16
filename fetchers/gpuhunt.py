"""
dstack gpuhunt public catalogs -> INDEPENDENT cross-check of our direct fetchers.

Source (verified 2026-09-15, source-discovery sweep): dstack publishes per-provider
price catalogs to a public-read S3 bucket, refreshed hourly by GitHub Actions
(dstackai/gpuhunt, .github/workflows/catalogs.yml, cron "5 * * * *"):

    GET {BASE}/{provider}/version              -> "YYYYMMDD-<run>"
    GET {BASE}/{provider}/{version}/catalog.zip -> <provider>.csv (14 columns)

CSV columns: instance_name, location, price (USD per INSTANCE-hour), cpu, memory,
gpu_count, gpu_name (normalized: H100, H200, B200, B300, RTXPRO6000 ...), gpu_memory,
spot (True/False), disk_size, gpu_vendor, flags, cpu_arch, provider_data.

Role: this is NOT a provider in the tables (every catalogued cloud already has a
direct fetcher, so records would only be superseded twins). It is the second
independent verifier next to ComputePrices for Danila's 2026-08-21 condition:
"no PAYG increase approval without double-checking automated competitor
benchmarks". main.py compares each direct on-demand price to the gpuhunt
value and raises a manifest warning on a material gap.

Terms: catalog objects are published with --acl public-read for anonymous
download (publish_catalog.sh); dstack's own client hard-codes these URLs. We
fetch each provider once per run (<= 8 requests).
"""
import csv
import io
import logging
import urllib.request
import zipfile
from typing import Dict, List, Optional

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


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_catalog(slug: str) -> List[dict]:
    """Rows of the provider's current catalog CSV (empty list on any failure)."""
    try:
        version = _get(f"{BASE}/{slug}/version", 30).decode("utf-8", "replace").strip()
        blob = _get(f"{BASE}/{slug}/{version}/catalog.zip", 90)
        z = zipfile.ZipFile(io.BytesIO(blob))
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        rows = list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8")))
        LAST_VERSIONS[slug] = version
        return rows
    except Exception as e:                      # network, zip, csv — all non-fatal
        logger.warning(f"gpuhunt {slug}: catalog unavailable ({e})")
        return []


def parse_min_on_demand(rows: List[dict], provider_key: str) -> Dict[tuple, float]:
    """(provider_key, gpu_model) -> cheapest on-demand USD per GPU-hour in these rows."""
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


def fetch_crosscheck(providers: Optional[Dict[str, str]] = None) -> Dict[tuple, float]:
    """Cheapest on-demand $/GPU-hr per (our provider key, gpu_model) from gpuhunt."""
    result: Dict[tuple, float] = {}
    for slug, key in (providers or PROVIDERS).items():
        rows = load_catalog(slug)
        if rows:
            result.update(parse_min_on_demand(rows, key))
    logger.info(f"gpuhunt cross-check: {len(result)} (provider, gpu) prices from "
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
