import json, sys
sys.path.insert(0, '.')
SCRATCH = "/private/tmp/claude-501/-Users-koenbrormann-Claude-PM/8c029e0b-e218-4e39-8765-0b340411eb97/scratchpad"
ILS_USD = 3.65   # approx; ILS is <3% of totals, flagged in output
DAYS = {"2026-04": 30, "2026-05": 31, "2026-06": 30, "2026-07": 5}
NEW_CAPACITY_SKUS = {"sku-e00fid4v0v8mqolddeuts",   # B300 eu-west2 (Paris) — first bills June
                     "sku-e00fd2b1b7h2b3zl24md9"}   # GB300 eu-north1 — first bills July, $0
META = json.load(open(f"{SCRATCH}/sku_meta.json"))

def usd(v, ccy): return v / ILS_USD if ccy == "ILS" else v

# rows: [month, sku, ccy, qty, unc, gross, cost] ; july: [sku, ccy, qty, unc, gross, cost, susp_unc, susp_cost]
monthly = json.load(open(f"{SCRATCH}/monthly.json"))
susp    = json.load(open(f"{SCRATCH}/suspended.json"))
july    = json.load(open(f"{SCRATCH}/july_1to5.json"))

from collections import defaultdict
# agg[(month, gpu, type, samestore_flag)] -> [unc_gpuh, cost_usd]  (uncovered = PAYG-proper; covered/reserve excluded)
agg  = defaultdict(lambda: [0.0, 0.0])
sagg = defaultdict(lambda: [0.0, 0.0])   # suspended-only portion
for m, sku, ccy, qty, unc, gross, cost in monthly:
    if sku not in META: continue
    gpu, region, typ = META[sku]
    ss = sku not in NEW_CAPACITY_SKUS
    agg[(m, gpu, typ, ss)][0] += unc
    agg[(m, gpu, typ, ss)][1] += usd(cost, ccy)
for m, sku, ccy, qty, unc, gross, cost in susp:
    gpu, region, typ = META[sku]
    ss = sku not in NEW_CAPACITY_SKUS
    sagg[(m, gpu, typ, ss)][0] += unc
    sagg[(m, gpu, typ, ss)][1] += usd(cost, ccy)
for sku, ccy, qty, unc, gross, cost, s_unc, s_cost in july:
    if sku not in META: continue
    gpu, region, typ = META[sku]
    ss = sku not in NEW_CAPACITY_SKUS
    agg[("2026-07", gpu, typ, ss)][0] += unc
    agg[("2026-07", gpu, typ, ss)][1] += usd(cost, ccy)
    sagg[("2026-07", gpu, typ, ss)][0] += s_unc
    sagg[("2026-07", gpu, typ, ss)][1] += usd(s_cost, ccy)

GPUS = ["H100", "H200", "B200", "B300", "L40S", "RTX6000"]
MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07"]

def slice_(m, gpu, typ, samestore=True, net=True):
    keys = [(m, gpu, typ, True)] + ([] if samestore else [(m, gpu, typ, False)])
    u = sum(agg[k][0] for k in keys); c = sum(agg[k][1] for k in keys)
    if net:
        u -= sum(sagg[k][0] for k in keys); c -= sum(sagg[k][1] for k in keys)
    return u / DAYS[m], c / DAYS[m]   # per-day

print("=" * 100)
print("PART 1 — JUNE-1 PRICE CHANGE IMPACT (basis: billed consumption, wo VAT, uncovered-only = PAYG-proper;")
print("reserve-covered usage excluded; suspended/debt accounts netted out = collectible; ILS @3.65; per-day rates)")
print("=" * 100)
for label, netflag in [("COLLECTIBLE (fraud/suspended netted out)", True), ("GROSS (incl suspended accts)", False)]:
    print(f"\n--- {label} — SAME-STORE (excl B300 eu-west2 + GB300, both new) ---")
    print(f"{'GPU':8}{'type':5}{'Apr GPU-h/d':>12}{'May GPU-h/d':>12}{'Jun GPU-h/d':>12}{'Jul1-5/d':>10} | {'May $/d':>9}{'Jun $/d':>9}{'Jul $/d':>9} | {'use Δ':>7}{'rev Δ':>7}{'$/GPUh May→Jun':>16}")
    for typ in ("od", "pvm"):
        tot = {m: [0.0, 0.0] for m in MONTHS}
        for gpu in GPUS:
            row = {m: slice_(m, gpu, typ, samestore=True, net=netflag) for m in MONTHS}
            for m in MONTHS:
                tot[m][0] += row[m][0]; tot[m][1] += row[m][1]
            if row["2026-05"][0] < 100: continue
            du = row["2026-06"][0] / row["2026-05"][0] - 1
            dr = row["2026-06"][1] / row["2026-05"][1] - 1 if row["2026-05"][1] else 0
            p5 = row["2026-05"][1] / row["2026-05"][0] if row["2026-05"][0] else 0
            p6 = row["2026-06"][1] / row["2026-06"][0] if row["2026-06"][0] else 0
            print(f"{gpu:8}{typ:5}{row['2026-04'][0]:12,.0f}{row['2026-05'][0]:12,.0f}{row['2026-06'][0]:12,.0f}{row['2026-07'][0]:10,.0f} | "
                  f"{row['2026-05'][1]:9,.0f}{row['2026-06'][1]:9,.0f}{row['2026-07'][1]:9,.0f} | {du:+7.0%}{dr:+7.0%}   ${p5:.2f}→${p6:.2f}")
        du = tot["2026-06"][0] / tot["2026-05"][0] - 1
        dr = tot["2026-06"][1] / tot["2026-05"][1] - 1
        dj = tot["2026-07"][1] / tot["2026-05"][1] - 1
        print(f"{'TOTAL':8}{typ:5}{tot['2026-04'][0]:12,.0f}{tot['2026-05'][0]:12,.0f}{tot['2026-06'][0]:12,.0f}{tot['2026-07'][0]:10,.0f} | "
              f"{tot['2026-05'][1]:9,.0f}{tot['2026-06'][1]:9,.0f}{tot['2026-07'][1]:9,.0f} | {du:+7.0%}{dr:+7.0%}   (Jul vs May rev: {dj:+.0%})")
    # blended
    b = {m: [sum(slice_(m,g,t,True,netflag)[i] for g in GPUS for t in ("od","pvm")) for i in (0,1)] for m in MONTHS}
    print(f"{'BLENDED':8}{'':5}{b['2026-04'][0]:12,.0f}{b['2026-05'][0]:12,.0f}{b['2026-06'][0]:12,.0f}{b['2026-07'][0]:10,.0f} | "
          f"{b['2026-05'][1]:9,.0f}{b['2026-06'][1]:9,.0f}{b['2026-07'][1]:9,.0f} | {b['2026-06'][0]/b['2026-05'][0]-1:+7.0%}{b['2026-06'][1]/b['2026-05'][1]-1:+7.0%}   (Jul rev/d vs May: {b['2026-07'][1]/b['2026-05'][1]-1:+.0%})")

# New capacity contribution (excluded from same-store)
nc = {m: sum(usd(r[6], r[2]) if False else 0 for r in []) for m in MONTHS}
b300new_jun = sum(usd(cost, ccy) for m, sku, ccy, q, u, g, cost in monthly if sku == "sku-e00fid4v0v8mqolddeuts" and m == "2026-06") / 30
b300new_jul = sum(usd(cost, ccy) for sku, ccy, q, u, g, cost, su, sc in july if sku == "sku-e00fid4v0v8mqolddeuts") / 5
print(f"\nNEW CAPACITY (excluded above): B300 eu-west2 (Paris): Jun ${b300new_jun:,.0f}/d -> Jul1-5 ${b300new_jul:,.0f}/d (all uncovered/PAYG, launched ~Jun 26)")

# fraud share
for m in ("2026-05", "2026-06", "2026-07"):
    su = sum(sagg[(m,g,t,ss)][1] for g in GPUS for t in ("od","pvm") for ss in (True,False))
    au = sum(agg[(m,g,t,ss)][1] for g in GPUS for t in ("od","pvm") for ss in (True,False))
    print(f"suspended-acct share of billed PAYG {m}: {su/au:.1%}  (${su/DAYS[m]:,.0f}/d of ${au/DAYS[m]:,.0f}/d)")

# ---------- PARTS 2-4: strategy on fresh snapshot ----------
from schema import PriceRecord
from diff import (SUMMARY_GPUS, compute_position, _best_comparable, enrich_comparability,
                  _representative_spot_floor, INTERRUPTIBLE_CTS)
from config import NEBIUS_COMMITTED_PRICES
recs = [PriceRecord.from_dict(r) for r in json.load(open(f"{SCRATCH}/snap.json"))]
enrich_comparability(recs)
payg = {}; pvm = {}
for r in recs:
    if r.provider == "nebius" and r.consumption_type == "on_demand":
        payg[r.gpu_model] = r.price_per_gpu_hour_usd
    if r.provider == "nebius" and r.consumption_type in INTERRUPTIBLE_CTS:
        pvm[r.gpu_model] = min(pvm.get(r.gpu_model, 9e9), r.price_per_gpu_hour_usd)
pos = {row["gpu"]: row for row in compute_position(recs) if row["tier_label"] == "on_demand"}
PROPOSAL = {"H100": 3.85, "H200": 4.73, "B200": 7.51, "B300": 8.24}

print("\n" + "=" * 100)
print("PART 2 — PEER-ANCHORED STRATEGY (slightly above peer median, capped below cheapest hyperscaler)")
print("(cluster-class peers only; snapshot 2026-07-06 all-live)")
print("=" * 100)
print(f"{'GPU':6}{'now':>7}{'peer med':>9}{'n':>3}{'floor peer':>11}{'cheap hyp':>10}{'@med+2%':>9}{'@med+5%':>9}{'@med+10%':>9}{'capped?':>8}{'Jul6 prop':>10}")
for gpu in ["H100", "H200", "B200", "B300", "L40S"]:
    p = pos.get(gpu) or {}
    med, n = p.get("median_peer"), p.get("total_peers", 0)
    hyp = _best_comparable(recs, gpu, "on_demand", tiers=["hyperscaler"])
    hyp_px = hyp.price_per_gpu_hour_usd if hyp else None
    cap = round(hyp_px * 0.97, 2) if hyp_px else None
    def tgt(prem):
        if med is None: return None
        v = med * (1 + prem)
        return min(v, cap) if cap else v
    t2, t5, t10 = tgt(0.02), tgt(0.05), tgt(0.10)
    capped = "yes" if (med and cap and med * 1.05 > cap) else ""
    cheapest = p.get("cheapest_peers", [])
    fl = f"{cheapest[0][0]} ${cheapest[0][1]:.2f}" if cheapest else "-"
    fmt = lambda v: f"${v:.2f}" if v else "  -"
    print(f"{gpu:6}{fmt(payg.get(gpu)):>7}{fmt(med):>9}{n:>3}{fl:>11}{fmt(hyp_px):>10}{fmt(t2):>9}{fmt(t5):>9}{fmt(t10):>9}{capped:>8}{fmt(PROPOSAL.get(gpu)):>10}")

print("\n" + "=" * 100)
print("PART 3 — LADDER CONSISTENCY: Reserve & PVM as % below PAYG (reserve = 12mo, 512+, 100% prepay)")
print("=" * 100)
print(f"{'GPU':6}{'PAYG now':>9}{'Reserve12':>10}{'resv disc':>10}{'PVM':>7}{'pvm disc':>9} | {'PAYG Jul6':>10}{'resv disc*':>11}{'pvm disc*':>10}")
for gpu in ["H100", "H200", "B200", "B300", "GB300"]:
    cur = payg.get(gpu)
    g = NEBIUS_COMMITTED_PRICES.get(gpu, {})
    t = g.get("above_512") or next(iter(g.values()), {})
    r12 = (t.get(12) or {}).get("100pct")
    pv = pvm.get(gpu)
    prop = PROPOSAL.get(gpu, cur)
    f = lambda v: f"${v:.2f}" if v else "  -"
    rd  = f"{r12/cur-1:+.0%}" if (r12 and cur) else "-"
    pd_ = f"{pv/cur-1:+.0%}" if (pv and cur) else "-"
    rd2 = f"{r12/prop-1:+.0%}" if (r12 and prop) else "-"
    pd2 = f"{pv/prop-1:+.0%}" if (pv and prop) else "-"
    print(f"{gpu:6}{f(cur):>9}{f(r12):>10}{rd:>10}{f(pv):>7}{pd_:>9} | {f(prop):>10}{rd2:>11}{pd2:>10}")

print("\n" + "=" * 100)
print("PART 4 — HYBRID: PAYG in band [reserve12 x1.6 floor -> hyperscaler x0.97 cap], peer median x1.05 anchor inside")
print("=" * 100)
print(f"{'GPU':6}{'floor':>7}{'anchor':>8}{'cap':>7}{'hybrid tgt':>11}{'now':>7}{'gap now':>8}{'Jul6 prop':>10}{'gap prop':>9}")
for gpu in ["H100", "H200", "B200", "B300"]:
    g = NEBIUS_COMMITTED_PRICES.get(gpu, {})
    r12 = ((g.get("above_512") or {}).get(12) or {}).get("100pct")
    floor = r12 * 1.6 if r12 else None
    p = pos.get(gpu) or {}
    med, n = p.get("median_peer"), p.get("total_peers", 0)
    hyp = _best_comparable(recs, gpu, "on_demand", tiers=["hyperscaler"])
    cap = hyp.price_per_gpu_hour_usd * 0.97 if hyp else None
    anchor = med * 1.05 if (med and n >= 2) else None
    cands = [v for v in (anchor,) if v is not None] or [floor]
    tgt = max(floor, min(cands[0], cap))
    cur = payg.get(gpu); prop = PROPOSAL.get(gpu)
    f = lambda v: f"${v:.2f}" if v else "  -"
    print(f"{gpu:6}{f(floor):>7}{f(anchor):>8}{f(cap):>7}{f(tgt):>11}{f(cur):>7}{(tgt/cur-1 if cur else 0):>+8.0%}{f(prop):>10}{(tgt/prop-1 if prop else 0):>+9.0%}")
print("\nHybrid rule: floor = reserve stays discounted >=37.5% vs PAYG; anchor = slightly-above-peers when >=2 cluster peers;")
print("cap = never above 97% of cheapest hyperscaler. Move toward target 3-5%/mo, gated on NER>threshold AND sell-through>95%.")
