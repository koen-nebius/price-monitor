"""
Nebius pricing fetcher.
Parses https://nebius.com/prices — prices are in a Next.js __NEXT_DATA__ JSON
as a highlight-table-block 2D table.
"""
import json
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

PRICING_URL = "https://nebius.com/prices"
SOURCE_URL = PRICING_URL

# Map display name fragment → normalized GPU model
GPU_NAME_MAP = {
    "gb300": "GB300",
    "gb200": "GB200",
    "b300":  "B300",
    "b200":  "B200",
    "h200":  "H200",
    "h100":  "H100",
    "rtx pro 6000": "RTX6000",   # Blackwell PRO 6000 96GB (inference/PAYG card); NOT RTX 6000 Ada
    "l40s":  "L40S",
}

# vCPUs per GPU for each model (used to derive gpu_count from table vcpu column)
VCPU_PER_GPU = {
    "H100": 16,
    "H200": 16,
    "B200": 20,
    "B300": 24,
    "GB200": 112,
    "GB300": 112,
    "L40S": 16,
    "RTX6000": 24,   # 1-GPU PCIe instance (24 vCPU)
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    records = []

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; price-monitor/1.0)"}
        req = urllib.request.Request(PRICING_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        records = _parse_html(html, now)
    except Exception as e:
        logger.error(f"Nebius scrape failed: {e}")

    if not records:
        logger.error("Nebius: no pricing data retrieved")
    else:
        logger.info(f"Nebius: {len(records)} records")
    return records


def _parse_html(html: str, now: str) -> List[PriceRecord]:
    # Primary: parse the __NEXT_DATA__ JSON table
    next_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if next_match:
        try:
            data = json.loads(next_match.group(1))
            rows = _find_pricing_table(data)
            if rows:
                records = _parse_table_rows(rows, now)
                if records:
                    return records
        except Exception as e:
            logger.debug(f"Nebius __NEXT_DATA__ parse failed: {e}")

    # Fallback: regex on raw HTML
    return _regex_fallback(html, now)


def _find_pricing_table(obj, depth: int = 0) -> Optional[list]:
    """
    Recursively search Next.js JSON for the GPU pricing table.
    The table lives in a double-encoded JSON string at
    props.pageProps.__APOLLO_STATE__["pages:..."].content — parse it specially.
    """
    if depth > 12:
        return None
    if isinstance(obj, str) and "highlight-table-block" in obj:
        # Double-encoded content string — try to parse and search inside
        try:
            inner = json.loads(obj)
            return _find_pricing_table(inner, depth + 1)
        except Exception:
            # Try regex to pull out the table content array directly
            m = re.search(r'"table"\s*:\s*\{"content"\s*:\s*(\[\[.*?\]\])', obj, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    pass
    elif isinstance(obj, list):
        if len(obj) > 2 and all(isinstance(row, list) for row in obj):
            flat = " ".join(str(c) for row in obj for c in row).lower()
            if any(g in flat for g in GPU_NAME_MAP):
                return obj
        for item in obj:
            result = _find_pricing_table(item, depth + 1)
            if result:
                return result
    elif isinstance(obj, dict):
        # Prioritise the Apollo state pages which hold the content string
        for key in ("content", "table", "blocks", "rows", "data", "pricing", "items", "children", "body"):
            if key in obj:
                result = _find_pricing_table(obj[key], depth + 1)
                if result:
                    return result
        for v in obj.values():
            result = _find_pricing_table(v, depth + 1)
            if result:
                return result
    return None


def _parse_table_rows(rows: list, now: str) -> List[PriceRecord]:
    """
    Parse a 2D table whose columns are:
      GPU name | vCPUs | RAM (GB) | Preemptible/GPU-hr | On-demand/GPU-hr
    """
    records = []
    seen = set()

    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        cells = [_cell_text(c) for c in row]
        if not cells[0]:
            continue

        gpu_model = _match_gpu(cells[0])
        if not gpu_model:
            continue

        # Columns 3 and 4 are Preemptible and On-demand prices
        preempt_price = _parse_price(cells[3])
        od_price = _parse_price(cells[4])

        # Derive gpu_count from vCPU column if possible.
        # Cap at 16: the Nebius pricing page sometimes has unexpected vCPU values
        # (e.g. showing cluster-level totals) that produce unrealistic gpu_counts.
        # Any result > 16 is treated as a parse artifact and reset to 1.
        try:
            vcpus = int(re.sub(r'[^0-9]', '', cells[1].split()[0]))
            gpu_count = max(1, round(vcpus / VCPU_PER_GPU.get(gpu_model, 16)))
            if gpu_count > 16:
                gpu_count = 1
        except (ValueError, IndexError):
            gpu_count = 1

        for ct, price in [("preemptible", preempt_price), ("on_demand", od_price)]:
            if price is None or price <= 0:
                continue
            key = (gpu_model, gpu_count, ct)
            if key in seen:
                continue
            seen.add(key)
            records.append(PriceRecord(
                provider="nebius",
                gpu_model=gpu_model,
                gpu_count=gpu_count,
                instance_type=f"nebius-{gpu_model.lower()}-{gpu_count}x",
                region="eu-north1",
                consumption_type=ct,
                price_per_hour_usd=price * gpu_count,
                price_per_gpu_hour_usd=price,
                fetched_at=now,
                source_url=SOURCE_URL,
                data_source="web_scrape",
            ))

    return records


def _cell_text(cell) -> str:
    """Extract plain text from a table cell (string or nested dict/list)."""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, dict):
        # Try common Next.js content node patterns
        for key in ("text", "value", "content", "children", "plain_text"):
            val = cell.get(key)
            if isinstance(val, str):
                return val.strip()
            if isinstance(val, list):
                return " ".join(_cell_text(v) for v in val).strip()
    if isinstance(cell, list):
        return " ".join(_cell_text(v) for v in cell).strip()
    return str(cell).strip()


def _parse_price(text: str) -> Optional[float]:
    """Extract a numeric price from a cell like '$3.40', 'from $1.82', '––', 'Contact us'."""
    if not text:
        return None
    m = re.search(r'\$\s*([\d,]+\.?\d*)', text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _regex_fallback(html: str, now: str) -> List[PriceRecord]:
    """Last-resort regex scan for GPU name + price pairs in the raw HTML."""
    records = []
    seen = set()

    for m in re.finditer(
        r'(GB300|GB200|B300|B200|H200|H100|L40S)[^<]{0,300}?\$\s*([\d,]+\.?\d*)',
        html, re.IGNORECASE | re.DOTALL,
    ):
        gpu_model = _match_gpu(m.group(1))
        if not gpu_model:
            continue
        try:
            price = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        if price <= 0 or price > 500:
            continue

        ctx = html[max(0, m.start() - 300): m.end() + 100].lower()
        if "preemptible" in ctx or "spot" in ctx:
            ct = "preemptible"
        else:
            ct = "on_demand"

        key = (gpu_model, 1, ct)
        if key in seen:
            continue
        seen.add(key)
        records.append(PriceRecord(
            provider="nebius",
            gpu_model=gpu_model,
            gpu_count=1,
            instance_type=f"nebius-{gpu_model.lower()}-1x",
            region="eu-north1",
            consumption_type=ct,
            price_per_hour_usd=price,
            price_per_gpu_hour_usd=price,
            fetched_at=now,
            source_url=SOURCE_URL,
            data_source="web_scrape",
        ))

    logger.info(f"Nebius regex fallback: {len(records)} records")
    return records


def _match_gpu(name: str) -> Optional[str]:
    name_lower = name.lower()
    for pattern, model in GPU_NAME_MAP.items():
        if pattern in name_lower:
            return model
    return None
