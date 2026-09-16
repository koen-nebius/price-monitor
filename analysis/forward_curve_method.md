# GPU committed-price benchmarks: methodology (v1.4, 2026-09-16)

What the Confluence pages "GPU Committed-Price Benchmarks — Internal Marks" (tables)
and "... — Interactive" (embedded single-file app) show, how the numbers are produced,
and what they are not. Built 2026-09-15 after Danila shared Ornn's paid forward curve;
revised the same day after Koen's first review (inputs, price basis, Reserve economics,
evidence classes, naming) and again after his second review (three defects: repeats
removed on a shared Slack message alone, "payback at signing" next to unrecovered
capital, unstated prepayment inside the default mark) with the page restructured into
three views. Code: `forward_curve.py`, `intel_quality.py`; template
`templates/forward_view.html` (one file, three views); publisher
`scripts/publish_forward_curve.py`; refreshes `scripts/refresh_reserve_tenor.py`
(weekly, CRM), `scripts/refresh_payg_realised.py` (weekly, realised PAYG) and
`scripts/intel_quality_refresh.py` (after bulk imports).

## Naming
"Committed-price benchmarks", not "forward curve": the x-axis is commitment length at
quote date. A delivery/start-date axis is not yet included, so the term structure is
not a future price path.

## Definition
For each GPU tier (H100, H200, B200, B300, GB200, GB300, VR) and commitment length
(3, 6, 12, 18, 24, 36, 60 months) a **mark**: the weighted median $/GPU-hr, at a
0%-prepay basis and today's price level, of the distinct observations we hold for that
cell **that state their payment terms**. Cells below three such observations print
"n/a" with a reason and are never interpolated, carried forward or filled from a model.

## Evidence classes (kept visible separately; no bid/ask spread is implied)
| class | file | what it is | enters the mark |
|---|---|---|---|
| competitor offers | `store/intel.csv` | offers/deals reported by sales in #price-intelligence. **Confirmed repeats removed** (`intel_quality.classify`): same provider, price and term within 7 days, or a seed row (May-2026 import) that repeats a retrieved row with the same or an anonymised provider or identical notes. 273 rows → 32 removed, 241 distinct offers on 2026-09-15; **5 rows that only resemble another row are kept and listed for review** (`store/intel_duplicates.csv`, column `action` = removed / review). A shared Slack message is not evidence of duplication: one message reported BoostRun (20k GPUs, US, Q4) and Nscale (15k GPUs, EU, Q1) at the same GB300 $3.80 for 36 months, two offers. Column `prepay_known` = 1 only when the quote states its prepayment (any non-zero value or an explicit zero); 118 of 273 do. When a same-provider repeat states the prepayment the earlier row lacked, the informative row is kept. | only rows with `prepay_known = 1`, weight 1 |
| Nebius achieved | `store/reserve_tenor.csv` | Nebius signed reserve deals from CRM deal reviews, per tier × tenor × close month × payment bucket (deals, GPUs, lo/median/hi). **Published and pooled only as aggregates of ≥ 3 deals** (`aggregate_asks`: merged per quarter × payment type, then per quarter, then over the whole window; what is still under 3 deals is counted but its price withheld, weight 0). Payment type is the only prepay signal (upfront / prepaid monthly / postpaid → 100 / 8 / 0 % proxy); a cross-bucket aggregate prints "mixed". | yes; each month weighs min(deals, 3) and an aggregate carries the summed weight |
| public contracts | `store/public_contracts.csv` | 12 announced multi-year contracts from the SemiAnalysis deal table; implied $/GPU-hr assumes 8,760 billed hours, i.e. a lower bound | **no**, shown as reference |

Offers with unstated payment terms (43 of the 123 in the 365-day window) are counted
per cell (`n_bid_unstated`) and their median at a 0% assumption is kept as a reference
(`bid_median_unstated`, `mark_all`), but they never enter a mark or a default
comparison. The interactive page can add them only through the explicitly labelled
choice "All, unstated counted as 0%" (market view) or "Include offers with unstated
prepayment, counted as 0%" (position view), and prints a flag while that is on.
Effect on 2026-09-15 (v1.2 pooled → v1.3 stated basis, stated-only date fit and
achieved aggregation): B300 3m $6.50 → $5.60 (Nebius achieved only), B300 12m $5.92 →
$5.48, VR 36m $6.05 → $7.15, GB300 60m $4.55 → $4.65; GB300 24m lost its mark (2
stated observations).

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
   verified 2026-09-15).
2. **Quote date → as-of quarter.** Two-way fixed effects on `log p0`, cell + quote
   quarter, pooled across tiers and **fitted on the stated-prepay offers and Nebius
   achieved only** (unstated offers are re-expressed with the result but cannot move
   it). On 2026-09-15 the factors are large: quotes from
   2025Q3–2026Q1 are lifted ×1.5–1.6, 2026Q2 ×1.2. The interactive page prints these
   factors, shows the **recent raw median** (last 90 days, as reported) next to every
   mark, and has a **recent only** switch (≤120 days, no date adjustment).
3. **Age weights.** ≤120 days: 1; ≤365 days: ½; older: dropped.
4. **Mark and confidence.** Weighted median of adjusted stated-prepay prices. `n`
   counts one observation per competitor offer and one per signed Nebius deal. `n < 3` →
   suppressed. "good" needs `n ≥ 6` from ≥ 2 providers with ≥ 2 in the last 120 days;
   otherwise "thin" and the failing test is named (`confidence_reason`). A cell with
   Nebius deals and no competitor offer is labelled "achieved only" (one provider, so
   never "good"). Also reported per cell: `mark_all` (reference, unstated counted at 0%),
   `mark_recent` (≤120 days), `recent_raw_median`, `n_providers`, `n_bid_unstated`,
   `ask_deals_withheld`.
5. **Tenor buckets.** ≤4 → 3m, ≤8 → 6m, ≤14 → 12m, ≤20 → 18m, ≤27 → 24m, ≤42 → 36m,
   longer → 60m. On-demand (term 0) is excluded.
6. **Sanity band** $0.5–15/GPU-hr after prepay normalisation.

## The 0-month anchor (on-demand)
Shown in the market view's "Other comparisons" section, never joined to the committed
marks, because on-demand is a different product (no commitment, cancellable): Nebius
list and preemptible list (latest scraper snapshot), Nebius **realised PAYG** price
(paid PAYG dollars ÷ paid PAYG GPU-hours, external unsuspended tenants, last 30 days,
`store/payg_realised.csv`), the enterprise-peer on-demand median for cluster-class SKUs,
the cheapest hyperscaler on-demand SKU, and on-demand competitor quotes from the last
90 days. On 2026-09-15: H100 list $3.85 / realised $3.55 / peer median $2.50; H200
$4.50 / $4.22 / $3.99; B300 $7.85 / $7.62 / $8.03.

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

## The interactive page (three views, one shared offer)
GPU, term, prepayment and the entered price are shared; switching views preserves
them. A deliberately entered price stays until "Use benchmark price" is pressed (the
benchmark is the comparable-offer median, then the mark, then the Finance rate). Each
view answers one question with one result line, one graph and details on demand.

- **Market benchmarks.** *How do prices vary with contract term?* One GPU (others off
  by default), one payment basis, marks by term on evenly spaced term categories with
  the selected term highlighted (the table view keeps the same GPU scope and its cells
  select GPU and term);
  offers filter (stated prepayment by default), recent-only switch, table alternative.
  On-demand, Finance rate card, hyperscaler list and modeled cost sit in an optional
  section and can be drawn on the chart. A summary line gives the selected term's
  mark, confidence, counts and recent range and hands over to the evaluator.
- **Market position.** *Where does this offer sit among comparable offers?* One term.
  Result: "Lower than X of N comparable offers" (no percentile). One axis, neutral dots
  for comparable offers, the median as a tick, one accent for your price; Nebius
  achieved optional. Clicking a dot opens the quote beside the graph (provider, date,
  term, prepayment, reported and adjusted price, Slack link, review note). The Finance
  check sits directly below: the selected segment's matching published rate or "No
  matching published rate", with the full rate card behind "View rate card".
  Comparison settings: adjusted (default) or raw within 90 days and 12.5 prepay
  points; unstated offers only through a labelled switch. Ranking needs three
  comparables; six or fewer is flagged thin.
- **Contract return.** *What does this contract return after costs?* GPU quantity and
  scenario live here. Result: the cash left per GPU (and in total) after capital and
  operating costs at expiry, or the capital left unrecovered. One graph: cumulative
  cash after capital per GPU by month with the zero line labelled "capital recovered",
  the position at signing and at expiry, and the recovery month. Cost components,
  assumptions (with exact totals) and "Compare price scenarios" (±$0.50) are
  disclosures. Totals print as $128.8m; exact values are in the assumptions.
- **PAYG (on-demand) as a term.** Not a curve point and never pooled. Market position
  compares a price with peer clouds' 8-GPU on-demand list prices plus quotes that say
  on-demand (last 90 days); hyperscaler and price-fighter list prices are drawn as
  squares, never counted; benchmark = peer on-demand median; no prepayment (the control
  is hidden, the user's prepayment and scenario choices are kept for the next term);
  contract floors hidden, parity ceiling and SemiAnalysis' modelled rental this month
  kept; the Finance check becomes Nebius' PAYG list, realised PAYG and preemptible.
  Contract return runs the PAYG occupancy scenario over a 24-month horizon (the
  portfolio payback standard is 22 months), stated in the sub-line.
- **Published list prices** (hyperscaler reserved tiers per term; on-demand lists for
  PAYG) appear as squares in the provider colours; rack rates beyond twice the
  comparable median are pinned at the right edge with a label. Hyperscaler reserved
  tiers exist in the scraper history only for H100, H200 and B200.
- **Data & method** and the footer carry counts, freshness, rules and limitations.

## Economics
Two labelled scenarios. *Reserve contract* (default): 100% of contracted hours are
billed, the prepayment share is received at signing and the remainder monthly, cash
opex per GPU-month from the portfolio model is deducted every month. **Capital is
"recovered" only when cumulative cash (receipts minus operating cost) stays at or above
capital through expiry**; the recovery month is the last crossing, not the first. An
upfront receipt that covers capital at signing but is consumed by later operating costs
is reported as "upfront cash covers capital at signing, operating costs consume it",
with the unrecovered amount at expiry (B300, 36 months, 100% prepay at $3.00:
$78,840 received, $25,611 operating cost, $66,840 capital → $13,611 per GPU
unrecovered; v1.2 printed "payback at signing" for this case). Contribution over the
term and its PV at 6.9% are shown. *PAYG cash-recovery scenario*: the portfolio model's
own 22-month floor logic at a chosen occupancy (default 75%), labelled as such. GB200,
GB300 and VR have no Nebius cost model; only the SemiAnalysis modeled floor is shown.

## SemiAnalysis references and hypothetical guidance (added 2026-09-15, evening)
Two SemiAnalysis sources exist and only one is on the page. The **GPU Pricing Index**
(monthly survey, 10 tenors, 25th–75th percentile ranges, 25% prepay assumed from 3 months)
is the true term structure; we hold seats but no export, and the API documentation Kyle
Connor promised on 2026-07-20 has not been located. It will be drawn as a shaded band on
both graphs once an export or the API is available. The **AI-Cloud TCO model** workbook
(Aug-10-2026 release, local download) is on the page now:
- `scripts/extract_sa_reference.py` writes `store/sa_reference.json` from the workbook:
  the base-case monthly market rental path per tier (Rental Price Forecasts), Full TCO
  capex per GPU, opex per GPU-month, capital/operating/total cost per hour, WACC, useful
  life, 5-year calibrated price (Neocloud Giant column; the total-cost row reproduces the
  cost floors already used: H100 $1.55 … VR $4.02), and SemiAnalysis' own floor/ceiling
  blocks (VR NVL72 $5.25 floor at ~15.6% IRR; ceiling = marketed dense-FP8 TFLOPS ratio ×
  the comparison SKU's 5-year market price, e.g. VR = 3.5 × GB300 $4.10 = $14.35).
- The builder turns the path into `sa.tiers[tier].now` (as-of month) and `term_avg[T]`
  (average of the next T months from the as-of month, or from the path start when it
  begins later, e.g. VR from 2027-01). On 2026-09-15, B300: now $6.16, 12m $5.28, 36m
  $3.63; GB300: $8.32 / $7.19 / $5.02. SemiAnalysis' 12-month averages sit within a few
  percent of our 12-month marks; from 24 months out its path falls about 45% a year, far
  below our marks (B300 36m $3.63 vs $4.91).
- Market benchmarks: optional dotted "SemiAnalysis path average" series with diamonds
  (Other comparisons). Market position: optional dashed line for the selected term.
  Never pooled into a mark; captioned as a modelled path.

**Guidance lines on Market position** (on by default; hypothetical, sources on hover):
- Floor 1: Finance's Tier-2 GM-approval floor from `Pricing model.xlsx` (segments
  `tier2_gm_floor_ai_native` / `_enterprise` in `store/nebius_reserve_grid.json`), the
  published column for the chosen prepayment or the nearest one, labelled as such.
- Floor 2: SemiAnalysis' cost-based floor, the price at which a 5-year contract with 15%
  prepaid earns ~15.6% project IRR on SemiAnalysis' Neocloud Giant inputs, reproduced
  for every tier by `scripts/sa_irr_floor.py` (`floor_irr_15_6` in `sa_reference.json`).
  The Full-TCO cash cost (no return) is quoted in text only.
- Ceiling: parity with the previous generation, `mark(prev, T)` re-based to the chosen
  prepayment × a delivered-performance multiple from `store/perf_multiples.json`
  (MLPerf-based, customer-realisable base with low/high and sources; H200←H100,
  B200←H200, B300←B200, GB200←B200, GB300←B300, VR←GB300). The multiple is editable in
  Comparison settings so a pricing discussion can test its own assumption; SemiAnalysis'
  marketed-FLOPS ceiling is not used because every offer on file sits far below it.
- Dots are coloured by provider (palette by frequency, "other" beyond ten) with a legend.

## Other evidence classes (added 2026-09-16, v1.4)

Three classes joined the page after the data-breadth review of 2026-09-16. Each is labelled, thresholded and shown beside the marks; none enters a mark.

**Nebius asked prices that did not (yet) sign** (`store/crm_asks.csv`, `scripts/refresh_crm_asks.py`, local weekly, same query rules as the signed-deal leg: Actual overview, reserve, no autorenewal, rack ÷ 72). Two classes, never pooled with each other or with anything else:

- *lost*: deals in stage Closed lost. A lost ask is an upper bound on what that customer would pay. The CRM stores `closed_lost_reason_name/desc` but the text never names a price or a competitor, so no competitor price is implied.
- *proposal*: deals in Commercial Proposal or Agreement signing. What we are asking now, not yet accepted.

Rows are per GPU × tenor bucket × class × close month × payment type (deal count, GPU sum, lo/median/hi). `forward_curve.load_crm_asks` merges rows over the last 365 days per GPU × tenor × class, takes the deal-weighted median of the monthly medians re-based to 0% prepayment through the payment-type proxy (upfront 100%, prepaid monthly 8%, postpaid 0%), applies no quote-date adjustment (stated on the page), and withholds the price of any cell under 3 deals (`CRM_ASK_MIN_DEALS`). The Market position graph draws them as hollow triangles (down = lost, up = open) re-based to the selected prepayment, with the deal count, months covered and 0%-basis asked range in the definitions block; both toggles default on and hide for PAYG (reserve deals only). First pull 2026-09-16: 345 cells since 2025-09; lost deal-months H200 147, H100 114, B200 113, B300 109, GB300 29, GB200 3, VR 1; open B300 40, B200 21, H200 17, GB300 14, H100 10, VR 1.

**Index prices** (`store/index_quotes.csv`, `load_index`). The three "SemiAnalysis GB300 index (draft)" rows (36/48/60 months at $5.60/5.50/5.40, 25% prepayment, audit 2026-09-15) had been sitting in `intel.csv` as offers and were pooled into GB300 marks. An index is a statistic over a market, not an offer, so they now live in their own file with `source`, `as_of`, `stat`, are re-based to 0% with the Finance convention and appear as crosses on Market benchmarks and a dotted line on Market position ("SemiAnalysis index (draft)"), toggle in Other comparisons. Source still to be confirmed with Danila; the SA Pricing Index proper (10 tenors, 25th–75th percentile) remains unexported.

**Short-term market prices** (`load_short_term`). SF Compute's public H100 clearing price (history.csv `consumption_type=spot`, provider `sfcompute`; 8-GPU InfiniBand nodes booked for hours to weeks) is shown at the PAYG term; Vast.ai's cheapest marketplace reservation (`reserved_short`, 1–6 months prepaid; only in the daily `store/latest.json`, not in history.csv) at the 3-month term. Entries under 8 GPUs are flagged "not cluster-class" and stay in the text; only cluster-class entries from a snapshot at most 7 days old are drawn (as squares, uncounted). Locally the daily snapshot is stale (2026-07-14), so Vast shows only in the GitHub Actions build. SF Compute fills (transacted term windows) stay parked: Koen is waitlisted for the API token.

**Intake fix, same day.** The 15 Sep #price-intelligence VR posts (Khaled Ibrahim: Jane Street prior CoreWeave deal ~$6.30 at 20% upfront for 30k VR, HRT low-mid $7s at 30% upfront; Nav Kala: Perplexity 5-year mid-$6 to low-$7 at 20–25% down) were added to `intel.csv` by hand with their Slack `message_ts`, so the inbox merge dedupes against them. Term is unknown for the first two (term 0, notes say so; they do not enter a tenor cell); Perplexity enters the 60-month cell at $6.75 and 22% prepayment (midpoints).

## Node configuration behind the prices (added 2026-09-16, v1.4)

Koen's ask: prices are quoted per GPU-hour, but the node behind a price differs in CPU, RAM, local storage and network, so a comparison should show what sits behind each price. `store/node_specs.json` (built by `scripts/merge_node_specs.py`) holds one entry per priced SKU: GPUs per node, vCPU, CPU model, RAM GB, local NVMe TB, network fabric and bandwidth, form factor, source URL, date, confidence and a `verified` flag. Sources: the providers' own documentation and pricing pages, researched per provider and then independently re-checked figure by figure against the source URL (135 entries, 129 verified, 22 providers, 2026-09-16); where a provider API reports vCPU and RAM (AWS, Azure) the daily snapshot cross-checks the documented figures (`api_check`). Nothing is estimated from a "typical HGX" configuration; unpublished figures stay null. Known gaps: Together, RunPod clusters and 1legion publish no CPU/RAM for their 8-GPU nodes; Nebius GB200/GB300 NVL72 rows are the pricing-page 112 vCPU / 800 GB per 4-GPU tray with no docs preset (unverified); Oracle Grace shapes list cores, not threads (vCPU inferred, unverified). Reported offers from #price-intelligence carry no configuration.

On the page, Market position gains "Node configuration behind these prices": Nebius' own node first, then every list price on the view (PAYG: peer, hyperscaler and platform on-demand plus short-term market; committed terms: hyperscaler reserved list) with SKU, GPUs, vCPU (and per GPU), RAM (and per GPU), NVMe, network, $/GPU-hr and $/node-hr, and a "vs Nebius node" column flagging configurations at least 25% lighter or heavier per GPU. Prices are never adjusted for configuration: the table makes the like-for-like question visible and leaves the judgement to the reader. The square tooltips carry the same configuration line. The list-price entries now record the priced SKU (`instance_type`) so the configuration matches the SKU that was priced, not another of the provider's nodes.

**Daily refresh (added 2026-09-16 pm, Koen's "start on 1").** The spec file is no longer a one-off. `history.py` persists `vcpu`, `ram_gb` and `node_gpus` per row (blank where the source publishes none), so configuration changes are visible next to price changes in `store/history.csv`. The fetchers populate those fields from data they already retrieve (no new endpoints; units: threads, GB as published, full physical node). `scripts/merge_node_specs.py --research store/node_spec_research.json --catalog` runs in the GitHub Action after the scrape and before the forward-curve build: precedence documentation > provider API snapshot > dstack gpuhunt catalog (`fetchers/gpuhunt.py fetch_specs`, catalogs for aws, azure, gcp, lambda, nebius, oracle, runpod, verda; the other providers publish no catalog object). Machine sources fill empty fields of a documented SKU (`filled_from`), report disagreements (`api_check`, `catalog_check`; GiB/GB and TiB/TB conventions are tolerated at 11%), and add SKUs only where documentation has none (confidence `catalog`). Field changes against the previous file are appended to `store/node_spec_changes.csv`. The page marks "sources disagree" where a machine source contradicts the documentation; on 2026-09-16 that is Azure ND H100/H200 v5 RAM (API 900/1100 vs docs 1900/1850 GiB), Oracle BM.GPU.GB200.4 vCPU (docs 144 cores vs catalog 256 threads), RunPod 1-GPU pods and one Lambda storage figure.

## Hosting
Embedded through the Forge "HTML" macro (Just Add+) as a child of the marks page,
published in `atlas_doc_format` by the daily job; also attached as forward_view.html
and published as a private Claude artifact for Koen. Local preview: a static server on
`store/forward_curve` (the desktop app's Browser pane cannot open claude.ai).

## Known limitations
- No delivery/start-date axis; cluster size, region, interconnect and credit quality
  are not controlled for.
- Competitor offers are sales-reported and loss-skewed; one in eight audited additions
  was wrong on a detail. Nebius achieved is thin at ≥ 24 months; GB200 and VR have no
  achieved observations.
- Quarter effects are pooled across tiers.
- Nebius achieved prepayment is a payment-type proxy, not a percentage (the CRM field is
  empty for every reserve deal).
- The CRM leg only sees deals stage-changed to Closed won in a deal review; on
  2026-09-15 the latest such deal closed 2026-08-07 although capacity approvals and
  Finance requests show later signings.

## Refresh cadence
Daily: `scrape.yml` builds and publishes both pages. Weekly (local): CRM aggregates and
realised PAYG. After bulk intel imports: `scripts/intel_quality_refresh.py` (rewrites
`prepay_known`, writes the removed/review report). Occasional: grid, economics, public
contracts files.

## Confidentiality
Aggregates only for Nebius achieved; no customer names in offer notes. Internal only;
never quote marks, achieved prices or the cost view to customers.
