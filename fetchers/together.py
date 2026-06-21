"""
Together AI (together.ai/pricing) — direct fetcher.

Together is a named enterprise peer (ClusterMAX Silver) but was previously aggregator-
only (ComputePrices), which reported its single-GPU/dedicated rates as "on-demand" and
carried no reserved tiers. The direct page exposes the real per-GPU CLUSTER rates (lower)
plus published reserved tiers, so this fixes both the accuracy and the reserved gap.

Page sections (server-rendered, so urllib works; Tavily ladder as fallback):
  GPU Clusters / On-demand:  HGX H100/H200/B200  -> on_demand (per-GPU cluster rate)
  Reserved (7-180 day terms): cheapest tier      -> committed_short_term (<=6mo)
  GB200/GB300 + 181+ day: "Contact us" (skipped)
"""
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord
from fetchers._tavily import fetch_text as tavily_fetch_text

logger = logging.getLogger(__name__)

URL = "https://www.together.ai/pricing"
_GPUS = ("H100", "H200", "B200")


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    raw = ""
    try:
        req = urllib.request.Request(URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Together plain fetch failed: {e}")

    records = _parse(raw, now) if raw else []
    if not records:
        tav = tavily_fetch_text(URL)
        if tav:
            records = _parse(tav, now)
            if records:
                logger.info("Together: parsed pricing via Tavily fallback")
    logger.info(f"Together: {len(records)} records "
                f"({', '.join(f'{r.gpu_model} {r.consumption_type} ${r.price_per_gpu_hour_usd:.2f}' for r in records) or 'none'})")
    return records


def _parse(raw: str, now: str) -> List[PriceRecord]:
    text = re.sub(r"<[^>]+>", " ", raw).replace("|", " ")
    text = re.sub(r"\s+", " ", text)

    best = {}   # (gpu, ct) -> price

    def _offer(gpu, ct, price):
        if 0.5 <= price <= 30 and ((gpu, ct) not in best or price < best[(gpu, ct)]):
            best[(gpu, ct)] = price

    # ── On-demand cluster: the "GPU Clusters / On-demand" zone (before "Reserve") ──
    i, j = text.find("GPU Clusters"), text.find("Reserve GPU capacity")
    od_zone = text[i:j] if (i >= 0 and j > i) else text
    for m in re.finditer(r'NVIDIA HGX (H100|H200|B200)\s+\$\s*([\d.]+)', od_zone):
        try:
            _offer(m.group(1), "on_demand", float(m.group(2)))
        except ValueError:
            pass

    # ── Reserved: "HGX <GPU> $7-30d $31-90d $91-180d" — cheapest = committed_short_term ──
    for m in re.finditer(
        r'NVIDIA HGX (H100|H200|B200)\s+\$\s*([\d.]+)\s+\$\s*([\d.]+)\s+\$\s*([\d.]+)', text
    ):
        try:
            cheapest = min(float(m.group(k)) for k in (2, 3, 4))
            _offer(m.group(1), "committed_short_term", cheapest)
        except ValueError:
            pass

    return [
        PriceRecord(
            provider="together", gpu_model=gpu, gpu_count=8,
            instance_type=f"together-hgx-{gpu.lower()}", region="global",
            consumption_type=ct, price_per_hour_usd=price * 8, price_per_gpu_hour_usd=price,
            fetched_at=now, source_url=URL, data_source="web_scrape",
            interconnect="InfiniBand", form_factor="SXM", node_gpus=8,
        )
        for (gpu, ct), price in best.items()
    ]
