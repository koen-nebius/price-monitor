# Internal GPU forward curve: methodology

What the "GPU Forward Curve — Internal Marks" Confluence page shows, how the marks
are produced, and what they are not. Built 2026-09-15 (Koen + Claude) after Danila
shared Ornn's paid forward curve (data.ornn.com/analytics/gpu/forward, $500/month,
current marks only, no history). Code: `forward_curve.py`; publisher:
`scripts/publish_forward_curve.py`; ask-side refresh: `scripts/refresh_reserve_tenor.py`.

## Definition
For each GPU tier (H100, H200, B200, B300, GB200, GB300, VR) and commitment length
(3, 6, 12, 18, 24, 36, 60 months) a **mark**: the weighted median $/GPU-hr, at a
0%-prepay basis and today's price level, of every dated observation we hold for that
cell. It is a *marked* curve of an illiquid market, the same thing Ornn publishes,
not a traded futures strip. A cell without enough evidence prints "n/a" with a
reason and is never interpolated, carried forward or filled from a model.

## Observation legs
| leg | file | what it is | side of the market |
|---|---|---|---|
| bid | `store/intel.csv` | competitor quotes / deals reported by sales in #price-intelligence, parsed with term and prepay (176 rows Jun-25 → Sep-26) | what buyers can get elsewhere; skews to losses |
| ask | `store/reserve_tenor.csv` | Nebius **signed** reserve deals from CRM deal reviews, aggregated per tier × tenor × close month (deals, GPUs, lo/median/hi) | what buyers actually paid us; aggregates only |

Reference columns (shown, never pooled into the mark): Nebius AE committed grid
(`config.NEBIUS_COMMITTED_PRICES`, 512+ GPUs, 100% upfront, as published), cheapest
hyperscaler reserved/committed list tier from the latest `store/history.csv`
snapshot, and the SemiAnalysis TCO-model cost floor per SKU (Aug-10-2026 model,
Full TCO row 147, Neocloud Giant profile: H100 1.55, H200 1.59, B200 2.07,
GB200 2.27, B300 2.43, GB300 2.79, VR 4.02 $/GPU-hr).

## Normalisation (declared choices, v1)
1. **Prepay → 0% equivalent.** `p0 = p / (1 − 0.06 × prepay_share)`. The 6% at
   full prepay sits between the Nebius AE grid (30% → 100% prepay is −3% on B300
   12m, −7% on GB300 36m) and the CoreWeave H100 ladder reported in
   #price-intelligence (25% → 100% = −4.4%). CRM prices are as-billed and treated
   as 0% (prepay is a payment term on the deal, not a discount in the hourly rate).
2. **Quote date → as-of quarter.** Two-way fixed effects on `log p0`,
   cell(tier, tenor) + quote quarter, pooled across tiers, solved by alternating
   means (50 iterations). Every observation is shifted by
   `effect[as-of quarter] − effect[its quarter]`. On 2026-09-15 the estimated
   quarter effects were 2025Q3 −0.40, 2025Q4 −0.43, 2026Q1 −0.41, 2026Q2 −0.21
   (log, vs 2026Q3): the spring-2026 repricing is ~35–50%, so mixing quote dates
   without this step is fatal. Pooling across tiers is a simplification; Hopper
   and Blackwell repriced by similar ratios in 2026H1 (H100 12m CRM medians 1.7 →
   2.5, B300 12m 3.45 → 5.3) but per-family effects are the first v2 item.
3. **Age weights.** ≤ 120 days: weight 1; ≤ 365 days: weight ½; older: dropped.
   Ask cells weigh `min(deals, 3)` so one mega-deal cannot dominate a cell.
4. **Mark.** Weighted median of adjusted prices. `n < 3` → suppressed. Confidence
   "good" needs `n ≥ 6` with ≥ 2 observations in the last 120 days, else "thin".
5. **Tenor buckets.** ≤4 → 3m, ≤8 → 6m, ≤14 → 12m, ≤20 → 18m, ≤27 → 24m,
   ≤42 → 36m, longer → 60m. On-demand (term 0) is excluded from the curve.
6. **Sanity band** $0.5–15/GPU-hr after prepay normalisation.

## Reading the page
- **Marks grid**: mark, confidence lozenge, `n (bid/ask)`, min–max of recent
  adjusted observations. **Shape** compares 36m to 12m (backwardation = 36m
  cheaper). The 3m column is the short-end (immediacy) premium.
- **Bid vs ask**: spread = ask/bid − 1. Positive on a cell where we still win =
  the market pays for availability/quality; negative = we are under the market.
- **History**: `store/forward_curve/marks_history.csv` appends one row per
  (as-of, tier, tenor) per daily run, so an in-house time series of marks accrues
  from day one (Ornn's API has none).

## Known limitations (v1)
- No delivery-date axis: a 36m quote for Q1-2027 delivery and one for immediate
  start land in the same cell. CRM has consumption start; intel only sometimes.
- Cluster size, region, interconnect and credit quality are not controlled for.
- Bid leg is sales-reported and loss-skewed; ask leg is small at ≥ 24 months
  (2–5 deals per tier). GB200 and VR have no ask observations.
- Quarter effects pooled across tiers; the latest quarter can be noisy early in
  the quarter.
- Nebius list reference is not prepay-normalised (shown as published).

## Refresh cadence and ownership
- Daily: GHA `scrape.yml` runs `forward_curve.py` after `main.py`, publishes the
  page (soft-fail) and commits `store/forward_curve/*`.
- Weekly (local, Sunday with the reserve-wins task): `scripts/refresh_reserve_tenor.py`
  via the YT data-client venv rewrites `store/reserve_tenor.csv` (YT is not
  reachable from GitHub Actions). Staleness shows on the page as the
  `reserve_tenor generated` date.
- Cross-checks worth adding: SemiAnalysis Pricing Index (10 tenors, seats held,
  API pending), Ornn forward marks (72h trial / $500 per month), Silicon Data.

## Confidentiality
Ask-side inputs are aggregates only (no customer names, no deal rows). The page is
marked internal only; marks are internal benchmarks and must never be quoted to
customers or pasted externally. Same rule as `reserve_wins_method.md`.
