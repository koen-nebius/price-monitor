"""
Quality rules for store/intel.csv shared by forward_curve.py, the inbox merge and the
refresh script (added 2026-09-15 after Koen's review of the committed-price benchmarks).

Two problems this fixes:
  1. Duplicate rows for one underlying offer. The May-2026 seed rows (message_ts
     'seed_YYYYMMDD_nn') repeat quotes that the parser later retrieved from Slack, and a
     few multi-provider messages log one joint offer once per provider (SFCompute/Ornn,
     Verda/CIVO). Counting them twice inflates n and fakes comparability.
  2. Unknown prepayment stored as 0 %. 157 of 273 rows carried prepay_pct=0 with nothing
     in the notes to support it; only 26 said zero explicitly. A quote without payment
     terms must stay "unknown", not become a 0 %-prepay comparable.

Nothing here deletes rows from intel.csv: dedupe() returns the canonical set for a
consumer, and the refresh script adds a prepay_known column plus a duplicates report.
"""
from __future__ import annotations

import re
from datetime import date

PREPAY_UNKNOWN_RE = re.compile(
    r"unspecif|unknown prepay|prepay unknown|prepay not|not sure|unclear|didn.t disclose|terms not|"
    r"no prepay info|prepay\?|prepay n/a|upfront unknown|payment terms unknown|w/o prepay info", re.I)
PREPAY_ZERO_RE = re.compile(
    r"(?<![\d.])0 ?%|\b(no prepay(?:ment)?|no upfront|zero prepay|without prepay|no down(?: ?payment)?|monthly(?: payment| terms| billing)?|"
    r"postpaid|pay as you go|no money down|nothing up ?front)\b", re.I)
PREPAY_ANY_RE = re.compile(r"prepa|upfront|up-front|down ?payment|% down|prepaid", re.I)


def prepay_known(row: dict) -> bool:
    """True when the row's prepayment is stated (any non-zero value, or an explicit zero
    in the notes); False when 0 % is just the parser default."""
    try:
        if float(row.get("prepay_pct") or 0) > 0:
            return True
    except (TypeError, ValueError):
        return False
    notes = row.get("notes") or ""
    if PREPAY_UNKNOWN_RE.search(notes):
        return False
    if PREPAY_ZERO_RE.search(notes):
        return True
    return False


def _norm_provider(p: str) -> str:
    p = (p or "").strip().lower()
    p = re.sub(r"\s*\((?:unconfirmed|inferred|likely|guess)[^)]*\)", "", p)
    p = p.replace("coreweave", "cw").replace("undisclosed hyperscaler", "undisclosed").replace("undisclosed neocloud", "undisclosed")
    return p


def _term_bucket(months) -> int:
    try:
        m = float(months or 0)
    except (TypeError, ValueError):
        return 0
    if m <= 0:
        return 0
    return 3 if m <= 4 else 6 if m <= 8 else 12 if m <= 14 else 18 if m <= 20 else 24 if m <= 27 else 36 if m <= 42 else 60


def _d(s: str):
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def is_seed(row: dict) -> bool:
    return str(row.get("message_ts", "")).startswith(("seed_", "audit_"))


def dedupe(rows: list[dict], window_days: int = 7):
    """Return (kept_rows, duplicates) where duplicates is a list of
    {"row": r, "duplicate_of": canonical_message_ts, "reason": ...}.

    Two rows are the same offer when gpu, term bucket and price (±$0.01) match, the
    dates are within `window_days`, and either the provider matches, one of them is a
    seed/audit row, or they come from the same Slack message. Canonical = the retrieved
    (non-seed) row, then the earliest."""
    order = sorted(rows, key=lambda r: (is_seed(r), r.get("message_date", ""), str(r.get("message_ts", ""))))
    kept, dups = [], []
    for r in order:
        try:
            price = float(r["price_per_gpu_hour_usd"])
        except (KeyError, TypeError, ValueError):
            continue
        rd = _d(r.get("message_date", ""))
        match = None
        for k in kept:
            if k["gpu_model"].upper() != (r.get("gpu_model") or "").upper():
                continue
            if _term_bucket(k.get("term_months")) != _term_bucket(r.get("term_months")):
                continue
            if abs(float(k["price_per_gpu_hour_usd"]) - price) > 0.011:
                continue
            kd = _d(k.get("message_date", ""))
            if rd and kd and abs((rd - kd).days) > window_days:
                continue
            same_msg = str(k.get("message_ts")) == str(r.get("message_ts")) and not is_seed(r)
            same_prov = _norm_provider(k.get("provider_name")) == _norm_provider(r.get("provider_name"))
            if same_prov or is_seed(r) or is_seed(k) or same_msg:
                match = (k, "same message" if same_msg else "seed duplicate" if (is_seed(r) or is_seed(k)) else "same provider/price/term within 7 days")
                break
        if match:
            dups.append({"row": r, "duplicate_of": match[0].get("message_ts"), "reason": match[1]})
        else:
            kept.append(r)
    return kept, dups
