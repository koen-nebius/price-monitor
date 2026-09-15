# Internal GPU forward curve: methodology (v1.1, 2026-09-15)

What the Confluence pages "GPU Forward Curve — Internal Marks" (tables) and
"GPU Forward Curve — Interactive" (embedded single-file app) show, how the marks are
produced, and what they are not. Built 2026-09-15 (Koen + Claude) after Danila shared
Ornn's paid forward curve (data.ornn.com/analytics/gpu/forward, $500/month, current
marks only, no history). Code: `forward_curve.py`; templates `templates/forward_view.html`
(curve) and `templates/position_view.html` (where-we-land ladder); publisher
`scripts/publish_forward_curve.py`; ask-side refresh `scripts/refresh_reserve_tenor.py`.

## Definition
For each GPU tier (H100, H200, B200, B300, GB200, GB300, VR) and commitment length
(3, 6, 12, 18, 24, 36, 60 months) a **mark**: the weighted median $/GPU-hr, at a
0%-prepay basis and today's price level, of every dated observation we hold for that
cell. A *marked* curve of an illiquid market, the same thing Ornn publishes, not a
traded strip. A cell without enough evidence prints "n/a" with a reason and is never
interpolated, carried forward or filled from a model.

## Observation legs (pooled into the mark)
| leg | file | what it is | weight |
|---|---|---|---|
| bid | `store/intel.csv` | competitor quotes / deals reported by sales in #price-intelligence, parsed with term and prepay. 273 rows after the 2026-09-15 audit added 97 quotes the parser had missed (Jan-2025 → Sep-2026; spot-checked 7 of 8 correct) | 1 |
| ask | `store/reserve_tenor.csv` | Nebius **signed** reserve deals from CRM deal reviews, aggregated per tier × tenor × close month × payment bucket (deals, GPUs, lo/median/hi). Aggregates only | min(deals, 3) |
| public | `store/public_contracts.csv` | publicly announced multi-year contracts from the SemiAnalysis deal table (12 of 20 kept: rows whose price is a SemiAnalysis assumption, blended fleets, per-system prices and undisclosed chips excluded). Implied $/GPU-hr assumes 8,760 billed hours, i.e. a lower bound | ½ |

Reference only (shown, never pooled): Finance reserve grid by segment and prepay
column (`store/nebius_reserve_grid.json`, versions 2026-06-11 and 2026-09-07), cheapest
hyperscaler reserved/committed list tier (`store/history.csv`), SemiAnalysis modeled
cost floor per SKU (Aug-10-2026 model, Full TCO row 147, Neocloud Giant), and Koen's
PAYG portfolio economics per SKU (`store/economics.json`, cash-recovery basis).

## Normalisation (declared choices)
1. **Prepay → 0% equivalent.** `discount(T, p) = 0.0345 × (T/12) × (1 − (1 − p)²)`,
   capped at 25%; `p0 = p / (1 − discount)`. This is Finance's money-cost convention:
   sheet "." of Pricing model.xlsx derives the 100/50/30% columns from the 0% column as
   12m −3.40/−2.63/−1.80% and 24m −6.97/−5.26/−3.57%; a three-parameter fit returns
   a = 0.0344/yr, linear in tenor, exponent 2.04 (rmse 0.02 pp). It also matches the
   mechanism (prepayment consumes the first p × T months at ~7%/yr money cost).
   **Not used:** the Sep-7 AI Native grid's 100→50% steps (5.8–13.0%, convex in p) are a
   commercial ladder steering buyers to full prepayment; using them to normalise market
   quotes would overstate every prepaid quote by 5–10 pp. Adversarially verified
   2026-09-15. CRM deals carry no prepay percentage (field empty for every reserve deal),
   so payment type is a proxy: upfront (prepaid, one-time) = 100%, prepaid monthly = 8%,
   postpaid = 0%.
2. **Quote date → as-of quarter.** Two-way fixed effects on `log p0`, cell(tier, tenor) +
   quote quarter, pooled across tiers, alternating means (50 iterations). Every
   observation is shifted by `effect[as-of quarter] − effect[its quarter]`. Estimated
   effects on 2026-09-15: 2025Q3 −0.40, 2025Q4 −0.43, 2026Q1 −0.41, 2026Q2 −0.21 (log,
   vs 2026Q3). Pooling is a v1 simplification; per-family effects are the first v2 item.
3. **Age weights.** ≤ 120 days: 1; ≤ 365 days: ½; older: dropped.
4. **Mark.** Weighted median of adjusted prices. `n < 3` → suppressed. "good" needs
   `n ≥ 6` with ≥ 2 observations in the last 120 days, else "thin".
5. **Tenor buckets.** ≤4 → 3m, ≤8 → 6m, ≤14 → 12m, ≤20 → 18m, ≤27 → 24m, ≤42 → 36m,
   longer → 60m. On-demand (term 0) is excluded from the curve.
6. **Sanity band** $0.5–15/GPU-hr after prepay normalisation.

## The interactive page
- **Curve tab.** Marks vs tenor per tier; prepay basis pills (0/30/50/100%) re-express
  every mark with the convention above; the quotes filter recomputes cells client-side
  from the exported (anonymised) observations, keeping only quotes whose own prepayment
  sits in the chosen band; grid and cost references overlay on demand.
- **Where we land tab** (design chosen by a three-proposal panel and two judges,
  2026-09-15: a tenor ladder beat a price book and a single-cell ladder). One shared
  $/GPU-hr axis, seven tenor rows, individual competitor quotes as ticks (raw prices,
  never normalised; brightness = recency, hollow = prepay mismatch, short = hyperscaler),
  Nebius signed median (◇), mark with range (|), Finance grid and hyperscaler list (▽),
  a draggable white rule for the candidate price. Headline: "under k of n comparable ·
  percentile · margin over cash cost · payback". Comparable = quotes ≤ 90 days old with
  prepayment within ±12.5 pp; below six comparables the rank falls back to all in-date
  quotes, below three to quotes up to 12 months old, and says so; below three it is
  suppressed. Not a scatter: term is seven categorical tenors with 3–25 quotes each; a
  scatter over-plots into stripes, invites a false trend line and cannot carry the
  reference set or the cost view.
- **Cost view.** From the PAYG portfolio model (Finance GPU Calculator inputs,
  cash-recovery basis, period 2026-08-31): cash cost per sold hour = monthly cash opex per
  installed GPU ÷ (730 × occupancy); 22-month floor adds capex/22; payback at a price =
  capex per GPU ÷ (price × 730 × occupancy − monthly cash opex). Occupancy default 75%
  (the model's sensitivity assumption, not current PAYG utilisation), stepper 50–95%.
  It is a PAYG cost view applied to a term price, not a Reserve P&L. GB200, GB300 and VR
  have no portfolio cost model; only the SemiAnalysis modeled floor is shown for them.

## Hosting
The interactive page is embedded through the Forge "HTML" macro (Just Add+, Modus
Create; extension key in `config.FORGE_HTML_MACRO_KEY`), which 47 Nebius pages already
use for script-bearing single-file dashboards. Published in `atlas_doc_format` by the
daily job as a child of the marks page; the same file is attached as forward_view.html
and published as a private Claude artifact for Koen. A Claude artifact shared "to the
organisation" reaches only claude.ai org members, not every employee behind SSO.

## Known limitations
- No delivery-date axis: a 36m quote for Q1-2027 delivery and one for immediate start
  land in the same cell. CRM has consumption start; intel only sometimes.
- Cluster size, region, interconnect and credit quality are not controlled for.
- Bid leg is sales-reported and loss-skewed (one in eight audited additions was wrong on
  a detail); ask leg is thin at ≥ 24 months; GB200 and VR have no ask observations.
- Quarter effects pooled across tiers; the latest quarter is noisy early in the quarter.
- Public-contract prices assume 100% billed hours and are shown as lower bounds.

## Refresh cadence and ownership
- Daily: GHA `scrape.yml` runs `forward_curve.py` after `main.py`, publishes both pages
  (soft-fail) and commits `store/forward_curve/*`. Manual re-publish: workflow
  "Publish Confluence pages (manual)".
- Weekly (local, Sunday with the reserve-wins task): `scripts/refresh_reserve_tenor.py`
  via the YT data-client venv rewrites `store/reserve_tenor.csv`.
- Occasional (manual): `store/nebius_reserve_grid.json` when Finance re-issues the grid;
  `store/economics.json` when the portfolio model's Economics sheet changes;
  `store/public_contracts.csv` when the SemiAnalysis deal table is updated.
- Cross-checks worth adding: SemiAnalysis Pricing Index (10 tenors, seats held, API
  pending), Ornn forward marks (72h trial / $500 per month), Silicon Data.

## Confidentiality
Ask-side inputs are aggregates only (no customer names, no deal rows); the intel notes
carry no customer names. Both pages are marked internal only; marks and the cost view
must never be quoted to customers or pasted externally.
