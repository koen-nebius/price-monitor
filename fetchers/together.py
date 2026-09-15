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

Guards (added 2026-09-06 after the 3 Sep digest reported a phantom -50% Together cut):
  * Together renders a "Preemptible Compute" column inside elements with class="hide";
    naive tag-stripping surfaces it as visible text, so hidden elements are removed first.
  * The on-demand price is the FIRST value after the model name in the on-demand zone
    (document order); the old "keep the lowest" rule silently preferred a preemptible row.
  * Product identity comes from the GPU Clusters section and a labeled price column.
    Missing or ambiguous on-demand boundaries produce no on-demand records. Price
    ordering relative to reserved tiers is not evidence of the commercial product.
"""
import logging
import re
import urllib.request
from datetime import datetime, timezone
from html import unescape
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


_HIDDEN_RE = re.compile(
    r'<(\w+)([^>]*\bclass="[^"]*\b(?:hide|hidden|is-hidden|d-none|visually-hidden)\b[^"]*"[^>]*)>.*?</\1>',
    re.S | re.I,
)


def _strip_hidden(raw: str) -> str:
    """Remove elements hidden via class (Together's preemptible column) before tag-stripping."""
    prev = None
    while prev != raw:
        prev = raw
        raw = _HIDDEN_RE.sub(" ", raw)
    return raw


def _pricing_lines(raw: str) -> List[str]:
    """Preserve heading boundaries and table columns in HTML and Tavily Markdown."""
    def clean(value):
        return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()

    raw = _strip_hidden(raw)
    raw = re.sub(r'<h([1-6])\b[^>]*>(.*?)</h\1\s*>',
                 lambda m: '\n' + '#' * int(m.group(1)) + ' ' + clean(m.group(2)) + '\n',
                 raw, flags=re.I | re.S)
    raw = re.sub(r'<tr\b[^>]*>(.*?)</tr\s*>',
                 lambda m: '\n| ' + ' | '.join(clean(c) for c in re.findall(
                     r'<t[dh]\b[^>]*>(.*?)</t[dh]\s*>', m.group(1), flags=re.I | re.S)) + ' |\n',
                 raw, flags=re.I | re.S)
    raw = re.sub(r'</?(?:div|p|br|table)\b[^>]*>', '\n', raw, flags=re.I)
    return [line for value in raw.splitlines() if (line := clean(value))]


def _sections(lines: List[str], title: str):
    """Read only explicitly named product sections, never the whole-page fallback."""
    for i, line in enumerate(lines):
        heading = re.fullmatch(r'(#{1,6})\s+(.+)', line)
        if not heading or heading.group(2).strip().casefold() != title.casefold():
            continue
        level = len(heading.group(1))
        end = i + 1
        while end < len(lines):
            next_heading = re.match(r'(#{1,6})\s+', lines[end])
            if next_heading and len(next_heading.group(1)) <= level:
                break
            end += 1
        yield lines[i + 1:end]


def _parse(raw: str, now: str) -> List[PriceRecord]:
    lines = _pricing_lines(raw)

    best = {}   # (gpu, ct) -> price

    def _offer(gpu, ct, price, first_wins=False):
        if not (0.5 <= price <= 30):
            return
        if (gpu, ct) not in best:
            best[(gpu, ct)] = price
        elif not first_wins and price < best[(gpu, ct)]:
            best[(gpu, ct)] = price

    def scan(section, allow_on_demand):
        od_context = False
        od_column = None
        reserved_terms = False
        for line in section:
            label = re.sub(r'^#{1,6}\s+', '', line).casefold()
            if '|' not in line:
                # A new product heading invalidates the previous table's columns.
                if re.match(r'^#{1,6}\s+', line) or re.match(
                        r'^(?:on-demand|preemptible|reserve(?:d)?\b)', label):
                    od_context = label == 'on-demand' and allow_on_demand
                    od_column = None
                    reserved_terms = False
                continue

            cells = [c.strip() for c in line.strip('|').split('|')]
            gpu_match = re.fullmatch(r'NVIDIA\s+HGX\s+(H100|H200|B200)', cells[0], re.I)
            if not gpu_match:
                # Empty Markdown separator/header rows do not replace real labels.
                if not any(re.search(r'[a-z]', c, re.I) for c in cells):
                    continue
                od_headers = [i for i, c in enumerate(cells) if re.match(r'^on-demand\b', c, re.I)]
                hourly_headers = [i for i, c in enumerate(cells) if c.casefold() == 'hourly']
                has_terms = all(re.search(term + r'\s*days\b', line, re.I)
                                for term in (r'7\s*[-–]\s*30', r'31\s*[-–]\s*90', r'91\s*[-–]\s*180'))
                if has_terms:
                    reserved_terms = True
                    # A standalone term header replaces the prior OD table. The
                    # current combined table uses a second "Pay as you go" header
                    # row beneath its explicitly labeled On-demand column.
                    if len(od_headers) == 1 and allow_on_demand:
                        od_column = od_headers[0]
                    elif not (cells[0].casefold() == 'pay as you go' and od_column is not None):
                        od_column = None
                else:
                    reserved_terms = False
                    od_column = (od_headers[0] if len(od_headers) == 1 and allow_on_demand
                                 else hourly_headers[0] if od_context and len(hourly_headers) == 1
                                 else None)
                continue

            prices = {}
            for i, cell in enumerate(cells[1:], 1):
                match = re.fullmatch(r'\$\s*(\d+(?:\.\d+)?)', cell)
                if match:
                    prices[i] = float(match.group(1))
            gpu = gpu_match.group(1).upper()
            if od_column in prices:
                _offer(gpu, 'on_demand', prices[od_column], first_wins=True)
            if reserved_terms:
                term_prices = [price for i, price in prices.items() if i != od_column]
                if len(term_prices) == 3:
                    _offer(gpu, 'committed_short_term', min(term_prices))

    for section in _sections(lines, 'GPU Clusters'):
        scan(section, allow_on_demand=True)
    for section in _sections(lines, 'Reserve GPU capacity'):
        scan(section, allow_on_demand=False)

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
