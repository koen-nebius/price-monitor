"""
Field-intelligence schema (ported in spirit from the ml-hiring-leads pilot's forced
structured-output approach).

The #price-intelligence extraction is done by the CCR agent, so we can't force a tool-
call schema the way a Python LLM wrapper would. Instead we enforce the same contract at
the data boundary: every intel.csv row must validate, or it's dropped. This protects the
decision-trigger "competitor field deal" column and the sales battlecards, which now read
directly from this file.
"""
from datetime import date
import math
from urllib.parse import urlparse

# Existing nine-column inbox rows remain accepted. Optional fields are preserved
# end-to-end; absent values stay unknown rather than being inferred from notes.
BASE_COLUMNS = ["message_ts", "message_date", "gpu_model", "price_per_gpu_hour_usd",
                "term_months", "prepay_pct", "provider_type", "provider_name", "notes", "prepay_known"]
QUOTE_COLUMNS = ["quote_id", "quote_status", "source_url", "source_observed_at", "expires_on",
                 "instance_type", "gpu_variant", "region", "gpu_count", "gpu_count_relation",
                 "delivery_start", "delivery_end", "currency", "tax_basis"]
INTEL_COLUMNS = BASE_COLUMNS + QUOTE_COLUMNS
QUOTE_STATUSES = {"unknown", "asking_price", "signed_deal"}


def scope_key(row):
    # Terms are exact evidence, even when a downstream chart buckets tenors.
    try:
        term = str(float(row.get("term_months") or 0))
    except (ValueError, TypeError, OverflowError):
        term = str(row.get("term_months") or "")
    return (term,) + tuple(str(row.get(key) or "").strip().lower() for key in QUOTE_COLUMNS)


def is_expired(row, as_of=None):
    value = row.get("expires_on")
    if not value: return False  # unknown expiry is qualified separately
    try:
        return date.fromisoformat(value) < (as_of or date.today())
    except (ValueError, TypeError):
        return True
GPU_MODELS = {"H100", "H200", "B200", "B300", "GB200", "GB300", "L40S", "RTX6000",
              "VR"}   # VR = Vera Rubin (added 2026-08-11; quotes were polluting GB300)
PROVIDER_TYPES = {
    "hyperscaler", "neocloud", "broker",
    "undisclosed_hyperscaler", "undisclosed_neocloud", "undisclosed",
}
PRICE_MIN, PRICE_MAX = 0.10, 60.0   # plausible $/GPU-hr across on-demand and committed


def validate_row(row: dict) -> list:
    """Return a list of problems for an intel row (empty list = valid)."""
    problems = []
    if (row.get("gpu_model") or "").strip().upper() not in GPU_MODELS:
        problems.append(f"gpu_model not in enum: {row.get('gpu_model')!r}")
    try:
        px = float(row.get("price_per_gpu_hour_usd", ""))
        if not (PRICE_MIN <= px <= PRICE_MAX):
            problems.append(f"price out of range: {px}")
    except (ValueError, TypeError):
        problems.append(f"non-numeric price: {row.get('price_per_gpu_hour_usd')!r}")
    for key, maximum in (("term_months", None), ("prepay_pct", 100)):
        try:
            value = float(row.get(key) or 0)
            if (isinstance(row.get(key), bool) or not math.isfinite(value) or value < 0
                    or maximum is not None and value > maximum):
                problems.append(key + " out of range")
        except (ValueError, TypeError, OverflowError):
            problems.append("bad " + key)
    if row.get("prepay_known") not in {None, "", "0", "1"}:
        problems.append("invalid prepay_known")
    if row.get("prepay_known") == "1" and row.get("prepay_pct") in {None, ""}:
        problems.append("known prepayment requires an explicit amount")
    if (row.get("provider_type") or "").strip().lower() not in PROVIDER_TYPES:
        problems.append(f"provider_type not in enum: {row.get('provider_type')!r}")
    if not (row.get("provider_name") or "").strip():
        problems.append("empty provider_name")
    if row.get("quote_status") and row["quote_status"] not in QUOTE_STATUSES:
        problems.append("invalid quote_status")
    if row.get("source_url"):
        try:
            link = urlparse(row["source_url"])
            if (link.scheme not in {"https", "http"} or not link.hostname
                    or link.username or link.password):
                problems.append("source_url must be an HTTP(S) evidence link without embedded credentials")
        except (ValueError, TypeError):
            problems.append("invalid source_url")
    dates = {}
    for key in ("message_date", "source_observed_at", "expires_on", "delivery_start", "delivery_end"):
        if row.get(key):
            try: dates[key] = date.fromisoformat(row[key])
            except (ValueError, TypeError): problems.append("invalid " + key + " (use YYYY-MM-DD)")
    if dates.get("expires_on") and dates.get("source_observed_at") and dates["expires_on"] < dates["source_observed_at"]:
        problems.append("expiry precedes quote observation")
    if dates.get("source_observed_at") and dates.get("message_date") and dates["source_observed_at"] > dates["message_date"]:
        problems.append("quote observation follows its reporting date")
    if dates.get("delivery_start") and dates.get("delivery_end") and dates["delivery_end"] < dates["delivery_start"]:
        problems.append("delivery window is reversed")
    if row.get("gpu_count"):
        try:
            count = float(row["gpu_count"])
            if isinstance(row["gpu_count"], bool) or not math.isfinite(count) or count <= 0 or not count.is_integer(): raise ValueError()
        except (ValueError, TypeError): problems.append("invalid gpu_count")
    if row.get("gpu_count_relation") and row["gpu_count_relation"] not in {"exact", "minimum", "unknown"}:
        problems.append("invalid gpu_count_relation")
    if row.get("currency") and row["currency"] != "USD":
        problems.append("USD/GPU-hour field requires USD currency")
    return problems


def is_valid(row: dict) -> bool:
    return not validate_row(row)
