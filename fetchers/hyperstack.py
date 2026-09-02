"""
Hyperstack (NexGen Cloud) pricing fetcher.

Data availability (verified June 2026):
  H100, H200: on_demand + reserved prices in static HTML at /gpu-pricing ✓
  H100: spot price also in HTML ✓
  B200: listed as "Contact us" — no public numeric price
  B300: reservation-only, no public on-demand price found
  GB200/GB300: not on pricing page

The Infrahub Pricebook API exists but requires authentication (401).
The public /gpu-pricing page contains prices in rendered HTML text.

Three pricing sections parsed from stripped page text:
  On-demand: "NVIDIA H200 SXM 141 22 225 $3.50"
             (GPU name | VRAM | vCPUs | RAM | $/GPU-hr)
  Reserved:  "NVIDIA H200 SXM $2.45 Reserve here"
             (GPU name | starting-from $/GPU-hr | "Reserve here")
  Spot VM:   "NVIDIA H100 PCIe $1.52" (under "Spot VM Pricing" header)

H100 ships in several form factors that all map to our single "H100" model
(on-demand: SXM $2.40, NVLink $1.95, PCIe/plain $1.90). ComputePrices and
main.py's Phase 1.9 cross-check both collapse variants to the CHEAPEST on-demand
price per (provider, gpu_model). We do the same here: keep the lowest price per
(gpu_model, consumption_type). Keeping the first DOM row instead picked the
priciest variant (SXM) and made Hyperstack look ~21% over its true cheapest price.
"""
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord
from fetchers._tavily import fetch_text as tavily_fetch_text

logger = logging.getLogger(__name__)

PRICING_URL = "https://www.hyperstack.cloud/gpu-pricing"
SOURCE_URL  = PRICING_URL

# None = skip (no public price / contact-sales)
HYPERSTACK_GPU_MAP = {
    "H200": "H200",
    "H100": "H100",
    "B200": "B200",   # public since ~Aug 2026 ($6.00 OD / $5.10 reserved)
    "B300": "B300",   # public since Aug 2026 ($7.40 OD)
    "A100": None,
    "RTX":  None,
    "L40":  None,
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    return _scrape_pricing(now)


def _scrape_pricing(now: str) -> List[PriceRecord]:
    html = ""
    try:
        req = urllib.request.Request(
            PRICING_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Hyperstack plain scrape failed: {e}")

    records = _parse_pricing(html, now) if html else []
    if not records:
        # Escalate to Tavily — renders the JS pricing page the plain scrape can miss.
        tav = tavily_fetch_text(PRICING_URL)
        if tav:
            records = _parse_pricing(tav, now)
            if records:
                logger.info("Hyperstack: parsed pricing via Tavily fallback")
    logger.info(f"Hyperstack scrape: {len(records)} records")
    return records


def _parse_pricing(raw: str, now: str) -> List[PriceRecord]:
    # Strip scripts/styles/tags; normalize markdown pipes -> spaces so the same
    # space-separated regexes match both raw HTML and Tavily markdown tables.
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', raw, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>',  ' ', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&nbsp;', ' ').replace('|', ' ')
    text = re.sub(r'\s+', ' ', text).strip()

    # (gpu_model, consumption_type) -> cheapest price seen. Multiple H100 form
    # factors collapse to one model; keep the lowest, matching how ComputePrices
    # and main.py's cross-check normalize variants (see module docstring).
    best: dict = {}

    def _offer(gpu_model: str, ct: str, price: float) -> None:
        if not (0.5 <= price <= 20):
            return
        key = (gpu_model, ct)
        if key not in best or price < best[key]:
            best[key] = price

    # ── On-demand: distinctive pattern "NVIDIA <GPU> <vram> <vcpu> <ram> $<price>" ──
    # Three numbers between GPU name and price distinguish on-demand rows from
    # reservation rows which have only "$price Reserve here"
    for m in re.finditer(
        r'NVIDIA\s+((?:H100|H200|B200|B300)(?:\s+\w+)?)\s+\d[\d.]+\s+\d+\s+\d+\s+\$?\s*([\d.]+)',
        text, re.IGNORECASE
    ):
        gpu_model = _match_gpu(m.group(1))
        if not gpu_model:
            continue
        try:
            _offer(gpu_model, "on_demand", float(m.group(2)))
        except ValueError:
            continue

    # ── Reserved: "NVIDIA <GPU> $<price> Reserve here" (starting-from price) ──
    for m in re.finditer(
        r'NVIDIA\s+((?:H100|H200|B200|B300)(?:\s+\w+)?)\s+\$?\s*([\d.]+)\s+Reserve\s+here',
        text, re.IGNORECASE
    ):
        gpu_model = _match_gpu(m.group(1))
        if not gpu_model:
            continue
        try:
            _offer(gpu_model, "reserved_1yr", float(m.group(2)))
        except ValueError:
            continue

    # ── Spot VM: after "Spot VM Pricing" header (not the nav "Spot VMs" menu) ──
    # Use rfind-style: find the occurrence that's followed by prices
    spot_start = -1
    for m in re.finditer(r'Spot VM Pricing', text):
        spot_start = m.start()  # take the last match
    if spot_start >= 0:
        spot_text = text[spot_start:spot_start + 500]
        for m in re.finditer(
            r'NVIDIA\s+((?:H100|H200|B200|B300)(?:\s+\w+)?)\s+\$?\s*([\d.]+)',
            spot_text, re.IGNORECASE
        ):
            gpu_model = _match_gpu(m.group(1))
            if not gpu_model:
                continue
            try:
                _offer(gpu_model, "spot", float(m.group(2)))
            except ValueError:
                continue

    return [
        _make_record(gpu_model, ct, price, now)
        for (gpu_model, ct), price in best.items()
    ]


# Largest public VM flavor per model (per-GPU rate is flat across sizes, so we
# represent node scale like verda.py does). Verified 2026-09-02 from
# hyperstack.cloud/gpu-pricing + docs flavors: B300's ONLY flavor is
# n3-B300-SXM6x8 (8× SXM6) — the old hardcoded gpu_count=1 made these SXM cards
# look like single-GPU VMs and silently dropped Hyperstack from the
# cluster-class (8×SXM) peer set (why B300 showed "1 peer only").
_MAX_NODE_GPUS = {"H100": 8, "H200": 8, "B200": 8, "B300": 8}


def _make_record(gpu_model: str, ct: str, price: float, now: str) -> PriceRecord:
    count = _MAX_NODE_GPUS.get(gpu_model, 1)
    return PriceRecord(
        provider="hyperstack",
        gpu_model=gpu_model,
        gpu_count=count,
        # instance_type is part of record_key() — keep the historical string so
        # the gpu_count fix doesn't fire fake removed/added diffs.
        instance_type=f"hyperstack-{gpu_model.lower()}",
        region="global",
        consumption_type=ct,
        price_per_hour_usd=round(price * count, 4),
        price_per_gpu_hour_usd=price,
        fetched_at=now,
        source_url=SOURCE_URL,
        data_source="web_scrape",
    )


def _match_gpu(name: str) -> Optional[str]:
    name_upper = name.upper()
    for pattern in sorted(HYPERSTACK_GPU_MAP.keys(), key=len, reverse=True):
        if pattern.upper() in name_upper:
            return HYPERSTACK_GPU_MAP[pattern]
    return None
