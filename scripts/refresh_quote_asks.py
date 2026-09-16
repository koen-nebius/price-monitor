#!/usr/bin/env python3
"""
Daily refresh of the Salesforce quote-ask ledger (2026-09-16, Koen: "build 1 and 2").
Nebius' own asked prices BEFORE close, from Salesforce CPQ quotes (live since 2026-08-06):
the live successor of the HubSpot 'proposal' class in store/crm_asks.csv, whose source table
froze at the 2026-08-10 CRM cutover.

Writes
  store/quote_asks_history.csv  change log of every live GPU quote line: one row per line per
                                observed state, a new row only when price, quantity, dates, tier,
                                status or approval changed (and one 'gone' row when a line
                                disappears). Opaque Salesforce ids only, no names. Salesforce keeps
                                quotes as current state only, so this file IS the ask-path history
                                and the base for a requested-vs-approved spread as quotes move
                                through approval. Daily cadence matters: a re-priced line
                                overwrites the earlier ask in the source.
  store/quote_asks.csv          aggregates per GPU x tenor bucket x class x month x prepay bucket
                                in the crm_asks.csv schema plus prepay_pct, list_ref_med,
                                share_at_list, disc_med_pct, source. forward_curve.load_crm_asks
                                reads it next to the HubSpot file (never pooled into a mark):
    proposal  quotes In Review / Approved / Ops Review on open opportunities: what we ask now
    lost      quote lines on opportunities closed lost after the cutover, or Rejected quotes
    signed    'Signed by Customer' quotes or closed-won opportunities (context only; the signed
              price also sits in deal_cohorts and reserve_tenor)
    Draft quotes are a rep's worksheet, not an ask: kept in the history, out of the aggregates
    unless --include-draft.

Reserve, AI Cloud lines only (Token Factory dedicated endpoints are per-GPU-hr managed inference
and are reported separately on stdout, never aggregated with raw GPU rentals; PAYG / Testing /
PROMO lines likewise). Plausibility band 0.3-50 $/GPU-hr; GB200/GB300 lines >= $40 are per rack
and divided by 72 (none seen so far).

Runs LOCALLY only (YT is not reachable from GitHub Actions):
    ~/nebo/analytics/libs/data_clients/yt_data_client/yt_mcp/.venv/bin/python \
        scripts/refresh_quote_asks.py [--dry-run] [--include-draft]

Confidentiality: the history file carries quote-level prices keyed by opaque Salesforce ids and
is INTERNAL (same standing as crm_intel_candidates.csv). The aggregates file is what renders.
"""
import csv
import os
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "store" / "quote_asks_history.csv"
OUT = ROOT / "store" / "quote_asks.csv"
CUTOVER = "2026-08-10"
TRACKED = ("H100", "H200", "B200", "B300", "GB200", "GB300", "VR")
PRICE_LO, PRICE_HI = 0.3, 50.0


def bucket_months(m: float) -> int:
    """Same buckets as scripts/refresh_deal_cohorts.py TENOR_CASE."""
    return 3 if m <= 4 else 6 if m <= 8 else 12 if m <= 14 else 18 if m <= 20 else 24 if m <= 27 else 36 if m <= 40 else 48 if m <= 50 else 60


# Row-level current state of every live GPU quote line, scripts/sql/quote_lines.sql: authored and
# tied out 2026-09-16 against three confirmed lines, re-run by an independent check (898 rows, one per
# line, no join fan-out, no name column). Kept in a .sql file so the regex backslashes reach CHYT intact.
SQL_PATH = ROOT / "scripts" / "sql" / "quote_lines.sql"
SQL = SQL_PATH.read_text() if SQL_PATH.exists() else "__SQL_PLACEHOLDER__"

STATE_KEYS = ["gpu", "product_family", "qli_type", "quantity", "price_gpu_hr", "custom_list_price", "discount_pct", "tier",
              "quote_status", "price_approval_status", "capacity_approval_status", "line_capacity_status", "cr_status",
              "start_dt", "end_dt", "payment_type", "prepay_pct", "data_center", "opp_stage", "cls"]
HIST_COLUMNS = ["observed", "quote_line_id", "quote_id", "opp_id", "oli_id"] + STATE_KEYS + ["term_m", "line_created", "line_updated"]
AGG_COLUMNS = ["generated_date", "gpu", "tenor_months", "stage_class", "close_month", "prepay_bucket",
               "deals", "lines", "gpus", "price_lo", "price_med", "price_hi",
               "prepay_pct", "list_ref_med", "share_at_list", "disc_med_pct", "source"]


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
    return x if x == x else None   # NaN -> None


def _s(v) -> str:
    """Dates and datetimes -> 'YYYY-MM-DD'; other values -> str; NaN/None -> ''."""
    if v is None:
        return ""
    s = str(v)
    if s in ("nan", "NaT", "None"):
        return ""
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else s


def classify(r: dict) -> str:
    st = (r.get("quote_status") or "")
    if r.get("opp_is_closed") in (True, 1, "1", "true", "True"):
        return "signed" if r.get("opp_is_won") in (True, 1, "1", "true", "True") else "lost"
    if st == "Rejected":
        return "lost"
    if st == "Signed by Customer":
        return "signed"
    if st == "Draft":
        return "draft"
    return "proposal"


def normalise(raw: list[dict]) -> list[dict]:
    """Query rows -> uniform typed dicts; derives price band, term, prepay percentage, class."""
    out, dropped = [], defaultdict(int)
    for r in raw:
        price = _f(r.get("price_gpu_hr"))
        gpu = (r.get("gpu") or "").upper()
        if price is None or price <= 0:
            dropped["no price"] += 1; continue
        if gpu in ("GB200", "GB300") and price >= 40:
            price = price / 72.0; dropped["rack->gpu /72"] += 1
        if not (PRICE_LO <= price <= PRICE_HI):
            dropped["price outside 0.3-50"] += 1; continue
        qty = _f(r.get("quantity")) or 0
        if r.get("rack_priced") in (True, 1, "1", "true", "True") and 0 < qty <= 16:
            qty *= 72                         # rack-priced line quoted in racks (2, 14, 15 seen); >16 is read as GPUs
        if qty <= 0 or qty > 50000:
            dropped["quantity implausible -> 0"] += 1; qty = 0   # GPU-hours typed into quantity (-256 x 8760) or 290,304
        term = _f(r.get("term_m"))
        if (term is None or term <= 0) and _f(r.get("number_of_hours")):
            term = _f(r.get("number_of_hours")) / 730.0
        pct = _f(r.get("cr_prepayment_pct"))
        if pct is None:
            amt, hours = _f(r.get("prepayment_amount")), _f(r.get("number_of_hours"))
            if amt and amt > 0 and hours and qty:
                total = price * qty * hours
                pct = round(100.0 * amt / total, 1) if total > 0 else None
                if pct is not None and pct > 100:
                    pct = None
        tier = (r.get("line_tier") or r.get("quote_tier") or "")
        row = {"quote_line_id": r.get("salesforce_quote_line_item_id"), "quote_id": r.get("salesforce_quote_id"),
               "opp_id": r.get("salesforce_opportunity_id") or "", "oli_id": r.get("salesforce_opportunity_line_item_id") or "",
               "gpu": gpu, "product_family": r.get("product_family") or "", "qli_type": r.get("qli_type") or "",
               "quantity": int(qty) if qty else 0, "price_gpu_hr": round(price, 4),
               "custom_list_price": _f(r.get("custom_list_price")), "discount_pct": _f(r.get("discount_pct")), "tier": tier,
               "quote_status": r.get("quote_status") or "", "price_approval_status": r.get("price_approval_status") or "",
               "capacity_approval_status": r.get("capacity_approval_status") or "", "line_capacity_status": r.get("line_capacity_status") or "",
               "cr_status": r.get("cr_status") or "", "start_dt": _s(r.get("start_dt")), "end_dt": _s(r.get("end_dt")),
               "payment_type": r.get("payment_type") or "", "prepay_pct": pct, "data_center": r.get("data_center") or r.get("region") or "",
               "opp_stage": r.get("opp_stage") or "", "term_m": round(term, 2) if term else None,
               "line_created": _s(r.get("line_created")), "line_updated": _s(r.get("line_updated")),
               "quote_created": _s(r.get("quote_created")), "opp_close_dt": _s(r.get("opp_close_dt")),
               "opp_is_closed": r.get("opp_is_closed"), "opp_is_won": r.get("opp_is_won")}
        row["cls"] = classify(row | {"opp_is_closed": r.get("opp_is_closed"), "opp_is_won": r.get("opp_is_won")})
        out.append(row)
    if dropped:
        print("dropped:", dict(dropped))
    return out


def _state(row: dict) -> tuple:
    return tuple("" if row.get(k) is None else str(row.get(k)) for k in STATE_KEYS)


def update_history(rows: list[dict], today: str, path: Path = HIST) -> tuple[int, int]:
    """Append changed / new / gone states. Returns (appended, gone)."""
    last: dict = {}
    existing: list[dict] = []
    if path.exists():
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                existing.append(r)
                last[r["quote_line_id"]] = r
    appended, gone = [], 0
    seen = set()
    for row in rows:
        lid = row["quote_line_id"]; seen.add(lid)
        prev = last.get(lid)
        if prev is None or _state(prev) != _state(row) or prev.get("quote_status") == "gone":
            appended.append({"observed": today, **{k: ("" if row.get(k) is None else row.get(k)) for k in HIST_COLUMNS if k != "observed"}})
    for lid, prev in last.items():
        if lid not in seen and prev.get("quote_status") != "gone":
            g = dict(prev); g["observed"] = today; g["quote_status"] = "gone"; g["cls"] = "gone"
            appended.append({k: g.get(k, "") for k in HIST_COLUMNS}); gone += 1
    if appended:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HIST_COLUMNS)
            if new:
                w.writeheader()
            w.writerows(appended)
    return len(appended), gone


def prepay_bucket(row: dict) -> str:
    pct = row.get("prepay_pct")
    if pct is not None:
        return "upfront" if pct >= 90 else "partial_prepay" if pct > 0 else "postpaid"
    return "prepaid_monthly" if row.get("payment_type") == "Prepaid" else "postpaid"


def aggregate(rows: list[dict], today: str, include_draft: bool = False) -> list[dict]:
    grp: dict = defaultdict(list)
    for r in rows:
        if r["cls"] == "draft" and not include_draft:
            continue
        if r["cls"] not in ("proposal", "lost", "signed", "draft"):
            continue
        if r["qli_type"] != "Reserve" or r["product_family"] != "AI Cloud" or r["gpu"] not in TRACKED:
            continue
        if not r["term_m"] or r["term_m"] <= 0:
            continue
        month_src = r["opp_close_dt"] if r["cls"] in ("lost", "signed") and r["opp_close_dt"] else (r["quote_created"] or r["line_created"])
        if not month_src:
            continue
        month = min(month_src[:10], today)[:7] + "-01"     # lost opps can carry a planned close date in the future
        cls = "proposal" if r["cls"] == "draft" else r["cls"]
        grp[(r["gpu"], bucket_months(r["term_m"]), cls, month, prepay_bucket(r))].append(r)
    out = []
    for key in sorted(grp):
        rs = grp[key]
        prices = [x["price_gpu_hr"] for x in rs]
        deals = len({x["opp_id"] or x["quote_id"] for x in rs})
        pcts = [x["prepay_pct"] for x in rs if x["prepay_pct"] is not None]
        lists = [x["custom_list_price"] for x in rs if x["custom_list_price"]]
        at_list = [x for x in rs if x["custom_list_price"] and abs(x["price_gpu_hr"] - x["custom_list_price"]) < 0.005]
        discs = [x["discount_pct"] for x in rs if x["discount_pct"] is not None]
        out.append({"generated_date": today, "gpu": key[0], "tenor_months": key[1], "stage_class": key[2], "close_month": key[3],
                    "prepay_bucket": key[4], "deals": deals, "lines": len(rs), "gpus": round(sum(x["quantity"] for x in rs)),
                    "price_lo": round(min(prices), 2), "price_med": round(statistics.median(prices), 2), "price_hi": round(max(prices), 2),
                    "prepay_pct": round(statistics.median(pcts), 1) if pcts else "",
                    "list_ref_med": round(statistics.median(lists), 2) if lists else "",
                    "share_at_list": round(len(at_list) / len(rs), 2) if lists else "",
                    "disc_med_pct": round(statistics.median(discs), 1) if discs else "", "source": "salesforce_quotes"})
    return out


def report(rows: list[dict], agg: list[dict]) -> None:
    by = defaultdict(lambda: defaultdict(int))
    for r in rows:
        by[(r["product_family"], r["qli_type"])][r["cls"]] += 1
    print("lines by family x type x class:")
    for k in sorted(by):
        print(f"  {k[0]:12} {k[1]:8} " + " ".join(f"{c}={n}" for c, n in sorted(by[k].items())))
    res = [r for r in rows if r["qli_type"] == "Reserve" and r["product_family"] == "AI Cloud" and r["gpu"] in TRACKED and r["cls"] in ("proposal", "lost", "signed")]
    print(f"reserve AI Cloud tracked lines in aggregates: {len(res)}; cells: {len(agg)}")
    for gpu in TRACKED:
        for cls in ("proposal", "lost", "signed"):
            xs = [r for r in res if r["gpu"] == gpu and r["cls"] == cls]
            if len(xs) < 2:
                continue
            p = [x["price_gpu_hr"] for x in xs]; lst = [x for x in xs if x["custom_list_price"] and abs(x["price_gpu_hr"] - x["custom_list_price"]) < 0.005]
            print(f"  {gpu:5} {cls:9} lines={len(xs):>3} deals={len({x['opp_id'] or x['quote_id'] for x in xs}):>3} gpus={sum(x['quantity'] for x in xs):>7,} "
                  f"med=${statistics.median(p):.2f} lo=${min(p):.2f} hi=${max(p):.2f} at_list={len(lst)}/{len(xs)}")
    tf = [r for r in rows if r["product_family"] != "AI Cloud"]
    payg = [r for r in rows if r["qli_type"] in ("PAYG", "Testing", "PROMO") and r["product_family"] == "AI Cloud"]
    print(f"not aggregated: {len(tf)} Token Factory / other-family lines, {len(payg)} PAYG/Testing/PROMO AI Cloud lines")
    for r in sorted(payg, key=lambda x: -x["quantity"])[:8]:
        print(f"    {r['qli_type']:8} {r['gpu']:5} q={r['quantity']:>4} ${r['price_gpu_hr']:.2f} list={r['custom_list_price']} {r['quote_status']} term={r['term_m']}")


def main(argv) -> int:
    dry = "--dry-run" in argv
    include_draft = "--include-draft" in argv
    if "__SQL_PLACEHOLDER__" in SQL:
        print("SQL not set"); return 2
    today = date.today().isoformat()
    res = _client().run_query_to_df(SQL, engine="chyt", return_query_link=True)
    df, link = res["df"], res["query_link"]
    if df is None or len(df) == 0:
        print("query returned no rows — NOT writing"); return 1
    print(f"{len(df)} live GPU quote lines ({link})")
    rows = normalise(df.to_dict("records"))
    agg = aggregate(rows, today, include_draft)
    report(rows, agg)
    bad = [a for a in agg if not (0.5 <= float(a["price_med"]) <= 15)]
    if bad:
        print(f"{len(bad)} cell medians outside $0.5-15:"); [print("   ", b) for b in bad]
    if dry:
        return 0
    n, gone = update_history(rows, today)
    print(f"history: {n} rows appended ({gone} gone) -> {HIST}")
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=AGG_COLUMNS); w.writeheader(); w.writerows(agg)
    print(f"wrote {OUT} ({len(agg)} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
