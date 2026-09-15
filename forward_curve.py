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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(ROOT))
from intel_quality import classify as intel_classify, dedupe as intel_dedupe, prepay_known as intel_prepay_known  # noqa: E402
STORE = ROOT / "store"
OUT_DIR = STORE / "forward_curve"
INTEL_CSV = STORE / "intel.csv"
RESERVE_TENOR_CSV = STORE / "reserve_tenor.csv"
HISTORY_CSV = STORE / "history.csv"
BODY_HTML = STORE / "forward_curve_body.html"

METHOD_VERSION = "1.3 (2026-09-15)"
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
        rows = [r for r in csv.DictReader(open(history, newline="")) if r.get("consumption_type") in ("on_demand", "spot", "preemptible")]
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
                if tag == "peer":
                    per[tier]["peer"].append((p, prov))
                elif tag == "hyperscaler":
                    per[tier]["hyper"].append((p, prov))
                else:
                    per[tier]["other"].append((p, prov))   # price fighters / platforms, PAYG term only
            for tier, d in per.items():
                if d["peer"]:
                    best = {}
                    for p, prov in d["peer"]:
                        best[prov] = min(p, best.get(prov, 99))
                    vals = sorted(best.values())
                    out[tier]["peer_od_median"] = round(statistics.median(vals), 2)
                    out[tier]["peer_od_n"] = len(vals)
                    out[tier]["peer_od_min"] = round(vals[0], 2)
                    out[tier]["peer_list"] = [{"provider": k, "price": round(v, 4)} for k, v in sorted(best.items(), key=lambda kv: kv[1])]
                if d["hyper"]:
                    p, prov = min(d["hyper"])
                    out[tier]["hyperscaler_od_min"] = round(p, 2); out[tier]["hyperscaler_od_provider"] = prov
                    hb = {}
                    for hp, hprov in d["hyper"]:
                        hb[hprov] = min(hp, hb.get(hprov, 99))
                    out[tier]["hyper_list"] = [{"provider": k, "price": round(v, 4)} for k, v in sorted(hb.items(), key=lambda kv: kv[1])]
                if d["other"]:
                    ob = {}
                    for op, oprov in d["other"]:
                        ob[oprov] = min(op, ob.get(oprov, 99))
                    out[tier]["other_list"] = [{"provider": k, "price": round(v, 4)} for k, v in sorted(ob.items(), key=lambda kv: kv[1])]
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


def load_list(path: Path = HISTORY_CSV) -> dict:
    """Latest public committed/reserved list prices: {(tier, tenor): {...}}."""
    if not path.exists():
        return {}
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("consumption_type") in LIST_TENOR:
                rows.append(r)
    if not rows:
        return {}
    latest = max(r["snapshot_date"] for r in rows)
    out = defaultdict(lambda: {"nebius": None, "hyperscaler_min": None, "hyperscaler_min_provider": "",
                               "peer_min": None, "peer_min_provider": "", "snapshot_date": latest})
    for r in rows:
        if r["snapshot_date"] != latest:
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
        prov = r["provider"]
        if prov == "nebius":
            cell["nebius"] = p if cell["nebius"] is None else min(cell["nebius"], p)
        elif prov in HYPERSCALERS:
            if cell["hyperscaler_min"] is None or p < cell["hyperscaler_min"]:
                cell["hyperscaler_min"], cell["hyperscaler_min_provider"] = p, prov
        else:
            if cell["peer_min"] is None or p < cell["peer_min"]:
                cell["peer_min"], cell["peer_min_provider"] = p, prov
    return dict(out)


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
          history=HISTORY_CSV, contracts=CONTRACTS_CSV, grid=GRID_JSON, segment=DEFAULT_SEGMENT) -> dict:
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

    lists = load_list(history)
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
                    "list_snapshot": list_date, "cost_floor": SA_COST_FLOOR_SOURCE},
        "grid": {"version": max(grids) if grids else None,
                 "segments": {k: {t: {int(float(m)): {int(float(pp)): v for pp, v in ps.items()} for m, ps in cells.items()}
                                  for t, cells in seg.items()}
                              for k, seg in (grids[max(grids)]["segments"].items() if grids else [])}},
        "marks": marks,
        "shape": shape,
        "observations": export,
        "economics": (json.loads(ECONOMICS_JSON.read_text()) if ECONOMICS_JSON.exists() else {}),
        "on_demand": load_on_demand(history, od_quotes, as_of=as_of),
        "sa": load_sa_reference(as_of),
        "perf": load_perf_multiples(),
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
    unst = f' · +{e["n_bid_unstated"]} unstated excluded' if e.get("n_bid_unstated") else ""
    if not e["has_mark"]:
        return _lozenge("n/a", "grey") + f'<br/><em>{e["n_obs"]} obs{unst}</em>'
    if e.get("achieved_only"):
        loz, why = _lozenge("achieved only", "blue"), " · Nebius deals, no competitor offer"
    else:
        loz = _lozenge(e["confidence"], "green" if e["confidence"] == "good" else "yellow")
        why = f' · thin: {e["confidence_reason"]}' if e["confidence"] == "thin" else ""
    rng = ("recent " if e.get("range_recent") else "all-obs range ") + _range(e["range_lo"], e["range_hi"])
    ach = f'{e["ask_deals"]} deals in {e["n_ask"]} aggregate{"s" if e["n_ask"] != 1 else ""}' if e["n_ask"] else "0 achieved"
    return (f'<strong>{_fmt(e["mark"])}</strong> {loz}'
            f'<br/><em>n={e["n_obs"]} ({e["n_bid"]} offers · {ach}){unst}{why} · {rng}</em>')


def render_confluence_body(result: dict, with_images: bool = False) -> str:
    as_of = result["as_of"]
    n = result["n_observations"]
    s = result["sources"]
    h = []
    h.append(f'<p><em>As of {as_of} — refreshed daily by the price-monitor build (method v{result["method_version"]}). '
             f'<strong>Marks are $/GPU-hr at 0% prepayment and today\'s price level</strong>; rate-card, list, raw and cost columns are shown as published. Sibling pages: '
             f'<a href="https://nebius.atlassian.net/wiki/spaces/PR/pages/1831469419">GPU Competitor Pricing — Daily Overview</a> · '
             f'<a href="https://nebius.atlassian.net/wiki/spaces/Billing/pages/1970110707">Competitor Spot &amp; Auction Pricing</a>. '
             f'<strong>Interactive version</strong> (three views: market benchmarks by term, where a price sits among comparable offers, '
             f'what a contract returns after costs): child page <em>GPU Committed-Price Benchmarks — Interactive</em> under this one, '
             f'embedded via the HTML macro; also attached here as forward_view.html.</em></p>')
    h.append('<div data-type="panel-warning"><p><strong>Read me first.</strong> These are <strong>committed-price benchmarks</strong>, not a traded curve: '
             'each cell is the weighted median of dated observations we hold — <em>competitor offers</em> reported in #price-intelligence '
             '(confirmed repeats removed; skews to losses) and <em>Nebius achieved</em> prices from CRM deal reviews '
             '(aggregates only) — shifted to today\'s price level and to a 0%-prepay basis. <strong>Only offers that state their prepayment enter a mark</strong>; '
             'offers with unstated terms are counted per cell and never pooled. The two classes are shown separately; the gap between them is descriptive only. '
             f'Nebius achieved prices appear only as aggregates of at least {ASK_MIN_DEALS} deals; thinner achieved evidence is counted but withheld. '
             'Public multi-year contracts are a reference and never pooled. Commitment length alone is not a delivery-date curve. Cells with fewer than '
             f'{MIN_OBS} observations in the last {MAX_AGE_DAYS} days are <strong>suppressed</strong>, never interpolated. '
             '<strong>Internal only</strong>: Nebius achieved prices are derived from confidential contracts; never quote marks to customers '
             'or paste this page externally.</p></div>')
    if with_images:
        h.append('<p><ac:image ac:width="900"><ri:attachment ri:filename="forward_curve.png"/></ac:image></p>')

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
    h.append(f'<p><em>Cell = mark on a stated-prepay basis, then a lozenge: good = n ≥ {GOOD_OBS} distinct stated-prepay observations from ≥ 2 providers with ≥ 2 in the last {RECENT_DAYS} days; '
             f'thin = one of those three tests fails (the failing test is named); achieved only = Nebius signed deals with no competitor offer in the cell (always one provider, so never "good"). '
             f'Then n (competitor offers / Nebius achieved aggregates), the number of offers excluded for unstated terms, and the min–max of recent adjusted observations (of all observations when none is recent). '
             f'n/a = suppressed. Shape compares the 36m mark to the 12m mark.</em></p>')

    # shape + references
    h.append('<h2>Curve shape and references</h2>')
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

    # bid vs ask detail
    h.append('<h2>Evidence by cell</h2>')
    h.append('<p><em>Competitor offers = median of offers reported by sales that state their prepayment (confirmed repeats removed; adjusted to today and 0% prepay); '
             'unstated = offers without payment terms, excluded from the mark, shown with their median at a 0% assumption for reference only; '
             'Nebius achieved = median of Nebius signed reserve prices (CRM aggregates, payment type as prepay proxy); recent raw = median of the last 90 days as reported, '
             'no date adjustment, for comparison with the adjusted mark; public contracts = implied lower bounds, reference only. Gap = achieved / offers − 1, descriptive, not a spread.</em></p>')
    h.append('<table><thead><tr><th>GPU</th><th>Tenor</th><th>Mark</th><th>Recent raw (n)</th><th>Competitor offers, stated prepay (n · providers)</th><th>Unstated terms, excluded (n · median at 0%)</th><th>Nebius achieved (n · deals)</th>'
             '<th>Public contracts (n)</th><th>Gap</th><th>Recent range</th><th>Status</th></tr></thead><tbody>')
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
                 f'<td>{_range(e["range_lo"], e["range_hi"])}</td><td>{status}</td></tr>')
    h.append('</tbody></table>')

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
                 "list_hyperscaler_min", "list_hyperscaler_provider", "cost_floor", "mark", "has_mark",
                 "range_lo", "range_hi", "range_recent", "confidence", "confidence_reason", "spread_pct", "reason")
    out = {k: result.get(k) for k in ("as_of", "method_version", "params", "quarter_effects_log", "n_observations", "sources", "grid", "shape", "economics", "on_demand", "sa")}
    perf = result.get("perf") or {}
    out["perf"] = {"_source": perf.get("_source"), "_method": perf.get("_method"), "_extracted": perf.get("_extracted"),
                   "pairs": [{"sku": p.get("sku"), "versus": p.get("versus"), "low": p.get("low"), "base": p.get("base"), "high": p.get("high"),
                              "basis": (p.get("basis") or "")[:260], "sources": [s[:160] for s in (p.get("sources") or [])[:3]]}
                             for p in perf.get("pairs", [])]}
    out["marks"] = [{k: m.get(k) for k in keep_mark} for m in result["marks"]]
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
        w = csv.DictWriter(f, fieldnames=cols)
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
