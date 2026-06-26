"""
PAYG pricing-strategy scenarios — reserve-based vs market-benchmark.

Action item from the 2026-06-22 PAYG Capacity check-in (for the July 6 pricing
decision): "prepare pricing strategy models comparing reserve-based pricing versus
market benchmark-based pricing." For each revenue-driver GPU it compares current
Nebius PAYG (on-demand) against two principled rules:

  (A) RESERVE-BASED:  PAYG = reserve(committed, above-512, 100%) x (1 + RESERVE_MARKUP)
      (Koen, 16:54: "PAYG ~60% above reserve". Reserve stays internal; it's the anchor.)
  (B) MARKET-BENCHMARK: PAYG = cluster-peer median x (1 + PEER_PREMIUM), held below the
      cheapest hyperscaler. (Anton/Andrei, 14:03/18:28: "below hyperscalers, slightly
      above new-cloud competitors.")

NER (per-GPU "not enough resources" error rate) is the demand-pressure guardrail: high
NER = demand exceeds available PAYG capacity (room to raise); low NER = ample capacity
(raising risks utilization, per the last big jump: -29% utilization for +2% revenue).

Dials are at the top — tune and re-run before sharing. NER is injected (DWH query).
Reuses diff.py's verified cluster-class peer + hyperscaler selection so the
market-benchmark side matches the daily dashboard exactly.
"""
import json
from pathlib import Path

from config import NEBIUS_COMMITTED_PRICES
from diff import (SUMMARY_GPUS, compute_position, _best_comparable,
                  enrich_comparability)
from schema import PriceRecord

# ── Tunable dials ────────────────────────────────────────────────────────────
RESERVE_MARKUP = 0.60   # PAYG target premium over the 1yr reserve rate
PEER_PREMIUM   = 0.05   # PAYG target premium over the cluster-peer median ("slightly above")
RESERVE_TERM   = 12     # which committed term (months) is the reserve anchor: 12=1yr
RESERVE_TIER   = "above_512"

# NER = demand-pressure guardrail. DEDUPED, PRODUCTION-ONLY real unmet demand on the PAYG
# pool, last 30d, from //home/dwh/nemax-prod/data/ods/capacity/ner_alerts (queries
# 447c996d / 43a14a26 / 92bfa2df, 2026-06-26; window 2026-05-27..06-26).
# IMPORTANT (analysis 2026-06-26): the raw alert count is NOT demand — it is retry-inflated
# ~150-230x (one project failing generates ~150-230 alerts/day), test regions (e0t/u0t/e1t)
# inflate B300/GB300, and a few projects make ~50-80% of each region's alerts. The honest
# measure is project_days = distinct (project, day) of unmet demand in PRODUCTION regions.
# Demand is region-concentrated (H100->Manchester e00; H200->Paris e01/Kansas u00/Man e00;
# B200->Kansas u00/Israel i00) and user-concentrated. GB300 production demand ~0 (all test).
NER_30D = {
    "H200":  {"project_days": 1553, "projects": 758, "region": "Paris/Kansas/Man", "conc": "~50% from 1 proj/region"},
    "H100":  {"project_days": 750,  "projects": 418, "region": "Manchester (e00)", "conc": "top2=46%, top5=79%"},
    "B200":  {"project_days": 1044, "projects": 300, "region": "Kansas/Israel",    "conc": "moderate"},
    "B300":  {"project_days": 520,  "projects": 477, "region": "UK + spread",      "conc": "low retry (real but small)"},
    "GB300": {"project_days": 0,    "projects": 0,   "region": "test-only",        "conc": "no real PAYG demand"},
}

STORE = Path(__file__).parent / "store"


def _ner_tier(pd_):
    if pd_ is None:     return "—"
    if pd_ >= 1000:     return "HIGH"
    if pd_ >= 500:      return "moderate"
    if pd_ >= 100:      return "low"
    return "none"


def _load_records():
    raw = json.load(open(STORE / "last_snapshot.json"))
    recs = []
    for r in raw:
        try:
            recs.append(PriceRecord(
                provider=r["provider"], gpu_model=r["gpu_model"],
                gpu_count=r.get("gpu_count", 1), instance_type=r.get("instance_type", ""),
                region=r.get("region", "global"), consumption_type=r["consumption_type"],
                price_per_hour_usd=r.get("price_per_hour_usd", 0),
                price_per_gpu_hour_usd=r["price_per_gpu_hour_usd"],
                fetched_at=r.get("fetched_at", ""), source_url=r.get("source_url", ""),
                data_source=r.get("data_source", ""),
                form_factor=r.get("form_factor"), interconnect=r.get("interconnect"),
                node_gpus=r.get("node_gpus")))
        except Exception:
            pass
    enrich_comparability(recs)
    return recs


def _reserve(gpu, term=RESERVE_TERM, tier=RESERVE_TIER, prepay="100pct"):
    g = NEBIUS_COMMITTED_PRICES.get(gpu, {})
    t = g.get(tier) or next(iter(g.values()), {})   # fall back to whatever tier exists
    row = t.get(term)
    if not row:
        # nearest available term
        for k in sorted(t):
            row = t[k]
            term = k
            break
    return (row.get(prepay) if row else None), term


def build(ner=None):
    ner = ner or NER_30D
    recs = _load_records()
    payg = {r.gpu_model: r.price_per_gpu_hour_usd for r in recs
            if r.provider == "nebius" and r.consumption_type == "on_demand"}
    pos = {row["gpu"]: row for row in compute_position(recs) if row["tier_label"] == "on_demand"}

    rows = []
    for gpu in SUMMARY_GPUS:
        cur = payg.get(gpu)
        reserve, rterm = _reserve(gpu)
        peer_med = (pos.get(gpu) or {}).get("median_peer")
        hyp = _best_comparable(recs, gpu, "on_demand", tiers=["hyperscaler"])
        hyp_px = hyp.price_per_gpu_hour_usd if hyp else None

        rb = reserve * (1 + RESERVE_MARKUP) if reserve else None
        mb = peer_med * (1 + PEER_PREMIUM) if peer_med else None
        mb_capped = (mb is not None and hyp_px is not None and mb >= hyp_px)
        if mb_capped:   # never price at/above the cheapest hyperscaler
            mb = round(hyp_px * 0.97, 2)

        rows.append({
            "gpu": gpu, "current": cur, "reserve": reserve, "rterm": rterm,
            "implied_markup": (cur / reserve - 1) if (cur and reserve) else None,
            "rb": rb, "rb_delta": (rb / cur - 1) if (rb and cur) else None,
            "peer_med": peer_med, "hyp": hyp_px,
            "mb": mb, "mb_delta": (mb / cur - 1) if (mb and cur) else None, "mb_capped": mb_capped,
            "ner": ner.get(gpu),
        })
    return rows


def _ner_str(n):
    if not n:
        return "—"
    pd_ = n.get("project_days")
    tier = _ner_tier(pd_)
    return f"{tier} ({pd_}pd, {n.get('region')})"


def _f(x, pct=False):
    if x is None:
        return "—"
    return f"{x*100:+.0f}%" if pct else f"${x:.2f}"


def render(rows):
    print(f"PAYG pricing scenarios  (reserve markup {RESERVE_MARKUP:+.0%} over {RESERVE_TERM}mo {RESERVE_TIER}; "
          f"peer premium {PEER_PREMIUM:+.0%})\n")
    hdr = ("GPU", "PAYG now", f"Resv{RESERVE_TERM}mo", "impl.mkup",
           "RESERVE-based (Δ)", "Peer med", "Cheap hyp", "MARKET-bench (Δ)", "NER real demand (30d, deduped)")
    print("{:<7}{:<10}{:<11}{:<10}{:<19}{:<10}{:<11}{:<19}{:<40}".format(*hdr))
    for r in rows:
        rb = f"{_f(r['rb'])} ({_f(r['rb_delta'],1)})" if r['rb'] else "—"
        mb = (f"{_f(r['mb'])} ({_f(r['mb_delta'],1)})" + (" cap" if r['mb_capped'] else "")) if r['mb'] else "—"
        print("{:<7}{:<10}{:<11}{:<10}{:<19}{:<10}{:<11}{:<19}{:<40}".format(
            r["gpu"], _f(r["current"]), _f(r["reserve"]), _f(r["implied_markup"], 1),
            rb, _f(r["peer_med"]), _f(r["hyp"]), mb, _ner_str(r["ner"])))


if __name__ == "__main__":
    render(build())
