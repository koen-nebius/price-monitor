#!/usr/bin/env python3
"""
Weekly refresh of store/payg_realised.csv — what Nebius actually realised per PAYG GPU-hour
in the last 30 days, per GPU type (external tenants, non-preemptible, unsuspended). The
achieved on-demand anchor ("0m") of the committed-price benchmarks; the list price is the
ask, this is what was collected.

Source: //home/dwh/sandbox/analytics/data/consumption/paid_cons_aggregated_to_gpu_level
(Analytics IR dataset behind the "GPU Prices & Discounts" page 1604682195):
contract price = paid PAYG consumption / paid PAYG GPU-hours (hours with non-zero paid
consumption only), i.e. the page's "Contract Price" definition. Aggregates only.

Runs LOCALLY (YT not reachable from GitHub Actions) with the yt data-client venv:
    ~/nebo/analytics/libs/data_clients/yt_data_client/yt_mcp/.venv/bin/python scripts/refresh_payg_realised.py
"""
import csv
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "store" / "payg_realised.csv"
TABLE = "//home/dwh/sandbox/analytics/data/consumption/paid_cons_aggregated_to_gpu_level"
WINDOW_DAYS = 30
TIER_CASE = ("multiIf(sku_gpu_type ILIKE '%GB300%', 'GB300', sku_gpu_type ILIKE '%GB200%', 'GB200', "
             "sku_gpu_type ILIKE '%B300%', 'B300', sku_gpu_type ILIKE '%B200%', 'B200', "
             "sku_gpu_type ILIKE '%H200%', 'H200', sku_gpu_type ILIKE '%H100%', 'H100', "
             "sku_gpu_type ILIKE '%L40S%', 'L40S', sku_gpu_type ILIKE '%RTX%', 'RTX6000', sku_gpu_type)")


def query(since: str) -> str:
    return f"""
SELECT {TIER_CASE} AS tier, sku_is_preemptible AS preemptible,
  uniqExact(tenant_id) AS tenants, round(sum(pricing_q_payg_gpu)) AS paid_gpu_hours,
  round(sum(paid_cons_payg) / sum(pricing_q_payg_gpu), 4) AS realised_usd_per_gpu_hour,
  round(quantile(0.5)(sku_price_actual), 4) AS median_list_price
FROM `{TABLE}`
WHERE utc_dt >= toDate('{since}') AND paid_cons_payg > 0 AND pricing_q_payg_gpu > 0
  AND coalesce(consumption_state_slug, '') NOT ILIKE '%suspend%'
  AND coalesce(tenant_segment, '') NOT ILIKE '%internal%'
GROUP BY tier, preemptible
HAVING paid_gpu_hours > 100
ORDER BY tier, preemptible
"""


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
    from yt_data_client import YtDataClient  # noqa: E402
    return YtDataClient()


def main(argv) -> int:
    dry = "--dry-run" in argv
    since = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    res = _client().run_query_to_df(query(since), engine="chyt", return_query_link=True)
    df, link = res["df"], res["query_link"]
    if df is None or len(df) == 0:
        print("query returned no rows — NOT writing the CSV")
        return 1
    rows = [{"generated_date": date.today().isoformat(), "window_days": WINDOW_DAYS, **r} for r in df.to_dict("records")]
    print(f"{len(rows)} rows ({link})")
    for r in rows:
        print(f"  {r['tier']:8} {'preemptible' if r['preemptible'] else 'on-demand  '} tenants={r['tenants']:>4} hours={int(r['paid_gpu_hours']):>9,} realised=${r['realised_usd_per_gpu_hour']:.2f} list~${(r['median_list_price'] or 0):.2f}")
    if dry:
        return 0
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
