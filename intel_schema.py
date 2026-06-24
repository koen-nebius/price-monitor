"""
Field-intelligence schema (ported in spirit from the ml-hiring-leads pilot's forced
structured-output approach).

The #price-intelligence extraction is done by the CCR agent, so we can't force a tool-
call schema the way a Python LLM wrapper would. Instead we enforce the same contract at
the data boundary: every intel.csv row must validate, or it's dropped. This protects the
decision-trigger "competitor field deal" column and the sales battlecards, which now read
directly from this file.
"""
GPU_MODELS = {"H100", "H200", "B200", "B300", "GB200", "GB300", "L40S", "RTX6000"}
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
    try:
        if int(float(row.get("term_months", "0") or 0)) < 0:
            problems.append("term_months negative")
    except (ValueError, TypeError):
        problems.append(f"bad term_months: {row.get('term_months')!r}")
    try:
        pp = int(float(row.get("prepay_pct", "0") or 0))
        if not (0 <= pp <= 100):
            problems.append(f"prepay_pct out of range: {pp}")
    except (ValueError, TypeError):
        problems.append(f"bad prepay_pct: {row.get('prepay_pct')!r}")
    if (row.get("provider_type") or "").strip().lower() not in PROVIDER_TYPES:
        problems.append(f"provider_type not in enum: {row.get('provider_type')!r}")
    if not (row.get("provider_name") or "").strip():
        problems.append("empty provider_name")
    return problems


def is_valid(row: dict) -> bool:
    return not validate_row(row)
