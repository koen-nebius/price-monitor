#!/usr/bin/env python3
"""
Weekly refresh of store/deal_cohorts.csv: Nebius CLOSED deals as three recorded-outcome
reference classes per GPU x tenor bucket, aggregates only (2026-09-16, Koen: "deduce PAYG and
Reserve pricing from what we already have").

  won                       achieved prices (signed)
  lost_capacity             recorded price on a deal labelled lost for capacity;
                            customer acceptance is not established by the category
  lost_price_or_competitor  recorded price on a deal labelled lost for price or competitor;
                            neither a competitor price nor a willingness-to-pay ceiling
  lost_other                disengagement, PoC, compliance, no decision (context only)
  (duplicates, test deals, qualification oversights, contract restructuring excluded)

Sources: Salesforce ODS (Fivetran, fresh to the hour) for closes on/after the
2026-08-10 CRM cutover; the frozen HubSpot deal-review mirror for closes before it.
Line items are the unit: per-GPU $/hr from unit_price (Salesforce racks quoted per
rack-hour, i.e. >= $40 on a GB200/GB300 SKU, are divided by 72), term from the line's
start/end dates, GPUs from oli_total_gpu_count (HubSpot: resource_quantity x rack size).
Token Factory endpoints, storage lines and build-time sanity opportunities are excluded.

Runs LOCALLY only (YT is not reachable from GitHub Actions):
    ~/nebo/analytics/libs/data_clients/yt_data_client/yt_mcp/.venv/bin/python \
        scripts/refresh_deal_cohorts.py [--dry-run] [--since 2026-01-01]

Confidentiality: aggregates only (opportunity counts, GPU sums, p25/median/p75 per cell,
cells with < 2 opportunities dropped). This reference threshold is distinct from
the 3-deal achieved-price mark threshold. Source line-item medians remain separate
in both published views; they are not combined into a purported pooled median.
Never customer names or deal-level rows.
"""
import csv
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "store" / "deal_cohorts.csv"
CUTOVER = "2026-08-10"
HS_TABLE = "//home/dwh/nemax-prod/data/cdm/crm/deal_review_line_items_enriched"
SF_OPP = "//home/dwh/nemax-prod/data/ods/salesforce/opportunity"
SF_OLI = "//home/dwh/nemax-prod/data/ods/salesforce/opportunity_line_item"
TRACKED = ("H100", "H200", "B200", "B300", "GB200", "GB300", "VR")
TENOR_CASE = ("multiIf(term_m <= 4, 3, term_m <= 8, 6, term_m <= 14, 12, term_m <= 20, 18, "
              "term_m <= 27, 24, term_m <= 40, 36, term_m <= 50, 48, 60)")
OUTCOME_CASE = """multiIf(won, 'won',
    positionCaseInsensitive(reason, 'capacity') > 0, 'lost_capacity',
    positionCaseInsensitive(reason, 'pric') > 0 OR positionCaseInsensitive(reason, 'competitor') > 0, 'lost_price_or_competitor',
    positionCaseInsensitive(reason, 'duplicate') > 0 OR positionCaseInsensitive(reason, 'test') > 0
      OR positionCaseInsensitive(reason, 'qualification') > 0 OR positionCaseInsensitive(reason, 'restructur') > 0
      OR positionCaseInsensitive(reason, 'ops out') > 0, 'excluded',
    'lost_other')"""
AGG = f"""
SELECT gpu, {TENOR_CASE} AS tenor_months, {OUTCOME_CASE} AS outcome,
  uniqExact(opp_id) AS opps, count() AS lines, round(sum(gpu_qty)) AS gpus,
  round(quantile(0.25)(price_gpu_hr), 2) AS price_p25, round(quantile(0.5)(price_gpu_hr), 2) AS price_med,
  round(quantile(0.75)(price_gpu_hr), 2) AS price_p75
FROM ({{inner}})
WHERE gpu IN {TRACKED} AND price_gpu_hr > 0.3 AND price_gpu_hr < 40 AND term_m > 0
GROUP BY gpu, tenor_months, outcome
HAVING opps >= 2 AND outcome != 'excluded'
ORDER BY gpu, tenor_months, outcome
"""

GPU_FROM_TEXT = """multiIf(match(t, '(?i)vera rubin|\\\\bVR\\\\b'), 'VR', match(t, '(?i)\\\\bGB300\\\\b'), 'GB300',
    match(t, '(?i)\\\\bGB200\\\\b'), 'GB200', match(t, '(?i)\\\\bB300\\\\b'), 'B300', match(t, '(?i)\\\\bB200\\\\b'), 'B200',
    match(t, '(?i)\\\\bH200\\\\b'), 'H200', match(t, '(?i)\\\\bH100\\\\b'), 'H100', NULL)"""


def hubspot_inner(since: str) -> str:
    gpu_expr = GPU_FROM_TEXT.replace("t,", "coalesce(product_name,''),")
    return f"""
  SELECT crm_deal_id AS opp_id, lower(deal_stage_name) LIKE '%won%' AS won,
    coalesce(closed_lost_reason_desc, '') AS reason,
    dateDiff('day', consumption_start_utc_dttm, consumption_end_utc_dttm) / 30.4 AS term_m,
    multiIf(coalesce(gpu_model_canonical,'') = 'H100 SXM', 'H100', gpu_model_canonical IS NOT NULL, gpu_model_canonical, {gpu_expr}) AS gpu,
    multiIf(lower(coalesce(unit,'')) IN ('rack','racks'), unit_price_calculated / 72, unit_price_calculated) AS price_gpu_hr,
    multiIf(lower(coalesce(unit,'')) IN ('rack','racks'), resource_quantity * 72, resource_quantity) AS gpu_qty
  FROM `{HS_TABLE}`
  WHERE overview_status = 'Actual overview'
    AND deal_stage_name IN ('Closed won', 'Closed lost', 'Closed Won', 'Closed Lost')
    AND close_utc_dttm >= toDateTime('{since} 00:00:00') AND close_utc_dttm < toDateTime('{CUTOVER} 00:00:00')
    AND consumption_start_utc_dttm IS NOT NULL AND consumption_end_utc_dttm IS NOT NULL
    AND coalesce(product_name, '') NOT ILIKE '%token factory%'"""


def salesforce_inner() -> str:
    gpu_expr = GPU_FROM_TEXT.replace("t,", "li.product_code,")
    return f"""
  SELECT o.salesforce_opportunity_id AS opp_id, o.is_won AS won, coalesce(o.loss_reason, '') AS reason,
    dateDiff('day', li.oli_start_dt, li.oli_end_dt) / 30.4 AS term_m,
    {gpu_expr} AS gpu,
    if((li.product_code LIKE '%GB200%' OR li.product_code LIKE '%GB300%') AND li.unit_price >= 40, li.unit_price / 72, li.unit_price) AS price_gpu_hr,
    if(li.oli_total_gpu_count >= 8, li.oli_total_gpu_count, if(li.quantity >= 8, li.quantity, li.oli_total_gpu_count)) AS gpu_qty
  FROM (SELECT salesforce_opportunity_id, is_won, loss_reason FROM `{SF_OPP}`
        WHERE is_closed AND NOT coalesce(is_deleted, false) AND close_dt >= toDate('{CUTOVER}')
          AND NOT opportunity_name LIKE 'JGSanity%') AS o
  JOIN (SELECT salesforce_opportunity_id, product_code, unit_price, quantity, oli_total_gpu_count, oli_start_dt, oli_end_dt
        FROM `{SF_OLI}`
        WHERE NOT coalesce(is_deleted, false) AND (product_code LIKE 'Nebius Platform:%' OR product_code LIKE 'Bare Metal:%')
          AND product_code NOT LIKE '%TF%' AND product_code NOT LIKE '%Token%'
          AND oli_start_dt IS NOT NULL AND oli_end_dt IS NOT NULL AND unit_price > 0) AS li
    ON o.salesforce_opportunity_id = li.salesforce_opportunity_id"""


COLUMNS = ["generated_date", "source", "window_from", "window_to", "gpu", "tenor_months", "outcome",
           "opps", "lines", "gpus", "price_p25", "price_med", "price_p75"]


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
    since = argv[argv.index("--since") + 1] if "--since" in argv else "2026-01-01"
    client = _client()
    today = date.today().isoformat()
    rows = []
    for source, inner, w_from, w_to in (("hubspot", hubspot_inner(since), since, CUTOVER),
                                        ("salesforce", salesforce_inner(), CUTOVER, today)):
        res = client.run_query_to_df(AGG.format(inner=inner), engine="chyt", return_query_link=True)
        df, link = res["df"], res["query_link"]
        n = 0 if df is None else len(df)
        print(f"{source}: {n} cells  ({link})")
        if n:
            for r in df.to_dict("records"):
                rows.append({"generated_date": today, "source": source, "window_from": w_from, "window_to": w_to,
                             **{k: r[k] for k in COLUMNS[4:]}})
    if not rows:
        print("no rows from either source — NOT writing"); return 1
    bad = [r for r in rows if not (0.5 <= float(r["price_med"]) <= 15)]
    print(f"{len(rows)} cells total, {len(bad)} medians outside $0.5-15")
    for r in bad:
        print("  outside band:", r)
    if dry:
        for r in rows:
            print(f"  {r['source']:10} {r['gpu']:5} {r['tenor_months']:>3}m {r['outcome']:24} opps={r['opps']:>3} gpus={int(r['gpus']):>6} "
                  f"p25={r['price_p25']} med={r['price_med']} p75={r['price_p75']}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS); w.writeheader(); w.writerows(rows)
    print(f"wrote {OUT} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
