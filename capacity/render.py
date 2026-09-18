"""Render scoped capacity observations without converting them into market claims.

Slack leads with matched eight-GPU configuration changes and current coverage;
its thread carries only changes and exceptions. Confluence leads with the
positive/absent/unknown node evidence, with the full source detail expandable.
Single-node, fleet, inference, marketplace and footprint signals stay separate.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from capacity import insights
from capacity.config import (
    CONFLUENCE_BASE_URL, FLAGSHIP_GPUS, FOOTPRINT_ONLY_GPUS, PROVIDER_LABELS,
    SECONDARY_GPUS, SIGNAL_CLASS, PENDING_ACTIVATION,
)
from capacity.insights import plural, region_label
from capacity.schema import AvailabilityRecord, CapacityDiffEntry

logger = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"

PENDING_LABELS = {
    "aws_capacity_blocks": "AWS Capacity Blocks (account needs CB service quota via AWS Support)",
    "hyperstack": "Hyperstack (free key pending)",
    "together": "Together AI (free key pending)",
    "verda": "Verda (free key pending)",
    "lambda": "Lambda (API key pending)",
}

# Method & semantics per provider — rendered on Confluence with the class and
# today's basis so no reader has to guess what a cell means.
METHOD = {
    "lambda":       ("instance",      "instance-types API: exact on-demand instance launchability by region; explicit empty list means no regions reported for that SKU, missing data is unknown; not 1ClickClusters stock, inventory quantities or quota"),
    "scaleway":     ("instance_stock", "public availability API: available / scarce / shortage for each exact GPU VM SKU and zone; not stock quantities, multi-node capacity or GPU-level price bookability"),
    "runpod":       ("live",          "GraphQL stock labels (1x and 8x cluster) + per-datacenter availability"),
    "voltage_park": ("live",          "public locations API: live rentable GPU counts per fabric"),
    "hyperstack":   ("live",          "stock API: per-region counts + restock forecast (pending free key)"),
    "verda":        ("live",          "instance-availability API per location (pending free key)"),
    "together":     ("inference",     "dedicated-inference instance API: replicas per exact instance/region, with exact or lower-bound relation; not raw GPU cluster stock or bookability"),
    "massedcompute": ("live",         "account inventory: exact SKU and reported region; quantity unit unverified; no multi-node or cluster assertion"),
    "aws":          ("spot",          "spot advisor pools (~weekly) now; Capacity Blocks lead time once IAM lands"),
    "vast":         ("marketplace",   "commodity marketplace depth: GPUs listed + floor price, not DC inventory"),
    "sfcompute":    ("marketplace",   "exchange clearing price (short-term reserve); not node stock"),
    "gmi":          ("self_reported", "pricing-page badges (provider-declared, unverifiable) — never counted"),
    "coreweave":    ("footprint",     "docs AZ matrix: where deployed, not whether in stock"),
    "crusoe":       ("footprint",     "docs zone matrix: where offered, not live stock; authenticated quantities shown separately when connected"),
    "gcp":          ("footprint",     "GPU zones docs page: where offered"),
    "azure":        ("footprint",     "retail price API: where priced (can overstate deployment)"),
    "nebius":       ("footprint",     "outside-in: docs region matrix + which SKUs are self-service vs sales-gated"),
    "shadeform":    ("aggregator",    "19-cloud aggregator booleans: fills gaps, marked ✱, never overrides a direct read"),
}

CLASS_LABEL = {
    "live": "live stock", "spot": "spot", "marketplace": "marketplace",
    "self_reported": "self-reported", "footprint": "footprint",
    "aggregator": "aggregator",
    "inference": "dedicated inference",
    "instance": "on-demand instance launchability",
    "instance_stock": "GPU instance stock status",
    "unverified_scope": "product scope unverified",
}


def _fresh_line(manifest: dict) -> Tuple[str, str]:
    """(short slack line, confluence html line)"""
    f = insights.freshness(manifest)
    n_act, n_live = len(f["activated"]), len(f["live"])
    bits = []
    if not f["failed"] and not f["stale"]:
        short = f"Feeds: all {n_live} live"
        color = "green"
    else:
        if f["stale"]:
            bits.append("cached today: " + ", ".join(PROVIDER_LABELS.get(p, p) for p in f["stale"]))
        if f["failed"]:
            bits.append("down: " + ", ".join(PROVIDER_LABELS.get(p, p) for p in f["failed"]))
        short = f"Feeds: {n_live}/{n_act} live ({'; '.join(bits)})"
        color = "yellow"
    pend = f" · {len(f['pending'])} awaiting access" if f["pending"] else ""
    pause = (" · access paused: " + ", ".join(PROVIDER_LABELS.get(p, p) for p in f["paused"])) if f["paused"] else ""
    if f["paused"] and not n_act:
        short = "Feeds: no active checks"
    if f["paused"]:
        color = "yellow"
    html = (f'<span data-type="status" data-color="{color}">'
            f'{n_live}/{n_act} feeds live</span>'
            + (f"<em>{pend}{pause}</em>" if pend or pause else ""))
    return short + pend + pause, html


def _baseline_label(old_records: List[AvailabilityRecord]) -> str:
    ts = [r.fetched_at for r in old_records if r.fetched_at]
    if not ts:
        return "first run"
    try:
        dt = datetime.fromisoformat(max(ts))
        return "since previous run, " + dt.strftime("%d %b %H:%M UTC")
    except ValueError:
        return "since previous run"


def _mark(read: dict) -> str:
    return "✱" if read["aggregator"] else ""


def _node_summary_line(records, manifest=None):
    parts = []
    for gpu in FLAGSHIP_GPUS:
        view = insights.node_summary(records, gpu, manifest)
        if view["reads"]:
            parts.append(f"{gpu} {len(view['available'])}/{len(view['checked'])}"
                         f" (+{len(view['unknown'])} unknown)" if view["checked"] else
                         f"{gpu} unknown ({len(view['unknown'])} sources)")
    return " · ".join(parts) or "No qualified 8-GPU observations"


def _node_change_lines(records, old_records, manifest=None):
    changes, coverage = insights.node_changes(records, old_records, manifest)
    lines = [f"{PROVIDER_LABELS.get(c['provider'], c['provider'])} {c['gpu']}: "
             f"{c['old']} → {c['new']} (observed 8-GPU configurations)" for c in changes]
    return lines, coverage


def _coverage_lines(coverage):
    lines = []
    for c in coverage:
        parts = []
        for key, label in (("added", "now checked"), ("lost", "no longer checked"),
                           ("changed_scope", "configuration coverage changed")):
            if c.get(key):
                names = ", ".join(PROVIDER_LABELS.get(p, p) for p in c[key])
                parts.append(f"{label}: {names}")
        lines.append(c["gpu"] + " — " + "; ".join(parts))
    return lines


def render_slack(records: List[AvailabilityRecord], diff: List[CapacityDiffEntry],
                 manifest: dict, old_records: List[AvailabilityRecord]) -> Tuple[str, str]:
    day = manifest.get("run_date") or datetime.now(timezone.utc).date().isoformat()
    changes, coverage = _node_change_lines(records, old_records, manifest)
    lines = [f"*GPU Capacity Daily — {day}*"]
    if changes:
        lines.append("*Changed:* " + " · ".join(changes[:3]))
        if len(changes) > 3:
            lines.append(f"{len(changes) - 3} more changes in thread.")
    else:
        lines.append("No 8-GPU availability changes across matched, checked sources."
                     if old_records else "Baseline: no previous observations to compare.")
    lines.append("*8-GPU node signals:* " + _node_summary_line(records, manifest))
    if coverage:
        gpus = list(dict.fromkeys(c["gpu"] for c in coverage))
        lines.append("*Coverage changed:* " + ", ".join(gpus) + "; details in thread.")
    lines.append("_Positive / checked providers; unknown excluded from denominator. "
                 "Single-node signals; multi-node capacity unverified._")
    fresh, _ = _fresh_line(manifest)
    url = _page_url()
    lines.append(f"_{fresh}_" + (f" · <{url}|full evidence and method>" if url else ""))
    return "\n".join(lines), _render_thread(records, diff, manifest, old_records)


# ── Slack thread ─────────────────────────────────────────────────────────────

def _normalize_partial(provider: str, detail: str) -> str:
    """Preserve the observed scope; limited does not mean one-GPU-only."""
    return detail or "Availability scope unreported"


def _short(detail: str, limit: int = 70) -> str:
    d = (detail or "").split(";")[0].strip()
    return d if len(d) <= limit else d[:limit - 1] + "…"


def _change_scope_verified(c: CapacityDiffEntry, records: List[AvailabilityRecord]) -> bool:
    verifier = {"together": insights.is_together_inference,
                "lambda": insights.is_lambda_instance,
                "scaleway": insights.is_scaleway_instance}.get(c.provider)
    if verifier is None:
        return True
    return any(verifier(r)
               and (r.gpu_model, r.region, r.consumption_type, r.instance_type)
               == (c.gpu_model, c.region, c.consumption_type, c.instance_type)
               for r in records)


def _describe_change(c: CapacityDiffEntry,
                     records: List[AvailabilityRecord] = None) -> str:
    prov = PROVIDER_LABELS.get(c.provider, c.provider)
    record = next((r for r in (records or [])
                   if (r.provider, r.gpu_model, r.region, r.consumption_type, r.instance_type)
                   == (c.provider, c.gpu_model, c.region, c.consumption_type, c.instance_type)), None)
    cls = insights.signal_class(record) if record else SIGNAL_CLASS.get(c.provider, "footprint")
    if c.provider == "together":
        if record and insights.is_together_inference(record):
            return (f"{prov} dedicated inference {c.instance_type} {c.region}: "
                    f"current replica headroom {_inference_headroom(record)} "
                    f"(observed {_observed_time(record.fetched_at)}; not raw GPU cluster stock)")
        return f"{prov} {c.gpu_model}: legacy product scope unverified; excluded from capacity comparisons"
    if c.provider == "lambda":
        if record and insights.is_lambda_instance(record):
            return (f"{prov} on-demand instance {c.instance_type} ({plural(record.gpu_count, 'GPU')}/instance) "
                    f"{c.region}: {_lambda_launchability(record)} "
                    f"(observed {_observed_time(record.fetched_at)}; not 1ClickClusters stock)")
        return f"{prov} {c.gpu_model}: legacy product scope unverified; excluded from capacity comparisons"
    if c.provider == "scaleway":
        if record and insights.is_scaleway_instance(record):
            transition = (f"{c.old_state} → {c.new_state}; "
                          if c.change_type == "state_change" else "")
            return (f"{prov} {c.instance_type} ({plural(record.gpu_count, 'GPU')}/instance) "
                    f"{c.region}: {transition}{_scaleway_stock_status(record)} "
                    f"(observed {_observed_time(record.fetched_at)}; single-instance status only)")
        return f"{prov} {c.gpu_model}: legacy product scope unverified; excluded from capacity comparisons"
    if record and insights.is_crusoe_api_quantity(record):
        scope = f"{prov} {c.gpu_model} {c.instance_type} {c.region}"
        if (c.change_type in {"state_change", "metric_move"}
                and c.old_value is not None and c.new_value is not None):
            return (f"{scope}: API quantity {c.old_value:g} → {c.new_value:g} "
                    "(provider units; this SKU/location only)")
        return f"{scope}: {c.detail} (API quantity; this SKU/location only)"
    tag = {"live": "live", "spot": "spot", "marketplace": "marketplace",
           "self_reported": "badge", "footprint": "footprint"}[cls]
    # A "live" tag on an aggregator-sourced row overstates trust — mark it.
    if cls == "live" and records is not None:
        for r in records:
            if (r.provider == c.provider and r.gpu_model == c.gpu_model
                    and r.region == c.region and r.instance_type == c.instance_type):
                if r.data_source == "aggregator":
                    tag = "via aggregator ✱"
                break
    where = (f" {c.instance_type}" if c.instance_type else "") + (f" {c.region}" if c.region and c.region != "global" else "")
    human = {"available": "positive signal", "limited": "limited signal", "sold_out": "zero reported",
             "not_offered": "not offered", "unknown": "unknown"}
    metric_name = {"offer_depth_gpus": "listed depth", "stock_level": "stock",
                   "regions_with_capacity": "regions with capacity",
                   "clearing_price_usd": "clearing price", "lead_time_days": "lead time"}
    if c.change_type == "state_change":
        suffix = "" if tag == "live" else f" ({tag})"
        scope = ""
        if record and record.provider == "hyperstack" and record.metric_type == "stock_level":
            scope = "aggregate GPU count"
        elif record and record.provider == "runpod" and record.region == "global":
            scope = "1x/8x stock labels"
        elif record and record.provider == "verda":
            scope = "deployable instance sizes"
        scoped = f" ({scope})" if scope else " (provider signal)"
        current = f"; now {record.detail}" if scope and record.detail else ""
        return (f"{prov} {c.gpu_model}{where}{scoped}: {human.get(c.old_state, c.old_state)} → "
                f"{human.get(c.new_state, c.new_state)}{current}{suffix}")
    if c.change_type == "metric_move":
        name = next((v for k, v in metric_name.items() if k in (c.detail or "")), None)
        if name and c.old_value is not None and c.new_value is not None:
            capped = "+, page-capped" if "page cap" in (c.detail or "") else ""
            suffix = "" if tag == "live" else f" ({tag})"
            return (f"{prov} {c.gpu_model}{where}: {name} "
                    f"{c.old_value:g} → {c.new_value:g}{capped}{suffix}")
        return f"{prov} {c.gpu_model}{where}: {c.detail} ({tag})"
    if c.change_type == "added":
        return f"{prov} {c.gpu_model}{where}: now tracked, {human.get(c.new_state, c.new_state)} ({tag})"
    return f"{prov} {c.gpu_model}{where}: {c.detail} ({tag})"


def _render_thread(records, diff, manifest, old_records) -> str:
    lines = [f"*Changes and exceptions — {_baseline_label(old_records)}*", ""]
    node_changes, coverage = _node_change_lines(records, old_records, manifest)
    lines.extend("• " + text for text in node_changes)
    lines.extend("• Coverage: " + text for text in _coverage_lines(coverage))
    changes = [c for c in diff if c.change_type in {"state_change", "metric_move"}
               and _change_scope_verified(c, records)]
    # Only actual observations that changed; static instance inventories live on
    # Confluence. Do not silently merge different shapes into a provider claim.
    scoped = [c for c in changes if c.provider in {"lambda", "scaleway", "together"}]
    if any(c.provider == "lambda" for c in scoped):
        lines.append("_Changes apply only to the named instance and region:_")
    if any(c.provider == "scaleway" for c in scoped):
        lines.append("_Changes apply only to the named instance and zone:_")
    material = scoped + [c for c in changes if c.region == "global"
                         and c.provider not in {"lambda", "scaleway", "together"}
                         and not _change_is_aggregator(c, records, old_records)]
    for c in material[:8]:
        lines.append("• " + _describe_change(c, records))
    if len(material) > 8:
        lines.append(f"_{len(material) - 8} further changes in the Confluence evidence._")
    if not node_changes and not material:
        lines.append("• No material changes in observed configurations.")

    # Failures, caches, scope exclusions and access holds remain visible. These
    # are exceptions, not a daily dump of every healthy provider's inventory.
    for provider, status in manifest.get("provider_status", {}).items():
        if status.get("status") not in {"live"}:
            label = {"aws_capacity_blocks": "AWS Capacity Blocks"}.get(provider, PROVIDER_LABELS.get(provider, provider))
            lines.append(f"• {label}: {_provider_read_freshness(provider, manifest)}; current availability unknown.")
            if provider == "crusoe" and status.get("status") == "paused":
                lines.append("_Historical API cache is not reused. Aggregator observations remain separate._")
            rows = [r for r in records if r.provider == provider and r.data_source == "official_api"]
            if status.get("status") in {"cached", "cache", "failed"} and rows:
                times = sorted({_observed_time(r.fetched_at) for r in rows})
                lines.append("_Observed: " + "; ".join(times) + "_")
                if len(times) > 1:
                    lines.append("_" + "; ".join("observed " + time for time in times) + "_")
                # One exact example is enough to explain the cached exception.
                row = rows[0]
                if insights.is_lambda_instance(row):
                    lines.append(f"• {row.instance_type} · {plural(row.gpu_count, 'GPU')}/instance: {_lambda_launchability(row)}")
                elif insights.is_together_inference(row):
                    lines.append(f"• {row.instance_type} · {plural(row.gpu_count, 'GPU') if row.gpu_count else 'Unreported GPU count'}/replica: {_inference_headroom(row)}")
    for provider, verifier in (("lambda", insights.is_lambda_instance),
                               ("scaleway", insights.is_scaleway_instance),
                               ("together", insights.is_together_inference)):
        legacy = [r for r in records if r.provider == provider and not verifier(r)]
        if legacy:
            qualifier = "legacy or aggregator observations excluded" if provider == "scaleway" else "legacy observations excluded"
            lines.append(f"• {PROVIDER_LABELS[provider]}: {len(legacy)} {qualifier}; exact product scope unverified.")
    lines.append("_Complete observations, timestamps, sources and all changes are on Confluence. "
                 "Unknown availability is not zero stock; no multi-node or customer-quota claim._")
    return "\n".join(lines)


def _change_is_aggregator(c: CapacityDiffEntry,
                          records: List[AvailabilityRecord],
                          old_records: List[AvailabilityRecord]) -> bool:
    for pool in (records, old_records):
        for r in pool:
            if (r.provider == c.provider and r.gpu_model == c.gpu_model
                    and r.region == c.region and r.consumption_type == c.consumption_type
                    and r.instance_type == c.instance_type):
                return r.data_source == "aggregator"
    return False


def _variant_tag(instance_type: str, gpu: str) -> str:
    """Short SKU-variant label: 'NVIDIA H100 80GB HBM3' → 'HBM3',
    '... Workstation Edition' → 'Workstation Edition'."""
    import re as _re
    t = (instance_type or "")
    for junk in ("NVIDIA", gpu, "PRO 6000", "RTX", "Blackwell"):
        t = t.replace(junk, " ")
    t = _re.sub(r"\d+\s*GB", " ", t, flags=_re.I)
    t = _re.sub(r"\s+", " ", t).strip(" -_.")
    words = t.split()
    return " ".join(words[-2:]) if words else "variant"


# ── Confluence ───────────────────────────────────────────────────────────────

def _esc(s) -> str:
    from html import escape
    return escape(str(s), quote=True)


def _status(color: str, text: str) -> str:
    return f'<span data-type="status" data-color="{color}">{_esc(text)}</span>'


def _read_for(t: dict) -> Tuple[str, str]:
    return "neutral", "Observed provider signals; node scope must be checked separately"


def _observed_time(value: str) -> str:
    if not value:
        return "timestamp not recorded"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        pass
    return value + " (timezone unconfirmed)"


def _provider_read_freshness(provider: str, manifest: dict = None) -> str:
    status = (manifest or {}).get("provider_status", {}).get(provider, {})
    state = status.get("status")
    age = status.get("cache_age_hours")
    age_note = f" (cache age {age:g}h)" if isinstance(age, (int, float)) else ""
    if state in {"cached", "cache"}:
        return "Cached observation; not refreshed this run" + age_note
    if state in {"failed", "error"}:
        if provider in PENDING_ACTIVATION:
            return "API access pending; no fresh observations" + age_note
        return "Fetch failed; observations not refreshed this run" + age_note
    if state == "paused":
        return "Access paused: " + status.get("reason", "authenticated checks suspended pending review")
    if state == "live":
        return "Fetched in this run; point-in-time observations"
    return "Refresh status not supplied; use each observation timestamp"


def _crusoe_read_freshness(manifest: dict = None) -> str:
    return _provider_read_freshness("crusoe", manifest)


def _scaleway_stock_status(row: AvailabilityRecord) -> str:
    return {"available": "available", "limited": "scarce", "sold_out": "shortage",
            "unknown": "stock status unknown"}.get(row.state, "stock status unknown")


def _scaleway_instance_table(records: List[AvailabilityRecord], manifest: dict = None) -> str:
    rows = insights.scaleway_instance_records(records)
    legacy = [r for r in records if r.provider == "scaleway" and not insights.is_scaleway_instance(r)]
    if not rows and not legacy and "scaleway" not in (manifest or {}).get("provider_status", {}):
        return ""
    h = ["<h2>Scaleway — stock by exact GPU instance and zone</h2>",
         f"<p><strong>{_esc(_provider_read_freshness('scaleway', manifest))}</strong></p>",
         "<p>Provider stock labels for the named single-instance SKU and zone. A small VM's availability "
         "does not apply to an 8-GPU node, and an 8-GPU node does not establish multi-node stock. "
         "Labels are not quantities and do not establish account quota. Exact eight-GPU shapes enter the single-node summary; excluded from multi-node claims, "
         "provider-wide stock claims and GPU-level price/bookability joins; listed prices remain independent.</p>"]
    if legacy:
        h.append(f"<p>{len(legacy)} legacy or aggregator Scaleway observations have unverified exact SKU scope "
                 "and are excluded from capacity comparisons.</p>")
    if not rows:
        h.append("<p>No verified exact-instance observations available.</p>")
        return "\n".join(h)
    h.extend(['<table data-layout="full-width"><tbody>',
              "<tr><th>GPU</th><th>Exact instance</th><th>GPUs per instance</th><th>Zone</th>"
              "<th>Stock status</th><th>Observed at</th><th>Evidence</th></tr>"])
    for row in rows:
        source = f'<a href="{_esc(row.source_url)}">Source</a>' if row.source_url else ""
        h.append(f"<tr><td>{_esc(row.gpu_model)}</td><td>{_esc(row.instance_type)}</td>"
                 f"<td>{row.gpu_count}</td><td>{_esc(row.region)}</td>"
                 f"<td>{_esc(_scaleway_stock_status(row))}</td>"
                 f"<td>{_esc(_observed_time(row.fetched_at))}</td>"
                 f"<td>{_esc(row.detail)} {source}</td></tr>")
    h.append("</tbody></table>")
    return "\n".join(h)


def _lambda_launchability(row: AvailabilityRecord) -> str:
    if row.state == "unknown" or row.metric_value is None:
        return "launchability unknown"
    if row.metric_type == "launchable_regions":
        return "none reported" if row.metric_value == 0 else f"{plural(row.metric_value, 'region')} reported"
    return "launchable in this region" if row.state == "available" else "not reported launchable in this region"


def _lambda_instance_views(records: List[AvailabilityRecord]) -> list:
    """One display row per exact shape/observation, not a cross-shape rollup."""
    groups = {}
    for row in insights.lambda_instance_records(records):
        key = (row.gpu_model, row.instance_type, row.gpu_count, row.fetched_at)
        groups.setdefault(key, []).append(row)
    views = []
    for rows in groups.values():
        summary = next((r for r in rows if r.region == "global"), None)
        regions = sorted({r.region for r in rows if r.region != "global" and r.state == "available"})
        if summary is None or summary.state == "unknown" or summary.metric_value is None:
            text = "complete availability unknown"
            if regions:
                text += "; reported regions: " + ", ".join(regions)
        elif summary.metric_value == 0:
            text = "none reported"
        elif regions:
            text = ", ".join(regions)
        else:
            text = _lambda_launchability(summary) + "; region names unreported"
        views.append((summary or rows[0], text))
    return views


def _lambda_instance_table(records: List[AvailabilityRecord], manifest: dict = None) -> str:
    views = _lambda_instance_views(records)
    legacy = [r for r in records if r.provider == "lambda" and not insights.is_lambda_instance(r)]
    if not views and not legacy and "lambda" not in (manifest or {}).get("provider_status", {}):
        return ""
    h = ["<h2>Lambda — on-demand instance launchability</h2>",
         f"<p><strong>{_esc(_provider_read_freshness('lambda', manifest))}</strong></p>",
         "<p>Launchability of each exact on-demand instance, <strong>not 1ClickClusters stock</strong>. "
         "Even an 8-GPU instance is a single node. Listed regions do not guarantee account quota "
         "or simultaneous launches. Region counts are not GPU or instance inventory quantities. "
         "Shapes are not summed; excluded from cluster tightness, provider-wide sellout claims "
         "and GPU-level price/bookability joins.</p>"]
    if legacy:
        h.append(f"<p>{len(legacy)} legacy Lambda observations have unverified product scope "
                 "and are excluded from capacity comparisons.</p>")
    if not views:
        h.append("<p>No verified exact-instance observations available.</p>")
        return "\n".join(h)
    h.extend(['<table data-layout="full-width"><tbody>',
              "<tr><th>GPU</th><th>Exact instance</th><th>GPUs per instance</th>"
              "<th>Reported launchable regions</th><th>Observed at</th><th>Evidence</th></tr>"])
    for row, region_text in views:
        source = f'<a href="{_esc(row.source_url)}">Source</a>' if row.source_url else ""
        h.append(f"<tr><td>{_esc(row.gpu_model)}</td><td>{_esc(row.instance_type)}</td>"
                 f"<td>{row.gpu_count}</td><td>{_esc(region_text)}</td>"
                 f"<td>{_esc(_observed_time(row.fetched_at))}</td>"
                 f"<td>{_esc(row.detail)} {source}</td></tr>")
    h.append("</tbody></table>")
    return "\n".join(h)


def _inference_headroom(record: AvailabilityRecord) -> str:
    value = record.metric_value
    relation = getattr(record, "quantity_relation", "")
    if record.state == "unknown" or value is None or relation not in {"RELATION_EQ", "RELATION_GTE"}:
        return "unknown replica headroom"
    unit = "replica" if value == 1 else "replicas"
    if relation == "RELATION_GTE":
        return f"≥{value:g} {unit} (lower bound)"
    return f"{value:g} {unit} (exact)"


def _together_inference_table(records: List[AvailabilityRecord], manifest: dict = None) -> str:
    rows = insights.together_inference_records(records)
    legacy = [r for r in records if r.provider == "together" and not insights.is_together_inference(r)]
    if not rows and not legacy:
        return ""
    h = ["<h2>Together AI — dedicated-inference replica headroom</h2>",
         f"<p><strong>{_esc(_provider_read_freshness('together', manifest))}</strong></p>",
         "<p>This API describes dedicated inference, <strong>not raw GPU rental or "
         "training-cluster stock</strong>. Headroom is replicas of the exact configuration "
         "in the reported region; a lower bound is not an exact inventory count. "
         "Configurations are not summed or converted into available GPU totals. "
         "Excluded from cluster tightness, fleet-wide sellout claims and GPU-rental price comparisons.</p>"]
    if legacy:
        h.append(f"<p>{len(legacy)} legacy Together observations have unverified product scope "
                 "and are excluded from capacity comparisons.</p>")
    if rows:
        h.extend(['<table data-layout="full-width"><tbody>',
                  "<tr><th>GPU</th><th>Exact instance</th><th>GPUs per replica</th>"
                  "<th>Region</th><th>Replica headroom</th><th>Observed at</th><th>Evidence</th></tr>"])
        for row in rows:
            count = getattr(row, "gpu_count", None)
            count_label = str(count) if count is not None else "Unreported"
            source = f'<a href="{_esc(row.source_url)}">Source</a>' if row.source_url else ""
            h.append(f"<tr><td>{_esc(row.gpu_model)}</td><td>{_esc(row.instance_type)}</td>"
                     f"<td>{_esc(count_label)}</td><td>{_esc(row.region)}</td>"
                     f"<td>{_esc(_inference_headroom(row))}</td>"
                     f"<td>{_esc(_observed_time(row.fetched_at))}</td>"
                     f"<td>{_esc(row.detail)} {source}</td></tr>")
        h.append("</tbody></table>")
    return "\n".join(h)


def _crusoe_quantity_table(records: List[AvailabilityRecord], manifest: dict = None) -> str:
    if (manifest or {}).get("provider_status", {}).get("crusoe", {}).get("status") == "paused":
        return ("<h2>Crusoe — authenticated access paused</h2>"
                f"<p><strong>{_esc(_crusoe_read_freshness(manifest))}</strong></p>"
                "<p>Authoritative availability unknown; not zero stock. Historical API cache is not reused. "
                "Any observations via an aggregator remain separate.</p>")
    rows = insights.crusoe_quantity_records(records)
    if not rows:
        return ""
    h = ["<h2>Crusoe API — quantities by exact instance and location</h2>",
         f"<p><strong>{_esc(_crusoe_read_freshness(manifest))}</strong></p>",
         "<p>Raw provider quantities, <strong>not GPU counts</strong>. Alternative "
         "instance shapes or slices can draw from overlapping capacity, so quantities "
         "are not summed. A positive value does not establish account quota, reservation "
         "eligibility or multi-node stock. Zero applies only to the reported SKU/location. "
         "These rows are excluded from cluster-stock totals and the price/bookability "
         "comparison until an exact offer match is established.</p>",
         '<table data-layout="full-width"><tbody>',
         "<tr><th>GPU</th><th>Exact instance</th><th>Location</th>"
         "<th>Quantity (provider units)</th><th>API observation</th>"
         "<th>Observed at</th><th>Evidence</th></tr>"]
    for row in rows:
        value = f"{row.metric_value:g}" if row.metric_value is not None else "Unreported"
        state = {"available": "Positive quantity", "sold_out": "Zero reported", "unknown": "Observation unconfirmed"}.get(row.state, row.state)
        source = f'<a href="{_esc(row.source_url)}">Source</a>' if row.source_url else ""
        h.append(f"<tr><td>{_esc(row.gpu_model)}</td><td>{_esc(row.instance_type)}</td>"
                 f"<td>{_esc(row.region)}</td><td>{_esc(value)}</td><td>{_esc(state)}</td>"
                 f"<td>{_esc(_observed_time(row.fetched_at))}</td>"
                 f"<td>{_esc(row.detail)} {source}</td></tr>")
    h.append("</tbody></table>")
    return "\n".join(h)


def render_confluence(records: List[AvailabilityRecord],
                      diff: List[CapacityDiffEntry],
                      manifest: dict,
                      old_records: List[AvailabilityRecord]) -> str:
    day = manifest.get("run_date") or datetime.now(timezone.utc).date().isoformat()
    completed = manifest.get("completed_at") or ""
    _, fresh_html = _fresh_line(manifest)
    changes, coverage = _node_change_lines(records, old_records, manifest)
    h = [f"<p><strong>GPU capacity observations — {_esc(day)}</strong></p>",
         f"<p>Run completed: {_esc(_observed_time(completed))}. {fresh_html}</p>",
         "<h2>Eight-GPU node availability</h2>",
         "<p>Positive signals for one eight-GPU instance, across the configurations observed. "
         "An absent signal applies only to those configurations. Unknown sources are excluded "
         "from the checked denominator. Multi-node capacity, account quota and actual launch "
         "success remain unverified; these observations do not establish customer-facing claims.</p>",
         '<table data-layout="full-width"><tbody>',
         "<tr><th>GPU</th><th>Positive / checked providers</th><th>Positive</th>"
         "<th>Absent in observed configurations</th><th>Unknown 8-GPU availability</th></tr>"]
    for gpu in FLAGSHIP_GPUS + SECONDARY_GPUS + FOOTPRINT_ONLY_GPUS:
        view = insights.node_summary(records, gpu, manifest)
        if not view["reads"]:
            continue
        names = lambda key: ", ".join(r["label"] for r in view[key]) or "—"
        ratio = f"{len(view['available'])}/{len(view['checked'])}" if view["checked"] else "Unknown"
        h.append(f"<tr><td><strong>{gpu}</strong></td><td>{ratio}</td>"
                 f"<td>{_esc(names('available'))}</td><td>{_esc(names('absent'))}</td>"
                 f"<td>{_esc(names('unknown'))}</td></tr>")
    h.append("</tbody></table>")
    h.append(f"<h2>What changed ({_esc(_baseline_label(old_records))})</h2>")
    if changes or coverage:
        h.append("<ul>" + "".join(f"<li>{_esc(t)}</li>" for t in changes) +
                 "".join(f"<li>Coverage: {_esc(t)}</li>" for t in _coverage_lines(coverage)) + "</ul>")
    else:
        h.append("<p>No 8-GPU changes across matched, checked sources.</p>" if old_records else
                 "<p>Baseline: no previous observations to compare.</p>")
    h.append("<p>RunPod Low at gpuCount=8 remains a positive node signal. Hyperstack aggregate "
             "GPU counts do not establish a contiguous node. Lambda and Scaleway exact "
             "eight-GPU instances are included as single-node observations. Dedicated inference, "
             "marketplace listings and aggregator booleans cannot establish node availability.</p>")
    h.append('<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">'
             'Provider evidence, market context and methodology</ac:parameter><ac:rich-text-body>')

    # 5 — Live matrix
    h.append("<h2>Live-Stock Matrix</h2>")
    h.append("<p><em>Only providers with a live or self-reported signal. "
             "✱ = via aggregator today (direct feed pending or down). GMI is "
             "provider-declared and never counted in verdicts.</em></p>")
    live_provs = [p for p in ("runpod", "voltage_park",
                              "verda", "hyperstack", "gmi")
                  if any(r.provider == p for r in records)]
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>GPU</th>" + "".join(f"<th>{_esc(PROVIDER_LABELS[p])}"
             + (" (badge)" if p == "gmi" else "") + "</th>" for p in live_provs) + "</tr>")
    state_disp = {"available": ("green", "Positive signal"), "limited": ("yellow", "Limited signal"),
                  "sold_out": ("red", "Zero reported"), "unknown": ("neutral", "?"),
                  "not_offered": ("neutral", "—")}
    for gpu in FLAGSHIP_GPUS + SECONDARY_GPUS:
        row = [f"<td><strong>{gpu}</strong></td>"]
        any_cell = False
        for p in live_provs:
            rows = [r for r in records if r.provider == p and r.gpu_model == gpu
                    and r.region == "global" and r.consumption_type == "on_demand"]
            if not rows:
                row.append("<td>—</td>")
                continue
            direct = [r for r in rows if r.data_source != "aggregator"]
            r0 = min(direct or rows, key=lambda r: {"available": 0, "limited": 1, "sold_out": 2}.get(r.state, 3))
            color, label = state_disp.get(r0.state, ("neutral", r0.state))
            mark = "✱" if r0.data_source == "aggregator" else ""
            cell = _status(color, label) + mark
            if r0.state != "not_offered":
                cell += f"<br/><em>{_esc(_short(r0.detail, 60))}</em>"
                any_cell = True
            row.append(f"<td>{cell}</td>")
        if any_cell:
            h.append("<tr>" + "".join(row) + "</tr>")
    h.append("</tbody></table>")

    # 6 — Footprint table (neutral)
    h.append(_crusoe_quantity_table(records, manifest))
    h.append(_together_inference_table(records, manifest))
    h.append(_lambda_instance_table(records, manifest))
    h.append(_scaleway_instance_table(records, manifest))
    h.append("<h2>Offering Footprint — where it is sold (NOT whether in stock)</h2>")
    fp_provs = ["coreweave", "crusoe", "gcp", "azure", "nebius"]
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>GPU</th>" + "".join(f"<th>{_esc(PROVIDER_LABELS[p])}</th>"
                                          for p in fp_provs) + "</tr>")
    unit = {"coreweave": "AZ", "crusoe": "zone", "gcp": "zone",
            "azure": "priced region", "nebius": "region"}
    for gpu in FLAGSHIP_GPUS + SECONDARY_GPUS + FOOTPRINT_ONLY_GPUS:
        row, any_cell = [f"<td><strong>{gpu}</strong></td>"], False
        for p in fp_provs:
            rows = [r for r in records if r.provider == p and r.gpu_model == gpu
                    and r.region == "global" and r.data_source != "aggregator"
                    and insights.signal_class(r) == "footprint"]
            if not rows or rows[0].state == "not_offered":
                row.append("<td>—</td>")
                continue
            n = rows[0].metric_value or 0
            extra = ""
            if p == "nebius" and "sales-gated" in rows[0].detail:
                extra = " (sales-gated)"
            row.append(f"<td>{plural(int(n), unit[p])}{_esc(extra)}</td>")
            any_cell = True
        if any_cell:
            h.append("<tr>" + "".join(row) + "</tr>")
    h.append("</tbody></table>")

    # 7 — Market gauges
    h.append("<h2>Market Gauges</h2>")
    h.append('<table data-layout="default"><tbody>')
    h.append("<tr><th>GPU</th><th>SF Compute clearing (short-term reserve)</th>"
             "<th>Vast depth (commodity)</th><th>AWS spot pools</th></tr>")
    for gpu in FLAGSHIP_GPUS + SECONDARY_GPUS:
        g = insights.market_gauges(records, gpu)
        if not any(g.get(k) for k in ("sfc_clearing", "vast_gpus", "aws_spot_regions")):
            continue
        sfc = f"${g['sfc_clearing']:.2f}" if g.get("sfc_clearing") else "no trades"
        vast = (f"{g['vast_gpus']} GPUs" + (f", floor {g['vast_floor']}" if g.get("vast_floor") else "")
                ) if g.get("vast_gpus") is not None else "—"
        spot = plural(g["aws_spot_regions"], "region") if g.get("aws_spot_regions") else "—"
        h.append(f"<tr><td><strong>{gpu}</strong></td><td>{_esc(sfc)}</td>"
                 f"<td>{_esc(vast)}</td><td>{_esc(spot)}</td></tr>")
    h.append("</tbody></table>")

    # 8 — Changes
    baseline = _baseline_label(old_records)
    h.append(f"<h2>Changes ({_esc(baseline)})</h2>")
    scoped_diff = [c for c in diff if _change_scope_verified(c, records)]
    material = [c for c in scoped_diff if c.change_type == "state_change" and c.region == "global"
                and SIGNAL_CLASS.get(c.provider) in ("live", "marketplace")]
    other = [c for c in scoped_diff if c not in material and c.change_type in ("state_change", "metric_move")]
    if material:
        h.append("<p><strong>Material (live/marketplace, provider-level):</strong></p><ul>")
        for c in material:
            h.append(f"<li>{_esc(_describe_change(c, records))}</li>")
        h.append("</ul>")
    if other:
        h.append(f"<p><strong>Other ({len(other)}):</strong></p><ul>")
        for c in other:
            h.append(f"<li>{_esc(_describe_change(c, records))}</li>")
        h.append("</ul>")
    if not material and not other:
        h.append("<p><em>No changes since the previous build.</em></p>")

    # 9 — Provider detail (expand macro, deduped, with SKU column)
    h.append("<h2>Provider Detail</h2>")
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>Provider</th><th>GPU</th><th>Region</th><th>SKU</th><th>Type</th>"
             "<th>State</th><th>Signal</th><th>Class</th><th>Observed (UTC)</th><th>Source</th></tr>")
    seen = set()
    order = {p: i for i, p in enumerate(PROVIDER_LABELS)}
    for r in sorted(records, key=lambda r: (order.get(r.provider, 99), r.gpu_model, r.region)):
        key = (r.provider, r.gpu_model, r.region, r.consumption_type, r.instance_type, r.state,
               r.fetched_at, r.data_source, r.metric_type, r.metric_value, r.detail)
        if key in seen:
            continue
        seen.add(key)
        cls = "aggregator" if r.data_source == "aggregator" else insights.signal_class(r)
        color, label = state_disp.get(r.state, ("neutral", r.state))
        class_label = CLASS_LABEL.get(cls, cls)
        if cls == "footprint":
            color, label = "neutral", "Listed" if r.metric_type == "listed_offering" else "Unknown"
        if insights.is_crusoe_api_quantity(r):
            color = "neutral"
            label = {"available": "Positive quantity", "sold_out": "Zero reported"}.get(r.state, "Unconfirmed")
            class_label = "API quantity (exact SKU/location)"
        detail = r.detail
        if r.provider == "together":
            color = "neutral"
            if insights.is_together_inference(r):
                label, class_label = _inference_headroom(r), "dedicated inference"
            else:
                label, class_label = "Unverified", "product scope unverified"
                detail = "Legacy observation excluded from capacity comparisons"
        if r.provider == "lambda":
            color = "neutral"
            if insights.is_lambda_instance(r):
                label, class_label = _lambda_launchability(r), "on-demand instance launchability"
            else:
                label, class_label = "Unverified", "product scope unverified"
                detail = "Legacy observation excluded from capacity comparisons"
        if r.provider == "scaleway":
            color = "neutral"
            if insights.is_scaleway_instance(r):
                label, class_label = _scaleway_stock_status(r), "GPU instance stock status"
            else:
                label, class_label = "Unverified", "exact SKU scope unverified"
                detail = "Legacy or aggregator observation excluded from capacity comparisons"
        h.append(f"<tr><td>{_esc(PROVIDER_LABELS.get(r.provider, r.provider))}</td>"
                 f"<td><strong>{_esc(r.gpu_model)}</strong></td><td>{_esc(r.region)}</td>"
                 f"<td>{_esc(r.instance_type or '—')}</td>"
                 f"<td>{_esc(r.consumption_type)}</td>"
                 f"<td>{_status(color, label)}</td>"
                 f"<td><em>{_esc(detail)}</em></td>"
                 f"<td>{_esc(class_label)}</td><td>{_esc(_observed_time(r.fetched_at))}</td>"
                 f'<td><a href="{_esc(r.source_url)}">{_esc(r.data_source)}</a></td></tr>')
    h.append("</tbody></table>")

    # 10 — Method
    h.append("<h2>Method &amp; Signal Classes</h2>")
    f = insights.freshness(manifest)
    basis = {}
    for p, s in manifest.get("provider_status", {}).items():
        prov_key = {"gcp_zones": "gcp", "azure_regions": "azure",
                    "aws_spot_advisor": "aws", "aws_capacity_blocks": "aws"}.get(p, p)
        basis.setdefault(prov_key, s.get("status"))
    h.append('<table data-layout="full-width"><tbody>')
    h.append("<tr><th>Provider</th><th>Class</th><th>Signal &amp; semantics</th>"
             "<th>Today's basis</th></tr>")
    for prov, (cls, sem) in METHOD.items():
        if (prov == "together" and any(r.provider == prov for r in records)
                and not insights.together_inference_records(records)):
            cls = "unverified_scope"
            sem = "Legacy observations have unverified product scope; excluded from live GPU/cluster stock and pricing comparisons."
        if (prov == "lambda" and any(r.provider == prov for r in records)
                and not insights.lambda_instance_records(records)):
            cls = "unverified_scope"
            sem = "Legacy observations have unverified product scope; excluded from live GPU/cluster stock and bookability comparisons."
        if (prov == "scaleway" and any(r.provider == prov for r in records)
                and not insights.scaleway_instance_records(records)):
            cls = "unverified_scope"
            sem = "Legacy or aggregator observations have unverified exact SKU scope; excluded from cluster stock and bookability comparisons."
        if prov == "crusoe" and insights.crusoe_quantity_records(records):
            cls = "API quantity (exact SKU/location)"
            sem = ("Authenticated /v1/capacities: raw quantity per instance/location; "
                   "provider units, not GPU counts. Alternative shapes/slices may overlap; "
                   "no cluster totals, account-quota claim or price join.")
            if any(r.provider == prov and insights.signal_class(r) == "footprint" for r in records):
                sem += " Documentation records remain footprint only."
        b = basis.get(prov, "—")
        if b == "paused":
            cls = "access paused"
            sem = _provider_read_freshness(prov, manifest) + "; authoritative availability unavailable, not zero stock."
        if prov in ("hyperstack", "verda") and prov in PENDING_ACTIVATION and b == "failed":
            b = "pending key (via Shadeform ✱)"
        elif b == "failed" and prov in {p for p in PENDING_ACTIVATION}:
            b = "pending activation"
        h.append(f"<tr><td>{_esc(PROVIDER_LABELS.get(prov, prov.title()))}</td>"
                 f"<td>{_esc(CLASS_LABEL.get(cls, cls))}</td><td>{_esc(sem)}</td>"
                 f"<td>{_esc(b)}</td></tr>")
    h.append("</tbody></table>")

    h.append("<h3>What this monitor cannot see</h3>")
    h.append("<ul>"
             "<li>Provider <strong>utilization</strong> and unobserved/private inventory: "
             "an unavailable observed configuration is not a fleet-wide sellout.</li>"
             "<li>Hyperscaler on-demand stockouts (AWS/GCP/Azure ICE) are not externally "
             "observable; AWS is proxied via spot pools until the Capacity Blocks IAM lands.</li>"
             "<li>Marketplace depth (Vast) measures listed supply, not datacenter inventory; "
             "falling depth = demand absorbing supply OR hosts delisting.</li>"
             "<li>Self-reported badges (GMI) are marketing statements and can rot.</li>"
             "<li>Levels are weak evidence; <strong>transitions and trends</strong> are the "
             "signal. Trend layer activates at 7 days of history (day "
             f"{insights.history_days()}/7).</li>"
             "</ul>")
    h.append("<p><em>Generated from the capacity monitor's recorded observations.</em></p>")
    h.append("</ac:rich-text-body></ac:structured-macro>")

    return "\n".join(h)


def _page_url() -> str:
    import json as _json
    meta = STORE_DIR / "confluence_page.json"
    if meta.exists():
        try:
            return _json.loads(meta.read_text()).get("url") or ""
        except ValueError:
            pass
    return ""


def write_artifacts(records: List[AvailabilityRecord],
                    diff: List[CapacityDiffEntry],
                    manifest: dict,
                    old_records: List[AvailabilityRecord] = None) -> None:
    old_records = old_records or []
    slack_msg, slack_thread = render_slack(records, diff, manifest, old_records)
    (STORE_DIR / "slack_message.txt").write_text(slack_msg)
    (STORE_DIR / "slack_thread.txt").write_text(slack_thread)
    (STORE_DIR / "confluence_body.html").write_text(
        render_confluence(records, diff, manifest, old_records))
    logger.info("Capacity artifacts written: slack_message.txt, slack_thread.txt, "
                "confluence_body.html")
