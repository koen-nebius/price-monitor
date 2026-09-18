"""Non-transactional catalogue evidence: quote-only products, tariffs and plan terms.

These records never become PriceRecords, benchmark votes or stock observations.
An unknown price or configuration is not represented by a numeric zero.
"""
import hashlib
import json
import math
from html import escape
from urllib.parse import urlparse

from report_freshness import observation_time, report_time

PRICE_STATUSES = {"quote_required", "published_unscoped", "plan_terms", "undated_reference"}


def normalize_offers(offers):
    result = []
    for raw in offers:
        row = dict(raw)
        for key in ("provider", "product_id", "price_status", "source_url"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError("Catalogue offer missing " + key)
        if row["price_status"] not in PRICE_STATUSES:
            raise ValueError("Unknown catalogue price status")
        source = urlparse(row["source_url"])
        if source.scheme not in {"https", "http"} or not source.hostname:
            raise ValueError("Catalogue evidence requires a source URL")
        row.setdefault("gpu_model", "")
        row.setdefault("product_family", "gpu_rental")
        row.setdefault("region", "unknown")
        row.setdefault("purchase_type", "unknown")
        row.setdefault("gpu_count", None)
        row.setdefault("gpu_count_relation", "unknown")
        row.setdefault("commercial_terms", {})
        row.setdefault("description", "")
        row.setdefault("observed_at", "")
        row.setdefault("retrieved_at", "")
        row.setdefault("currency", "USD" if row.get("price_per_gpu_hour_usd") is not None else "")
        if row["gpu_count_relation"] not in {"exact", "minimum", "unknown"}:
            raise ValueError("Invalid catalogue quantity relation")
        if not isinstance(row["commercial_terms"], dict):
            raise ValueError("Catalogue terms must be an object")
        for key in ("gpu_count", "price_per_gpu_hour_usd"):
            value = row.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value <= 0):
                raise ValueError("Invalid catalogue " + key)
        if (row["gpu_count"] is None) != (row["gpu_count_relation"] == "unknown"):
            raise ValueError("Catalogue quantity requires an explicit exact/minimum relation")
        if row["price_status"] in {"quote_required", "plan_terms"} and any(
                key.startswith("price_per_") and value is not None for key, value in row.items()):
            raise ValueError("Quote-only products and plan terms cannot contain a numeric tariff")
        if row.get("price_per_gpu_hour_usd") is not None and row["currency"] != "USD":
            raise ValueError("USD tariff has non-USD currency")
        for key in ("observed_at", "retrieved_at"):
            if row[key]:
                observation_time(row[key])
        identity = {key: row.get(key) for key in (
            "provider", "product_id", "gpu_model", "product_family", "region", "purchase_type",
            "gpu_count", "gpu_count_relation", "offer_variant", "commercial_terms", "source_url")}
        row["offer_id"] = "catalogue:" + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        row["comparison_eligible"] = False
        row["availability"] = "not_established"
        result.append(row)
    # Remove only identical observations. Conflicting prices remain inspectable.
    return list({json.dumps(row, sort_keys=True): row for row in result}.values())


def build_catalogue_report(offers, as_of, source_health=None):
    now = report_time(as_of)
    rows = normalize_offers(offers)
    for row in rows:
        try:
            age = (now - observation_time(row["observed_at"])).total_seconds() / 3600
        except (ValueError, TypeError):
            age = None
        row["age_hours"] = round(age, 1) if age is not None else None
        row["freshness"] = "recent" if age is not None and -24 <= age <= 48 else "dated" if age is not None else "unknown"
    return {"schema_version": 1, "as_of": now.isoformat(), "offers": rows,
            "source_health": source_health or {},
            "scope": "Published catalogue and commercial-plan evidence. Prices may require a quote or lack "
                     "an exact purchasable configuration. No price comparison or availability is inferred."}


def render_catalogue(report):
    esc = lambda value: escape(str(value), quote=True)
    h = ['<h3>Quote-only products, unscoped tariffs and commercial plans</h3>',
         '<p>' + esc(report["scope"]) + '</p>',
         '<table><tbody><tr><th>Provider / product</th><th>GPU / quantity</th><th>Region / purchase type</th>'
         '<th>Published price</th><th>Terms and limits</th><th>Source observation</th></tr>']
    for row in report["offers"]:
        quantity = row.get("gpu_count")
        count = "quantity unknown" if quantity is None else f'{quantity:g}{"+" if row["gpu_count_relation"] == "minimum" else ""} GPUs'
        px = row.get("price_per_gpu_hour_usd")
        price = f'${px:.4f}/GPU-h; configuration unqualified' if px is not None else {
            "quote_required": "Quote required", "plan_terms": "Plan terms; no numeric tariff",
            "undated_reference": "Undated reference", "published_unscoped": "Unscoped tariff"}[row["price_status"]]
        terms = "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in row["commercial_terms"].items())
        source = f'<a href="{esc(row["source_url"])}">Source</a>: {esc(row["observed_at"] or "observation date unknown")} ({esc(row["freshness"])})'
        values = (row["provider"] + " / " + row["product_id"],
                  (row["gpu_model"] or "GPU applicability not specified") + " / " + count,
                  row["region"] + " / " + row["purchase_type"], price,
                  "; ".join(x for x in (row["description"], terms) if x))
        h.append('<tr>' + ''.join('<td>' + esc(value) + '</td>' for value in values) + '<td>' + source + '</td></tr>')
    if not report["offers"]:
        h.append('<tr><td colspan="6">No catalogue evidence collected in this run; product absence is not established.</td></tr>')
    h.append('</tbody></table>')
    return '\n'.join(h)
