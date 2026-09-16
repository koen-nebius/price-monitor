#!/usr/bin/env python3
"""
Weekly refresh of store/crm_asks.csv — Nebius' OWN asked prices from the CRM deal-review
table, aggregated by GPU x tenor bucket x stage class x close month:
  lost      deals in stage 'Closed lost'      -> prices that did not win (an upper bound on what
                                                 that customer would pay; loss reasons are not
                                                 recorded in a usable way, so no competitor price)
  proposal  'Commercial Proposal' and 'Agreement signing' -> what we are asking today, in flight
Companion to scripts/refresh_reserve_tenor.py (signed deals); same inclusion rules, same
rack -> GPU conversion, same sanity band, same venv:

    ~/nebo/analytics/libs/data_clients/yt_data_client/yt_mcp/.venv/bin/python scripts/refresh_crm_asks.py [--dry-run]

Confidentiality: aggregates only (deal counts, GPU sums, lo/median/hi). forward_curve.py pools
nothing from this file into a mark; it shows the aggregates on the Market position graph as
two labelled Nebius reference markers, and only when an aggregate holds >= 3 deals.
"""
import csv
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "store" / "crm_asks.csv"
TABLE = "//home/dwh/nemax-prod/data/cdm/crm/deal_review_line_items_enriched"
SINCE = "2025-09-01"
TRACKED = ("H100", "H200", "B200", "B300", "GB200", "GB300", "VR")
TENOR_CASE = ("multiIf(term_m <= 4, 3, term_m <= 8, 6, term_m <= 14, 12, term_m <= 20, 18, "
              "term_m <= 27, 24, term_m <= 40, 36, term_m <= 50, 48, 60)")

QUERY = f"""
SELECT gpu, {TENOR_CASE} AS tenor_months, stage_class,
  toString(toStartOfMonth(close_utc_dttm)) AS close_month, prepay_bucket,
  uniqExact(crm_deal_id) AS deals, count() AS lines, round(sum(gpu_qty)) AS gpus,
  round(min(price_gpu_hr),2) AS price_lo, round(quantile(0.5)(price_gpu_hr),2) AS price_med,
  round(max(price_gpu_hr),2) AS price_hi
FROM (
  SELECT crm_deal_id, close_utc_dttm, gpu, stage_class,
    multiIf(payment_type_slug = 'PREPAID' AND billing_frequency_slug = 'ONE_TIME', 'upfront',
            payment_type_slug = 'PREPAID', 'prepaid_monthly', 'postpaid') AS prepay_bucket,
    dateDiff('day', consumption_start_utc_dttm, consumption_end_utc_dttm)/30.4 AS term_m,
    multiIf(lower(coalesce(unit,'')) IN ('gpu','gpus'), unit_price_calculated,
            lower(coalesce(unit,'')) IN ('rack','racks'), unit_price_calculated / gpus_per_rack, NULL) AS price_gpu_hr,
    multiIf(lower(coalesce(unit,'')) IN ('gpu','gpus'), resource_quantity,
            lower(coalesce(unit,'')) IN ('rack','racks'), resource_quantity * gpus_per_rack, NULL) AS gpu_qty
  FROM (
    SELECT crm_deal_id, unit, unit_price_calculated, resource_quantity, close_utc_dttm,
      consumption_start_utc_dttm, consumption_end_utc_dttm, payment_type_slug, billing_frequency_slug,
      multiIf(lower(deal_stage_name) LIKE '%lost%', 'lost',
              lower(deal_stage_name) LIKE '%commercial proposal%' OR lower(deal_stage_name) LIKE '%agreement signing%', 'proposal', NULL) AS stage_class,
      multiIf(coalesce(gpu_model_canonical,'') = 'H100 SXM', 'H100',
              gpu_model_canonical IS NOT NULL, gpu_model_canonical,
              match(coalesce(product_name,''), '(?i)vera rubin'), 'VR',
              match(coalesce(product_name,''), '(?i)\\bGB300\\b'), 'GB300',
              match(coalesce(product_name,''), '(?i)\\bGB200\\b'), 'GB200',
              match(coalesce(product_name,''), '(?i)\\bB300\\b'), 'B300',
              match(coalesce(product_name,''), '(?i)\\bB200\\b'), 'B200',
              match(coalesce(product_name,''), '(?i)\\bH200\\b'), 'H200',
              match(coalesce(product_name,''), '(?i)\\bH100\\b'), 'H100', NULL) AS gpu,
      coalesce(toFloat64OrNull(extract(coalesce(product_name,''), 'GPU-(\\d+)')),
               if(coalesce(product_name,'') ILIKE '%NVL72%'
                  OR coalesce(gpu_model_canonical,'') IN ('GB200','GB300')
                  OR match(coalesce(product_name,''), '(?i)\\bGB[23]00\\b'), 72., NULL)) AS gpus_per_rack
    FROM `{TABLE}`
    WHERE overview_status = 'Actual overview'
      AND close_utc_dttm >= toDateTime('{SINCE}')
      AND lower(coalesce(consumption_type_slug,'')) LIKE '%reserve%'
      AND coalesce(autorenewal,'No') != 'Yes'
      AND consumption_start_utc_dttm IS NOT NULL AND consumption_end_utc_dttm IS NOT NULL
  )
  WHERE stage_class IS NOT NULL
)
WHERE gpu IN {TRACKED} AND price_gpu_hr > 0.2 AND price_gpu_hr < 20
GROUP BY gpu, tenor_months, stage_class, close_month, prepay_bucket
ORDER BY gpu, tenor_months, stage_class, close_month, prepay_bucket
"""

COLUMNS = ["generated_date", "gpu", "tenor_months", "stage_class", "close_month", "prepay_bucket",
           "deals", "lines", "gpus", "price_lo", "price_med", "price_hi"]


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


def main(argv) -> int:
    dry = "--dry-run" in argv
    res = _client().run_query_to_df(QUERY, engine="chyt", return_query_link=True)
    df, link = res["df"], res["query_link"]
    if df is None or len(df) == 0:
        print("query returned no rows — NOT writing the CSV")
        return 1
    today = date.today().isoformat()
    rows = [{"generated_date": today, **{k: r[k] for k in COLUMNS[1:]}} for r in df.to_dict("records")]
    by = {}
    for r in rows:
        k = (r["gpu"], r["stage_class"])
        by[k] = by.get(k, 0) + int(r["deals"])
    print(f"{len(rows)} cells ({link})")
    for k in sorted(by):
        print(f"  {k[0]:6} {k[1]:9} {by[k]:>4} deal-cells")
    if dry:
        for r in rows[:12]:
            print(r)
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
