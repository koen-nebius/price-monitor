"""Dated observation coverage, separate from supplier inventory or market share."""
from collections import Counter, defaultdict
from html import escape

from comparability import is_public_benchmark_eligible
from config import NEBIUS_GPUS
from report_freshness import observation_time, publication_records, report_time
from source_priority import PROVIDER_ALIASES, canonicalize_provider_sources


def _supplier(provider):
    return {"aws_capacity_blocks": "aws", "sfcompute_fills": "sfcompute",
            "coreweave_plans": "coreweave", "crusoe_public": "crusoe",
            "vast_reserved": "vast",
            "aws_spot_advisor": "aws", "gcp_zones": "gcp", "azure_regions": "azure",
            "nebius_committed": "nebius"}.get(
                provider, PROVIDER_ALIASES.get(provider, provider))


def _age(value, now):
    try:
        return round((now - observation_time(value)).total_seconds() / 3600, 1)
    except (ValueError, TypeError, AttributeError):
        return None


def _health(status):
    return [{"source": p, **value} for p, value in sorted((status or {}).items())]


def _finish(kind, groups, now, health, target_providers):
    cells = []
    for key, entries in sorted(groups.items()):
        counts = Counter(e["status"] for e in entries)
        dates = [e["observed_at"] for e in entries if e["age_hours"] is not None]
        latest = max(dates, key=observation_time) if dates else ""
        cells.append(dict(zip(("provider", "gpu", "region", "purchase_type"), key),
                          observations=len(entries), statuses=dict(sorted(counts.items())),
                          direct_instance_observations=sum(e.get("direct_instance", False) for e in entries),
                          latest_observed_at=latest, latest_age_hours=_age(latest, now),
                          feeds=sorted({e["feed"] for e in entries}),
                          evidence_types=sorted({e["evidence_type"] for e in entries}),
                          configurations=sorted({e["configuration"] for e in entries}),
                          reasons=sorted({e["reason"] for e in entries if e["reason"]})))
    suppliers = sorted({_supplier(p) for p in target_providers} |
                       {c["provider"] for c in cells})
    suppliers = [p for p in suppliers if p not in {"nebius", "computeprices", "shadeform"}]
    gaps = []
    for provider in suppliers:
        for gpu in NEBIUS_GPUS:
            observed = {c["purchase_type"] for c in cells
                        if (c["provider"], c["gpu"]) == (provider, gpu)}
            missing = []
            if "on_demand" not in observed:
                missing.append("on-demand")
            if not observed.intersection({"spot", "preemptible"}):
                missing.append("spot/preemptible")
            if not any(t.startswith(("reserved", "committed", "capacity_block")) for t in observed):
                missing.append("reservation/commitment")
            if missing:
                gaps.append({"provider": provider, "gpu": gpu,
                             "unobserved_purchase_types": missing})
    return {"schema_version": 1, "kind": kind, "as_of": now.isoformat(),
            "scope": "Recorded observations and configured collector panel; not a complete market census. "
                     "Unobserved means no record in this snapshot, not unavailable or not offered. "
                     "Regions remain provider-native; feeds may repeat the same underlying source. "
                     "Own Nebius observations are excluded from competitor counts.",
            "tracked_gpus": list(NEBIUS_GPUS), "tracked_competitors": suppliers,
            "observations": sum(c["observations"] for c in cells), "observed_cells": len(cells),
            "cells": cells, "unobserved": gaps, "source_health": _health(health)}


def build_price_coverage(records, as_of, provider_status=None, target_providers=None, catalogue_offers=None, quote_report=None):
    """Use publication gates; a fresh price never establishes availability."""
    now = report_time(as_of)
    groups = defaultdict(list)
    for row in canonicalize_provider_sources(records):
        provider = _supplier(row.provider)
        if provider == "nebius" or row.gpu_model not in NEBIUS_GPUS:
            continue
        eligible, notices = publication_records([row], now)
        reason = "; ".join(notices)
        status = "reference_only"
        if eligible and is_public_benchmark_eligible(eligible[0]):
            status = "fresh_price"
        elif eligible:
            reason = row.price_basis or "not eligible for the public benchmark"
        if row.available is False:
            status = "reported_unavailable"
        stamp = row.source_observed_at or row.fetched_at
        # A feed with an unknown upstream date must not display retrieval time
        # as its source observation time.
        if row.source_feed in {"computeprices", "shadeform", "skypilot"}:
            stamp = row.source_observed_at
        key = provider, row.gpu_model, row.region or "unknown", row.consumption_type or "unknown"
        groups[key].append({"status": status, "reason": reason,
                            "observed_at": stamp, "age_hours": _age(stamp, now),
                            "feed": row.source_feed or row.provider,
                            "evidence_type": row.price_basis or row.source_type or row.data_source or "unknown",
                            "configuration": " / ".join(str(x) for x in (
                                row.instance_type, f"{row.gpu_count:g} GPUs", row.offer_variant) if x)})
    from offer_catalogue import build_catalogue_report
    catalogue = build_catalogue_report(catalogue_offers or [], now)
    for row in catalogue["offers"]:
        if row["gpu_model"] not in NEBIUS_GPUS: continue
        key = row["provider"], row["gpu_model"], row["region"], row["purchase_type"]
        groups[key].append({"status": "quote_required" if row["price_status"] == "quote_required" else "reference_only",
                            "reason": row["description"], "observed_at": row["observed_at"],
                            "age_hours": row["age_hours"], "feed": "provider catalogue",
                            "evidence_type": row["price_status"], "configuration": row["product_id"]})
    report = _finish("pricing", groups, now, provider_status, target_providers or (provider_status or {}))
    report["commercial_plan_observations"] = sum(1 for row in catalogue["offers"] if row["price_status"] == "plan_terms")
    report["priority_neoclouds"] = build_priority_coverage(report, quote_report)
    return report


def build_capacity_coverage(records, as_of, provider_status=None, target_providers=None):
    from capacity.insights import signal_class
    now = report_time(as_of)
    groups = defaultdict(list)
    for row in records:
        provider = _supplier(row.provider)
        if provider == "nebius" or row.gpu_model not in NEBIUS_GPUS:
            continue
        age = _age(row.fetched_at, now)
        if row.data_source == "aggregator":
            feed = "shadeform"  # the current capacity aggregator collector
        elif row.provider == "crusoe" and row.metric_type == "listed_offering":
            feed = "crusoe_public"
        elif row.provider == "aws":
            feed = "aws_capacity_blocks" if row.metric_type == "lead_time_days" else "aws_spot_advisor"
        else:
            feed = {"gcp": "gcp_zones", "azure": "azure_regions"}.get(row.provider, row.provider)
        source = (provider_status or {}).get(feed, {})
        reason = source.get("reason") or ""
        status = "observed_signal"
        if age is None or age < -24 or age > 48:
            status, reason = "reference_only", "observation outside 48-hour window or date unknown"
        elif source.get("status") in {"failed", "error", "empty", "unknown", "missing", "paused", "cache", "cached", "partial", "pending"}:
            status = "source_problem"
            reason = reason or "source status: " + source["status"]
        elif row.state == "unknown":
            status, reason = "unknown_signal", row.detail
        key = provider, row.gpu_model, row.region or "unknown", row.consumption_type or "unknown"
        groups[key].append({"status": status, "reason": reason,
                            "observed_at": row.fetched_at, "age_hours": age,
                            "feed": feed,
                            "direct_instance": (status == "observed_signal" and source.get("status") == "live"
                                and row.region not in {"", "unknown", "global", "unspecified"}
                                and bool(row.instance_type) and row.data_source == "official_api"
                                and signal_class(row) in {"instance", "instance_stock", "live"}
                                and row.metric_type in {"instance_launchability", "instance_stock_status", "provider_quantity"}),
                            "evidence_type": ("aggregator" if row.data_source == "aggregator" else signal_class(row)) + ": " + row.metric_type,
                            "configuration": row.instance_type or "unspecified"})
    return _finish("capacity", groups, now, provider_status, target_providers or (provider_status or {}))


LABELS = {"fresh_price": "Fresh eligible price", "reference_only": "Reference only", "quote_required": "Quote required",
          "reported_unavailable": "Reported unavailable", "observed_signal": "Observed signal",
          "unknown_signal": "Unknown signal", "source_problem": "Source problem"}


def build_priority_coverage(pricing_report, quote_report=None, capacity_report=None):
    """Count observed cells by evidence class, never coverage of a guessed market."""
    providers = []
    for provider in ("coreweave", "lambda", "crusoe"):
        prices = [c for c in pricing_report["cells"] if c["provider"] == provider]
        quotes = next((p["statuses"] for p in (quote_report or {}).get("providers", [])
                       if p["provider"] == provider), {})
        capacity = [c for c in (capacity_report or {}).get("cells", []) if c["provider"] == provider]
        # Eligibility is established per observation before different feeds and
        # scopes are grouped into one cell. Older saved reports fail closed.
        exact = [c for c in capacity if c.get("direct_instance_observations", 0) > 0]
        providers.append({"provider": provider,
                          "fresh_price_cells": sum(c["statuses"].get("fresh_price", 0) > 0 for c in prices),
                          "quote_required_cells": sum(c["statuses"].get("quote_required", 0) > 0 for c in prices),
                          "qualified_asking_prices": quotes.get("qualified_asking_price", 0),
                          "qualified_signed_deals": quotes.get("qualified_signed_deal", 0),
                          "quotes_requiring_review": sum(v for k, v in quotes.items() if not k.startswith("qualified_")),
                          "direct_instance_availability_cells": len(exact) if capacity_report is not None else None,
                          "footprint_cells": sum(any(e.endswith(": listed_offering") for e in c["evidence_types"]) for c in capacity)
                                              if capacity_report is not None else None})
    return {"pricing_as_of": pricing_report["as_of"], "quotes_as_of": (quote_report or {}).get("as_of"),
            "capacity_as_of": (capacity_report or {}).get("as_of"), "providers": providers,
            "scope": "Counts are observed provider/GPU/region/purchase-type cells, not market completeness or inventory. "
                     "Multiple feeds in one cell count once. A fresh price is not a matched configuration or an available booking. "
                     "Quote qualification checks required fields, not independent verification. "
                     "Direct instance availability includes available and unavailable states; it is not multi-node capacity. "
                     "Unknown, stale, failed, partial and footprint-only availability is excluded from that count."}


def render_priority_coverage(report):
    esc = lambda value: escape(str(value), quote=True)
    h = ['<h3>CoreWeave, Lambda and Crusoe coverage</h3>', '<p>' + esc(report["scope"]) + '</p>',
         '<p>Price source run: ' + esc(report["pricing_as_of"]) + '; quote review: ' + esc(report["quotes_as_of"] or 'not loaded') +
         '; capacity source run: ' + esc(report["capacity_as_of"] or 'not loaded in this price report') + '.</p>',
         '<table><tbody><tr><th>Provider</th><th>Fresh price cells</th><th>Quote-only cells</th>'
         '<th>Qualified asking / signed</th><th>Quotes requiring review</th><th>Direct instance availability cells</th><th>Footprint cells</th></tr>']
    for p in report["providers"]:
        values = (p["provider"], p["fresh_price_cells"], p["quote_required_cells"],
                  f'{p["qualified_asking_prices"]} / {p["qualified_signed_deals"]}', p["quotes_requiring_review"],
                  p["direct_instance_availability_cells"], p["footprint_cells"])
        h.append('<tr>' + ''.join('<td>' + esc(v if v is not None else 'not loaded') + '</td>' for v in values) + '</tr>')
    return '\n'.join(h) + '</tbody></table>'


def render_coverage(report):
    """Confluence-compatible HTML; full coverage stays behind an expand."""
    esc = lambda value: escape(str(value), quote=True)
    h = [f'<p>As of {esc(report["as_of"])}. {report["observed_cells"]} observed cells across '
         f'{len({c["provider"] for c in report["cells"]})} competitors. {esc(report["scope"])}</p>']
    if report.get("priority_neoclouds"):
        h.append(render_priority_coverage(report["priority_neoclouds"]))
    h.append("<p>Fresh price means eligible under the publication rules, not configuration-equivalent "
             "cluster pricing. Observed capacity signals retain their evidence type; a footprint, "
             "marketing label or single-instance signal is not verified multi-node stock.</p>")
    h.append('<table><tbody><tr><th>Competitor</th><th>GPU</th><th>Region</th><th>Purchase type</th>'
             '<th>Coverage</th><th>Configuration</th><th>Latest source observation</th>'
             '<th>Feeds / evidence</th><th>Limits</th></tr>')
    for c in report["cells"]:
        status = "; ".join(f"{LABELS[k]}: {v}" for k, v in c["statuses"].items())
        stamp = c["latest_observed_at"] or "unknown"
        age = c["latest_age_hours"]
        if age is not None:
            stamp += f" ({age:g}h old)"
        values = (c["provider"], c["gpu"], c["region"], c["purchase_type"], status,
                  "; ".join(c["configurations"]), stamp,
                  "; ".join(c["feeds"] + c["evidence_types"]), "; ".join(c["reasons"]))
        h.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in values) + "</tr>")
    h.append("</tbody></table><h3>Unobserved parts of the tracked panel</h3>"
             "<p>These are collection gaps to investigate, not claims that a provider sells every GPU "
             "or purchase type. No geography is invented for an unobserved offer.</p>"
             "<table><tbody><tr><th>Competitor</th><th>GPU</th><th>Unobserved purchase types</th></tr>")
    for g in report["unobserved"]:
        h.append(f'<tr><td>{esc(g["provider"])}</td><td>{esc(g["gpu"])}</td>'
                 f'<td>{esc(", ".join(g["unobserved_purchase_types"]))}</td></tr>')
    h.append("</tbody></table><h3>Collector health</h3><table><tbody>"
             "<tr><th>Source</th><th>Status</th><th>Reason / limit</th></tr>")
    for source in report["source_health"]:
        h.append(f'<tr><td>{esc(source["source"])}</td><td>{esc(source.get("status", "unknown"))}</td>'
                 f'<td>{esc(source.get("reason") or source.get("error") or "")}</td></tr>')
    h.append("</tbody></table>")
    return "\n".join(h)
