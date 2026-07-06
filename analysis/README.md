# PAYG price-change impact: collectible view (method)

Built 2026-07-06 for the monthly pricing check-in. Measures the June-1 2026 price
change on a **same-store, collectible, PAYG-proper** basis:

- Source: `//home/dwh/nemax-prod/data/ods/billing/consumption_metrics/<month>-01`
  (billed, wo VAT). GPU SKU ids and classification in `sku_meta.json`.
- PAYG-proper = `uncovered_pricing_quantity` / its cost only (reserve-covered excluded).
- Same-store = exclude SKUs first billing after the change (Jun 2026: B300 eu-west2
  `sku-e00fid4v0v8mqolddeuts`, GB300 `sku-e00fd2b1b7h2b3zl24md9`).
- Collectible = net out billing groups with consumption_state=suspended
  (billing_groups_changefeed, argMax over version). Cross-validate against the
  confirmed-fraud list (gitlab kiparis/self-service-analytics, project 1126;
  refresh via "Claude PM"/scripts/refresh-fraud-ids.py -> raw/fraud-contracts.csv,
  join on contract_id via billing_groups_changefeed.contract_id).
- ILS converted at 3.65 (immaterial, <3% of totals).
- `pricing_checkin.py` reads monthly.json / suspended.json / july_1to5.json produced
  by the queries above (see file headers), prints impact + strategy tables.

KNOWN LAG: fraud/debt suspension detection matures over ~2-4 weeks, so the latest
month's collectible is overstated at first read. Re-run after month close + ~1 week.
July-2026 rerun scheduled (see Claude scheduled task "PAYG collectible rerun - July").
