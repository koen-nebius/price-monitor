#!/usr/bin/env python3
"""
Internal GPU forward curve — marked, not traded.

Builds a price-by-tenor curve per GPU tier from the observations this repo already
collects, the same way a broker "marks" an illiquid forward market: pool every
dated observation, normalise it to a common basis, take a robust central value per
(tier, tenor) cell, and refuse to print a mark where the evidence is too thin.

Legs (what is pooled into the mark):
  bid  store/intel.csv           competitor quotes/deals reported by sales in
                                 #price-intelligence (what buyers can get elsewhere;
                                 skews to losses — the pressure side)
  ask  store/reserve_tenor.csv   Nebius SIGNED reserve deals, aggregated per
                                 tier x tenor x close month (what buyers actually
                                 paid us — the won side; weekly local refresh via
                                 scripts/refresh_reserve_tenor.py)
Reference only (shown next to the mark, never pooled into it):
  list store/history.csv         latest public committed/reserved list prices
                                 (hyperscaler rack rates, Nebius AE grid)
  cost SA_COST_FLOOR             SemiAnalysis TCO model cost floor per SKU

Normalisation (declared, not fitted from theory):
  1. prepay -> 0%-prepay equivalent:  p0 = p / (1 - PREPAY_K * prepay_frac).
     PREPAY_K = 0.06 sits between the Nebius AE grid (30% -> 100% prepay is
     -3% B300 12m, -7% GB300 36m) and the CoreWeave H100 ladder (25% -> 100% is
     -4.4%).
  2. quote-date -> as-of quarter: two-way fixed effects on log(p0),
     cell(tier, tenor) + quarter(quote date), pooled across tiers, estimated by
     alternating means; every observation is shifted by
     (effect[as-of quarter] - effect[its quarter]). Pooling is a v1 simplification
     (Hopper and Blackwell repriced by similar ratios in 2026H1: H100 12m CRM
     medians 1.7 -> 2.5, B300 12m 3.45 -> 5.3).
  3. age weights: <= RECENT_DAYS full weight, <= MAX_AGE_DAYS half weight, older
     dropped; ask cells weigh min(deals, 3) so one mega-deal cannot dominate.
  4. mark = weighted median of adjusted prices; suppressed when fewer than
     MIN_OBS observations inside MAX_AGE_DAYS (a null price with a reason —
     never interpolated or carried forward, same contract as Ornn's API).

Outputs (all under store/):
  forward_curve/latest.json        marks + references + parameters + provenance
  forward_curve/marks_history.csv  one row per (as-of, tier, tenor); appended per
                                   run so an in-house history of marks accrues
                                   from day one (Ornn keeps none on its API)
  forward_curve/forward_curve.svg  chart (stdlib SVG, no dependencies)
  forward_curve/forward_curve.png  same chart via matplotlib when installed
  forward_curve/forward_view.html  self-contained internal view (inline SVG +
                                   tables; attachable to Confluence)
  forward_curve_body.html          Confluence page body (HTML+ dialect; publish
                                   with scripts/publish_forward_curve.py)

Usage: python3 forward_curve.py [--as-of YYYY-MM-DD] [--with-images] [--quiet]
  --with-images  embed <ac:image> tags for the attached PNG in the Confluence body
                 (only the GHA publisher, which uploads the attachment, sets this).

Confidentiality: the ask leg is aggregates only (no customer names, no deal rows);
the page says "internal only" and never quotes deal-level detail.
"""
from __future__ import annotations

import argparse
import csv
import re
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(ROOT))
from intel_quality import classify as intel_classify, dedupe as intel_dedupe, prepay_known as intel_prepay_known  # noqa: E402
from history import load_comparison_history  # noqa: E402
from report_freshness import committed_reference_fresh  # noqa: E402
STORE = ROOT / "store"
OUT_DIR = STORE / "forward_curve"
INTEL_CSV = STORE / "intel.csv"
RESERVE_TENOR_CSV = STORE / "reserve_tenor.csv"
DEAL_COHORTS_CSV = STORE / "deal_cohorts.csv"   # CRM closed-deal cohorts (scripts/refresh_deal_cohorts.py, weekly)
HISTORY_CSV = STORE / "history.csv"
BODY_HTML = STORE / "forward_curve_body.html"

METHOD_VERSION = "1.4.1 (2026-09-18)"
TIERS = ["H100", "H200", "B200", "B300", "GB200", "GB300", "VR"]
TENORS = [3, 6, 12, 18, 24, 36, 60]            # months; buckets, see bucket_months()
TENOR_LABEL = {3: "3m", 6: "6m", 12: "12m", 18: "18m", 24: "24m", 36: "36m", 60: "60m"}
# Prepay normalisation, v1.1: Finance's money-cost convention. Sheet "." of the Finance
# "Pricing model.xlsx" derives the 100/50/30 % columns from the 0 % column as
# 12m: -3.40 / -2.63 / -1.80 %, 24m: -6.97 / -5.26 / -3.57 %. A three-parameter fit to
# those six cells returns a=0.0344/yr, linear in tenor, g(p)=1-(1-p)^2 (rmse 0.02pp), i.e.
# discount = 0.0345 * years * (2p - p^2): prepaying consumes the first p*T months at a
# ~7 %/yr money cost. The Sep-7 AI Native grid steps (100->50 % = 6-13 %) are a
# commercial ladder steering buyers to full prepay, NOT a money-cost convention, and are
# deliberately not used to normalise market quotes (verified 2026-09-15, see method note).
PREPAY_A = 0.0345         # per year of tenor at 100 % prepay
PREPAY_M = 2.0            # g(p) = 1 - (1 - p) ** PREPAY_M  (concave: g(.3)=.51, g(.5)=.75, g(.75)=.94)
PREPAY_CAP = 0.25         # never more than 25 % (5-yr / 100 % would be 17 %)
PREPAY_BUCKET_PCT = {"upfront": 100, "prepaid_monthly": 8, "postpaid": 0}   # CRM proxy buckets
GRID_JSON = STORE / "nebius_reserve_grid.json"
CONTRACTS_CSV = STORE / "public_contracts.csv"
ECONOMICS_JSON = STORE / "economics.json"
PAYG_REALISED_CSV = STORE / "payg_realised.csv"
SA_REFERENCE_JSON = STORE / "sa_reference.json"        # scripts/extract_sa_reference.py (local, from the SA TCO workbook)
PERF_MULTIPLES_JSON = STORE / "perf_multiples.json"    # delivered-performance multiples between generations (sourced)
INDEX_QUOTES_CSV = STORE / "index_quotes.csv"          # survey/index prices (SemiAnalysis draft index): class "index", never pooled
CRM_ASKS_CSV = STORE / "crm_asks.csv"                  # scripts/refresh_crm_asks.py (local, weekly): Nebius lost and open asked prices, aggregates only (HubSpot mirror, frozen 2026-08-10)
QUOTE_ASKS_CSV = STORE / "quote_asks.csv"              # scripts/refresh_quote_asks.py (local, daily): the same classes from live Salesforce quotes since the cutover
ASK_TO_CLOSE_CSV = STORE / "ask_to_close.csv"          # scripts/backfill_hubspot_ask_paths.py (one-off): first ask -> final price paths per GPU x tenor x outcome
LATEST_SNAPSHOT_JSON = STORE / "latest.json"           # main.py daily provider snapshot; marketplace short reservations live only here
NODE_SPECS_JSON = STORE / "node_specs.json"            # scripts/merge_node_specs.py: node configuration behind each priced SKU
CRM_ASK_MIN_DEALS = 3                                  # a lost/open aggregate under this many deals is counted, its price withheld
CRM_ASK_WINDOW_DAYS = 365
DEFAULT_SEGMENT = "ai_native_above_512"
RECENT_DAYS = 120
MAX_AGE_DAYS = 365
MIN_OBS = 3
GOOD_OBS = 6
PRICE_MIN, PRICE_MAX = 0.5, 15.0               # $/GPU-hr sanity band for curve cells
FE_ITERATIONS = 50

# SemiAnalysis "AI Cloud TCO Model Update — August 10 2026", sheet Full TCO row 147
# ("Neocloud Giant" profile: 6-yr life, 80% utilisation, $0.087/kWh, PUE 1.35,
# WACC 10.25%). Third-party modeled cost, $/GPU-hr, shown as a floor reference only.
SA_COST_FLOOR = {"H100": 1.55, "H200": 1.59, "B200": 2.07, "GB200": 2.27,
                 "B300": 2.43, "GB300": 2.79, "VR": 4.02}
SA_COST_FLOOR_SOURCE = ("SemiAnalysis AI-Cloud TCO model (Aug-10-2026), Full TCO row 147, "
                        "Neocloud Giant profile; VR = NVL72 2300W variant")

# public list consumption types -> tenor months (history.csv)
LIST_TENOR = {"committed_short_term": 3, "capacity_block": 3, "committed_9mo": 9,
              "reserved_1yr": 12, "committed_1yr": 12, "committed_18mo": 18,
              "committed_2yr": 24, "reserved_3yr": 36, "committed_3yr": 36,
              "committed_4yr": 48}
HYPERSCALERS = {"aws", "gcp", "azure", "oracle"}


# ----------------------------------------------------------------------------- helpers
def bucket_months(months) -> int | None:
    """Map a raw term (months) to a curve tenor bucket; None for on-demand/unknown."""
    try:
        m = float(months)
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None
    if m <= 4:
        return 3
    if m <= 8:
        return 6
    if m <= 14:
        return 12
    if m <= 20:
        return 18
    if m <= 27:
        return 24
    if m <= 42:
        return 36
    return 60


def quarter_of(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def prepay_discount(tenor_months, prepay_pct) -> float:
    """Fraction by which a quote at `prepay_pct` sits below the same quote at 0 % prepay."""
    try:
        frac = max(0.0, min(1.0, float(prepay_pct or 0) / 100.0))
        years = max(0.25, float(tenor_months or 12) / 12.0)
    except (TypeError, ValueError):
        return 0.0
    return min(PREPAY_CAP, PREPAY_A * years * (1.0 - (1.0 - frac) ** PREPAY_M))


def prepay_normalise(price: float, prepay_pct, tenor_months=12) -> float:
    """Quote -> 0 %-prepay equivalent."""
    return price / (1.0 - prepay_discount(tenor_months, prepay_pct))


def price_at_prepay(p0: float, tenor_months, prepay_pct) -> float:
    """0 %-prepay price -> price at a chosen prepay share (inverse of prepay_normalise)."""
    return p0 * (1.0 - prepay_discount(tenor_months, prepay_pct))


def weighted_median(values, weights) -> float:
    if not values:
        return None
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return v
    return pairs[-1][0]


def _parse_date(s: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


# ----------------------------------------------------------------------------- loaders
def load_bid(path: Path = INTEL_CSV) -> list[dict]:
    """Competitor offers from #price-intelligence -> observations (side=bid).
    Confirmed repeats are removed (intel_quality.classify: same provider/price/term within
    7 days, seed rows repeating a retrieved row); offers that merely look alike (same Slack
    message, different provider) are kept and carry `review` = True. Each observation
    carries `known` = prepayment stated in the quote; only known observations enter a
    mark, unknown ones are counted separately and never ranked by default."""
    obs = []
    if not path.exists():
        return obs
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    kept, removed, review = intel_classify(rows)
    load_bid.duplicates = len(removed)  # noqa: attribute on function, read by build()
    load_bid.review = len(review)
    review_ids = {id(x["row"]): x for x in review}
    for r in kept:
        tier = (r.get("gpu_model") or "").strip().upper()
        if tier not in TIERS:
            continue
        try:
            price = float(r["price_per_gpu_hour_usd"])
        except (KeyError, ValueError, TypeError):
            continue
        d = _parse_date(r.get("message_date", ""))
        tenor = bucket_months(r.get("term_months"))
        if not d or tenor is None:
            continue
        try:
            months = float(r.get("term_months") or 0)
        except (TypeError, ValueError):
            months = 0.0
        known = (r.get("prepay_known") == "1") if r.get("prepay_known") not in (None, "") else intel_prepay_known(r)
        rv = review_ids.get(id(r))
        obs.append({"side": "bid", "tier": tier, "tenor": tenor, "months": months, "date": d,
                    "price_raw": price, "prepay_pct": float(r.get("prepay_pct") or 0), "known": known,
                    "weight": 1.0, "provider": r.get("provider_name", ""),
                    "provider_type": r.get("provider_type", ""), "ts": str(r.get("message_ts", "")),
                    "review": bool(rv), "similar_provider": (rv or {}).get("similar_provider", ""),
                    "source": f"intel:{r.get('message_ts', '')}"})
    return obs


def load_ask(path: Path = RESERVE_TENOR_CSV) -> list[dict]:
    """Nebius signed reserve aggregates -> observations (side=ask), one per cell-month."""
    obs = []
    if not path.exists():
        return obs
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            tier = (r.get("gpu") or "").strip().upper()
            if tier not in TIERS:
                continue
            try:
                price = float(r["price_med"])
                deals = int(float(r.get("deals") or 1))
            except (KeyError, ValueError, TypeError):
                continue
            d = _parse_date(r.get("close_month", ""))
            tenor = bucket_months(r.get("tenor_months"))
            if not d or tenor is None:
                continue
            bucket = (r.get("prepay_bucket") or "postpaid").strip()
            obs.append({"side": "ask", "tier": tier, "tenor": tenor, "months": float(r.get("tenor_months") or tenor),
                        "date": d + timedelta(days=14),      # month midpoint
                        "price_raw": price, "prepay_pct": float(PREPAY_BUCKET_PCT.get(bucket, 0)),
                        "prepay_bucket": bucket, "known": True,
                        "weight": float(min(deals, 3)), "deals": deals,
                        "provider": "Nebius", "provider_type": "nebius",
                        "source": f"reserve_tenor:{r.get('generated_date', '')}"})
    return obs


def load_contracts(path: Path = CONTRACTS_CSV) -> list[dict]:
    """Publicly announced multi-year GPU rental contracts (SemiAnalysis 'AI Cloud Deal
    Information' extraction) -> observations (side=public). Implied $/GPU-hr assumes
    8760 billed hours and ignores prepay time-value, so weight 0.5 and prepay as given."""
    obs = []
    if not path.exists():
        return obs
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            tier = (r.get("tier") or "").strip().upper()
            if tier not in TIERS:
                continue
            try:
                price = float(r["implied_price_gpu_hr"])
                months = float(r["term_months"])
            except (KeyError, ValueError, TypeError):
                continue
            d = _parse_date((r.get("announced") or "")[:10]) or _parse_date((r.get("announced") or "") + "-01")
            tenor = bucket_months(months)
            if not d or tenor is None:
                continue
            try:
                prepay = float(r.get("prepay_pct") or 0)
            except (TypeError, ValueError):
                prepay = 0.0
            obs.append({"side": "public", "tier": tier, "tenor": tenor, "months": months, "date": d,
                        "price_raw": price, "prepay_pct": prepay, "weight": 0.0, "known": True,   # shown, never pooled
                        "provider": r.get("provider", ""), "provider_type": "public_contract",
                        "customer": r.get("customer", ""), "source": f"sa_contracts:{r.get('source_cell', '')}"})
    return obs


def load_on_demand(history: Path = HISTORY_CSV, intel_obs: list | None = None, realised: Path = PAYG_REALISED_CSV,
                   as_of: date | None = None) -> dict:
    """The 0-month anchor per tier, kept as separate classes (never pooled into the curve):
    Nebius on-demand list and preemptible list (latest scraper snapshot), enterprise-peer
    on-demand median and cheapest hyperscaler on-demand for cluster-class SKUs (>= 8 GPUs
    when the node size is known), on-demand competitor quotes from #price-intelligence
    (term 0, last 90 days) and Nebius realised PAYG $/GPU-hour (last 30 days, external,
    non-preemptible, from the Analytics consumption dataset)."""
    out = {t: {} for t in TIERS}
    try:
        from config import provider_tag
    except Exception:  # pragma: no cover
        provider_tag = lambda p: "peer"  # noqa: E731
    if history.exists():
        rows = [r for r in load_comparison_history(history) if r.get("consumption_type") in ("on_demand", "spot", "preemptible")]
        if rows:
            latest = max(r["snapshot_date"] for r in rows)
            per = defaultdict(lambda: {"peer": [], "hyper": [], "other": []})
            for r in rows:
                if r["snapshot_date"] != latest:
                    continue
                tier = r["gpu_model"].upper()
                if tier not in TIERS:
                    continue
                try:
                    p = float(r["price_per_gpu_hour_usd"]); gc = int(float(r.get("gpu_count") or 0))
                except (TypeError, ValueError):
                    continue
                prov, ct = r["provider"], r["consumption_type"]
                if prov == "nebius":
                    out[tier]["nebius_list" if ct == "on_demand" else "nebius_preemptible"] = min(p, out[tier].get("nebius_list" if ct == "on_demand" else "nebius_preemptible", 99))
                    continue
                if ct != "on_demand" or (gc and gc < 8):
                    continue
                tag = provider_tag(prov)
                inst = r.get("instance_type") or ""
                if tag == "peer":
                    per[tier]["peer"].append((p, prov, inst))
                elif tag == "hyperscaler":
                    per[tier]["hyper"].append((p, prov, inst))
                else:
                    per[tier]["other"].append((p, prov, inst))   # price fighters / platforms, PAYG term only
            for tier, d in per.items():
                def cheapest(rows):   # cheapest SKU per provider: {provider: (price, instance_type)}
                    best = {}
                    for p, prov, inst in rows:
                        if prov not in best or p < best[prov][0]:
                            best[prov] = (p, inst)
                    return best
                def as_list(best):
                    return [{"provider": k, "price": round(v[0], 4), "instance_type": v[1]} for k, v in sorted(best.items(), key=lambda kv: kv[1][0])]
                if d["peer"]:
                    best = cheapest(d["peer"])
                    vals = sorted(v[0] for v in best.values())
                    out[tier]["peer_od_median"] = round(statistics.median(vals), 2)
                    out[tier]["peer_od_n"] = len(vals)
                    out[tier]["peer_od_min"] = round(vals[0], 2)
                    out[tier]["peer_list"] = as_list(best)
                if d["hyper"]:
                    p, prov, _ = min(d["hyper"])
                    out[tier]["hyperscaler_od_min"] = round(p, 2); out[tier]["hyperscaler_od_provider"] = prov
                    out[tier]["hyper_list"] = as_list(cheapest(d["hyper"]))
                if d["other"]:
                    out[tier]["other_list"] = as_list(cheapest(d["other"]))
            for tier in out:
                out[tier]["list_snapshot"] = latest
    if intel_obs:
        as_of = as_of or date.today()
        for tier in TIERS:
            q = [o for o in intel_obs if o["tier"] == tier and (o.get("months") or 0) == 0 and (as_of - o["date"]).days <= 90]
            if q:
                out[tier]["quotes_od_median"] = round(statistics.median([o["price_raw"] for o in q]), 2)
                out[tier]["quotes_od_n"] = len(q)
                out[tier]["quotes"] = [{"provider": o.get("provider", ""), "price": round(o["price_raw"], 4), "date": o["date"].isoformat(),
                                        "ts": str(o.get("ts", "")), "known": bool(o.get("known", False))} for o in sorted(q, key=lambda o: o["date"])]
    if realised.exists():
        for r in csv.DictReader(open(realised, newline="")):
            tier = (r.get("tier") or "").upper()
            if tier not in TIERS:
                continue
            key = "realised_preemptible" if str(r.get("preemptible")).lower() in ("true", "1") else "realised_payg"
            try:
                out[tier][key] = round(float(r["realised_usd_per_gpu_hour"]), 2)
                out[tier][key + "_hours"] = int(float(r.get("paid_gpu_hours") or 0))
                out[tier]["realised_window_days"] = int(float(r.get("window_days") or 30))
                out[tier]["realised_generated"] = r.get("generated_date")
            except (TypeError, ValueError):
                continue
    return out


def load_cohorts(path: Path = DEAL_COHORTS_CSV) -> list[dict]:
    """Nebius closed-deal cohorts per GPU x tenor x recorded outcome, aggregates only.
    Loss categories do not establish customer price acceptance or willingness to pay;
    from scripts/refresh_deal_cohorts.py. Reference class: rendered, never pooled."""
    out = []
    if not path.exists():
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("gpu") not in TIERS:
                continue
            try:
                out.append({"gpu": r["gpu"], "tenor_months": int(float(r["tenor_months"])), "outcome": r["outcome"],
                            "opps": int(float(r["opps"])), "lines": int(float(r.get("lines") or 0)), "gpus": int(float(r["gpus"] or 0)),
                            "p25": float(r["price_p25"]), "med": float(r["price_med"]), "p75": float(r["price_p75"]),
                            "source": r.get("source", ""), "window_from": r.get("window_from", ""),
                            "window_to": r.get("window_to", ""), "generated": r.get("generated_date", "")})
            except (KeyError, ValueError, TypeError):
                continue
    return out


def load_ask_paths(path: Path = ASK_TO_CLOSE_CSV) -> list[dict]:
    """HubSpot-era ask-to-close price paths per GPU x tenor x outcome (first asked price at a deal
    review -> final price, share revised), aggregates only, from
    scripts/backfill_hubspot_ask_paths.py. Reference class: rendered, never pooled."""
    out = []
    if not path.exists():
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("gpu") not in TIERS:
                continue
            try:
                out.append({"gpu": r["gpu"], "tenor_months": int(float(r["tenor_months"])), "outcome": r["outcome"],
                            "line_items": int(float(r["line_items"])), "deals": int(float(r["deals"])), "gpus": int(float(r["gpus"] or 0)),
                            "first_ask_med": float(r["first_ask_med"]), "final_med": float(r["final_med"]), "ratio_med": float(r["ratio_med"]),
                            "share_revised": float(r["share_revised"]), "revision_med_pct": float(r["revision_med_pct"]) if r.get("revision_med_pct") else None,
                            "days_med": float(r["days_med"]), "window_from": r.get("window_from", ""), "window_to": r.get("window_to", ""),
                            "generated": r.get("generated_date", "")})
            except (KeyError, ValueError, TypeError):
                continue
    return out


def load_grid(path: Path = GRID_JSON) -> dict:
    """Nebius Finance reserve grid(s): {version: {segments: {seg: {tier: {months: {prepay: price}}}}}}."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("grids", {})
    except Exception:
        return {}


def load_sa_reference(as_of: date, path: Path = SA_REFERENCE_JSON) -> dict:
    """SemiAnalysis references per tier, as of a month: the modelled market rental price for
    the as-of month ('now'), the average of the modelled path over each tenor starting at the
    as-of month ('term_avg', what a customer would pay on average if the path came true), the
    Full-TCO cash cost per hour, SemiAnalysis' own IRR floor when computed, and the 5-year
    calibrated price. A model reference for the page: never pooled into a mark."""
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text())
    except Exception:
        return {}
    ym = as_of.strftime("%Y-%m")
    out = {"_source": d.get("_source"), "_version": d.get("_version"), "_extracted": d.get("_extracted"), "tiers": {}}
    for tier, t in d.get("tiers", {}).items():
        p = t.get("rental_path_monthly") or {}
        months = sorted(p)
        start = ym if ym in p else next((m for m in months if m >= ym), None)
        term_avg = {}
        for T in TENORS:
            if start is None:
                term_avg[T] = None
                continue
            window = [m for m in months if m >= start][:T]
            term_avg[T] = round(sum(p[m] for m in window) / T, 3) if len(window) == T else None
        out["tiers"][tier] = {
            "now": p.get(ym), "path_starts": months[0] if months else None, "term_avg": term_avg,
            "cost_per_hour": t.get("total_cost_per_hour"), "capex_per_gpu": t.get("capex_per_gpu_usd"),
            "opex_per_gpu_month": t.get("opex_per_gpu_month_usd"), "wacc": t.get("wacc"),
            "floor_irr_15_6": t.get("floor_irr_15_6"), "floor_irr_wacc": t.get("floor_irr_wacc"),
            "calibrated_5y_price": t.get("calibrated_5y_price"),
        }
    return out


def load_perf_multiples(path: Path = PERF_MULTIPLES_JSON) -> dict:
    """Delivered-performance multiples between generations for the parity ceiling; {} if absent."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def grid_reference(grids: dict, tier: str, tenor: int, segment: str = DEFAULT_SEGMENT):
    """Latest grid cell for (tier, tenor bucket) in a segment: {version, months, prices{prepay: p}} or None."""
    if not grids:
        return None
    version = max(grids)
    seg = grids[version].get("segments", {}).get(segment, {})
    cells = seg.get(tier, {})
    for m, prices in cells.items():
        if bucket_months(float(m)) == tenor:
            return {"version": version, "segment": segment, "months": int(float(m)),
                    "prices": {int(float(k)): v for k, v in prices.items()}}
    return None


def load_list(path: Path = HISTORY_CSV, as_of: date | None = None) -> dict:
    """Latest list references; expired manually verified Nebius commitments are withheld."""
    if not path.exists():
        return {}
    fresh_nebius, _, _ = committed_reference_fresh((as_of or date.today()).isoformat())
    rows = [r for r in load_comparison_history(path) if r.get("consumption_type") in LIST_TENOR]
    if not rows:
        return {}
    latest = max(r["snapshot_date"] for r in rows)
    out = defaultdict(lambda: {"nebius": None, "hyperscaler_min": None, "hyperscaler_min_provider": "",
                               "peer_min": None, "peer_min_provider": "", "snapshot_date": latest,
                               "hyper_list": {}, "peer_list": {}})
    for r in rows:
        if r["snapshot_date"] != latest:
            continue
        if r["provider"] == "nebius" and r["consumption_type"].startswith("committed") and not fresh_nebius:
            continue
        tier = r["gpu_model"].upper()
        tenor = bucket_months(LIST_TENOR[r["consumption_type"]])
        if tier not in TIERS or tenor is None:
            continue
        try:
            p = float(r["price_per_gpu_hour_usd"])
        except (ValueError, TypeError):
            continue
        cell = out[(tier, tenor)]
        prov, inst = r["provider"], r.get("instance_type") or ""
        if prov == "nebius":
            cell["nebius"] = p if cell["nebius"] is None else min(cell["nebius"], p)
        elif prov in HYPERSCALERS:
            if cell["hyperscaler_min"] is None or p < cell["hyperscaler_min"]:
                cell["hyperscaler_min"], cell["hyperscaler_min_provider"] = p, prov
            if prov not in cell["hyper_list"] or p < cell["hyper_list"][prov][0]:
                cell["hyper_list"][prov] = (p, inst)
        else:
            if cell["peer_min"] is None or p < cell["peer_min"]:
                cell["peer_min"], cell["peer_min_provider"] = p, prov
            if prov not in cell["peer_list"] or p < cell["peer_list"][prov][0]:
                cell["peer_list"][prov] = (p, inst)
    for cell in out.values():   # every published reserved/committed list price per provider, cheapest SKU per provider
        cell["hyper_list"] = [{"provider": k, "price": round(v[0], 4), "instance_type": v[1]} for k, v in sorted(cell["hyper_list"].items(), key=lambda kv: kv[1][0])]
        cell["peer_list"] = [{"provider": k, "price": round(v[0], 4), "instance_type": v[1]} for k, v in sorted(cell["peer_list"].items(), key=lambda kv: kv[1][0])]
    return dict(out)


# ----------------------------------------------------------------------------- other evidence classes (shown, never pooled)
def load_index(path: Path = INDEX_QUOTES_CSV) -> dict:
    """Survey or index prices (the SemiAnalysis GPU Pricing Index draft rows that used to sit in
    intel.csv as if they were offers): {tier: [{tenor, months, prepay, price, p0, source, ...}]}.
    An index is a statistic over a market, not an offer, so it is a labelled reference and never
    enters a mark. p0 = the price re-based to 0% prepayment with the Finance convention."""
    out: dict = {}
    if not path.exists():
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            tier = (r.get("gpu_model") or "").strip().upper()
            if tier not in TIERS:
                continue
            try:
                price = float(r["price_per_gpu_hour_usd"]); months = float(r.get("tenor_months") or 0)
                prepay = float(r.get("prepay_pct") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            tenor = bucket_months(months)
            if tenor is None:
                continue
            out.setdefault(tier, []).append({"source": r.get("source", ""), "as_of": r.get("as_of", ""), "tenor": tenor, "months": months,
                                             "prepay": prepay, "price": price, "p0": round(prepay_normalise(price, prepay, months), 4),
                                             "stat": r.get("stat", "median"), "notes": r.get("notes", "")})
    return out


def load_crm_asks(path: Path = CRM_ASKS_CSV, as_of: date | None = None, min_deals: int = CRM_ASK_MIN_DEALS,
                  quote_path: Path | None = QUOTE_ASKS_CSV) -> dict:
    """Nebius' own asked prices that did not (yet) sign, from the CRM deal-review table through
    scripts/refresh_crm_asks.py, per tier x tenor x class:
      lost      asked prices on deals in stage Closed lost; this class does not distinguish
                loss reasons or establish willingness to pay, acceptance or a competitor price
      proposal  deals in Commercial Proposal or Agreement signing: what we are asking now
    Two files in one schema: the HubSpot deal-review aggregates (path; that mirror froze at the
    2026-08-10 cutover) and the live Salesforce quote aggregates (quote_path, daily). Once the quote
    file has rows, the HubSpot 'proposal' rows are dropped: they are frozen open asks that would
    otherwise look current. 'lost' rows from both eras are kept (they are disjoint in time).
    Each file holds one row per month x payment bucket (deal count, GPU sum, lo/median/hi).
    Each class cell merges its rows over the last CRM_ASK_WINDOW_DAYS; the price is the
    deal-weighted median of the monthly medians re-based to 0% prepayment through the stated
    prepay_pct where the row has one (Salesforce), else the payment-type proxy (HubSpot); no
    quote-date adjustment, stated on the page. Cells under min_deals are counted and their prices
    withheld. Never pooled into a mark."""
    as_of = as_of or date.today()
    out: dict = {"cells": {}, "generated": None, "min_deals": min_deals, "window_days": CRM_ASK_WINDOW_DAYS,
                 "withheld_cells": 0, "deals": {"lost": 0, "proposal": 0}, "sources": [], "proposal_source": None}
    files = []
    for fp, src in ((path, "hubspot"), (quote_path, "salesforce_quotes")):
        if fp is None or not fp.exists():
            continue
        with open(fp, newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            files.append((src, rows)); out["sources"].append(src)
    live_quotes = any(src == "salesforce_quotes" for src, _ in files)
    out["proposal_source"] = "salesforce_quotes" if live_quotes else ("hubspot" if files else None)
    if not files:
        return out
    grp: dict = defaultdict(list)
    for src, rows in files:
        for r in rows:
            out["generated"] = max(out["generated"] or "", r.get("generated_date") or "") or None
            tier = (r.get("gpu") or "").strip().upper(); cls = (r.get("stage_class") or "").strip()
            if tier not in TIERS or cls not in ("lost", "proposal"):
                continue
            if src == "hubspot" and cls == "proposal" and live_quotes:
                continue   # frozen open asks, superseded by the live quote file
            d = _parse_date(r.get("close_month", ""))
            tenor = bucket_months(r.get("tenor_months"))
            if not d or tenor is None or (as_of - d).days > CRM_ASK_WINDOW_DAYS:
                continue
            try:
                med = float(r["price_med"]); lo = float(r.get("price_lo") or med); hi = float(r.get("price_hi") or med)
                deals = int(float(r.get("deals") or 1)); gpus = float(r.get("gpus") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(v) for v in (med, lo, hi)):
                continue
            if not math.isfinite(gpus):
                gpus = 0.0   # rack-priced lines without a known GPUs-per-rack sum to NaN in the export
            try:
                pp = float(r.get("prepay_pct"))              # stated percentage (Salesforce quotes)
                if not math.isfinite(pp):
                    raise ValueError
            except (TypeError, ValueError):
                pp = float(PREPAY_BUCKET_PCT.get((r.get("prepay_bucket") or "postpaid").strip(), 0))
            grp[(tier, tenor, cls)].append({"p0": prepay_normalise(med, pp, tenor), "lo0": prepay_normalise(lo, pp, tenor),
                                            "hi0": prepay_normalise(hi, pp, tenor), "deals": deals, "gpus": gpus, "month": d, "src": src})
    for (tier, tenor, cls), rows in sorted(grp.items()):
        deals = sum(x["deals"] for x in rows)
        out["deals"][cls] += deals
        entry = {"deals": deals, "months": len({x["month"] for x in rows}), "gpus": round(sum(x["gpus"] for x in rows)),
                 "first_month": min(x["month"] for x in rows).isoformat()[:7], "latest_month": max(x["month"] for x in rows).isoformat()[:7],
                 "withheld": deals < min_deals, "sources": sorted({x["src"] for x in rows})}
        if deals >= min_deals:
            entry.update({"p0": round(weighted_median([x["p0"] for x in rows], [x["deals"] for x in rows]), 2),
                          "lo": round(min(x["lo0"] for x in rows), 2), "hi": round(max(x["hi0"] for x in rows), 2)})
        else:
            out["withheld_cells"] += 1
        out["cells"].setdefault(tier, {}).setdefault(tenor, {})[cls] = entry
    return out


def load_node_specs(path: Path = NODE_SPECS_JSON) -> dict:
    """Node configuration behind each provider's priced GPU SKU (GPUs per node, vCPU, RAM,
    local NVMe, network), keyed by normalised provider key then tier; see
    scripts/merge_node_specs.py. Display-only: prices are never adjusted for configuration,
    the page shows the configuration beside the price so the reader can judge like for like."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def provider_key(p: str) -> str:
    """History provider id -> node_specs key (drop the computeprices 'cp_' prefix and '-com' suffix)."""
    p = (p or "").strip().lower()
    if p.startswith("cp_"):
        p = p[3:]
    if p.endswith("-com"):
        p = p[:-4]
    return {"vast_reserved": "vast", "lambda_labs": "lambda"}.get(p, p)


_EXCHANGES = {"sfcompute": "SF Compute"}


def load_short_term(history: Path = HISTORY_CSV, snapshot: Path = LATEST_SNAPSHOT_JSON, as_of: date | None = None) -> dict:
    """Short-term market prices outside the committed-contract classes, per tier, sorted by price:
      clearing     exchange clearing price (SF Compute H100: 8-GPU InfiniBand nodes booked for hours to
                   weeks), from the latest history.csv snapshot -> shown against the PAYG cell
      marketplace  cheapest marketplace short reservation (Vast.ai, 1-6 months prepaid, usually 1-2 GPU
                   hosts), from the latest daily snapshot (not kept in history.csv) -> the 3-month cell
    Entries under 8 GPUs are flagged not cluster-class; a snapshot older than 7 days is flagged stale.
    Labelled references, never pooled."""
    as_of = as_of or date.today()
    out: dict = {t: [] for t in TIERS}
    if history.exists():
        rows = [r for r in load_comparison_history(history)
                if r.get("consumption_type") == "spot" and r.get("provider") in _EXCHANGES]
        if rows:
            latest = max(r["snapshot_date"] for r in rows)
            for r in rows:
                if r["snapshot_date"] != latest:
                    continue
                tier = (r.get("gpu_model") or "").upper()
                if tier not in TIERS:
                    continue
                try:
                    p = float(r["price_per_gpu_hour_usd"]); gc = int(float(r.get("gpu_count") or 0))
                except (KeyError, TypeError, ValueError):
                    continue
                d = _parse_date(latest)
                out[tier].append({"provider": _EXCHANGES[r["provider"]], "key": provider_key(r["provider"]), "kind": "clearing", "tenor": 0, "months": 0, "price": round(p, 4),
                                  "gpus": gc, "date": latest, "stale": bool(d and (as_of - d).days > 7), "cluster_class": gc >= 8,
                                  "note": "exchange clearing price for short bookings (hours to weeks) on 8-GPU InfiniBand nodes; a spot market, not a committed contract"})
    if snapshot.exists():
        try:
            rows = json.loads(snapshot.read_text())
        except (OSError, ValueError):
            rows = []
        rows = rows if isinstance(rows, list) else []
        dates = [str(r.get("fetched_at", ""))[:10] for r in rows if r.get("fetched_at")]
        snap = max(dates) if dates else None
        sd = _parse_date(snap or "")
        stale = not sd or (as_of - sd).days > 7
        for r in rows:
            if r.get("consumption_type") != "reserved_short":
                continue
            tier = str(r.get("gpu_model", "")).upper()
            if tier not in TIERS:
                continue
            try:
                p = float(r["price_per_gpu_hour_usd"]); gc = int(float(r.get("gpu_count") or 0))
            except (KeyError, TypeError, ValueError):
                continue
            prov = str(r.get("provider", ""))
            out[tier].append({"provider": "Vast.ai" if prov.startswith("vast") else prov, "key": provider_key(prov), "kind": "marketplace", "tenor": 3, "months": 3,
                              "price": round(p, 4), "gpus": gc, "date": snap, "stale": stale, "cluster_class": gc >= 8,
                              "note": f"cheapest marketplace reservation (1-6 months prepaid), {gc}-GPU host, {r.get('region', '')}"
                                      + ("" if gc >= 8 else "; not cluster-class")})
    return {t: sorted(v, key=lambda x: x["price"]) for t, v in out.items() if v}


# ----------------------------------------------------------------------------- model
def quarter_effects(obs: list[dict], as_of: date) -> dict:
    """Two-way fixed effects on log(p0): cell(tier,tenor) + quarter. Returns quarter -> log effect,
    normalised so the as-of quarter (or the latest quarter with data) is 0."""
    if not obs:
        return {}
    cells, q_eff = {}, defaultdict(float)
    for _ in range(FE_ITERATIONS):
        g = defaultdict(lambda: [0.0, 0.0])
        for o in obs:
            k = (o["tier"], o["tenor"])
            g[k][0] += o["weight"] * (o["logp0"] - q_eff[o["quarter"]])
            g[k][1] += o["weight"]
        cells = {k: v[0] / v[1] for k, v in g.items()}
        g = defaultdict(lambda: [0.0, 0.0])
        for o in obs:
            g[o["quarter"]][0] += o["weight"] * (o["logp0"] - cells[(o["tier"], o["tenor"])])
            g[o["quarter"]][1] += o["weight"]
        q_eff = {k: v[0] / v[1] for k, v in g.items()}
    ref_q = quarter_of(as_of)
    if ref_q not in q_eff:
        ref_q = max(q_eff)
    ref = q_eff[ref_q]
    return {k: v - ref for k, v in sorted(q_eff.items())}


ASK_MIN_DEALS = 3   # Nebius achieved prices are pooled and shown only as aggregates of at least this many deals


def aggregate_asks(asks: list[dict], min_deals: int = ASK_MIN_DEALS) -> list[dict]:
    """Merge per-month Nebius achieved rows into aggregates of >= min_deals deals so that
    no single deal's price is published: first per (tier, tenor, payment bucket, quarter),
    then per (tier, tenor, quarter) across buckets, then per (tier, tenor) over the whole
    window. Whatever is still under min_deals is kept for counting only (withheld=True: no
    price, weight 0, never pooled). Weights are preserved (sum of the member weights);
    prices are deal-weighted medians of the members' raw, 0%-basis and date-adjusted prices."""
    def merge(group):
        group = sorted(group, key=lambda o: o["date"])
        dw = [float(o.get("deals", 1)) for o in group]
        base = dict(group[-1])
        base.update({
            "price_raw": weighted_median([o["price_raw"] for o in group], dw),
            "p0": weighted_median([o["p0"] for o in group], dw),
            "p_adj": weighted_median([o["p_adj"] for o in group], dw),
            "months": sum(o["months"] * d for o, d in zip(group, dw)) / sum(dw),
            "prepay_pct": group[0]["prepay_pct"] if len({o["prepay_pct"] for o in group}) == 1 else None,
            "prepay_bucket": group[0].get("prepay_bucket") if len({o.get("prepay_bucket") for o in group}) == 1 else "mixed",
            "deals": int(sum(dw)), "weight": sum(o["weight"] for o in group), "lines": sum(o.get("lines", 1) for o in group),
            "date_from": min(o.get("date_from", o["date"]) for o in group), "age_days": min(o["age_days"] for o in group),
        })
        base["logp0"] = math.log(base["p0"])
        return base

    def level(rows, keyf):
        groups = defaultdict(list)
        for o in rows:
            groups[keyf(o)].append(o)
        return [merge(g) for g in groups.values()]

    out, rest = [], list(asks)
    for keyf in (lambda o: (o["tier"], o["tenor"], o.get("prepay_bucket"), o["quarter"]),
                 lambda o: (o["tier"], o["tenor"], o["quarter"]),
                 lambda o: (o["tier"], o["tenor"])):
        if not rest:
            break
        merged = level(rest, keyf)
        out += [m for m in merged if m["deals"] >= min_deals]
        rest = [m for m in merged if m["deals"] < min_deals]
    for m in rest:
        m.update({"withheld": True, "weight": 0.0})
    return sorted(out + rest, key=lambda o: (o["tier"], o["tenor"], o["date"]))


def build(as_of: date | None = None, intel=INTEL_CSV, reserve=RESERVE_TENOR_CSV,
          history=HISTORY_CSV, contracts=CONTRACTS_CSV, grid=GRID_JSON, segment=DEFAULT_SEGMENT,
          crm_asks=CRM_ASKS_CSV, index=INDEX_QUOTES_CSV, snapshot=LATEST_SNAPSHOT_JSON) -> dict:
    as_of = as_of or date.today()
    raw = load_bid(intel) + load_ask(reserve) + load_contracts(contracts)
    grids = load_grid(grid)
    # on-demand competitor quotes (term 0) for the 0-month anchor, deduplicated like the rest
    od_quotes = []
    if intel.exists():
        kept, _ = intel_dedupe(list(csv.DictReader(open(intel, newline=""))))
        for r in kept:
            try:
                if float(r.get("term_months") or 0) != 0:
                    continue
                # term 0 in intel.csv also means "term unknown"; only rows that say on-demand count here
                if not re.search(r"on[- ]?demand|\bpayg\b|pay[- ]as[- ]you[- ]go|hourly|no commit|\bOD\b", str(r.get("notes", "")), re.I):
                    continue
                d = _parse_date(r.get("message_date", "")); tier = (r.get("gpu_model") or "").upper()
                if d and tier in TIERS:
                    od_quotes.append({"tier": tier, "months": 0, "date": d, "price_raw": float(r["price_per_gpu_hour_usd"]),
                                      "provider": r.get("provider_name", ""), "ts": str(r.get("message_ts", ""))})
            except (TypeError, ValueError):
                continue
    obs = []
    for o in raw:
        age = (as_of - o["date"]).days
        if age < -3 or age > MAX_AGE_DAYS:
            continue
        p0 = prepay_normalise(o["price_raw"], o["prepay_pct"], o.get("months") or o["tenor"])
        if not (PRICE_MIN <= p0 <= PRICE_MAX):
            continue
        o = dict(o, age_days=age, p0=p0, logp0=math.log(p0), quarter=quarter_of(o["date"]),
                 weight=o["weight"] * (1.0 if age <= RECENT_DAYS else 0.5))
        obs.append(o)
    # date normalisation is fitted on the same evidence that enters the marks: offers that state
    # their prepayment plus Nebius achieved; unstated offers are only re-expressed with the result
    fit_obs = [o for o in obs if o["weight"] > 0 and (o["side"] != "bid" or o.get("known"))]
    q_eff = quarter_effects(fit_obs, as_of)
    for o in obs:
        o["p_adj"] = math.exp(o["logp0"] - q_eff.get(o["quarter"], 0.0))
    # Nebius achieved only as aggregates of >= ASK_MIN_DEALS deals (no single deal's price is published)
    obs = [o for o in obs if o["side"] != "ask"] + aggregate_asks([o for o in obs if o["side"] == "ask"])

    lists = load_list(history, as_of=as_of)
    by_cell = defaultdict(list)
    for o in obs:
        by_cell[(o["tier"], o["tenor"])].append(o)

    marks = []
    for tier in TIERS:
        for tenor in TENORS:
            allc = by_cell.get((tier, tenor), [])
            bids_all = [o for o in allc if o["side"] == "bid"]
            bids = [o for o in bids_all if o.get("known")]          # offers that state their prepayment
            bids_unstated = [o for o in bids_all if not o.get("known")]
            asks_all = [o for o in allc if o["side"] == "ask"]       # Nebius achieved aggregates (payment type known)
            asks = [o for o in asks_all if not o.get("withheld")]
            asks_withheld = [o for o in asks_all if o.get("withheld")]
            pubs = [o for o in allc if o["side"] == "public"]
            cell = bids + asks                                       # the mark's evidence: one payment basis
            cell_all = bids_all + asks                               # reference only: unstated terms counted at 0%
            ask_deals = sum(o.get("deals", 0) for o in asks)
            ask_deals_withheld = sum(o.get("deals", 0) for o in asks_withheld)
            # n counts distinct observations: one per competitor offer, one per signed Nebius deal
            # (deals are pooled as aggregates only for confidentiality, not because they are one observation)
            obs_count = lambda rows: sum(o.get("deals", 1) if o["side"] == "ask" else 1 for o in rows)  # noqa: E731
            n, n_recent = obs_count(cell), obs_count([o for o in cell if o["age_days"] <= RECENT_DAYS])
            providers = {(o.get("provider") or "").strip().lower() for o in bids if o.get("provider")} | ({"nebius"} if asks else set())
            recent_raw = [o["price_raw"] for o in cell if o["age_days"] <= 90]
            recent_adj = [o for o in cell if o["age_days"] <= RECENT_DAYS]
            ref = lists.get((tier, tenor), {})
            gref = grid_reference(grids, tier, tenor, segment)
            entry = {
                "tier": tier, "tenor_months": tenor, "label": TENOR_LABEL[tenor],
                "n_obs": n, "n_recent": n_recent, "n_bid": len(bids), "n_ask": len(asks), "n_public": len(pubs),
                "n_known": n, "n_bid_unstated": len(bids_unstated), "n_all": obs_count(cell_all),
                "n_providers": len(providers), "ask_deals": ask_deals,
                "n_ask_withheld": len(asks_withheld), "ask_deals_withheld": ask_deals_withheld,
                "achieved_only": bool(asks) and not bids,
                "n_old": n - n_recent,
                "recent_raw_median": round(statistics.median(recent_raw), 2) if recent_raw else None,
                "n_recent90": len(recent_raw),
                "mark_recent": (round(weighted_median([o["p0"] for o in recent_adj], [o["weight"] for o in recent_adj]), 2)
                                if len(recent_adj) >= MIN_OBS else None),
                "mark_known": None,   # set below: equals the mark (stated-prepay basis) when published
                "mark_all": (round(weighted_median([o["p_adj"] for o in cell_all], [o["weight"] for o in cell_all]), 2)
                             if len(cell_all) >= MIN_OBS else None),
                "public_median": round(statistics.median([o["p_adj"] for o in pubs]), 2) if pubs else None,
                "grid": gref,
                "grid_100": (gref or {}).get("prices", {}).get(100),
                "grid_50": (gref or {}).get("prices", {}).get(50),
                "bid_median": round(weighted_median([o["p_adj"] for o in bids],
                                                    [o["weight"] for o in bids]), 2) if bids else None,
                "bid_median_all": round(weighted_median([o["p_adj"] for o in bids_all],
                                                        [o["weight"] for o in bids_all]), 2) if bids_all else None,
                "bid_median_unstated": round(weighted_median([o["p_adj"] for o in bids_unstated],
                                                             [o["weight"] for o in bids_unstated]), 2) if bids_unstated else None,
                "ask_median": round(weighted_median([o["p_adj"] for o in asks],
                                                    [o["weight"] for o in asks]), 2) if asks else None,
                "list_nebius": ref.get("nebius"),
                "list_hyperscaler_min": ref.get("hyperscaler_min"),
                "list_hyperscaler_provider": ref.get("hyperscaler_min_provider", ""),
                "list_hyperscalers": ref.get("hyper_list", []),
                "list_peers": ref.get("peer_list", []),
                "list_peer_min": ref.get("peer_min"),
                "list_peer_provider": ref.get("peer_min_provider", ""),
                "cost_floor": SA_COST_FLOOR.get(tier),
            }
            if n >= MIN_OBS:
                vals, wts = [o["p_adj"] for o in cell], [o["weight"] for o in cell]
                mark = weighted_median(vals, wts)
                recent_vals = [o["p_adj"] for o in cell if o["age_days"] <= RECENT_DAYS] or vals
                good = n >= GOOD_OBS and n_recent >= 2 and len(providers) >= 2
                entry.update({
                    "mark": round(mark, 2), "mark_known": round(mark, 2), "has_mark": True,
                    "range_lo": round(min(recent_vals), 2), "range_hi": round(max(recent_vals), 2),
                    "range_recent": bool([o for o in cell if o["age_days"] <= RECENT_DAYS]),
                    "confidence": "good" if good else "thin",
                    "confidence_reason": (None if good else
                                          f"fewer than {GOOD_OBS} observations" if n < GOOD_OBS else
                                          "one provider only" if len(providers) < 2 else
                                          "fewer than 2 observations in the last 120 days"),
                    "spread_pct": (round(entry["ask_median"] / entry["bid_median"] - 1, 3)
                                   if entry["bid_median"] and entry["ask_median"] else None),
                    "reason": None,
                })
            else:
                entry.update({"mark": None, "has_mark": False, "range_lo": None, "range_hi": None, "range_recent": None,
                              "confidence": "suppressed", "confidence_reason": None, "spread_pct": None,
                              "reason": (f"insufficient_data ({n} stated-prepay obs in {MAX_AGE_DAYS}d, need {MIN_OBS}"
                                         + (f"; {len(bids_unstated)} more with unstated terms" if bids_unstated else "") + ")")})
            marks.append(entry)

    shape = []
    for tier in TIERS:
        m = {e["tenor_months"]: e for e in marks if e["tier"] == tier}
        def mk(t):
            return m[t]["mark"] if m.get(t, {}).get("has_mark") else None
        m3, m12, m36 = mk(3), mk(12), mk(36)
        shape.append({
            "tier": tier,
            "mark_3m": m3, "mark_12m": m12, "mark_36m": m36, "mark_60m": mk(60),
            "slope_12_36_pct": round(m36 / m12 - 1, 3) if (m12 and m36) else None,
            "short_end_premium_pct": round(m3 / m12 - 1, 3) if (m3 and m12) else None,
            "structure": (None if not (m12 and m36) else
                          "backwardation" if m36 < m12 * 0.98 else
                          "contango" if m36 > m12 * 1.02 else "flat"),
            "cost_floor": SA_COST_FLOOR.get(tier),
        })

    # anonymised per-observation export for the interactive view (client-side filters)
    export = []
    for o in obs:
        withheld = bool(o.get("withheld"))
        row = {"side": o["side"], "tier": o["tier"], "tenor": o["tenor"], "months": round(o.get("months") or 0, 2),
               "date": o["date"].isoformat(), "price": None if withheld else round(o["price_raw"], 3),
               "p0": None if withheld else round(o["p0"], 5), "pa": None if withheld else round(o["p_adj"], 5),
               "q": o["quarter"], "prepay": o["prepay_pct"], "known": bool(o.get("known")),
               "w": o["weight"], "ptype": o.get("provider_type", ""), "provider": o.get("provider", ""),
               "ts": o.get("ts", ""), "review": bool(o.get("review")), "similar": o.get("similar_provider", "")}
        if o["side"] == "ask":
            row.update({"deals": o.get("deals"), "bucket": o.get("prepay_bucket"), "withheld": withheld,
                        "lines": o.get("lines", 1), "date_from": o.get("date_from", o["date"]).isoformat()})
            row["provider"] = "Nebius"
        export.append(row)

    src_dates = {"intel_latest": max((o["date"] for o in raw if o["side"] == "bid"), default=None),
                 "reserve_tenor_generated": None, "contracts": sum(1 for o in raw if o["side"] == "public"),
                 "grid_version": max(grids) if grids else None}
    if RESERVE_TENOR_CSV.exists():
        with open(RESERVE_TENOR_CSV, newline="") as f:
            first = next(csv.DictReader(f), None)
            src_dates["reserve_tenor_generated"] = (first or {}).get("generated_date")
    list_date = next((v["snapshot_date"] for v in lists.values()), None)
    committed_fresh, committed_verified, _ = committed_reference_fresh(as_of.isoformat())

    return {
        "as_of": as_of.isoformat(),
        "method_version": METHOD_VERSION,
        "params": {"prepay_a": PREPAY_A, "prepay_m": PREPAY_M, "prepay_cap": PREPAY_CAP,
                   "prepay_bucket_pct": PREPAY_BUCKET_PCT, "segment": segment,
                   "recent_days": RECENT_DAYS, "max_age_days": MAX_AGE_DAYS,
                   "min_obs": MIN_OBS, "good_obs": GOOD_OBS, "price_band": [PRICE_MIN, PRICE_MAX],
                   "tenors_months": TENORS, "tiers": TIERS},
        "quarter_effects_log": {k: round(v, 4) for k, v in q_eff.items()},
        "n_observations": {"bid": sum(1 for o in obs if o["side"] == "bid"),
                           "ask_withheld": sum(1 for o in obs if o["side"] == "ask" and o.get("withheld")),
                           "ask_deals_withheld": sum(o.get("deals", 0) for o in obs if o["side"] == "ask" and o.get("withheld")),
                           "ask_min_deals": ASK_MIN_DEALS,
                           "bid_known_prepay": sum(1 for o in obs if o["side"] == "bid" and o.get("known")),
                           "bid_unstated_prepay": sum(1 for o in obs if o["side"] == "bid" and not o.get("known")),
                           "bid_duplicates_removed": getattr(load_bid, "duplicates", 0),
                           "bid_review": getattr(load_bid, "review", 0),
                           "bid_in_window_review": sum(1 for o in obs if o["side"] == "bid" and o.get("review")),
                           "ask": sum(1 for o in obs if o["side"] == "ask" and not o.get("withheld")),
                           "public": sum(1 for o in obs if o["side"] == "public"),
                           "ask_deals": sum(o.get("deals", 0) for o in obs if o["side"] == "ask" and not o.get("withheld"))},
        "sources": {"intel_latest_quote": src_dates["intel_latest"].isoformat() if src_dates["intel_latest"] else None,
                    "reserve_tenor_generated": src_dates["reserve_tenor_generated"],
                    "public_contracts": src_dates["contracts"], "grid_version": src_dates["grid_version"],
                    "list_snapshot": list_date, "cost_floor": SA_COST_FLOOR_SOURCE,
                    "nebius_committed_reference_verified": committed_verified,
                    "nebius_committed_reference_eligible": committed_fresh},
        "grid": {"version": max(grids) if grids else None,
                 "segments": {k: {t: {int(float(m)): {int(float(pp)): v for pp, v in ps.items()} for m, ps in cells.items()}
                                  for t, cells in seg.items()}
                              for k, seg in (grids[max(grids)]["segments"].items() if grids else [])}},
        "marks": marks,
        "shape": shape,
        "observations": export,
        "economics": (json.loads(ECONOMICS_JSON.read_text()) if ECONOMICS_JSON.exists() else {}),
        "on_demand": load_on_demand(history, od_quotes, as_of=as_of),
        "cohorts": load_cohorts(),
        "ask_paths": load_ask_paths(),
        "sa": load_sa_reference(as_of),
        "perf": load_perf_multiples(),
        # other evidence classes: shown on the page with their own labels, never pooled into a mark
        "index": load_index(index),
        "crm_asks": load_crm_asks(crm_asks, as_of=as_of),
        "short_term": load_short_term(history, snapshot, as_of=as_of),
        "node_specs": load_node_specs(),
    }


# ----------------------------------------------------------------------------- charts
_COLOURS = {"H100": "#4a90d9", "H200": "#7b61ff", "B200": "#00b97a", "B300": "#f5c842",
            "GB200": "#e09030", "GB300": "#e05252", "VR": "#c94fb0"}


def render_svg(result: dict, width=880, height=420) -> str:
    """Dependency-free line chart: mark vs tenor per tier (log-x), suppressed cells omitted."""
    pad_l, pad_r, pad_t, pad_b = 56, 150, 28, 44
    W, H = width - pad_l - pad_r, height - pad_t - pad_b
    xs = TENORS
    lx = [math.log(x) for x in xs]
    x0, x1 = min(lx), max(lx)
    marks = [e for e in result["marks"] if e["has_mark"]]
    if not marks:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><text x="20" y="40">no marks</text></svg>'
    ymax = max(e["mark"] for e in marks) * 1.12
    ymin = 0.0

    def X(t):
        return pad_l + (math.log(t) - x0) / (x1 - x0) * W

    def Y(p):
        return pad_t + H - (p - ymin) / (ymax - ymin) * H

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="-apple-system,Segoe UI,Roboto,sans-serif" font-size="12">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    # grid + axes
    step = 1.0 if ymax <= 8 else 2.0
    y = 0.0
    while y <= ymax:
        out.append(f'<line x1="{pad_l}" y1="{Y(y):.1f}" x2="{pad_l + W}" y2="{Y(y):.1f}" stroke="#e6e8ef"/>')
        out.append(f'<text x="{pad_l - 8}" y="{Y(y) + 4:.1f}" text-anchor="end" fill="#555">${y:.0f}</text>')
        y += step
    for t in xs:
        out.append(f'<line x1="{X(t):.1f}" y1="{pad_t}" x2="{X(t):.1f}" y2="{pad_t + H}" stroke="#f0f1f5"/>')
        out.append(f'<text x="{X(t):.1f}" y="{pad_t + H + 18}" text-anchor="middle" fill="#555">{TENOR_LABEL[t]}</text>')
    out.append(f'<text x="{pad_l + W / 2:.0f}" y="{height - 8}" text-anchor="middle" fill="#333">commitment length (log scale)</text>')
    out.append(f'<text x="14" y="{pad_t + H / 2:.0f}" transform="rotate(-90 14 {pad_t + H / 2:.0f})" text-anchor="middle" fill="#333">$/GPU-hr, 0% prepay, as-of {result["as_of"]}</text>')
    # series
    legend_y = pad_t + 4
    for tier in TIERS:
        pts = [(e["tenor_months"], e["mark"], e["confidence"]) for e in marks if e["tier"] == tier]
        if not pts:
            continue
        c = _COLOURS[tier]
        path = " ".join(f'{"M" if i == 0 else "L"}{X(t):.1f},{Y(p):.1f}' for i, (t, p, _) in enumerate(pts))
        if len(pts) > 1:
            out.append(f'<path d="{path}" fill="none" stroke="{c}" stroke-width="2.2"/>')
        for t, p, conf in pts:
            r, fill = (4.5, c) if conf == "good" else (4.0, "#ffffff")
            out.append(f'<circle cx="{X(t):.1f}" cy="{Y(p):.1f}" r="{r}" fill="{fill}" stroke="{c}" stroke-width="2"><title>{tier} {TENOR_LABEL[t]}: ${p:.2f} ({conf})</title></circle>')
        out.append(f'<rect x="{pad_l + W + 16}" y="{legend_y - 9}" width="14" height="3" fill="{c}"/>')
        out.append(f'<text x="{pad_l + W + 36}" y="{legend_y - 4}" fill="#222">{tier}</text>')
        legend_y += 18
    out.append(f'<text x="{pad_l + W + 16}" y="{legend_y + 6}" fill="#666" font-size="11">filled = good (n≥{GOOD_OBS})</text>')
    out.append(f'<text x="{pad_l + W + 16}" y="{legend_y + 22}" fill="#666" font-size="11">hollow = thin (n {MIN_OBS}–{GOOD_OBS - 1})</text>')
    out.append(f'<text x="{pad_l + W + 16}" y="{legend_y + 38}" fill="#666" font-size="11">missing = suppressed</text>')
    out.append("</svg>")
    return "\n".join(out)


def render_png(result: dict, path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    fig, ax = plt.subplots(figsize=(9.5, 4.6), dpi=130)
    for tier in TIERS:
        pts = [(e["tenor_months"], e["mark"], e["confidence"]) for e in result["marks"]
               if e["tier"] == tier and e["has_mark"]]
        if not pts:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        ax.plot(xs, ys, "-", color=_COLOURS[tier], lw=2, label=tier)
        for t, p, conf in pts:
            ax.plot([t], [p], "o", ms=6, mfc=_COLOURS[tier] if conf == "good" else "white",
                    mec=_COLOURS[tier], mew=1.8)
    ax.set_xscale("log")
    ax.set_xticks(TENORS)
    ax.set_xticklabels([TENOR_LABEL[t] for t in TENORS])
    ax.minorticks_off()
    ax.set_ylim(bottom=0)
    ax.set_xlabel("commitment length (log scale)")
    ax.set_ylabel(f"$/GPU-hr, 0% prepay, as-of {result['as_of']}")
    ax.set_title("Internal GPU forward curve — marks (filled = good, hollow = thin)", fontsize=11)
    ax.grid(True, axis="y", alpha=.3)
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True


# ----------------------------------------------------------------------------- HTML
def _fmt(p, nd=2):
    return f"${p:.{nd}f}" if isinstance(p, (int, float)) else "—"


def _pct(x):
    return f"{x:+.0%}" if isinstance(x, (int, float)) else "—"


def _range(lo, hi):
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return "—"
    return _fmt(lo) if abs(hi - lo) < 0.005 else f"{_fmt(lo)}–{_fmt(hi)}"


def _lozenge(text, colour):
    return f'<span data-type="status" data-color="{colour}">{text}</span>'


def _mark_cell(e: dict) -> str:
    """Compact overview; full counts, ranges and failure reasons remain in evidence."""
    if not e["has_mark"]:
        return _lozenge("n/a", "grey") + f'<br/><em>{e["n_obs"]} obs</em>'
    label = "achieved only" if e.get("achieved_only") else e["confidence"]
    colour = "blue" if e.get("achieved_only") else "green" if e["confidence"] == "good" else "yellow"
    return (f'<strong>{_fmt(e["mark"])}</strong> {_lozenge(label, colour)}'
            f'<br/><em>{e["n_bid"]} offers · {e["ask_deals"]} Nebius deals</em>')


def _input_dates(rows: list[dict]) -> str:
    dates = sorted({str(r["generated"]) for r in rows if r.get("generated")})
    return escape(", ".join(dates) if dates else "not available")


def _cohort_window(source: str, start: str, end: str) -> str:
    # The HubSpot query excludes the cutover date; Salesforce is an extraction as of end.
    until = "to before" if source == "hubspot" else "to"
    return f"{escape(start or 'unknown')} {until} {escape(end or 'unknown')}"


COHORT_LABELS = (("won", "closed won"), ("lost_capacity", "lost: capacity"),
                 ("lost_price_or_competitor", "lost: price or competitor"))


def _render_cohorts(coh: list[dict]) -> str:
    """Preserve each source's line-item median; aggregate medians cannot be pooled."""
    h = ['<p>Each cell is a source-specific line-item median in $/GPU-hr as recorded at close '
         '(opportunities; lines; GPUs), with at least 2 opportunities. Sources and close windows '
         'remain separate; no combined median is inferred. These references never enter a mark. '
         'Loss labels are CRM reason categories: capacity does not establish price acceptance, '
         'and price or competitor does not establish a willingness-to-pay ceiling.</p>']
    groups = defaultdict(list)
    for c in coh:
        groups[(c.get("source", ""), c.get("window_from", ""), c.get("window_to", ""), c.get("generated", ""))].append(c)
    for (source, start, end, generated), rows in sorted(groups.items()):
        label = {"hubspot": "HubSpot", "salesforce": "Salesforce"}.get(source, source or "Unknown source")
        h.append(f'<h3>{escape(label)}</h3><p>Close window: {_cohort_window(source, start, end)}; '
                 f'input generated {escape(generated or "unknown")}.</p>')
        h.append('<table data-layout="wide"><thead><tr><th>GPU</th><th>Recorded outcome</th>' +
                 "".join(f'<th>{TENOR_LABEL[t]}</th>' for t in TENORS) + '</tr></thead><tbody>')
        for tier in TIERS:
            for outcome, outcome_label in COHORT_LABELS:
                cells = []
                for tenor in TENORS:
                    cs = [c for c in rows if c["gpu"] == tier and c["outcome"] == outcome and c["tenor_months"] == tenor]
                    cells.append('<br/>'.join(
                        f'{_fmt(c["med"])} <em>({c["opps"]}; {c.get("lines", "—")}; {c["gpus"]:,})</em>'
                        for c in cs) if cs else "—")
                if any(c != "—" for c in cells):
                    h.append(f'<tr><td><strong>{tier}</strong></td><td>{outcome_label}</td>' +
                             "".join(f'<td>{c}</td>' for c in cells) + '</tr>')
        h.append('</tbody></table>')
    return "\n".join(h)


def render_confluence_body(result: dict, with_images: bool = False) -> str:
    as_of = result["as_of"]
    n = result["n_observations"]
    s = result["sources"]
    coh = result.get("cohorts") or []
    ap = [a for a in (result.get("ask_paths") or []) if a["outcome"] in dict(COHORT_LABELS)]
    h = []
    h.append(f'<p><strong>Committed-price benchmarks · built {as_of}</strong> · $/GPU-hr, '
             f'0% prepayment, adjusted to the build date. Method v{result["method_version"]}.</p>')
    h.append(f'<p><strong>Input dates:</strong> latest reported offer {s.get("intel_latest_quote") or "not available"}; '
             f'achieved-price extract {s.get("reserve_tenor_generated") or "not available"}; '
             f'Finance grid {s.get("grid_version") or "not available"}; public list snapshot {s.get("list_snapshot") or "not available"}. '
             f'Outcome extract {_input_dates(coh)}; historical path extract {_input_dates(ap)}. A daily build does not refresh every input.</p>')
    if s.get("nebius_committed_reference_eligible") is False:
        h.append(f'<p>The manual Nebius committed-list reference was verified {escape(s.get("nebius_committed_reference_verified") or "on an unknown date")} '
                 'and is excluded from current list comparisons. The separately dated Finance grid remains a labelled reference.</p>')
    h.append('<div data-type="panel-warning"><p><strong>Internal reference.</strong> Marks combine sales-reported competitor offers '
             '(skewed toward lost deals) and Nebius achieved aggregates on a common payment basis. '
             'Only stated-payment offers enter marks; the time axis is contract length, not delivery date. '
             'Do not quote these confidential benchmarks or achieved prices to customers.</p></div>')
    h.append('<p><strong>Explore:</strong> child page <em>GPU Committed-Price Benchmarks — Interactive</em> '
             '(Market benchmarks, Market position, Contract return), also attached as forward_view.html. '
             '<a href="https://nebius.atlassian.net/wiki/spaces/PR/pages/1831469419">Daily competitor pricing</a> · '
             '<a href="https://nebius.atlassian.net/wiki/spaces/Billing/pages/1970110707">Spot and auction pricing</a>.</p>')
    if with_images:
        h.append('<div data-type="expand" data-title="Chart by commitment length">'
                 '<p><ac:image ac:width="900"><ri:attachment ri:filename="forward_curve.png"/></ac:image></p></div>')

    # marks grid
    h.append('<h2>Marks — $/GPU-hr by commitment length</h2>')
    h.append('<table data-layout="wide"><thead><tr><th>GPU</th>' +
             "".join(f'<th>{TENOR_LABEL[t]}</th>' for t in TENORS) +
             '<th>Shape</th></tr></thead><tbody>')
    shape_by = {x["tier"]: x for x in result["shape"]}
    for tier in TIERS:
        cells = [e for e in result["marks"] if e["tier"] == tier]
        if not any(e["has_mark"] for e in cells):
            continue
        sh = shape_by[tier]
        struct = sh["structure"]
        struct_txt = ("—" if not struct else
                      _lozenge((f'36m {abs(round(sh["slope_12_36_pct"] * 100))}% below 12m' if struct == "backwardation" else
                                f'36m {abs(round(sh["slope_12_36_pct"] * 100))}% above 12m' if struct == "contango" else "36m level with 12m"),
                               "red" if struct == "backwardation" else "green" if struct == "contango" else "grey"))
        h.append(f'<tr><td><strong>{tier}</strong></td>' +
                 "".join(f'<td>{_mark_cell(e)}</td>' for e in cells) +
                 f'<td>{struct_txt}</td></tr>')
    h.append('</tbody></table>')
    h.append(f'<p>Good: at least {GOOD_OBS} observations, 2 providers and 2 observations within {RECENT_DAYS} days; '
             'thin: a confidence test fails; achieved only: Nebius deals with no competitor offer. '
             f'n/a: fewer than {MIN_OBS} eligible observations in {MAX_AGE_DAYS} days. '
             'Shape compares the 36-month and 12-month marks; it is descriptive.</p>')
    h.append(f'<p><strong>Aggregation thresholds:</strong> the achieved-price mark leg and lost/open asked-price markers '
             f'require at least {ASK_MIN_DEALS} and {CRM_ASK_MIN_DEALS} deals respectively. '
             'The separate outcome and historical-path reference tables retain their 2-opportunity/deal minimum. '
             'Prices with unstated payment terms are excluded from marks. Full evidence and limitations follow.</p>')

    # CRM deal-outcome cohorts: source medians stay separate, never enter a mark.
    if coh:
        h.append('<div data-type="expand" data-title="Nebius deal outcomes — source medians and cohorts">')
        h.append(_render_cohorts(coh))
        h.append('</div>')

    # ask-to-close paths (HubSpot era, reference class, never pooled)
    ap_labels = COHORT_LABELS
    if ap:
        h.append('<div data-type="expand" data-title="Historical ask-to-close paths — HubSpot deal reviews">')
        h.append(f'<p><em>Every GPU deal line priced at a twice-weekly deal review between {ap[0]["window_from"]} and {ap[0]["window_to"]} (the HubSpot mirror froze at the CRM cutover), '
                 'followed from its first observed priced review to its last priced snapshot. Cell = median first asked price → median final recorded price (deals; share of lines whose price was revised), at least 2 deals. '
                 'Lines already priced at the first source snapshot are excluded because their earlier history is unknown. '
                 'Outcome and final price come from the same last priced snapshot, even if the stage changed later. '
                 'Lost-case prices are recorded asks; loss reasons do not prove acceptance or a price ceiling. Salesforce keeps no such history; '
                 'scripts/refresh_quote_asks.py rebuilds it from daily quote snapshots.</em></p>')
        h.append('<table data-layout="wide"><thead><tr><th>GPU</th><th>Outcome</th>' +
                 "".join(f'<th>{TENOR_LABEL[t]}</th>' for t in TENORS) + '</tr></thead><tbody>')
        for tier in TIERS:
            for outcome, label in ap_labels:
                cells = []
                for t in TENORS:
                    cs = [a for a in ap if a["gpu"] == tier and a["tenor_months"] == t and a["outcome"] == outcome]
                    if not cs:
                        cells.append("—"); continue
                    a = cs[0]
                    cells.append(f'${a["first_ask_med"]:.2f} → ${a["final_med"]:.2f} <span style="color:#6b6b76">({a["deals"]}, {round(100 * a["share_revised"])}% revised)</span>')
                if all(x == "—" for x in cells):
                    continue
                h.append(f'<tr><td><strong>{tier}</strong></td><td>{label}</td>' + "".join(f'<td>{x}</td>' for x in cells) + '</tr>')
        h.append('</tbody></table>')
        h.append(f'<p><em>Input generated {_input_dates(ap)}; the HubSpot source is frozen. Reference only, never pooled into marks. Internal only: derived from CRM.</em></p></div>')

    # shape + references
    h.append('<div data-type="expand" data-title="Curve shape, Finance grid and public references">')
    h.append('<table><thead><tr><th>GPU</th><th>3m</th><th>12m</th><th>36m</th><th>60m</th>'
             '<th>3m vs 12m</th><th>36m vs 12m</th>'
             '<th>Nebius grid 24m / 36m (100% · 50% prepay)</th><th>Cheapest hyperscaler list 12m / 36m</th>'
             '<th>SA cost floor</th></tr></thead><tbody>')
    for tier in TIERS:
        sh = shape_by[tier]
        if not any(v for k, v in sh.items() if k.startswith("mark_")):
            continue
        m = {e["tenor_months"]: e for e in result["marks"] if e["tier"] == tier}
        neb = (f'{_fmt(m[24]["grid_100"])} · {_fmt(m[24]["grid_50"])} / {_fmt(m[36]["grid_100"])} · {_fmt(m[36]["grid_50"])}')
        hyp = (f'{_fmt(m[12]["list_hyperscaler_min"])} <em>{m[12]["list_hyperscaler_provider"]}</em> / '
               f'{_fmt(m[36]["list_hyperscaler_min"])} <em>{m[36]["list_hyperscaler_provider"]}</em>')
        h.append(f'<tr><td><strong>{tier}</strong></td><td>{_fmt(sh["mark_3m"])}</td><td>{_fmt(sh["mark_12m"])}</td>'
                 f'<td>{_fmt(sh["mark_36m"])}</td><td>{_fmt(sh["mark_60m"])}</td>'
                 f'<td>{_pct(sh["short_end_premium_pct"])}</td><td>{_pct(sh["slope_12_36_pct"])}</td>'
                 f'<td>{neb}</td><td>{hyp}</td><td>{_fmt(sh["cost_floor"])}</td></tr>')
    h.append('</tbody></table>')
    h.append(f'<p><em>Nebius grid = Finance reserve price grid version {s.get("grid_version")}, segment {result["params"].get("segment")} '
             f'(Pricing model.xlsx, NebiusFinance/GPU), shown as published, i.e. NOT prepay-normalised; 12m Blackwell cells are "per request" in that grid. Hyperscaler list = cheapest of AWS/GCP/Azure/Oracle reserved/committed tier '
             f'in the {s.get("list_snapshot") or "latest"} snapshot (rack rates; enterprise customers pay far less). SA cost floor = {SA_COST_FLOOR_SOURCE} — a third-party modeled cost, not Nebius COGS.</em></p>')

    h.append('</div>')

    # bid vs ask detail
    h.append('<div data-type="expand" data-title="Full evidence by cell — counts, ranges and confidence">')
    h.append('<p><em>Competitor offers = median of offers reported by sales that state their prepayment (confirmed repeats removed; adjusted to today and 0% prepay); '
             'unstated = offers without payment terms, excluded from the mark, shown with their median at a 0% assumption for reference only; '
             'Nebius achieved = median of Nebius signed reserve prices (CRM aggregates, payment type as prepay proxy); recent raw = median of the last 90 days as reported, '
             'no date adjustment, for comparison with the adjusted mark; public contracts = implied lower bounds, reference only. Gap = achieved / offers − 1, descriptive, not a spread.</em></p>')
    h.append('<table><thead><tr><th>GPU</th><th>Tenor</th><th>Mark</th><th>Recent raw (n)</th><th>Competitor offers, stated prepay (n · providers)</th><th>Unstated terms, excluded (n · median at 0%)</th><th>Nebius achieved (n · deals)</th>'
             '<th>Public contracts (n)</th><th>Gap</th><th>Adjusted range</th><th>Status</th></tr></thead><tbody>')
    for e in result["marks"]:
        if e["n_all"] == 0 and e["n_public"] == 0:
            continue
        status = (_lozenge("suppressed", "grey") if not e["has_mark"] else
                  _lozenge("achieved only", "blue") if e.get("achieved_only") else
                  _lozenge(e["confidence"], "green" if e["confidence"] == "good" else "yellow"))
        ask_txt = f'{_fmt(e["ask_median"])} ({e["n_ask"]} · {e["ask_deals"]})' if e["n_ask"] else "—"
        wd = e.get("ask_deals_withheld") or 0
        if wd:
            ask_txt += f' · {wd} deal{"s" if wd != 1 else ""} withheld (under {ASK_MIN_DEALS})'
        h.append(f'<tr><td><strong>{e["tier"]}</strong></td><td>{e["label"]}</td><td><strong>{_fmt(e["mark"])}</strong></td>'
                 f'<td>{_fmt(e.get("recent_raw_median"))} ({e.get("n_recent90", 0)})</td>'
                 f'<td>{_fmt(e["bid_median"])} ({e["n_bid"]} · {e.get("n_providers", "–")})</td>'
                 f'<td>{e["n_bid_unstated"]}{" · " + _fmt(e["bid_median_unstated"]) if e["n_bid_unstated"] else ""}</td>'
                 f'<td>{ask_txt}</td>'
                 f'<td>{_fmt(e["public_median"])} ({e["n_public"]})</td>'
                 f'<td>{_pct(e["spread_pct"])}</td>'
                 f'<td>{"recent" if e.get("range_recent") else "all observations"}: {_range(e["range_lo"], e["range_hi"])}</td><td>{status}'
                 f'{" · " + escape(e.get("confidence_reason") or e.get("reason") or "") if e.get("confidence_reason") or e.get("reason") else ""}</td></tr>')
    h.append('</tbody></table>')

    h.append('</div>')

    # method
    qe = ", ".join(f'{k} {v:+.2f}' for k, v in result["quarter_effects_log"].items())
    h.append('<div data-type="expand" data-title="Method, parameters and provenance">')
    h.append(f'<p><strong>Observations in window:</strong> {n["bid"]} distinct competitor offers ({n.get("bid_duplicates_removed", 0)} confirmed repeats removed, '
             f'{n.get("bid_review", 0)} similar offers kept and listed for review in store/intel_duplicates.csv; {n.get("bid_known_prepay", 0)} state their prepayment and enter marks, '
             f'{n.get("bid_unstated_prepay", 0)} do not and are counted only; latest {s.get("intel_latest_quote")}), '
             f'{n["ask"]} Nebius achieved aggregates covering {n["ask_deals"]} signed deals, each aggregate at least {ASK_MIN_DEALS} deals (quarter × payment type, then quarter, then the whole window); '
             f'{n.get("ask_deals_withheld", 0)} further deals in {n.get("ask_withheld", 0)} thinner groups are counted but their prices withheld (reserve_tenor.csv generated {s.get("reserve_tenor_generated")}), '
             f'{n.get("public", 0)} public announced contracts shown as reference (never pooled). Nebius grid version {s.get("grid_version")}, segment {result["params"].get("segment")}.</p>')
    h.append('<ol>'
             f'<li><strong>Prepay normalisation:</strong> discount = {PREPAY_A:.4f} × years of tenor × (1 − (1 − prepay share)²), capped {PREPAY_CAP:.0%}; '
             'every quote is expressed at 0% prepay. This is Finance\'s money-cost convention (sheet "." of Pricing model.xlsx: '
             '12m −3.4/−2.63/−1.8% and 24m −6.97/−5.26/−3.57% for 100/50/30% prepay; three-parameter fit rmse 0.02pp). '
             'The Sep-7 grid\'s much larger 100→50% steps are a commercial ladder, shown as policy and not used to normalise. Nebius CRM deals carry no prepay percentage, '
             f'so payment type is used as a proxy: upfront = {PREPAY_BUCKET_PCT["upfront"]}%, prepaid monthly = {PREPAY_BUCKET_PCT["prepaid_monthly"]}%, postpaid = 0%.</li>'
             f'<li><strong>Quote-date normalisation:</strong> two-way fixed effects on log p₀ (tier×tenor cell + quote quarter, pooled across tiers); '
             f'each observation is shifted to the as-of quarter. Estimated quarter effects (log, vs as-of): {qe}. '
             'Pooling across tiers is a v1 simplification; Hopper and Blackwell repriced by similar ratios in 2026H1.</li>'
             f'<li><strong>Weights:</strong> observations ≤ {RECENT_DAYS} days old count 1, ≤ {MAX_AGE_DAYS} days count ½, older are dropped; '
             'each Nebius achieved month weighs min(deals, 3) so a single mega-deal cannot dominate, and an aggregate carries the summed weight of its months; '
             'n counts one observation per competitor offer and one per signed Nebius deal. Public announced contracts are not pooled (implied rates assume 8,760 billed hours and are lower bounds).</li>'
             f'<li><strong>Mark:</strong> weighted median of adjusted prices per cell on one payment basis: competitor offers that state their prepayment plus Nebius achieved cells (payment type known). '
             f'Suppressed below {MIN_OBS} such observations, never interpolated or carried forward. "Good" needs six observations from at least two providers with two in the last 120 days. '
             'Offers without a stated prepayment never enter a mark; they are counted per cell, their median at a 0% assumption is shown for reference, and the interactive page can add them to a comparison only through an explicitly labelled switch.</li>'
             '<li><strong>Duplicates:</strong> a row is removed only as a confirmed repeat (same provider, price and term within 7 days, or a seed row repeating a retrieved row with the same or an anonymised provider or identical notes). '
             'Rows that only share a Slack message with another provider, or seed rows matching another provider, are kept and listed for review; one message can carry several providers\' offers at one price.</li>'
             f'<li><strong>Other evidence classes (never pooled):</strong> Nebius asked prices on deals that closed lost and on open proposals (HubSpot deal reviews until the 2026-08-10 cutover, live Salesforce quotes in review or approved since; aggregates of at least {CRM_ASK_MIN_DEALS} deals per GPU, term and class, thinner cells withheld); '
             'Nebius deal outcomes and ask-to-close paths (expandable tables above, minimum 2 opportunities/deals, source medians kept separate); '
             'the SemiAnalysis draft GB300 contract index (Aug-2026; source to be confirmed), moved out of the offer file because an index is not an offer; '
             'short-term market prices (SF Compute H100 clearing price, Vast.ai marketplace reservations) against the PAYG and 3-month cells.</li>'
             f'<li><strong>Tenor buckets:</strong> ≤4 → 3m, ≤8 → 6m, ≤14 → 12m, ≤20 → 18m, ≤27 → 24m, ≤42 → 36m, longer → 60m. '
             f'Sanity band {_fmt(PRICE_MIN)}–{_fmt(PRICE_MAX)}.</li>'
             '<li><strong>Not modelled yet (v2):</strong> delivery-date axis (forward start vs immediate), cluster size, region/interconnect, '
             'per-family time effects, credit quality. The curve is a term structure at quote date, not a delivery-month strip.</li>'
             '</ol>')
    h.append('<p><em>Code: forward_curve.py in the price-monitor repo; method note analysis/forward_curve_method.md; marks history accrues in '
             'store/forward_curve/marks_history.csv. Ask-side refresh: scripts/refresh_reserve_tenor.py (weekly, local YT access). '
             'External cross-checks: SemiAnalysis Pricing Index (10 tenors, seats held), Ornn forward marks (paid), Silicon Data forward curve (paid).</em></p>')
    h.append('</div>')
    return "\n".join(h)


TEMPLATE = ROOT / "templates" / "forward_view.html"


def view_payload(result: dict) -> dict:
    """Compact copy of the build result for the interactive page: drops fields the view
    never reads and rounds numbers, so the embedded JSON stays small enough for
    Confluence macro bodies and connector calls."""
    keep_mark = ("tier", "tenor_months", "label", "n_obs", "n_recent", "n_old", "n_bid", "n_ask", "n_public", "ask_deals",
                 "n_ask_withheld", "ask_deals_withheld", "achieved_only",
                 "n_known", "n_bid_unstated", "n_all", "n_providers", "recent_raw_median", "n_recent90", "mark_recent", "mark_known", "mark_all",
                 "bid_median", "bid_median_all", "bid_median_unstated", "ask_median", "public_median", "grid", "grid_100", "grid_50", "list_nebius",
                 "list_hyperscaler_min", "list_hyperscaler_provider", "list_hyperscalers", "list_peers", "cost_floor", "mark", "has_mark",
                 "range_lo", "range_hi", "range_recent", "confidence", "confidence_reason", "spread_pct", "reason")
    out = {k: result.get(k) for k in ("as_of", "method_version", "params", "quarter_effects_log", "n_observations", "sources", "grid", "shape", "economics", "on_demand", "sa", "index", "crm_asks", "short_term", "cohorts", "ask_paths")}
    perf = result.get("perf") or {}
    out["perf"] = {"_source": perf.get("_source"), "_method": perf.get("_method"), "_extracted": perf.get("_extracted"),
                   "pairs": [{"sku": p.get("sku"), "versus": p.get("versus"), "low": p.get("low"), "base": p.get("base"), "high": p.get("high"),
                              "basis": (p.get("basis") or "")[:260], "sources": [s[:160] for s in (p.get("sources") or [])[:3]]}
                             for p in perf.get("pairs", [])]}
    out["marks"] = [{k: m.get(k) for k in keep_mark} for m in result["marks"]]
    # node configuration: the page needs the figures and provenance, not the research notes
    ns = result.get("node_specs") or {}
    keep_spec = ("instance_type", "node_gpus", "vcpu", "cpu_model", "ram_gb", "local_storage_tb", "form_factor", "source_url", "as_of", "confidence", "verified")
    disagree = lambda e: any("mismatch" in str(e.get(k, "")) for k in ("api_check", "catalog_check"))   # a machine source contradicts the documentation
    out["node_specs"] = {"_generated": ns.get("_generated"),
                         "providers": {prov: {tier: [{**{k: e.get(k) for k in keep_spec}, "network": (e.get("network") or "")[:90], "disagree": disagree(e),
                                                       "source": (e.get("source") or "")[:60]} for e in entries]
                                              for tier, entries in tiers.items() if tier in TIERS}
                                       for prov, tiers in (ns.get("providers") or {}).items()}}
    out["observations"] = [{"side": o["side"], "tier": o["tier"], "tenor": o["tenor"], "months": o.get("months"),
                            "date": o["date"], "price": o["price"], "p0": o["p0"], "pa": o.get("pa"), "q": o["q"], "prepay": o["prepay"], "known": o.get("known", False), "w": o["w"],
                            "ptype": o.get("ptype", ""), "provider": o.get("provider", ""), "ts": o.get("ts", ""),
                            **({"review": True, "similar": o.get("similar", "")} if o.get("review") else {}),
                            **({"deals": o.get("deals"), "bucket": o.get("bucket"), "withheld": o.get("withheld", False),
                                "lines": o.get("lines", 1), "date_from": o.get("date_from", o["date"])} if o["side"] == "ask" else {})}
                           for o in result.get("observations", [])]
    return out


def render_view_fragment(result: dict) -> str:
    """Interactive view body (title/style/markup/script) with the curve data inlined.
    Reads templates/forward_view.html; the same fragment is published as a Claude
    artifact, embedded in the Confluence HTML macro and, wrapped by render_view_html(),
    attached to the Confluence page."""
    tpl = TEMPLATE.read_text()
    data = json.dumps(view_payload(result), separators=(",", ":"), default=str).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", data)


def render_view_html(result: dict, svg: str = "") -> str:
    """Self-contained standalone page around the interactive fragment (offline-safe:
    Google Fonts are optional, every face has a system fallback)."""
    frag = render_view_fragment(result)
    cut = frag.index("</style>") + len("</style>")
    head, body = frag[:cut], frag[cut:]
    return ('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            + head + '\n</head><body>\n' + body + '\n</body></html>')


# ----------------------------------------------------------------------------- main
def write_outputs(result: dict, with_images: bool = False, quiet: bool = False) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "latest.json").write_text(json.dumps(result, indent=1, default=str))
    svg = render_svg(result)
    (OUT_DIR / "forward_curve.svg").write_text(svg)
    png_ok = render_png(result, OUT_DIR / "forward_curve.png")
    (OUT_DIR / "forward_view.html").write_text(render_view_html(result, svg))
    BODY_HTML.write_text(render_confluence_body(result, with_images=with_images))
    # marks history (append, idempotent per as_of)
    hist = OUT_DIR / "marks_history.csv"
    cols = ["as_of", "tier", "tenor_months", "mark", "confidence", "n_obs", "n_bid", "n_ask",
            "bid_median", "ask_median", "range_lo", "range_hi", "list_nebius", "method_version"]
    existing = []
    if hist.exists():
        with open(hist, newline="") as f:
            existing = [r for r in csv.DictReader(f) if r.get("as_of") != result["as_of"]]
    with open(hist, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(existing)
        for e in result["marks"]:
            w.writerow({"as_of": result["as_of"], "tier": e["tier"], "tenor_months": e["tenor_months"],
                        "mark": e["mark"], "confidence": e["confidence"], "n_obs": e["n_obs"],
                        "n_bid": e["n_bid"], "n_ask": e["n_ask"], "bid_median": e["bid_median"],
                        "ask_median": e["ask_median"], "range_lo": e["range_lo"], "range_hi": e["range_hi"],
                        "list_nebius": e["list_nebius"], "method_version": result["method_version"]})
    if not quiet:
        print(f"forward curve as of {result['as_of']}: {sum(1 for e in result['marks'] if e['has_mark'])} marks, "
              f"{sum(1 for e in result['marks'] if not e['has_mark'] and e['n_obs'])} suppressed cells with data; "
              f"png={'yes' if png_ok else 'no (matplotlib missing)'}; body {BODY_HTML.stat().st_size // 1024} KB")
        print("tier   " + "".join(TENOR_LABEL[t].rjust(9) for t in TENORS))
        for tier in TIERS:
            line = f"{tier:6} "
            for t in TENORS:
                e = next(x for x in result["marks"] if x["tier"] == tier and x["tenor_months"] == t)
                line += (f"{e['mark']:6.2f}{'*' if e['confidence'] == 'thin' else ' '}({e['n_obs']})" if e["has_mark"]
                         else f"   —  ({e['n_obs']})").rjust(9)
            print(line)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--with-images", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    as_of = date.fromisoformat(a.as_of) if a.as_of else None
    result = build(as_of)
    write_outputs(result, with_images=a.with_images, quiet=a.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
