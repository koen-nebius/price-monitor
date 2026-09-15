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
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "store"
OUT_DIR = STORE / "forward_curve"
INTEL_CSV = STORE / "intel.csv"
RESERVE_TENOR_CSV = STORE / "reserve_tenor.csv"
HISTORY_CSV = STORE / "history.csv"
BODY_HTML = STORE / "forward_curve_body.html"

METHOD_VERSION = "1.0 (2026-09-15)"
TIERS = ["H100", "H200", "B200", "B300", "GB200", "GB300", "VR"]
TENORS = [3, 6, 12, 18, 24, 36, 60]            # months; buckets, see bucket_months()
TENOR_LABEL = {3: "3m", 6: "6m", 12: "12m", 18: "18m", 24: "24m", 36: "36m", 60: "60m"}
PREPAY_K = 0.06
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


def prepay_normalise(price: float, prepay_pct) -> float:
    try:
        frac = max(0.0, min(1.0, float(prepay_pct or 0) / 100.0))
    except (TypeError, ValueError):
        frac = 0.0
    return price / (1.0 - PREPAY_K * frac)


def weighted_median(values, weights) -> float:
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
    """Field-intel quotes -> observations (side=bid)."""
    obs = []
    if not path.exists():
        return obs
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
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
            obs.append({"side": "bid", "tier": tier, "tenor": tenor, "date": d,
                        "price_raw": price, "prepay_pct": r.get("prepay_pct") or 0,
                        "weight": 1.0, "provider": r.get("provider_name", ""),
                        "provider_type": r.get("provider_type", ""),
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
            obs.append({"side": "ask", "tier": tier, "tenor": tenor,
                        "date": d + timedelta(days=14),      # month midpoint
                        "price_raw": price, "prepay_pct": 0,   # CRM price = as-billed
                        "weight": float(min(deals, 3)), "deals": deals,
                        "provider": "Nebius", "provider_type": "nebius",
                        "source": f"reserve_tenor:{r.get('generated_date', '')}"})
    return obs


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


def build(as_of: date | None = None, intel=INTEL_CSV, reserve=RESERVE_TENOR_CSV,
          history=HISTORY_CSV) -> dict:
    as_of = as_of or date.today()
    raw = load_bid(intel) + load_ask(reserve)
    obs = []
    for o in raw:
        age = (as_of - o["date"]).days
        if age < -3 or age > MAX_AGE_DAYS:
            continue
        p0 = prepay_normalise(o["price_raw"], o["prepay_pct"])
        if not (PRICE_MIN <= p0 <= PRICE_MAX):
            continue
        o = dict(o, age_days=age, p0=p0, logp0=math.log(p0), quarter=quarter_of(o["date"]),
                 weight=o["weight"] * (1.0 if age <= RECENT_DAYS else 0.5))
        obs.append(o)
    q_eff = quarter_effects(obs, as_of)
    for o in obs:
        o["p_adj"] = math.exp(o["logp0"] - q_eff.get(o["quarter"], 0.0))

    lists = load_list(history)
    by_cell = defaultdict(list)
    for o in obs:
        by_cell[(o["tier"], o["tenor"])].append(o)

    marks = []
    for tier in TIERS:
        for tenor in TENORS:
            cell = by_cell.get((tier, tenor), [])
            bids = [o for o in cell if o["side"] == "bid"]
            asks = [o for o in cell if o["side"] == "ask"]
            n, n_recent = len(cell), sum(1 for o in cell if o["age_days"] <= RECENT_DAYS)
            ask_deals = sum(o.get("deals", 0) for o in asks)
            ref = lists.get((tier, tenor), {})
            entry = {
                "tier": tier, "tenor_months": tenor, "label": TENOR_LABEL[tenor],
                "n_obs": n, "n_recent": n_recent, "n_bid": len(bids), "n_ask": len(asks),
                "ask_deals": ask_deals,
                "bid_median": round(weighted_median([o["p_adj"] for o in bids],
                                                    [o["weight"] for o in bids]), 2) if bids else None,
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
                entry.update({
                    "mark": round(mark, 2), "has_mark": True,
                    "range_lo": round(min(recent_vals), 2), "range_hi": round(max(recent_vals), 2),
                    "confidence": "good" if n >= GOOD_OBS and n_recent >= 2 else "thin",
                    "spread_pct": (round(entry["ask_median"] / entry["bid_median"] - 1, 3)
                                   if entry["bid_median"] and entry["ask_median"] else None),
                    "reason": None,
                })
            else:
                entry.update({"mark": None, "has_mark": False, "range_lo": None, "range_hi": None,
                              "confidence": "suppressed", "spread_pct": None,
                              "reason": f"insufficient_data ({n} obs in {MAX_AGE_DAYS}d, need {MIN_OBS})"})
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

    src_dates = {"intel_latest": max((o["date"] for o in raw if o["side"] == "bid"), default=None),
                 "reserve_tenor_generated": None}
    if RESERVE_TENOR_CSV.exists():
        with open(RESERVE_TENOR_CSV, newline="") as f:
            first = next(csv.DictReader(f), None)
            src_dates["reserve_tenor_generated"] = (first or {}).get("generated_date")
    list_date = next((v["snapshot_date"] for v in lists.values()), None)

    return {
        "as_of": as_of.isoformat(),
        "method_version": METHOD_VERSION,
        "params": {"prepay_k": PREPAY_K, "recent_days": RECENT_DAYS, "max_age_days": MAX_AGE_DAYS,
                   "min_obs": MIN_OBS, "good_obs": GOOD_OBS, "price_band": [PRICE_MIN, PRICE_MAX],
                   "tenors_months": TENORS, "tiers": TIERS},
        "quarter_effects_log": {k: round(v, 3) for k, v in q_eff.items()},
        "n_observations": {"bid": sum(1 for o in obs if o["side"] == "bid"),
                           "ask": sum(1 for o in obs if o["side"] == "ask"),
                           "ask_deals": sum(o.get("deals", 0) for o in obs if o["side"] == "ask")},
        "sources": {"intel_latest_quote": src_dates["intel_latest"].isoformat() if src_dates["intel_latest"] else None,
                    "reserve_tenor_generated": src_dates["reserve_tenor_generated"],
                    "list_snapshot": list_date, "cost_floor": SA_COST_FLOOR_SOURCE},
        "marks": marks,
        "shape": shape,
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
    if not e["has_mark"]:
        return _lozenge("n/a", "grey") + f'<br/><small>{e["n_obs"]} obs</small>'
    colour = "green" if e["confidence"] == "good" else "yellow"
    return (f'<strong>{_fmt(e["mark"])}</strong> {_lozenge(e["confidence"], colour)}'
            f'<br/><small>n={e["n_obs"]} ({e["n_bid"]}b/{e["n_ask"]}a) · recent {_range(e["range_lo"], e["range_hi"])}</small>')


def render_confluence_body(result: dict, with_images: bool = False) -> str:
    as_of = result["as_of"]
    n = result["n_observations"]
    s = result["sources"]
    h = []
    h.append(f'<p><em>As of {as_of} — refreshed daily by the price-monitor build (method v{result["method_version"]}). '
             f'All prices <strong>$/GPU-hr, normalised to 0% prepay</strong>. Sibling pages: '
             f'<a href="https://nebius.atlassian.net/wiki/spaces/PR/pages/1831469419">GPU Competitor Pricing — Daily Overview</a> · '
             f'<a href="https://nebius.atlassian.net/wiki/spaces/Billing/pages/1970110707">Competitor Spot &amp; Auction Pricing</a>.</em></p>')
    h.append('<div data-type="panel-warning"><p><strong>Read me first.</strong> This is a <strong>marked</strong> curve, not a traded one: '
             'each cell is the weighted median of dated observations we hold — competitor quotes reported in #price-intelligence '
             '(the <em>bid</em> side, skews to losses) and Nebius signed reserve deals from CRM deal reviews (the <em>ask</em> side, '
             'aggregates only) — shifted to today\'s price level and to a 0%-prepay basis. Cells with fewer than '
             f'{MIN_OBS} observations in the last {MAX_AGE_DAYS} days are <strong>suppressed</strong>, never interpolated. '
             '<strong>Internal only</strong>: the ask side is derived from confidential contracts; never quote marks to customers '
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
                      _lozenge(f'{struct} {_pct(sh["slope_12_36_pct"])} 12→36m',
                               "red" if struct == "backwardation" else "green" if struct == "contango" else "grey"))
        h.append(f'<tr><td><strong>{tier}</strong></td>' +
                 "".join(f'<td>{_mark_cell(e)}</td>' for e in cells) +
                 f'<td>{struct_txt}</td></tr>')
    h.append('</tbody></table>')
    h.append(f'<p><em>Cell = mark, confidence lozenge (good = n ≥ {GOOD_OBS} with ≥ 2 in the last {RECENT_DAYS} days; '
             f'thin = {MIN_OBS}–{GOOD_OBS - 1}), then n (bid/ask split) and the min–max of recent adjusted observations. '
             f'n/a = suppressed. Shape compares the 36m mark to the 12m mark; the 3m column is the short-end (immediacy) premium.</em></p>')

    # shape + references
    h.append('<h2>Curve shape and references</h2>')
    h.append('<table><thead><tr><th>GPU</th><th>3m</th><th>12m</th><th>36m</th><th>60m</th>'
             '<th>Short-end premium (3m vs 12m)</th><th>12→36m slope</th>'
             '<th>Nebius list 12m / 36m (512+, 100% upfront)</th><th>Cheapest hyperscaler list 12m / 36m</th>'
             '<th>SA cost floor</th></tr></thead><tbody>')
    for tier in TIERS:
        sh = shape_by[tier]
        if not any(v for k, v in sh.items() if k.startswith("mark_")):
            continue
        m = {e["tenor_months"]: e for e in result["marks"] if e["tier"] == tier}
        neb = f'{_fmt(m[12]["list_nebius"])} / {_fmt(m[36]["list_nebius"])}'
        hyp = (f'{_fmt(m[12]["list_hyperscaler_min"])} <small>{m[12]["list_hyperscaler_provider"]}</small> / '
               f'{_fmt(m[36]["list_hyperscaler_min"])} <small>{m[36]["list_hyperscaler_provider"]}</small>')
        h.append(f'<tr><td><strong>{tier}</strong></td><td>{_fmt(sh["mark_3m"])}</td><td>{_fmt(sh["mark_12m"])}</td>'
                 f'<td>{_fmt(sh["mark_36m"])}</td><td>{_fmt(sh["mark_60m"])}</td>'
                 f'<td>{_pct(sh["short_end_premium_pct"])}</td><td>{_pct(sh["slope_12_36_pct"])}</td>'
                 f'<td>{neb}</td><td>{hyp}</td><td>{_fmt(sh["cost_floor"])}</td></tr>')
    h.append('</tbody></table>')
    h.append(f'<p><em>Nebius list from the AE committed grid tracked in this repo (config.NEBIUS_COMMITTED_PRICES, 512+ GPUs, 100% upfront, '
             f'shown as published, i.e. NOT prepay-normalised). Hyperscaler list = cheapest of AWS/GCP/Azure/Oracle reserved/committed tier '
             f'in the {s.get("list_snapshot") or "latest"} snapshot (rack rates; enterprise customers pay far less). SA cost floor = {SA_COST_FLOOR_SOURCE} — a third-party modeled cost, not Nebius COGS.</em></p>')

    # bid vs ask detail
    h.append('<h2>Bid vs ask by cell</h2>')
    h.append('<p><em>Bid = median of competitor quotes seen by customers (field intel); ask = median of Nebius signed reserve prices '
             '(CRM, aggregates). Spread = ask / bid − 1. A large positive spread on a cell where we still win says the market pays for '
             'our availability/quality; a negative spread says we are leaving money on the table.</em></p>')
    h.append('<table><thead><tr><th>GPU</th><th>Tenor</th><th>Mark</th><th>Bid median (n)</th><th>Ask median (n · deals)</th>'
             '<th>Spread</th><th>Recent range</th><th>Status</th></tr></thead><tbody>')
    for e in result["marks"]:
        if e["n_obs"] == 0:
            continue
        status = (_lozenge("suppressed", "grey") if not e["has_mark"] else
                  _lozenge(e["confidence"], "green" if e["confidence"] == "good" else "yellow"))
        h.append(f'<tr><td><strong>{e["tier"]}</strong></td><td>{e["label"]}</td><td><strong>{_fmt(e["mark"])}</strong></td>'
                 f'<td>{_fmt(e["bid_median"])} ({e["n_bid"]})</td>'
                 f'<td>{_fmt(e["ask_median"])} ({e["n_ask"]} · {e["ask_deals"]})</td>'
                 f'<td>{_pct(e["spread_pct"])}</td>'
                 f'<td>{_range(e["range_lo"], e["range_hi"])}</td><td>{status}</td></tr>')
    h.append('</tbody></table>')

    # method
    qe = ", ".join(f'{k} {v:+.2f}' for k, v in result["quarter_effects_log"].items())
    h.append('<div data-type="expand" data-title="Method, parameters and provenance">')
    h.append(f'<p><strong>Observations in window:</strong> {n["bid"]} bid quotes (field intel, latest {s.get("intel_latest_quote")}), '
             f'{n["ask"]} ask cells covering {n["ask_deals"]} signed deals (reserve_tenor.csv generated {s.get("reserve_tenor_generated")}).</p>')
    h.append('<ol>'
             f'<li><strong>Prepay normalisation:</strong> p₀ = p ÷ (1 − {PREPAY_K} × prepay share). Calibrated between the Nebius AE grid '
             '(30%→100% prepay = −3% on B300 12m, −7% on GB300 36m) and the CoreWeave H100 ladder (25%→100% = −4.4%).</li>'
             f'<li><strong>Quote-date normalisation:</strong> two-way fixed effects on log p₀ (tier×tenor cell + quote quarter, pooled across tiers); '
             f'each observation is shifted to the as-of quarter. Estimated quarter effects (log, vs as-of): {qe}. '
             'Pooling across tiers is a v1 simplification; Hopper and Blackwell repriced by similar ratios in 2026H1.</li>'
             f'<li><strong>Weights:</strong> observations ≤ {RECENT_DAYS} days old count 1, ≤ {MAX_AGE_DAYS} days count ½, older are dropped; '
             'ask cells weigh min(deals, 3) so a single mega-deal cannot dominate.</li>'
             f'<li><strong>Mark:</strong> weighted median of adjusted prices per cell; suppressed below {MIN_OBS} observations, never interpolated or carried forward.</li>'
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


def render_view_html(result: dict, svg: str) -> str:
    """Self-contained internal view (no CDN, no JS dependencies)."""
    rows = []
    for tier in TIERS:
        cells = [e for e in result["marks"] if e["tier"] == tier]
        if not any(e["has_mark"] for e in cells):
            continue
        tds = []
        for e in cells:
            if e["has_mark"]:
                cls = "good" if e["confidence"] == "good" else "thin"
                tds.append(f'<td class="{cls}" title="bid {_fmt(e["bid_median"])} / ask {_fmt(e["ask_median"])}">'
                           f'<b>{_fmt(e["mark"])}</b><br><small>n={e["n_obs"]}</small></td>')
            else:
                tds.append(f'<td class="na"><small>n/a<br>{e["n_obs"]} obs</small></td>')
        rows.append(f'<tr><th>{tier}</th>{"".join(tds)}</tr>')
    shape_rows = "".join(
        f'<tr><th>{x["tier"]}</th><td>{_fmt(x["mark_12m"])}</td><td>{_fmt(x["mark_36m"])}</td>'
        f'<td>{_pct(x["slope_12_36_pct"])}</td><td>{_pct(x["short_end_premium_pct"])}</td>'
        f'<td>{x["structure"] or "—"}</td><td>{_fmt(x["cost_floor"])}</td></tr>'
        for x in result["shape"] if x["mark_12m"] or x["mark_36m"])
    data_json = json.dumps(result, default=str)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU Forward Curve — internal marks {result['as_of']}</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;color:#1b1e2d;margin:0;padding:20px 24px;background:#f7f8fb}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#666;margin-bottom:14px}}
.warn{{background:#fff6d6;border:1px solid #f0d36b;border-radius:6px;padding:10px 14px;margin:12px 0;max-width:1000px}}
table{{border-collapse:collapse;margin:10px 0 18px;background:#fff}} th,td{{border:1px solid #e1e4ec;padding:6px 10px;text-align:center}}
th{{background:#eef0f6}} td.good{{background:#e6f7ef}} td.thin{{background:#fff8e1}} td.na{{color:#999;background:#f3f4f7}}
svg{{max-width:100%;height:auto;background:#fff;border:1px solid #e1e4ec;border-radius:6px}}
.foot{{color:#666;font-size:12px;max-width:1000px}}
</style></head><body>
<h1>Internal GPU forward curve — marks</h1>
<div class="sub">As of {result['as_of']} · $/GPU-hr, 0% prepay · method v{result['method_version']} · price-monitor repo</div>
<div class="warn"><b>Internal only.</b> Marked from field intel (bid) and Nebius signed reserve deals (ask, aggregates). Cells under {MIN_OBS} observations are suppressed, never interpolated. Do not quote to customers.</div>
{svg}
<h2>Marks by tenor</h2>
<table><thead><tr><th>GPU</th>{''.join(f'<th>{TENOR_LABEL[t]}</th>' for t in TENORS)}</tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Curve shape</h2>
<table><thead><tr><th>GPU</th><th>12m</th><th>36m</th><th>12→36m</th><th>3m vs 12m</th><th>Structure</th><th>SA cost floor</th></tr></thead><tbody>{shape_rows}</tbody></table>
<p class="foot">Green = good confidence (n ≥ {GOOD_OBS}), yellow = thin ({MIN_OBS}–{GOOD_OBS - 1}), grey = suppressed. Hover a cell for bid/ask medians. Full method on the Confluence page and in analysis/forward_curve_method.md.</p>
<script type="application/json" id="forward-curve-data">{data_json}</script>
</body></html>"""


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
