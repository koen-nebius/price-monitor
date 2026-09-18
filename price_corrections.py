"""Non-destructive, narrowly scoped corrections for analytical price comparisons.

Evidence: commit 2acbdf22b67ab3dabbd0fc76bc50f901f93efa50 and dated
history.csv observations. The corrected parser first appears in the daily history
on 2026-09-17. Raw snapshots and raw history must never be overwritten by this
module. Correct BEFORE taking daily minima: a corrected denominator may change
which exact SKU/region is cheapest. A cheapest-only CSV cannot reconstruct that
selection; its corrected row remains an observed reference, not a recomputed floor.

Azure: derive the correct per-GPU value from the recorded VM hourly value and
the exact documented card fraction. Together: the bad observation is a different
product (preemptible); omit it from OD comparisons rather than inventing historic
OD prices by doubling. No provider calls or file writes occur here.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime
import math
from typing import Iterable, Mapping, Optional

from schema import PriceRecord

EVIDENCE_COMMIT = "2acbdf22b67ab3dabbd0fc76bc50f901f93efa50"
AZURE_CORRECTION = "azure-rtx-fractional-gpu-2026-09-17"
TOGETHER_CORRECTION = "together-preemptible-mislabeled-od-2026-09-17"
AWS_STATIC_CORRECTION = "aws-legacy-static-capacity-block-2026-09-18"
AZURE_COUNTS = {
    f"standard_{size}_xl_rtxpro6000bse_v6": count
    for size, count in (
        ("nc36ds", .25), ("nc72ds", .5), ("nc144ds", 1), ("nc288ds", 2),
        ("nc24lds", .25), ("nc36lds", .25), ("nc72lds", .5),
        ("nc144lds", 1), ("nc288lds", 2),
    )
}
TOGETHER_PRICES = {"H100": (1.99, 3.99), "H200": (2.99, 5.99), "B200": (4.09, 8.19)}
_AWS_STATIC = {
    "H100": ("p5.48xlarge", 8, 3.933),
    "H200": ("p5e.48xlarge", 8, 4.975),
    "B200": ("p6-b200.48xlarge", 8, 10.296),
    "B300": ("p6-b300.48xlarge", 8, 11.70),
    "GB200": ("p6e.36xlarge", 36, 10.582),
}
AUDIT_COLUMNS = [
    "correction_id", "correction_reason", "original_price_per_gpu_hour_usd",
    "original_gpu_count", "comparison_eligible", "correction_evidence",
]


def _number(value) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _day(value) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    value = str(value or "")[:10]
    try:
        date.fromisoformat(value)
        return value
    except ValueError:
        return ""


def _close(a, b, *, tolerance=.00015) -> bool:
    a, b = _number(a), _number(b)
    return a is not None and b is not None and abs(a - b) <= tolerance


def _eligible(value) -> bool:
    return value not in (False, "False", "false", "0", 0)


def _updates(row: Mapping, snapshot_date=None) -> dict:
    # Idempotent even when a derived CSV is passed back through the helper.
    if row.get("correction_id"):
        return {}
    day = _day(snapshot_date or row.get("snapshot_date") or row.get("fetched_at"))
    provider, gpu = row.get("provider"), row.get("gpu_model")
    instance = str(row.get("instance_type", ""))
    price = _number(row.get("price_per_gpu_hour_usd"))
    hourly = _number(row.get("price_per_hour_usd"))
    count = _number(row.get("gpu_count"))
    ct = row.get("consumption_type")
    base = {
        "original_price_per_gpu_hour_usd": price,
        "original_gpu_count": count,
    }

    expected = AZURE_COUNTS.get(instance.casefold())
    if (provider == "azure" and gpu in {"RTX6000", "RTXPRO6000"}
            and "2026-08-25" <= day <= "2026-09-16" and expected is not None
            and row.get("data_source") == "official_api"
            and ct in {"on_demand", "spot", "low_priority", "reserved_1yr", "reserved_3yr"}
            and count is not None and _close(count, expected * 4)
            and price is not None and price > 0 and hourly is not None and hourly > 0
            and _close(hourly, price * count, tolerance=.001)):
        changes = {
            **base, "gpu_count": expected,
            "price_per_gpu_hour_usd": hourly / expected,
            "correction_id": AZURE_CORRECTION,
            "correction_reason": "Exact Azure RTX VM card fraction corrected; recorded VM hourly price unchanged",
            "comparison_eligible": True,
        }
        if _close(row.get("node_gpus"), count):
            changes["node_gpus"] = expected
        return changes

    prices = TOGETHER_PRICES.get(gpu)
    if (provider == "together" and prices and ct == "on_demand"
            and "2026-09-03" <= day <= "2026-09-16"
            and instance == f"together-hgx-{gpu.lower()}"
            and row.get("region") == "global" and row.get("data_source") == "web_scrape"
            and _close(count, 8) and _close(price, prices[0])
            and _close(hourly, prices[0] * 8)):
        return {
            **base, "correction_id": TOGETHER_CORRECTION,
            "correction_reason": "Recorded preemptible column was mislabeled on-demand; historical OD value not reconstructed",
            "comparison_eligible": False,
        }

    legacy = _AWS_STATIC.get(gpu)
    if (provider == "aws" and ct == "capacity_block" and legacy
            and instance == legacy[0] and _close(count, legacy[1])
            and _close(price, legacy[2]) and _close(hourly, legacy[1] * legacy[2])
            and row.get("region") == "us-east-1" and row.get("data_source") == "official_api"):
        return {
            **base, "correction_id": AWS_STATIC_CORRECTION,
            "correction_reason": "Legacy June constants were stamped as fresh API data; use separately fetched published Capacity Block rates",
            "comparison_eligible": False,
        }
    return {}


@dataclass(frozen=True)
class CorrectionResult:
    record: PriceRecord
    correction_id: str
    reason: str
    comparison_eligible: bool


def correct_record(record: PriceRecord, snapshot_date=None) -> CorrectionResult:
    """Return a corrected COPY and audit status; never mutate an observation."""
    copied = replace(record, **_updates(record.to_dict(), snapshot_date))
    return CorrectionResult(copied, copied.correction_id, copied.correction_reason,
                            _eligible(copied.comparison_eligible))


def correct_snapshot(records: Iterable[PriceRecord], snapshot_date=None,
                     *, include_excluded=False) -> list[PriceRecord]:
    results = [correct_record(r, snapshot_date) for r in records]
    return [r.record for r in results if include_excluded or r.comparison_eligible]


def correct_history_rows(rows: Iterable[Mapping], *, include_excluded=False) -> list[dict]:
    """Copy rows onto a consistent basis; retain raw values in audit columns.

    For aggregate historical positions use a common provider cohort across the
    comparison window; excluded Together OD rows do not mean its price rose.
    """
    out = []
    for row in rows:
        copied = {**row, **_updates(row)}
        copied.setdefault("comparison_eligible", True)
        if copied.get("correction_id"):
            copied["correction_evidence"] = (
                "fetchers/aws.py legacy constant table" if copied["correction_id"] == AWS_STATIC_CORRECTION
                else EVIDENCE_COMMIT)
        if include_excluded or _eligible(copied.get("comparison_eligible")):
            out.append(copied)
    return out


def known_restatement(old: PriceRecord, new: PriceRecord,
                      old_date=None, new_date=None) -> Optional[str]:
    """Identify the verified parser transition, never suppress a genuine price move.

    Compare Azure on the corrected record returned by correct_record even when
    this returns None: an underlying VM price change can coincide with the fix.
    Together's missing OD baseline cannot establish any change across the gap.
    """
    key = lambda r: (r.provider, r.gpu_model, r.instance_type, r.region, r.consumption_type)
    if key(old) != key(new):
        return None
    previous = correct_record(old, old_date)
    current = correct_record(new, new_date)
    if previous.correction_id == AZURE_CORRECTION and current.comparison_eligible:
        old_p = previous.record.price_per_gpu_hour_usd
        new_p = current.record.price_per_gpu_hour_usd
        if old_p > 0 and abs(new_p - old_p) / old_p <= .001:
            return previous.reason
    if (previous.correction_id == TOGETHER_CORRECTION
            and _day(new_date or new.fetched_at) == "2026-09-17"
            and _close(new.price_per_gpu_hour_usd, TOGETHER_PRICES[new.gpu_model][1])
            and _close(new.price_per_hour_usd, new.price_per_gpu_hour_usd * 8)
            and current.comparison_eligible):
        return previous.reason
    return None
