"""Crusoe's public GPU tariff catalogue, with explicit purchase columns.

A GPU-hour tariff does not identify a purchasable VM configuration or location.
Keep these numeric and quote-only references in LAST_CATALOGUE_OFFERS; no
synthetic one-GPU PriceRecords or inferred cluster configurations are emitted.
"""
import logging
import re
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import List, Optional

from schema import PriceRecord
from config import MANUAL_PRICES

logger = logging.getLogger(__name__)
PRICING_URL = "https://www.crusoe.ai/cloud/pricing"
SOURCE_URL = PRICING_URL
GPU_NAME_MAP = {model: model for model in ("GB200", "GB300", "B200", "B300", "H200", "H100", "L40S")}
LAST_CATALOGUE_OFFERS = []
LAST_FETCH_HEALTH = {}


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "root", "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, text):
        self.stack[-1]["children"].append(text)


def _nodes(node, css_class=None, tag=None):
    if not isinstance(node, dict):
        return
    classes = node["attrs"].get("class", "").split()
    if "w-condition-invisible" in classes or node["attrs"].get("aria-hidden") == "true":
        return
    if (css_class is None or css_class in classes) and (tag is None or node["tag"] == tag):
        yield node
    for child in node["children"]:
        yield from _nodes(child, css_class, tag)


def _text(node):
    if isinstance(node, str):
        return node
    if node["tag"] in {"script", "style", "svg"}:
        return ""
    return " ".join(_text(child) for child in node["children"])


def _clean(node):
    return " ".join(_text(node).split())


def _match_gpu(name: str) -> Optional[str]:
    name = name.strip().upper()
    return next((model for token, model in GPU_NAME_MAP.items()
                 if re.search(rf"\b{token}\b", name)), None)


def _catalogue_row(model, description, purchase, now, price=None, column=""):
    ident = re.sub(r"[^a-z0-9]+", "-", description.lower()).strip("-")
    row = dict(provider="crusoe", product_id=f"public-tariff-{ident}", gpu_model=model,
               region="unspecified", purchase_type=purchase,
               price_status="quote_required" if price is None else "published_unscoped",
               source_url=SOURCE_URL, observed_at=now, retrieved_at=now,
               gpu_count=None, gpu_count_relation="unknown", description=description,
               commercial_terms={"price_column": column, "billing_unit": "USD/GPU-hour",
                                 "configuration": "VM SKU, purchase GPU count and region are not specified"},
               product_family="gpu_rental")
    if price is not None:
        row.update(price_per_gpu_hour_usd=price, currency="USD")
    return row


def _parse_html(html: str, now: str) -> List[PriceRecord]:
    """Parse the GPU table only; column headers determine purchase type."""
    tree = _Tree()
    tree.feed(html)
    offers, invalid, excluded = [], 0, set()
    seen = set()
    matched_tables = 0
    for group in _nodes(tree.root, "pricing-gap"):
        headings = list(_nodes(group, "pricing-table-heading"))
        if not headings:
            continue
        labels = [_clean(node) for node in _nodes(headings[0], "pricing-heading")]
        if not labels or labels[0].lower() != "gpu model":
            continue
        matched_tables += 1
        columns = {"on-demand": "on_demand", "current spot": "spot", "spot": "spot"}
        purchases = [columns.get(label.lower()) for label in labels[1:]]
        if not purchases or any(p is None for p in purchases) or len(set(purchases)) != len(purchases):
            invalid += 1
            continue
        for node in _nodes(group, "pricing_gpu-item"):
            titles = list(_nodes(node, tag="h4"))
            prices = list(_nodes(node, "pricing-rich"))
            if len(titles) != 1 or len(prices) != len(purchases):
                invalid += 1
                continue
            name = _clean(titles[0])
            model = _match_gpu(name)
            if not model:
                excluded.add(name)
                continue
            tags = [_clean(tag) for tag in _nodes(node, "pricing-tag")]
            description = " ".join([name] + tags)
            for purchase, label, cell in zip(purchases, labels[1:], prices):
                text = _clean(cell)
                match = re.fullmatch(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*GPU[- ]hr", text, re.I)
                if match and float(match[1]) > 0:
                    offer = _catalogue_row(model, description, purchase, now, float(match[1]), label)
                elif re.fullmatch(r"contact\s+sales", text, re.I):
                    offer = _catalogue_row(model, description, purchase, now, column=label)
                else:
                    invalid += 1
                    continue
                key = (offer["product_id"], purchase, offer["price_status"], offer.get("price_per_gpu_hour_usd"))
                if key not in seen:
                    seen.add(key)
                    offers.append(offer)
    LAST_CATALOGUE_OFFERS[:] = offers
    LAST_FETCH_HEALTH.update(
        status="partial" if invalid else "catalogue_only" if offers else "failed",
        reason=("GPU catalogue contains unreadable rows or purchase columns" if invalid
                else "published GPU tariffs and quote-only products; purchase configuration is unspecified" if offers
                else "no tracked GPU table could be established"),
        matched_tables=matched_tables, invalid_cells=invalid, excluded_gpu_models=sorted(excluded),
        numeric_catalogue_count=sum(o["price_status"] == "published_unscoped" for o in offers),
        quote_required_count=sum(o["price_status"] == "quote_required" for o in offers),
        catalogue_count=len(offers), record_count=0,
    )
    return []


def _manual_catalogue(now):
    """Scalar config has neither a source link nor date: require a quote-ledger entry."""
    rejected = sum(isinstance(key, tuple) and len(key) == 4 and key[0] == "crusoe"
                   for key in MANUAL_PRICES)
    LAST_FETCH_HEALTH["undated_manual_rates_rejected"] = rejected
    if rejected:
        LAST_FETCH_HEALTH["manual_warning"] = "undated manual rates require a dated, source-linked quote-ledger entry"
    return []


def _manual_records(scraped, now):
    """Compatibility guard: undated manual entries never become PriceRecords."""
    return []


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    LAST_CATALOGUE_OFFERS.clear()
    LAST_FETCH_HEALTH.clear()
    LAST_FETCH_HEALTH.update(status="failed", reason="public pricing page not retrieved", record_count=0)
    try:
        req = urllib.request.Request(PRICING_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; price-monitor)"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        _parse_html(html, now)
    except Exception as exc:
        LAST_FETCH_HEALTH.update(reason="public pricing page retrieval failed", error_code=type(exc).__name__)
        logger.error("Crusoe public pricing fetch failed (%s)", type(exc).__name__)
    manual = _manual_catalogue(now)
    LAST_CATALOGUE_OFFERS.extend(manual)
    LAST_FETCH_HEALTH.update(manual_reference_count=len(manual), catalogue_count=len(LAST_CATALOGUE_OFFERS))
    logger.info("Crusoe: %s catalogue references; %s", len(LAST_CATALOGUE_OFFERS), LAST_FETCH_HEALTH["status"])
    return []
