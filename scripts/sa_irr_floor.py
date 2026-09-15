#!/usr/bin/env python3
"""
SemiAnalysis' cost-based floor for our tiers: the price at which a 5-year contract with 15%
prepaid earns ~15.6% project IRR on SemiAnalysis' Neocloud Giant inputs. Replicates the
'Business Case' sheet of the AI-Cloud TCO model (traced formula by formula and validated to
the cent against the sheet's cached H100 cash flows on 2026-09-15; independently re-derived
the same day) and writes `floor_irr_15_6`, `floor_irr_wacc` and `floor_irr_15_6_no_prepay`
into store/sa_reference.json for every tier.

    python3 scripts/extract_sa_reference.py <workbook>   # first: capex, W/GPU, GPUs per server, WACC
    python3 scripts/sa_irr_floor.py                       # then: floors + reproduction check

Method (Business Case rows 118-159, 219-346), one "server" of N logical GPUs (N = Full TCO
r139: 72 for NVL72 racks, 8 for HGX), start 2025-12-30, monthly columns at month-ends:
  revenue      = P x N x 24 x days (33 days in month 1), 100% billed, flat price for the term
  hosting      = 150 $/kW/mth x 1.03^year x W/GPU x N          (W = Full TCO r77, all-in)
  electricity  = 0.087 $/kWh x 1.03^year x N x 24 x days x W/1000 x PUE 1.35 x 80% utilisation
  R&M 30 and AMC 10 $/server/mth (AMC from month 14); depreciation capex/6 yrs; install 1,000/3 yrs
  tax 20% of PBT (negative tax allowed); unlevered project cash flow (debt cancels exactly)
  working capital: AR 45 days of revenue less the prepay unwind, AP 90 days of hosting+power
  prepay = 15% of the 5-year contract value in month 1 as unearned revenue, unwound 1/60 per month
  project IRR = XIRR (actual/365) of months 1..60; no residual value, no datacenter capex
Known discrepancy: SemiAnalysis' printed VR floor ($5.249) is a pre-Aug-10 snapshot; with the
Aug-10 VR capex the same method gives $4.89, with the July-13 capex ($128,194/GPU) $5.246.
"""
import calendar
import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "store" / "sa_reference.json"
XLSX_START = dt.date(2025, 12, 30)
HURDLE = 0.156   # SemiAnalysis' "GB300 NVL72 breakeven IRR (input)" on the Cost-Value sheet: an exogenous hurdle


def month_ends(n):
    out, y, m = [], 2026, 1
    for _ in range(n):
        out.append(dt.date(y, m, calendar.monthrange(y, m)[1]))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def xirr(cfs, dates, lo=-0.99, hi=50.0):
    d0 = dates[0]
    npv = lambda r: sum(cf / (1 + r) ** ((d - d0).days / 365.0) for cf, d in zip(cfs, dates))  # noqa: E731
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def business_case_irr(price, capex_per_gpu, w_gpu, n_gpu, *, prepay_pct=0.15, prepay_years=5, term_years=5,
                      colo=150.0, colo_esc=0.03, elec=0.087, elec_esc=0.03, pue=1.35, util=0.80,
                      rm_per_server=30.0, amc_per_server=10.0, amc_start_year=1, tax=0.20,
                      dep_years=6, install_per_server=1000.0, amort_years=3, ar_days=45, ap_days=90,
                      capex_scale=1.0):
    n_months = term_years * 12
    dates = month_ends(n_months)
    N = n_gpu
    capex = capex_per_gpu * n_gpu * capex_scale
    install = install_per_server
    prepay = price * N * 24 * 365 * prepay_years * prepay_pct
    cfs, prev_ar, prev_ap = [], 0.0, 0.0
    for k, d in enumerate(dates):
        first = k == 0
        days_rev = (d - XLSX_START).days + 1 if first else (d - dates[k - 1]).days
        days = (d - XLSX_START).days if first else (d - dates[k - 1]).days
        yrs_since = (d - XLSX_START).days / 365.0
        rev = price * N * 24 * days_rev
        esc_idx = int(((d - XLSX_START).days - 10) // 365)
        hosting = colo * (1 + colo_esc) ** esc_idx * w_gpu / 1000 * N
        elec_cost = elec * (1 + elec_esc) ** esc_idx * N * 24 * days * w_gpu / 1000 * pue * util
        amc = amc_per_server if (d - dates[0]).days / 365.0 > amc_start_year else 0.0
        dep = capex / dep_years * days / 365.0 if round(yrs_since, 2) <= dep_years else 0.0
        amort = install / amort_years * days / 365.0 if round(yrs_since, 2) <= amort_years else 0.0
        ebit = rev - hosting - elec_cost - rm_per_server - amc - dep - amort
        unwind = -(prepay / n_months)
        ar = min(ar_days, (d - XLSX_START).days) / days * rev + unwind
        ap = min(ap_days, (d - XLSX_START).days) / days * (hosting + elec_cost)
        wc = -(ar - prev_ar) + (ap - prev_ap)
        prev_ar, prev_ap = ar, ap
        d_unearned = (prepay if first else 0.0) + unwind
        inv = -(capex + install) if first else 0.0
        cfs.append(ebit * (1 - tax) + dep + amort + wc + d_unearned + inv)
    return xirr(cfs, dates)


def solve_price(target_irr, lo=0.5, hi=60.0, **kw):
    f = lambda p: business_case_irr(p, **kw) - target_irr  # noqa: E731
    grid = [(p, f(p)) for p in (lo + (hi - lo) * i / 60 for i in range(61))]
    grid = [(p, v) for p, v in grid if not math.isnan(v)]
    lo = max(p for p, v in grid if v < 0)
    hi = min(p for p, v in grid if v > 0)
    for _ in range(100):
        mid = (lo + hi) / 2
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# SemiAnalysis' own worked blocks (Cost-Value Visualization), used as the reproduction test
SA_VR_FLOOR, SA_VR_CAPEX_JULY = 5.24905360496408, 128194.485      # July-13 VR capex implied by Revisions!O90
SA_VR_TABLE = {4.0: -0.0177678853, 4.2: 0.0094459444, 4.4: 0.0368233532, 4.6: 0.0644356281, 4.8: 0.0923511177,
               5.0: 0.1206363976, 5.2: 0.1493572176, 5.4: 0.1785793483, 5.6: 0.2083693683, 5.8: 0.2387953103, 6.0: 0.2699274242}
SA_MI_FLOOR = 4.125132195582058
MI4XX_WB = dict(capex_per_gpu=92671.5092, w_gpu=3581.4557, n_gpu=72)   # Full TCO col P "MI4XX (WB)", Neocloud Giant
VR_W, VR_N = 3273.159651864607, 72


def main() -> int:
    ref = json.loads(REF.read_text())
    vr_fit = solve_price(HURDLE, capex_per_gpu=SA_VR_CAPEX_JULY, w_gpu=VR_W, n_gpu=VR_N)
    vr_err = max(abs(business_case_irr(p, capex_per_gpu=SA_VR_CAPEX_JULY, w_gpu=VR_W, n_gpu=VR_N) - v) * 100 for p, v in SA_VR_TABLE.items())
    mi_fit = solve_price(HURDLE, **MI4XX_WB)
    print(f"reproduction: VR floor {vr_fit:.4f} vs SA {SA_VR_FLOOR:.4f} (July-13 capex; max IRR-table error {vr_err:.3f} pp); "
          f"MI4XX floor {mi_fit:.4f} vs SA {SA_MI_FLOOR:.4f}")
    ok = abs(vr_fit - SA_VR_FLOOR) < 0.02 and abs(mi_fit - SA_MI_FLOOR) < 0.05
    if not ok:
        print("reproduction FAILED: not writing floors")
        return 1
    print(f"{'tier':6} {'capex/GPU':>10} {'W/GPU':>6} {'N':>3}  {'15.6% IRR':>9} {'no prepay':>9} {'at WACC':>8}   cash cost r147")
    for tier, t in ref["tiers"].items():
        w, n = t.get("all_in_watts_per_gpu"), t.get("gpus_per_server")
        if not (t.get("capex_per_gpu_usd") and w and n):
            print(f"{tier:6} inputs missing, skipped"); continue
        args = dict(capex_per_gpu=t["capex_per_gpu_usd"], w_gpu=w, n_gpu=int(n))
        f156 = solve_price(HURDLE, **args)
        fnp = solve_price(HURDLE, prepay_pct=0.0, **args)
        fw = solve_price(t.get("wacc") or 0.1025, **args)
        t.update({"floor_irr_15_6": round(f156, 3), "floor_irr_15_6_no_prepay": round(fnp, 3), "floor_irr_wacc": round(fw, 3),
                  "floor_irr_method": "SemiAnalysis Business Case replica: 5-year contract, 15% prepaid, Neocloud Giant inputs, 20% tax, no residual; hurdle 15.6%"})
        print(f"{tier:6} {t['capex_per_gpu_usd']:>10,.0f} {w:>6.0f} {int(n):>3}  {f156:>9.3f} {fnp:>9.3f} {fw:>8.3f}   {t.get('total_cost_per_hour')}")
    ref["_floor_irr"] = {"hurdle": HURDLE, "prepay": 0.15, "term_years": 5, "computed": dt.date.today().isoformat(),
                         "reproduction": {"vr_floor_sa": SA_VR_FLOOR, "vr_floor_reproduced_july_capex": round(vr_fit, 4), "mi4xx_floor_sa": SA_MI_FLOOR, "mi4xx_floor_reproduced": round(mi_fit, 4)},
                         "note": "SemiAnalysis' printed VR floor uses its July-13 VR capex ($128,194/GPU); with the Aug-10 capex the same method gives the floor stored here."}
    REF.write_text(json.dumps(ref, indent=1))
    print(f"updated {REF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
