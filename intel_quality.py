"""
Quality rules for store/intel.csv shared by forward_curve.py, the inbox merge and the
refresh script (added 2026-09-15 after Koen's review of the committed-price benchmarks,
tightened the same day after his second review).

Two problems this fixes:
  1. Repeated rows for one underlying offer. The May-2026 seed rows (message_ts
     'seed_YYYYMMDD_nn') repeat quotes that the parser later retrieved from Slack, and a
     few rows re-log the same provider/price/term within days. Counting them twice
     inflates n and fakes comparability.
  2. Unknown prepayment stored as 0 %. 157 of 273 rows carried prepay_pct=0 with nothing
     in the notes to support it; only 26 said zero explicitly. A quote without payment
     terms must stay "unknown", not become a 0 %-prepay comparable.

Removal is automatic only for CONFIRMED repeats; offers that merely look alike are kept
and flagged for review. A shared Slack message is not evidence of duplication on its
own: one message can report several providers' offers at the same price (BoostRun 20k
GPUs US Q4 vs Nscale 15k GPUs EU Q1, both GB300 $3.80 for 36 months, were two offers).

Nothing here deletes rows from intel.csv: classify() returns the canonical set for a
consumer, and the refresh script adds a prepay_known column plus the duplicates report
(removed and review rows, with the action stated per row).
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

# provider labels that name nobody: a seed row "Undisclosed" matching a retrieved row "Mistral"
# on the same day, price, term and GPU is the same offer, anonymised at seeding time
GENERIC_PROVIDER_RE = re.compile(r"^(undisclosed|unknown|unnamed|unspecified|n/?a|tbd|a |an |various|multiple|several|competitor|provider|neocloud|hyperscaler|broker|reseller)\b", re.I)
NOTES_SIMILARITY_CONFIRMED = 0.6   # token Jaccard of the two notes fields at or above this = same wording = same offer
REASON_SAME_PROVIDER = "same provider, price and term within 7 days"
REASON_SEED_SAME_PROVIDER = "seed row repeats a retrieved row (same provider)"
REASON_SEED_ANONYMISED = "seed row repeats a retrieved row (provider anonymised at seeding)"
REASON_SEED_SAME_NOTES = "seed row repeats a retrieved row (same notes)"
REVIEW_SAME_MESSAGE = "same Slack message, different provider: kept, review"
REVIEW_SEED_OTHER_PROVIDER = "seed row matches a retrieved row of another provider: kept, review"
REVIEW_SAME_PROVIDER_OTHER_TERMS = "same provider, price and term but different stated prepayment: kept, review"


def _pct(row: dict) -> float:
    try:
        return round(float(row.get("prepay_pct") or 0), 1)
    except (TypeError, ValueError):
        return 0.0


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


def _generic_provider(p: str) -> bool:
    n = _norm_provider(p)
    return not n or bool(GENERIC_PROVIDER_RE.match(n))


def _tokens(s: str) -> set:
    return {t for t in re.split(r"[^a-z0-9%$.]+", (s or "").lower()) if t}


def notes_similarity(a: str, b: str) -> float:
    """Token Jaccard similarity of two notes fields (0 when either is empty)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


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


def _same_offer_shape(k: dict, r: dict, price: float, window_days: int) -> bool:
    if k["gpu_model"].upper() != (r.get("gpu_model") or "").upper():
        return False
    if _term_bucket(k.get("term_months")) != _term_bucket(r.get("term_months")):
        return False
    if abs(float(k["price_per_gpu_hour_usd"]) - price) > 0.011:
        return False
    rd, kd = _d(r.get("message_date", "")), _d(k.get("message_date", ""))
    if rd and kd and abs((rd - kd).days) > window_days:
        return False
    return True


def classify(rows: list[dict], window_days: int = 7):
    """Split rows into (kept, removed, review).

    Rows are candidates for the same offer when GPU, term bucket and price (±$0.01) match
    within `window_days`. A candidate is REMOVED (confirmed repeat) only when
      - the normalised provider is the same, or
      - one row is a seed/audit row on the same date and the other names the provider the
        seed anonymised ("Undisclosed", "unknown neocloud"...), or their notes match word
        for word (token Jaccard ≥ 0.6).
    A candidate that only shares the Slack message, or a seed row whose match names a
    different provider, is KEPT and listed in `review`. Canonical = the retrieved
    (non-seed) row, then the earliest. `removed`/`review` entries are
    {"row", "duplicate_of"/"similar_to", "reason"}."""
    order = sorted(rows, key=lambda r: (is_seed(r), r.get("message_date", ""), str(r.get("message_ts", ""))))
    kept, removed, review = [], [], []
    for r in order:
        try:
            price = float(r["price_per_gpu_hour_usd"])
        except (KeyError, TypeError, ValueError):
            continue
        confirmed = None
        similar = None
        for k in kept:
            if not _same_offer_shape(k, r, price, window_days):
                continue
            same_prov = _norm_provider(k.get("provider_name")) == _norm_provider(r.get("provider_name")) and not _generic_provider(r.get("provider_name"))
            seed_pair = is_seed(r) != is_seed(k)
            same_day = (r.get("message_date") or "")[:10] == (k.get("message_date") or "")[:10]
            same_msg = str(k.get("message_ts")) == str(r.get("message_ts")) and not is_seed(r)
            # two quotes that both state their prepayment, and state different ones, are two quotes
            prepay_conflict = prepay_known(r) and prepay_known(k) and _pct(r) != _pct(k)
            if same_prov and prepay_conflict:
                if similar is None:
                    similar = (k, REVIEW_SAME_PROVIDER_OTHER_TERMS)
                continue
            if same_prov:
                confirmed = (k, REASON_SEED_SAME_PROVIDER if seed_pair else REASON_SAME_PROVIDER)
                break
            if _norm_provider(k.get("provider_name")) == _norm_provider(r.get("provider_name")) and _generic_provider(r.get("provider_name")) and seed_pair and same_day:
                confirmed = (k, REASON_SEED_SAME_PROVIDER)   # both "Undisclosed", seed repeats the retrieved row
                break
            if seed_pair and same_day and (_generic_provider(r.get("provider_name")) or _generic_provider(k.get("provider_name"))):
                confirmed = (k, REASON_SEED_ANONYMISED)
                break
            if seed_pair and same_day and notes_similarity(r.get("notes"), k.get("notes")) >= NOTES_SIMILARITY_CONFIRMED:
                confirmed = (k, REASON_SEED_SAME_NOTES)
                break
            if same_msg and similar is None:
                similar = (k, REVIEW_SAME_MESSAGE)
            elif seed_pair and similar is None:
                similar = (k, REVIEW_SEED_OTHER_PROVIDER)
        if confirmed:
            k = confirmed[0]
            if prepay_known(r) and not prepay_known(k) and not is_seed(r):
                # same offer, but this row states the payment terms the earlier one lacked: keep the informative row
                kept[kept.index(k)] = r
                removed.append({"row": k, "duplicate_of": r.get("message_ts"), "reason": confirmed[1] + "; later row states prepayment, kept instead"})
                for x in review:
                    if x.get("similar_to") == k.get("message_ts") and x.get("similar_provider") == k.get("provider_name"):
                        x["similar_to"] = r.get("message_ts")
            else:
                removed.append({"row": r, "duplicate_of": k.get("message_ts"), "reason": confirmed[1]})
            continue
        kept.append(r)
        if similar:
            review.append({"row": r, "similar_to": similar[0].get("message_ts"), "similar_provider": similar[0].get("provider_name", ""), "reason": similar[1]})
    return kept, removed, review


def dedupe(rows: list[dict], window_days: int = 7):
    """Backward-compatible view of classify(): (kept_rows, removed) where removed lists
    only confirmed repeats. Offers flagged for review stay in kept_rows."""
    kept, removed, _ = classify(rows, window_days)
    return kept, removed
