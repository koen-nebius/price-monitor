"""Neutral, read-only cross-checks of the same priced configuration.

Family minima are deliberately not evidence of agreement or disagreement.
Source agreement may repeat one provider rate card; independence is unproven.
"""
import math
import re
from collections import Counter
from datetime import datetime, timezone

from source_priority import canonical_provider

UNKNOWN = {"", "unknown", "unspecified", "global", "n/a", "none"}
MAX_AGE_HOURS = 48


def _text(value):
    return str(value or "").strip().casefold()


def _known(value):
    return _text(value) not in UNKNOWN


def _positive(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _clock(value):
    if isinstance(value, datetime):
        result = value
    else:
        raw = re.sub(r"(T\d{2}:\d{2}:\d{2})\.(\d+)",
                     lambda m: m[1] + "." + (m[2] + "000000")[:6],
                     str(value).replace("Z", "+00:00"))
        result = datetime.fromisoformat(raw)
    if result.tzinfo is None:
        raise ValueError("source timezone unknown")
    return result.astimezone(timezone.utc)


def source_identity(row):
    return row.source_feed or row.data_source or "unknown"


def observation_identity(row):
    return {"provider": canonical_provider(row.provider), "source": source_identity(row),
            "offer_id": row.offer_id, "instance_type": row.instance_type,
            "gpu_model": row.gpu_model, "gpu_count": row.gpu_count,
            "gpu_count_relation": getattr(row, "gpu_count_relation", "exact"),
            "region": row.region, "consumption_type": row.consumption_type,
            "commitment_months": row.commitment_months, "gpu_variant": row.gpu_variant,
            "offer_variant": row.offer_variant, "form_factor": row.form_factor,
            "node_gpus": row.node_gpus, "vcpu": row.vcpu, "ram_gb": row.ram_gb,
            "price_basis": row.price_basis, "source_url": row.source_url,
            "fetched_at": row.fetched_at, "source_observed_at": row.source_observed_at}


def _freshness_reason(row, now):
    times = [("retrieval", row.fetched_at)]
    if row.source_feed or "aggregator" in row.data_source:
        times.append(("source observation", row.source_observed_at))
    for label, value in times:
        try:
            age = (now - _clock(value)).total_seconds() / 3600
        except (ValueError, TypeError, OverflowError):
            return label + " timestamp unknown or invalid"
        if age < 0 or age > MAX_AGE_HOURS:
            return label + " outside the 48-hour comparison window"
    return None


def _term(row):
    ct = _text(row.consumption_type)
    months = row.commitment_months
    if months is None:
        match = re.fullmatch(r"(?:reserved|committed)_(\d+)(yr|mo)", ct)
        if match:
            months = int(match[1]) * (12 if match[2] == "yr" else 1)
    lo, hi = getattr(row, "term_min_days", None), getattr(row, "term_max_days", None)
    label = _text(getattr(row, "term_label", ""))
    if ct in {"on_demand", "spot", "preemptible"}:
        return ct, months, lo, hi, label
    if months is not None and type(months) is int and months > 0:
        return "commitment", months, lo, hi, label
    if type(lo) is int and type(hi) is int and 0 < lo <= hi:
        return "term_interval", None, lo, hi, label
    return None


def _sku(row):
    # ComputePrices generates a descriptive name locally; its documented feed
    # does not carry a provider SKU. That name must not prove offer identity.
    if row.source_feed == "computeprices" or row.provider.startswith("cp_"):
        return ""
    return _text(row.instance_type) if _known(row.instance_type) else ""


def comparable_reason(direct, reference, now):
    """None only if all material supplied dimensions establish the same offer."""
    if canonical_provider(direct.provider) != canonical_provider(reference.provider):
        return "different provider"
    if direct.gpu_model != reference.gpu_model:
        return "different GPU family"
    if source_identity(direct) == source_identity(reference):
        return "same observation source"
    for row in (direct, reference):
        if not row.comparison_eligible:
            return "source observation is reference-only"
        reason = _freshness_reason(row, now)
        if reason:
            return reason
        if not (_positive(row.price_per_gpu_hour_usd) and _positive(row.price_per_hour_usd)
                and _positive(row.gpu_count)):
            return "invalid price or GPU count"
        if not math.isclose(row.price_per_gpu_hour_usd * row.gpu_count,
                            row.price_per_hour_usd, rel_tol=1e-5, abs_tol=1e-6):
            return "price denominator does not reconcile"
        if getattr(row, "gpu_count_relation", "exact") != "exact":
            return "GPU count is not exact"
    if direct.gpu_count != reference.gpu_count:
        return "different GPUs per priced configuration"
    if not _known(direct.region) or not _known(reference.region):
        return "region unknown or unqualified"
    if _text(direct.region) != _text(reference.region):
        return "different region"
    if _term(direct) is None or _term(reference) is None:
        return "commercial term unknown"
    if _term(direct) != _term(reference):
        return "different billing tier or commercial term"
    if _text(direct.price_basis) != _text(reference.price_basis):
        return "price basis differs or is unspecified by one source"

    # An explicitly different/one-sided commercial variant cannot be washed out
    # by an otherwise matching GPU name (e.g. Secure vs Community; high RAM).
    for field in ("offer_variant", "gpu_variant", "form_factor", "node_gpus", "vcpu", "ram_gb", "storage_gb", "interconnect"):
        left, right = getattr(direct, field), getattr(reference, field)
        l_known = _known(left) if isinstance(left, str) else _positive(left)
        r_known = _known(right) if isinstance(right, str) else _positive(right)
        if l_known != r_known:
            return field + " unknown in one source"
        if l_known and ((left != right) if not isinstance(left, str) and not isinstance(right, str)
                        else _text(left) != _text(right)):
            return "different " + field
    left_sku, right_sku = _sku(direct), _sku(reference)
    if left_sku and right_sku:
        return None if left_sku == right_sku else "different provider SKU"
    # Without an exact provider SKU, require a complete shared host description.
    # GPU family/count/price alone never qualifies the configuration.
    if not all(_positive(getattr(row, field)) for row in (direct, reference)
               for field in ("vcpu", "ram_gb", "node_gpus")):
        return "provider SKU or complete host configuration unavailable"
    if not all(_known(row.form_factor) and _known(row.gpu_variant) for row in (direct, reference)):
        return "GPU variant or form factor unknown"
    if _text(direct.gpu_variant) != _text(reference.gpu_variant):
        return "different GPU variant"
    return None


def compare_price_observations(direct_records, reference_records, as_of=None, threshold_pct=5.0):
    """Return inspectable comparisons without mutating either source's records."""
    now = _clock(as_of) if as_of is not None else datetime.now(timezone.utc)
    direct = [r for r in direct_records if not r.source_feed
              and r.data_source in {"official_api", "web_scrape", "api", "provider_page"}]
    results, warnings = [], []
    for reference in reference_records:
        candidates = [r for r in direct if canonical_provider(r.provider) == canonical_provider(reference.provider)
                      and r.gpu_model == reference.gpu_model]
        reasons, matches = [], []
        for candidate in candidates:
            reason = comparable_reason(candidate, reference, now)
            if reason is None:
                matches.append(candidate)
            else:
                reasons.append(reason)
        item = {"reference": observation_identity(reference), "independent_confirmation": False}
        if not matches or len({r.price_per_gpu_hour_usd for r in matches}) != 1:
            item.update(status="not_comparable", reasons=(sorted(set(reasons)) if not matches and reasons
                         else ["conflicting direct observations for the exact offer"] if matches
                         else ["no direct observation of this provider and GPU family"]))
        else:
            primary = matches[0]
            delta = reference.price_per_gpu_hour_usd - primary.price_per_gpu_hour_usd
            gap = delta / primary.price_per_gpu_hour_usd * 100
            status = "matched_disagreement" if abs(gap) > threshold_pct else "matched_agreement"
            item.update(status=status, direct=observation_identity(primary),
                        direct_price=primary.price_per_gpu_hour_usd,
                        reference_price=reference.price_per_gpu_hour_usd, delta_usd=delta, delta_pct=gap,
                        note="Same offer across sources; common upstream rate-card lineage is possible.")
            if status == "matched_disagreement":
                warnings.append(
                    f"cross-check: {canonical_provider(primary.provider)} {primary.instance_type} "
                    f"{primary.region} {primary.consumption_type}: direct ${primary.price_per_gpu_hour_usd:.4f} "
                    f"vs {source_identity(reference)} ${reference.price_per_gpu_hour_usd:.4f}/GPU-h "
                    f"({gap:+.1f}%); exact-configuration source discrepancy, cause unverified")
        results.append(item)
    return {"summary": dict(Counter(item["status"] for item in results)),
            "comparisons": results, "warnings": warnings,
            "independence": "Not established; source agreement can repeat one rate card."}
