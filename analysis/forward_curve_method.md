# GPU committed-price benchmarks: methodology (v1.2, 2026-09-15)

What the Confluence pages "GPU Committed-Price Benchmarks — Internal Marks" (tables)
and "... — Interactive" (embedded single-file app) show, how the numbers are produced,
and what they are not. Built 2026-09-15 after Danila shared Ornn's paid forward curve;
revised the same day after Koen's review (inputs, price basis, Reserve economics,
evidence classes, naming). Code: `forward_curve.py`, `intel_quality.py`;
templates `templates/forward_view.html` (curve) and `templates/position_view.html`
(where-we-land ladder); publisher `scripts/publish_forward_curve.py`; refreshes
`scripts/refresh_reserve_tenor.py` (weekly, CRM) and `scripts/intel_quality_refresh.py`
(after bulk imports).

## Naming
"Committed-price benchmarks", not "forward curve": the x-axis is commitment length at
quote date. A delivery/start-date axis is not yet included, so the term structure is
not a future price path.

## Definition
For each GPU tier (H100, H200, B200, B300, GB200, GB300, VR) and commitment length
(3, 6, 12, 18, 24, 36, 60 months) a **mark**: the weighted median $/GPU-hr, at a
0%-prepay basis and today's price level, of the distinct observations we hold for
that cell. Cells below three distinct observations print "n/a" with a reason and are
never interpolated, carried forward or filled from a model.

## Evidence classes (kept visible separately; no bid/ask spread is implied)
| class | file | what it is | pooled into the mark |
|---|---|---|---|
| competitor offers | `store/intel.csv` | offers/deals reported by sales in #price-intelligence, **deduplicated by underlying offer** (`intel_quality.dedupe`: seed repeats, same-message multi-provider rows, same provider/price/term within 7 days). 273 rows → 236 distinct offers on 2026-09-15 (37 duplicates listed in `store/intel_duplicates.csv`). Column `prepay_known` = 1 only when the quote states its prepayment (any non-zero value or an explicit zero); 96 of 273 do. | yes, weight 1 |
| Nebius achieved | `store/reserve_tenor.csv` | Nebius signed reserve deals from CRM deal reviews, aggregated per tier × tenor × close month × payment bucket (deals, GPUs, lo/median/hi). Aggregates only. Payment type is the only prepay signal (upfront / prepaid monthly / postpaid → 100 / 8 / 0 % proxy). | yes, weight min(deals, 3) |
| public contracts | `store/public_contracts.csv` | 12 announced multi-year contracts from the SemiAnalysis deal table; implied $/GPU-hr assumes 8,760 billed hours, i.e. a lower bound | **no**, shown as reference |

References (shown, never pooled): Finance reserve grid by segment and prepay column
(`store/nebius_reserve_grid.json`, 2026-06-11 and 2026-09-07; shown only in the
column Finance publishes, never re-based), cheapest hyperscaler reserved/committed list
tier, SemiAnalysis modeled cost floor per SKU, and Koen's PAYG portfolio economics per
SKU (`store/economics.json`).

## Normalisation (declared choices)
1. **Prepay → 0% equivalent.** `discount(T, p) = 0.0345 × (T/12) × (1 − (1 − p)²)`,
   capped at 25%; `p0 = p / (1 − discount)`. Finance's money-cost convention (sheet "."
   of Pricing model.xlsx; three-parameter fit rmse 0.02 pp). The Sep-7 grid's 100→50%
   steps (5.8–13.0%) are a commercial ladder and are not used to normalise (adversarially
   verified 2026-09-15). Offers with `prepay_known = 0` are counted at 0% for the pooled
   mark and flagged; the interactive page excludes them from ranked comparisons by default.
2. **Quote date → as-of quarter.** Two-way fixed effects on `log p0`, cell + quote
   quarter, pooled across tiers. On 2026-09-15 the factors are large: quotes from
   2025Q3–2026Q1 are lifted ×1.5–1.6, 2026Q2 ×1.2. The interactive page prints these
   factors, shows the **recent raw median** (last 90 days, as reported) next to every
   adjusted mark, and has a **recent only** switch (≤120 days, no date adjustment).
3. **Age weights.** ≤120 days: 1; ≤365 days: ½; older: dropped.
4. **Mark and confidence.** Weighted median of adjusted prices. `n < 3` → suppressed.
   "good" needs `n ≥ 6` distinct observations from ≥ 2 providers with ≥ 2 in the last
   120 days; otherwise "thin". Also reported per cell: `mark_known` (stated-prepay
   observations only), `mark_recent` (≤120 days), `recent_raw_median`, `n_providers`.
5. **Tenor buckets.** ≤4 → 3m, ≤8 → 6m, ≤14 → 12m, ≤20 → 18m, ≤27 → 24m, ≤42 → 36m,
   longer → 60m. On-demand (term 0) is excluded.
6. **Sanity band** $0.5–15/GPU-hr after prepay normalisation.

## The 0-month anchor (on-demand)
Shown as a separate column on the curve, never joined to the committed marks, because
on-demand is a different product (no commitment, cancellable): Nebius list and
preemptible list (latest scraper snapshot), Nebius **realised PAYG** price (paid PAYG
dollars ÷ paid PAYG GPU-hours, external unsuspended tenants, last 30 days,
`store/payg_realised.csv`, weekly local refresh `scripts/refresh_payg_realised.py`),
the enterprise-peer on-demand median for cluster-class SKUs, the cheapest hyperscaler
on-demand SKU (pinned above the axis when far off), and on-demand competitor quotes
from the last 90 days. On 2026-09-15: H100 list $3.85 / realised $3.55 / peer median
$2.50; H200 $4.50 / $4.22 / $3.99; B300 $7.85 / $7.62 / $8.03.

## Model estimates and extrapolation (policy)
Koen's own pricing workbooks (GB300 and Vera Rubin weighted models, the VR forward-term
model, the Anthropic reconciliation) and the SemiAnalysis rental-price paths are
**model estimates**, not observations: the weighted models consumed the same field
intel, so feeding them back would be circular, and the SA paths are value-based
calibrations that sit 2–3× above transacted GB300/VR prices. They may be shown as a
labelled "model" series or used as priors for empty cells, never pooled into a mark.
A true forward curve (delivery-date axis) would combine an expected spot path (SA or
in-house) with the observed term premia here; that is the v2 route, not extrapolation
across tenors from a single workbook.

## The interactive page
- **Curve tab.** Marks vs tenor per tier; prepay basis 0/25/50/75/100%; filters: all /
  stated prepay only / prepay bands; recent only; grid and cost references. Tier and
  prepay basis are synchronised with the other tab.
- **Where we land tab.** Built for a price decision: title line (GPU · term · prepay ·
  candidate), four tiles (where we land, contract economics for a chosen GPU count,
  Finance grid, price sensitivity ±$0.50), then the ladder for the selected terms only
  (multi-select chips, default 12/24/36 months); observations and notes fold away.
  One shared $/GPU-hr axis, one row per selected tenor, every competitor
  offer as a dot (hollow when prepayment is not stated), Nebius achieved (◇, adjusted),
  the mark with its recent range, Finance grid and hyperscaler marks (▽; grid only in
  the published prepay column, dashed when that column does not exist), public contracts
  as dashed diamonds, and a draggable rule for the candidate price. **Ranking basis** is
  explicit and single: by default all prices are adjusted to today's level and to the
  selected prepayment and only offers that state their prepayment are ranked; the raw
  view ranks reported prices from the last 90 days within 12.5 prepay points. Percentile
  needs six comparables, ordinal three; otherwise not ranked. Each dot opens the
  observation list (provider, date, term, prepay, reported and adjusted price, Slack
  link). A copy button emits the deal-review line with basis and date.
- **Economics.** Two labelled modes. *Reserve contract* (default): 100% of contracted
  hours are billed, the prepayment share is received at signing and the remainder
  monthly, cash opex per GPU-month from the portfolio model is deducted, and payback is
  the month cumulative cash covers capex per GPU; contribution over the term and its PV
  at 6.9% are shown; capex not recovered within the term is stated as a residual with an
  explicit resale scenario. *PAYG cash-recovery scenario*: the portfolio model's own
  22-month floor logic at a chosen occupancy (default 75%), labelled as such. GB200,
  GB300 and VR have no Nebius cost model; only the SemiAnalysis modeled floor is shown.

## Hosting
Embedded through the Forge "HTML" macro (Just Add+) as a child of the marks page,
published in `atlas_doc_format` by the daily job; also attached as forward_view.html
and published as a private Claude artifact for Koen.

## Known limitations
- No delivery/start-date axis; cluster size, region, interconnect and credit quality
  are not controlled for.
- Competitor offers are sales-reported and loss-skewed; one in eight audited additions
  was wrong on a detail. Nebius achieved is thin at ≥ 24 months; GB200 and VR have no
  achieved observations.
- Quarter effects are pooled across tiers.
- Nebius achieved prepayment is a payment-type proxy, not a percentage (the CRM field is
  empty for every reserve deal).

## Refresh cadence
Daily: `scrape.yml` builds and publishes both pages. Weekly (local): CRM aggregates.
After bulk intel imports: `scripts/intel_quality_refresh.py` (rewrites `prepay_known`,
writes the duplicates report). Occasional: grid, economics, public contracts files.

## Confidentiality
Aggregates only for Nebius achieved; no customer names in offer notes. Internal only;
never quote marks, achieved prices or the cost view to customers.
