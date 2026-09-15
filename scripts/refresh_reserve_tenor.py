#!/usr/bin/env python3
"""
Weekly refresh of store/reserve_tenor.csv — Nebius SIGNED reserve deals aggregated
by GPU x tenor bucket x close month. The "ask / transacted" leg of the internal
GPU forward curve (forward_curve.py). Companion to the rolling-30d
store/reserve_wins.csv (same inclusion rules, see analysis/reserve_wins_method.md).

Runs LOCALLY only (YT is not reachable from GitHub Actions). Invoke with the YT
data-client venv so yt.wrapper + pandas are available:

    ~/nebo/analytics/libs/data_clients/yt_data_client/yt_mcp/.venv/bin/python \
        scripts/refresh_reserve_tenor.py            # writes the CSV
    ... --dry-run                                    # prints, no write

Env (defaults match the yt MCP hook in ~/.claude/settings.json):
    YT_PROXY            https://planck.yt.nebius.yt
    YT_CONFIG_PROFILE   planck
    REQUESTS_CA_BUNDLE  ~/.yt/ca-certificates/planck
    YT_TOKEN            read from ~/.yt/token_planck when unset

Confidentiality: aggregates only (deal counts, GPU sums, lo/median/hi $/GPU-hr per
cell). Never customer names or deal-level rows — reserve contracts carry
price-confidentiality clauses and the page audience is wider than CRM permissions.

Inclusion rules mirror reserve_wins (2026-08-15 fixes): overview_status='Actual
overview' (de-dup of per-meeting snapshots), stage contains 'won', consumption
type contains 'reserve', autorenewal != 'Yes', rack-priced NVL72 lines converted
to $/GPU-hr BEFORE the 0.2-20 sanity bound, 'H100 SXM' -> H100, Vera Rubin from
product_name. Tenor = consumption window in months, bucketed to 3/6/12/18/24/36/
48/60 (see TENOR_CASE). History from 2025-01-01 so quote-date normalisation in
forward_curve.py has depth.
"""
import csv
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "store" / "reserve_tenor.csv"
TABLE = "//home/dwh/nemax-prod/data/cdm/crm/deal_review_line_items_enriched"
SINCE = "2025-01-01"
TRACKED = ("H100", "H200", "B200", "B300", "GB200", "GB300", "VR")
TENOR_CASE = ("multiIf(term_m <= 4, 3, term_m <= 8, 6, term_m <= 14, 12, term_m <= 20, 18, "
              "term_m <= 27, 24, term_m <= 40, 36, term_m <= 50, 48, 60)")

QUERY = f"""
SELECT gpu, {TENOR_CASE} AS tenor_months,
  toString(toStartOfMonth(close_utc_dttm)) AS close_month,
  uniqExact(crm_deal_id) AS deals, count() AS lines, round(sum(gpu_qty)) AS gpus,
  round(min(price_gpu_hr),2) AS price_lo, round(quantile(0.5)(price_gpu_hr),2) AS price_med,
  round(max(price_gpu_hr),2) AS price_hi
FROM (
  SELECT crm_deal_id, close_utc_dttm, gpu,
    dateDiff('day', consumption_start_utc_dttm, consumption_end_utc_dttm)/30.4 AS term_m,
    multiIf(lower(coalesce(unit,'')) IN ('gpu','gpus'), unit_price_calculated,
            lower(coalesce(unit,'')) IN ('rack','racks'), unit_price_calculated / gpus_per_rack, NULL) AS price_gpu_hr,
    multiIf(lower(coalesce(unit,'')) IN ('gpu','gpus'), resource_quantity,
            lower(coalesce(unit,'')) IN ('rack','racks'), resource_quantity * gpus_per_rack, NULL) AS gpu_qty
  FROM (
    SELECT crm_deal_id, unit, unit_price_calculated, resource_quantity, close_utc_dttm,
      consumption_start_utc_dttm, consumption_end_utc_dttm,
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
      AND lower(deal_stage_name) LIKE '%won%'
      AND lower(coalesce(consumption_type_slug,'')) LIKE '%reserve%'
      AND coalesce(autorenewal,'No') != 'Yes'
      AND consumption_start_utc_dttm IS NOT NULL AND consumption_end_utc_dttm IS NOT NULL
  )
)
WHERE gpu IN {TRACKED} AND price_gpu_hr > 0.2 AND price_gpu_hr < 20
GROUP BY gpu, tenor_months, close_month
ORDER BY gpu, tenor_months, close_month
"""

COLUMNS = ["generated_date", "gpu", "tenor_months", "close_month", "deals", "lines",
           "gpus", "price_lo", "price_med", "price_hi"]


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
    rows = []
    for r in df.to_dict("records"):
        rows.append({"generated_date": today, **{k: r[k] for k in COLUMNS[1:]}})
    # sanity: medians inside the same bands the reserve_wins task uses
    bad = [r for r in rows if not (0.5 <= float(r["price_med"]) <= 15)]
    print(f"{len(rows)} cells, {sum(int(r['deals']) for r in rows)} deal-cells, "
          f"{len(bad)} medians outside $0.5-15  ({link})")
    for r in bad:
        print("  outside band:", r)
    if dry:
        for r in rows[:15]:
            print(r)
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
