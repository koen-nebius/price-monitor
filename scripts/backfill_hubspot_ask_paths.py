#!/usr/bin/env python3
"""
One-off backfill of the HubSpot-era ask-to-close price paths (2026-09-16, Koen: "build 1 and 2").

The deal-review mirror cdm/crm/deal_review_line_items_daily kept a full snapshot of every
deal line item at every twice-weekly deal review (177 review dates, 2024-10-15 .. 2026-08-10,
frozen since the Salesforce cutover). Read per (line item, review snapshot) it is the only source that shows
the PATH of our asked price from the first review to the close: first ask, every revision,
the final price, and the outcome. Nothing in Salesforce keeps that history (quotes are
current-state only; scripts/refresh_quote_asks.py rebuilds it going forward).

Writes
  store/ask_to_close_paths.csv  one row per GPU line item that carried a price in at least one
                                snapshot: first/last snapshot and price, number of revisions,
                                the compact path (date:price at each change), final stage and
                                outcome, GPU, quantity, term, payment type, loss category.
                                Opaque HubSpot ids only, no names. INTERNAL.
  store/ask_to_close.csv        aggregates per GPU x tenor bucket x outcome, cells with >= 2
                                deals: first-ask median, final median, median final/first ratio,
                                share of lines revised, median revision among revised lines,
                                median days from first review to final. forward_curve renders
                                it as a reference class (never pooled into a mark).

Rack-priced lines (GB200/GB300 NVL72, 'GPU-72' products) are divided by GPUs per rack before
anything else; plausibility band 0.3-40 $/GPU-hr per snapshot. Outcome, final price, quantity
and term are read at the line's LAST PRICED snapshot (15 of 3,800 lines lose their price before
a later stage change; the price and the stage are kept from one snapshot on purpose).
Left-censoring: a line already priced at the first review in the pull has an unknown earlier
history (first ask, revisions, timing). Such lines are flagged left_censored in the paths file
and excluded from the aggregates; pull the whole mirror (default --since 2024-10-01) so only
lines older than the mirror itself are affected.

Runs LOCALLY only (YT is not reachable from GitHub Actions):
    ~/nebo/analytics/libs/data_clients/yt_data_client/yt_mcp/.venv/bin/python \
        scripts/backfill_hubspot_ask_paths.py [--dry-run] [--since 2024-10-01] [--raw-cache /path/raw.csv]
"""
import csv
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATHS = ROOT / "store" / "ask_to_close_paths.csv"
OUT = ROOT / "store" / "ask_to_close.csv"
TRACKED = ("H100", "H200", "B200", "B300", "GB200", "GB300", "VR")
PRICE_LO, PRICE_HI = 0.3, 40.0


def bucket_months(m: float) -> int:
    return 3 if m <= 4 else 6 if m <= 8 else 12 if m <= 14 else 18 if m <= 20 else 24 if m <= 27 else 36 if m <= 40 else 48 if m <= 50 else 60


# Per (crm_line_item_id, meeting snapshot) rows for GPU line items, scripts/sql/hubspot_ask_paths.sql:
# authored and tied out 2026-09-16 against four confirmed line items and re-run by an independent check
# (382,707 pairs, 4,172 line items, 167 snapshots). The query client returns at most 10,000 rows per
# call (Query Tracker cap), so the pull is sharded on cityHash64(crm_line_item_id) % SHARDS.
SQL_PATH = ROOT / "scripts" / "sql" / "hubspot_ask_paths.sql"
SQL = SQL_PATH.read_text() if SQL_PATH.exists() else "__SQL_PLACEHOLDER__"
SHARDS = 64
ROW_CAP = 10000
WORKERS = 4                    # concurrent CHYT shard pulls
FREEZE = "2026-08-10"          # the mirror stopped moving here; later snapshots are identical copies
EPOCH = date(1970, 1, 1)

PATH_COLUMNS = ["generated_date", "crm_line_item_id", "crm_deal_id", "gpu", "unit", "gpus_per_rack", "gpu_qty", "term_m",
                "consumption_type", "payment_type", "left_censored", "first_dt", "first_price", "final_dt", "last_dt", "last_price", "last_price_effective", "n_snapshots",
                "n_revisions", "path", "final_stage", "outcome", "first_won_dt", "lost_category", "capacity_status",
                "data_center", "days_first_to_final", "change_pct"]
AGG_COLUMNS = ["generated_date", "window_from", "window_to", "gpu", "tenor_months", "outcome", "line_items", "deals", "gpus",
               "first_ask_med", "final_med", "ratio_med", "share_revised", "revision_med_pct", "days_med"]


def _client():
    home = Path.home()
    os.environ.setdefault("YT_PROXY", "https://planck.yt.nebius.yt")
    os.environ.setdefault("YT_CONFIG_PROFILE", "planck")
    os.environ.setdefault("REQUESTS_CA_BUNDLE", str(home / ".yt" / "ca-certificates" / "planck"))
    if not os.environ.get("YT_TOKEN"):
        tok = home / ".yt" / "token_planck"
        if tok.exists():
            os.environ["YT_TOKEN"] = tok.read_text().strip()
    sys.path.insert(0, str(home / "nebo" / "analytics" / "libs" / "data_clients" / "yt_data_client"))
    from yt_data_client import YtDataClient  # noqa: E402  (venv-only import)
    return YtDataClient()


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _d(v) -> str:
    """Date column -> 'YYYY-MM-DD'; the client hands Date columns back as days since 1970 (int) or as text."""
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if v != v:
            return ""
        return (EPOCH + timedelta(days=int(v))).isoformat()
    s = str(v)
    return "" if s in ("nan", "NaT", "None") else s[:10]


def per_gpu(raw, unit: str, gpu: str, gpr):
    """$ per unit-hour -> $ per GPU-hour with the two mislabel corrections seen in the mirror:
    rack rows priced under $40 are already per GPU; GB/VR rows under unit 'gpu' at >= $40 are per rack."""
    if raw is None:
        return None
    if unit == "rack":
        return raw if raw < 40 else (raw / gpr if gpr else None)
    if gpu in ("GB200", "GB300", "VR") and raw >= 40 and gpr:
        return raw / gpr
    return raw


def outcome_of(stage: str, lost_category: str = "") -> str:
    s = (stage or "").lower(); c = (lost_category or "").lower()
    if "won" in s:
        return "won"
    if "lost" in s:
        if "capacity" in c:
            return "lost_capacity"
        if "pric" in c or "competitor" in c:
            return "lost_price_or_competitor"
        return "lost_other"
    if "disqualif" in s:
        return "disqualified"
    return "open_at_freeze"


def build_paths(raw: list[dict], today: str) -> list[dict]:
    by_line: dict = defaultdict(list)
    for r in raw:
        by_line[str(r["crm_line_item_id"])].append(r)
    first_snapshot = min(_d(r["meeting_dt"]) for r in raw) if raw else ""
    print(f"first review snapshot in the pull: {first_snapshot}")
    out, dropped = [], defaultdict(int)
    for lid, snaps in by_line.items():
        snaps = [r for r in snaps if _d(r["meeting_dt"]) <= FREEZE]     # frozen copies after the cutover add nothing
        snaps.sort(key=lambda r: _d(r["meeting_dt"]))
        if not snaps:
            dropped["only post-freeze snapshots"] += 1; continue
        if any("token factory" in (r.get("deal_stage_name") or "").lower() for r in snaps):
            dropped["token factory line"] += 1; continue
        priced = []
        for r in snaps:
            unit = (r.get("unit") or "gpu").lower().rstrip("s") or "gpu"
            gpu = (r.get("gpu") or "").upper(); gpr = _f(r.get("gpus_per_rack"))
            raw_p = _f(r.get("unit_price_raw"))
            p = per_gpu(round(raw_p + 1e-9, 2) if raw_p is not None else None, unit, gpu, gpr)   # +1e-9: half-cent floats round the same everywhere
            if p is None or p <= 0:
                continue
            if not (PRICE_LO <= p <= PRICE_HI):
                dropped["snapshot price outside band"] += 1; continue
            calc = _f(r.get("unit_price_calculated"))
            eff = per_gpu(round(calc + 1e-9, 2), unit, gpu, gpr) if calc and calc > 0 else None
            if eff is None or not (PRICE_LO <= eff <= PRICE_HI):
                eff = p
            priced.append((_d(r["meeting_dt"]), round(p, 4), round(eff, 4), r, unit))
        if not priced:
            dropped["line never priced in band"] += 1; continue
        last = priced[-1][3]
        gpu = (last.get("gpu") or "").upper()
        if gpu not in TRACKED:
            dropped["gpu not tracked"] += 1; continue
        path, prev = [], None
        for dt, p, _, _, _ in priced:
            if prev is None or abs(p - prev) >= 0.005:
                path.append(f"{dt}:{p:g}"); prev = p
        first_dt, first_p = priced[0][0], priced[0][1]
        last_dt, last_p, last_eff = priced[-1][0], priced[-1][1], priced[-1][2]
        won_dts = [_d(r["meeting_dt"]) for r in snaps if "won" in (r.get("deal_stage_name") or "").lower()]
        final_stage = (last.get("deal_stage_name") or "")
        final_dt = last_dt
        for r in snaps:                                   # first snapshot already in the final stage (won / lost / disqualified)
            if (r.get("deal_stage_name") or "") == final_stage and _d(r["meeting_dt"]) >= first_dt:
                final_dt = _d(r["meeting_dt"]); break
        d0 = date.fromisoformat(first_dt); d1 = date.fromisoformat(max(final_dt, first_dt))
        term = _f(last.get("term_m"))
        qty = _f(last.get("gpu_qty")) or 0
        if qty > 50000:
            dropped["gpu_qty > 50,000 -> 0"] += 1; qty = 0        # 4,032 'racks' x 72 and similar entry errors
        out.append({"generated_date": today, "crm_line_item_id": lid, "crm_deal_id": str(last.get("crm_deal_id") or ""), "gpu": gpu,
                    "unit": priced[-1][4], "gpus_per_rack": int(_f(last.get("gpus_per_rack")) or 0) or "",
                    "gpu_qty": round(qty), "term_m": round(term, 2) if term else "",
                    "consumption_type": last.get("consumption_type_slug") or "", "payment_type": last.get("payment_type_slug") or "",
                    "left_censored": int(first_dt == first_snapshot), "first_dt": first_dt, "first_price": first_p, "final_dt": final_dt, "last_dt": last_dt, "last_price": last_p, "last_price_effective": last_eff,
                    "n_snapshots": len(priced), "n_revisions": len(path) - 1, "path": ">".join(path),
                    "final_stage": last.get("deal_stage_name") or "", "outcome": outcome_of(last.get("deal_stage_name"), last.get("lost_category")),
                    "first_won_dt": min(won_dts) if won_dts else "", "lost_category": last.get("lost_category") or "",
                    "capacity_status": last.get("capacity_approval_status") or "", "data_center": last.get("data_center_location_slug") or "",
                    "days_first_to_final": (d1 - d0).days, "change_pct": round(100.0 * (last_eff - first_p) / first_p, 1)})
    if dropped:
        print("dropped:", dict(dropped))
    return out


def aggregate(paths: list[dict], today: str) -> list[dict]:
    grp: dict = defaultdict(list)
    for p in paths:
        if p["left_censored"]:
            continue                                   # first ask unknown: no path to measure
        if not p["term_m"] or float(p["term_m"]) <= 0 or (p["consumption_type"] or "").upper() not in ("RESERVE", ""):
            continue
        grp[(p["gpu"], bucket_months(float(p["term_m"])), p["outcome"])].append(p)
    w_from = min(p["first_dt"] for p in paths) if paths else ""
    w_to = max(p["final_dt"] for p in paths) if paths else ""
    out = []
    for key in sorted(grp):
        ps = grp[key]
        deals = len({p["crm_deal_id"] for p in ps})
        if deals < 2:
            continue
        revised = [p for p in ps if p["n_revisions"] > 0]
        out.append({"generated_date": today, "window_from": w_from, "window_to": w_to, "gpu": key[0], "tenor_months": key[1], "outcome": key[2],
                    "line_items": len(ps), "deals": deals, "gpus": round(sum(p["gpu_qty"] for p in ps)),
                    "first_ask_med": round(statistics.median(p["first_price"] for p in ps), 2),
                    "final_med": round(statistics.median(p["last_price_effective"] for p in ps), 2),
                    "ratio_med": round(statistics.median(p["last_price_effective"] / p["first_price"] for p in ps), 3),
                    "share_revised": round(len(revised) / len(ps), 2),
                    "revision_med_pct": round(statistics.median(p["change_pct"] for p in revised), 1) if revised else "",
                    "days_med": round(statistics.median(p["days_first_to_final"] for p in ps))})
    return out


def report(paths: list[dict], agg: list[dict]) -> None:
    print(f"{len(paths)} priced GPU line items; {sum(1 for p in paths if p['n_revisions'])} revised at least once; "
          f"{sum(1 for p in paths if p['left_censored'])} left-censored (already priced at the first review in the pull; excluded from aggregates)")
    by = defaultdict(int)
    for p in paths:
        by[p["outcome"]] += 1
    print("outcomes:", dict(by))
    for a in agg:
        if a["tenor_months"] in (12, 24, 36) and a["outcome"] in ("won", "lost_price_or_competitor", "lost_capacity"):
            print(f"  {a['gpu']:5} {a['tenor_months']:>2}m {a['outcome']:5} lines={a['line_items']:>3} deals={a['deals']:>3} gpus={a['gpus']:>7,} "
                  f"first=${a['first_ask_med']} final=${a['final_med']} ratio={a['ratio_med']} revised={a['share_revised']} "
                  f"rev_med={a['revision_med_pct']}% days={a['days_med']}")


def main(argv) -> int:
    dry = "--dry-run" in argv
    if "__SQL_PLACEHOLDER__" in SQL:
        print("SQL not set"); return 2
    today = date.today().isoformat()
    since = argv[argv.index("--since") + 1] if "--since" in argv else "2024-10-01"
    cache = Path(argv[argv.index("--raw-cache") + 1]) if "--raw-cache" in argv else None
    if cache and cache.exists():
        import pandas as pd
        raw = pd.read_csv(cache, dtype={"crm_line_item_id": str, "crm_deal_id": str}, keep_default_na=False, na_values=[""]).to_dict("records")
        print(f"raw rows from cache {cache}: {len(raw)}")
    else:
        from concurrent.futures import ThreadPoolExecutor
        def pull(k):
            client = _client()                       # one client per worker
            res = client.run_query_to_df(SQL.format(since=since, shards=SHARDS, shard=k), engine="chyt", return_query_link=True)
            return k, res["df"], res["query_link"]
        frames = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for k, df, link in ex.map(pull, range(SHARDS)):
                n = 0 if df is None else len(df)
                if n >= ROW_CAP:
                    print(f"shard {k}: {n} rows hits the {ROW_CAP}-row client cap; raise SHARDS. NOT writing."); return 1
                print(f"shard {k:>2}/{SHARDS}: {n:>5} rows  ({link})", flush=True)
                if n:
                    frames.append(df)
        if not frames:
            print("query returned no rows — NOT writing"); return 1
        import pandas as pd
        full = pd.concat(frames, ignore_index=True)
        if cache:
            full.to_csv(cache, index=False); print(f"raw cached -> {cache}")
        raw = full.to_dict("records")
    print(f"{len(raw)} line-item x snapshot rows, {len({str(r['crm_line_item_id']) for r in raw})} line items")
    paths = build_paths(raw, today)
    agg = aggregate(paths, today)
    report(paths, agg)
    bad = [a for a in agg if not (0.5 <= a["final_med"] <= 15)]
    if bad:
        print(f"{len(bad)} cells with final median outside $0.5-15:"); [print("   ", b) for b in bad]
    if dry:
        return 0
    with open(PATHS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PATH_COLUMNS); w.writeheader(); w.writerows(paths)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=AGG_COLUMNS); w.writeheader(); w.writerows(agg)
    print(f"wrote {PATHS} ({len(paths)} rows) and {OUT} ({len(agg)} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
