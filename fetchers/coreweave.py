"""
CoreWeave pricing fetcher.
Scrapes https://www.coreweave.com/pricing, preserving published regional
instance rows and host variants. Prices are per instance-hour.
Page structure: table rows with h3[data-product], instance-price/spot-price spans,
and spec cells rendered "<value> <label>" (GPU Count, VRAM, vCPUs, System RAM).
"""
import hashlib
import json
from html import unescape
from html.parser import HTMLParser
import logging
import math
import re
import urllib.request
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord

logger = logging.getLogger(__name__)

PRICING_URL = "https://www.coreweave.com/pricing"
SOURCE_URL = PRICING_URL

NEBIUS_GPUS = {"H100", "H200", "B200", "B300", "GB200", "GB300", "L40S", "RTX6000"}

# Map product_id (from data-product attr) → (gpu_model, gpu_count)
PRODUCT_MAP = {
    "hgx-h100":           ("H100",  8),
    "hgx-h200":           ("H200",  8),
    "nvidia-b200":        ("B200",  8),
    "nvidia-b300":        ("B300",  8),
    # GB200 NVL72: CoreWeave sells in 4-GPU units at $42/hr → $10.50/GPU-hr
    # (the "NVL72" is the chip generation name; their SKU GPU count = 4)
    "nvidia-gb200-nvl72": ("GB200",  4),
    "nvidia-gb300-nvl72": ("GB300",  4),
    # GH200 = Grace Hopper Superchip (H100 GPU + Grace CPU, 96GB HBM3).
    # Different form factor and memory spec from HGX H100 — exclude to keep
    # the H100 bucket clean and avoid inflating the CoreWeave H100 price.
    # "nvidia-gh200":     ("H100",  1),  # excluded
    "nvidia-l40s":        ("L40S",  8),
    "nvidia-rtx-pro-6000-blackwell-server-edition": ("RTX6000", 8),
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        req = urllib.request.Request(PRICING_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        records = _parse_html(html, now)
        logger.info(f"CoreWeave: {len(records)} records")
        return records
    except Exception as e:
        logger.error(f"CoreWeave scrape failed: {e}")
        return []


class _PricingRows(HTMLParser):
    """Read each complete responsive table row under its published region heading."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.rows, self.parts = [], []
        self.depth = 0
        self.heading = None
        self.region = "unknown"
        self.row_region = "unknown"

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if not self.depth and tag == "h5":
            self.heading = []
        if tag == "div" and "table-row-v2" in attrs.get("class", "").split():
            if not self.depth:
                self.parts, self.row_region = [], self.region
                self.depth = 1
            else:
                self.depth += 1
        elif self.depth and tag == "div":
            self.depth += 1
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.depth:
            self.parts.append(f"</{tag}>")
            if tag == "div":
                self.depth -= 1
                if not self.depth:
                    self.rows.append((self.row_region, "".join(self.parts)))
        if tag == "h5" and self.heading is not None:
            match = re.search(r"REGION:\s*(.+)", "".join(self.heading), re.I)
            if match:
                self.region = unescape(match.group(1)).strip().upper()
            self.heading = None

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)
        if self.heading is not None:
            self.heading.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def _parse_html(html: str, now: str) -> List[PriceRecord]:
    parser = _PricingRows()
    parser.feed(html)
    records, seen = [], set()
    for region, block in parser.rows:
        heading = re.search(r'<h3\b[^>]*data-product=["\']([^"\']+)["\'][^>]*>(.*?)</h3>', block, re.S | re.I)
        if not heading:
            continue
        product_id = heading.group(1)
        title = unescape(re.sub(r"<[^>]+>", " ", heading.group(2))).strip()
        gpu_model, _ = _match_product(product_id)
        if gpu_model is None:
            continue
        text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", block))).strip()
        # The priced unit is the actual row's GPU Count, never an NVL72 name.
        gpu_count = _spec(text, "GPU Count")
        if not gpu_count:
            continue
        vcpu = _spec(text, "vCPUs")
        ram_gb = _spec(text, "System RAM", float)
        storage_tb = _spec(text, "Local Storage (TB)", float)
        variant = ("High Memory" if "(High Memory)" in title else
                   "Standard Memory" if "(Standard Memory)" in title else "")
        form_factor = ("SXM" if "HGX" in title else "NVL" if "NVL72" in title else
                       "PCIe" if gpu_model in {"L40S", "RTX6000"} else "unknown")
        for ct, label in (("on_demand", "On-Demand Price"), ("spot", "Spot Price")):
            match = re.search(re.escape(label) + r":\s*\$([\d,]+(?:\.\d+)?)", text)
            if not match:
                continue  # Contact sales and N/A are not numeric offers.
            price = float(match.group(1).replace(",", ""))
            if not math.isfinite(price) or price <= 0:
                continue
            identity = (product_id, title, gpu_count, vcpu, ram_gb, storage_tb, region, ct)
            offer_id = "coreweave:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
            record = PriceRecord(
                provider="coreweave", gpu_model=gpu_model, gpu_count=gpu_count,
                instance_type=product_id, region=region, consumption_type=ct,
                price_per_hour_usd=price, price_per_gpu_hour_usd=price / gpu_count,
                vcpu=vcpu, ram_gb=ram_gb,
                # CoreWeave explicitly defines 1 TB = 1024 GB on this page.
                storage_gb=storage_tb * 1024 if storage_tb is not None else None,
                node_gpus=gpu_count, form_factor=form_factor, interconnect="unknown",
                fetched_at=now, source_observed_at=now, source_url=SOURCE_URL,
                data_source="web_scrape", parser_version="direct-offers-1",
                offer_id=offer_id, gpu_variant=title, offer_variant=variant,
                price_basis="public_instance_rate",
                comparison_eligible=not (gpu_model == "RTX6000" and not variant),
                correction_reason=("CoreWeave RTX host memory variant is unspecified"
                                   if gpu_model == "RTX6000" and not variant else ""),
            )
            observation = json.dumps(record.to_dict(), sort_keys=True)
            if observation not in seen:
                seen.add(observation)
                records.append(record)
    return records


def _match_product(product_id: str) -> tuple:
    pid = product_id.lower()
    # Exact match first
    if pid in PRODUCT_MAP:
        return PRODUCT_MAP[pid]
    # Prefix match for variants like nvidia-hgx-h100-80gb
    for key, val in PRODUCT_MAP.items():
        if pid.startswith(key) or key in pid:
            return val
    return None, None


def _spec(text: str, label: str, cast=int):
    """Value of a spec cell rendered '<value> <label>' in the row text, e.g.
    '144 vCPUs', '2,048 System RAM', '4^1 GPU Count' (^1 = footnote marker).
    None when the row has no such cell."""
    m = re.search(r'(\d[\d,]*(?:\.\d+)?)(?:\^\d+)?\s+' + re.escape(label), text)
    if not m:
        return None
    try:
        numeric = float(m.group(1).replace(",", ""))
        if not math.isfinite(numeric) or (cast is int and not numeric.is_integer()):
            return None
        val = cast(numeric)
    except (ValueError, OverflowError):
        return None
    return val if val > 0 else None  # a 0/garbage cell is "unknown", not a spec
