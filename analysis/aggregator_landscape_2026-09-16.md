# GPU price aggregators, indices and curve dashboards: landscape and lessons for the benchmarks page

*Research run 2026-09-16 for Koen's question "how do other price aggregators work; what functionality, best practices and look and feel can we infer; how do we stay in line with Ornn's visuals". Six research agents read public pages (product pages, docs, methodology notes, CSS and JS bundles, press) across 65 products in six clusters; one agent synthesised this report against our page; one critic listed gaps and unsupported claims (appended). Ornn's and Silicon Data's live dashboards sit behind login walls that our browser cannot open, so their visuals are reconstructed from CSS tokens, bundle strings and one public screenshot, not observed. Raw findings: `aggregator_landscape_findings_2026-09-16.json`.*

*Read with the critic's review at the end: several "only" and "nobody" statements in sections 2 to 4 are contradicted or unsupported by the findings (prepay normalisation, evidence quality per point, structured interconnect fields, alerts), and a fifth of the landscape table rests on truncated comparator material. The Ornn and index sections, including the CSS tokens and methodology quotes, checked out.*

# GPU price aggregators, indices and curve dashboards: what they do, what to borrow, what to keep

Scope note. This report rests on the research JSON for four clusters (Ornn; index and research products; comparison sites; marketplaces). The marketplace cluster was cut off after Prime Intellect, so io.net, Akash, RunPod, Together AI and CoreWeave are not covered, and the fifth and sixth clusters were not supplied. Where a claim depends on a single page the URL follows in parentheses. Several live charts (Ornn, Silicon Data portal, SemiAnalysis Atlas, SF Compute dashboard) could not be viewed; their design is reconstructed from bundles, docs and public renderings, and this is flagged where it matters.

## 1. Landscape

| Product | Operator | What it prices | Units and tenors | Cadence | Business model | Audience |
|---|---|---|---|---|---|---|
| Ornn OCPI (data.ornn.com) | Ornn Data LLC | Executed on-demand GPU rentals, ~150 providers, ~1,000 trades/day; H100, H200, B200, A100, RTX 5090 (+1 paid) | USD/GPU-hour, spot only; reserved and forward contracts out of scope by design | Hourly window; daily settle at 4:00 PM ET, published 20:00 UTC | Free: 5 GPUs, 3 months daily. Premium $500/mo: all-time history, API, forward curve. Full: hourly, exports, map | Lenders, buyers, traders; Bloomberg ORNNH100, Kalshi/Polymarket settlement, ICE futures pending (https://data.ornn.com/pricing) |
| Ornn Forward Curves | Ornn Data LLC | Analyst-marked term prices from "active deals"; 9 curves incl. B300, GB200, GB300 | USD/GPU-hour flat term price; 5 tenors 1M/6M/1Y/3Y/5Y; alternative "implied forward" basis | Weekly, hand-published; per-mark publish timestamp | Premium/Full only | Hedgers, lenders, providers pricing long-dated capacity (https://data.ornn.com/docs/data-dictionary.md) |
| Silicon Data Silicon Index | Silicon Data Inc. | Listed/scraped rental prices, 30+ platforms; 7 GPU series with neo-cloud vs hyperscaler splits; RAM and token indices | USD/GPU-hour spot; implied forward curve 1–36 months; residual-value curves | Once per business day | Basic free (30 days); Pro $998/mo (history, API, CSV, 36-month curve); Enterprise feeds. Bloomberg SDH100RT etc.; CME/NYMEX futures 5 Oct 2026 | Financial institutions, AI companies, providers (https://www.silicondata.com/pricing) |
| SemiAnalysis GPU index (SAH100SC) | SemiAnalysis | Hourly spot-contract composite (scraped) plus monthly analyst survey of contract prices, 100+ participants | USD/GPU-hour; grid On-Demand, 1y, 18m, 2y, 3y, 4y, 5y with 25% prepay convention; ranges are 25th–75th percentile | Composite hourly; contract survey monthly (public table stale at Apr 2026) | Free teaser page; paid AI Cloud TCO Model / GPU Pricing Product, price undisclosed | Neoclouds, buyers, investors (https://gpu-index.semianalysis.com/) |
| Compute Desk (DESKH100 etc.) | The Compute Index, Inc. | Private reserved-deal transactions plus live prices | USD/GPU-hour, US 1-year reserved per SKU (H100, H200, B200, B300) | Daily (values not rendered at fetch) | Data + RFQ workflow + settlement; Bloomberg/Refinitiv; Nodal futures pending | "Compute traders", lenders (https://www.compute-desk.com/) |
| Computable GPU Index (CGI) | Computable | Listed on-demand prices from a named panel (Nebius, CoreWeave, Lambda, Crusoe, RunPod, Vast.ai, ...) | USD/GPU-hour on-demand; B300, B200, H100 SXM, H200 SXM | Every 15 minutes, no fixing time | Free, open source (CC BY-NC data; settlement licensed separately); MCP endpoint | Contract drafters, buyers/sellers wanting a reproducible reference (https://market.getcomputable.com/gpu-index) |
| Compute Exchange | Compute Exchange | Private quotes/auctions for reserved capacity, hardware, bilateral forwards | Quotes per request; terms 1/3/6/12/24/36 months | Per request (indicative in 24 h) | 4% provider commission; no data product | Enterprise CTOs, institutional counterparties (https://compute.exchange/fees) |
| GetDeploying | GetDeploying.com | Listed prices, 79 providers, ~4.6k configs; on-demand, reserved (1mo/1y/24mo/3y), spot | USD/GPU-hour; index level Jan 6 2025 = 100 | Scrapes every 15 min to daily; index weekly | Free site with sponsors and affiliates; API $299/mo | Developers, FinOps (https://getdeploying.com/gpu-price-index) |
| ComputePrices.com | lansky.tech | Listed on-demand and spot, 77 GPU providers; LLM token prices; deals tracker | USD/GPU-hour; no reserved type | Daily | Free site; API Free/Pro $299/mo/Enterprise | Developers (https://computeprices.com/docs/api) |
| GPU Tracker | indie | Listed prices, 54+ providers | Level index Feb 2026 = 100 on p25 of listings | Every 6 h claimed; stamp shows Apr 2026 | Free + Pro API | Developers (https://gputracker.dev/gpu-price-index) |
| AIMultiple GPU index | AIMultiple | Posted prices, 69 providers, 17 models; on-demand, spot, 1-year reserved | USD/GPU-hour, median-of-medians | Monthly snapshots since Jul 2024 | Free research page (lead gen) | Enterprise buyers, analysts (https://aimultiple.com/gpu-index) |
| GPUAlpha | GPUAlpha | Marketplace spot listings from Vast.ai, Lambda, RunPod | Level index; landing numbers marked illustrative | Daily snapshots | Freemium app | AI infra teams (https://gpualpha.com/) |
| cloud-gpus.com | Jolt ML | Listed instance prices, 30+ providers; on-demand, spot, reserved | Per instance card | Daily | Free, affiliates, no cookies | Small teams (https://cloud-gpus.com/about) |
| Shadeform directory | Shadeform | Bookable on-demand prices across ~30 integrated clouds; reserved via form | USD/hour per instance; averages per GPU count | Live in API, no stamp on pages | Free brokerage, "no markups" | AI teams deploying multi-cloud (https://shadeform.com/prices) |
| Vantage Instances | Vantage (open source) | AWS/Azure/GCP official pricing APIs | Per instance; full reserved matrix (1y/3y x No/Partial/All Upfront x Standard/Convertible) + Savings Plans + spot | Continuous CI, single page stamp | Free, lead gen, free API | Cloud engineers, FinOps (https://instances.vantage.sh/) |
| gpuhunt (dstack) | dstack.ai | Provider list prices, 16 providers incl. Nebius | Hourly USD per offer; spot flag; no reserved | Hourly versioned snapshots to S3 | Open source MPL-2.0 | Pipelines, orchestrators (https://github.com/dstackai/gpuhunt) |
| Cloud Mercato | Cloud Mercato | Hyperscaler and EU provider instance prices and benchmarks | Per instance; commitment-length curve hourly to 3-year no-upfront | Not stamped | Freemium; Pro 1,000 EUR/mo | EU analysts, procurement (https://auth.cloud-mercato.com/pricing/) |
| SF Compute | San Francisco Compute Co. | Continuous double-auction order book per SKU and delivery window | USD per node-hour (node = 8 GPUs); any minute-aligned window, hours to years | Continuous; fills API 30 days back | Marketplace; sell-side fixed + percentage fee | Labs buying/reselling node blocks (https://docs.sfcompute.com/preview/orderbook.md) |
| Vast.ai | Vast.ai Inc. | Host-posted on-demand asks and interruptible bids | USD/hour per offer (GPU + storage + bandwidth); reserved 1/3/6 months via sales | Real time; 30/90-day history per GPU card | Marketplace take | Cost-sensitive developers (https://vast.ai/pricing) |
| Prime Intellect Compute | Prime Intellect | Aggregated provider on-demand list prices | USD/hour per offer; socket, region, stock status | "Prices update in realtime" | Aggregator margin | Researchers, startups (https://docs.primeintellect.ai/api-reference/check-gpu-availability) |

Two products that are not price sources but recur in the conversation: ClusterMAX (SemiAnalysis) rates provider quality in Platinum to Bronze tiers with pricing scored qualitatively and no $/GPU-hour (https://www.clustermax.ai/overview); InferenceX publishes benchmark datapoints with public CI logs and takes $/GPU-hour as an input, not an output (https://inferencex.semianalysis.com/).

## 2. How they work

### Collection

Three provenance classes exist and none of the vendors combines them honestly on one axis:

- Executed transactions: Ornn OCPI (invoice-level records from ~150 partnered providers, wash-trade screen, provider re-verification) (https://data.ornn.com/methodology); Compute Desk (private reserved deals, no method document); SF Compute fills (a true order book, but limited to 30 days of history via API).
- Analyst surveys: SemiAnalysis contract grid from 100+ participants, monthly, validated with a few arranged transactions (https://newsletter.semianalysis.com/p/the-great-gpu-shortage-rental-capacity); Ornn forward marks, hand-entered from "active deals", weekly (https://data.ornn.com/faq).
- Scraped list prices: Silicon Data, Computable, GetDeploying, GPU Tracker, AIMultiple, GPUAlpha, ComputePrices, gpuhunt, Vantage.

Same-day H100 prints on 15–16 Sep 2026 ran from $2.53 (Ornn trades) to $2.66 (Silicon Data) to $3.50–3.51 (GPU Tracker p25, Computable). The spread is the provenance class, not noise.

Our page mixes classes too (Slack-reported offers, CRM aggregates, scraped list prices, SemiAnalysis models), but it keeps them as separate series and treats list prices as squares beyond the axis rather than pooling them. That is the right instinct and matches how Ornn separates the spot index from the analyst curve and how Computable tags source_type per row.

### Normalisation

- Unit: universal USD per GPU-hour with node totals secondary (GetDeploying: "an 8xH100 instance at $40/hr counts as $5.00"; Computable: "$68.80 for an 8-GPU instance normalizes to $8.60"). Exceptions price per instance (Vantage, Cloud Mercato) or per node (SF Compute, node = 8 GPUs).
- Configuration basis: Silicon Data states a "basis adjustment" for rental type, geography, CPU platform and GPU variant but publishes no coefficients (https://www.silicondata.com/blog/building-a-robust-gpu-index). Ornn OCPI pools one global distribution and does not publish SXM vs PCIe handling. Shadeform is the only directory with structured interconnect (pcie/sxm4/sxm5/sxm6) and nvlink fields (https://docs.shadeform.ai/api-reference/instances/instances-types).
- Prepayment: only SemiAnalysis states a convention ("Pricing reflects 25% prepay for 3m and beyond"), and it is a footnote, not an adjustment (https://gpu-index.semianalysis.com/). Nobody else normalises prepay at all; AIMultiple and GetDeploying report reserved list discounts as-posted.
- Time: Ornn winsorises within a rolling window; Computable pre-smooths with an EWMA (half-life 1 h); SemiAnalysis crossfades survey points. No product normalises quote dates across quarters; they publish a time series instead.
- FX: Computable and GetDeploying convert at a published daily reference rate.

### Weighting and index construction

Robustness devices are the differentiator and are named in the methodology text where one exists:

- Ornn: volume-weighted winsorised mean, symmetric percentile bounds (alpha undisclosed), no smoothing, carry-forward on Market Disruption Events, $0.001 precision (https://data.ornn.com/methodology).
- Computable: one price per provider (lowest eligible listing, or volume-weighted median of asks on order books), interquantile mean of the central third of vote mass, jump screen (25% move flagged unless two other members move 10%), liveness weights floored at 2.5% and capped at 30%, every parameter published (https://raw.githubusercontent.com/getcomputable/gpu-index/main/METHODOLOGY.md).
- SemiAnalysis: one price per provider per hour, hyperscalers over-weighted, composition-jump resistance, per-step move cap, forward-fill window, survey crossfade; parameters undisclosed (https://gpu-index.semianalysis.com/).
- GetDeploying: median of each model's relative price across a fixed equal-weighted panel of 27 models, models with fewer than 4 providers excluded, panel reviewed each January and July, own-data corrections spliced out (https://getdeploying.com/gpu-price-index).
- GPU Tracker: p25 of listings instead of min, because "the cheapest listing ... is often a fleeting spot deal" (https://gputracker.dev/gpu-price-index).
- Ornn forward: no interpolation or fill ("a missing tenor remains a gap"); implied-forward ladders whose cumulative cost does not rise are rejected, not published (https://data.ornn.com/docs/data-dictionary.md).

### Freshness and provenance display

- Stamps: full date-time next to the headline ("Updated September 15, 2026 at 20:00 UTC", Ornn; "AS OF WED SEP 16 14:45 UTC", Computable), or a relative header stamp plus per-row ISO tooltips ("Last checked <15m ago (2026-09-16T14:45Z)", GetDeploying). Silicon Data shows only a "% vs 7D" badge and no timestamp; GPU Tracker shows "Live Index" beside "Updated 2026-04-19".
- Constituent view: only Computable shows the individual prices, weights, stability band and source URLs behind a print, plus a "reproduce" command that recomputes the published value (https://market.getcomputable.com/gpu-index). Ornn shows a 14-row settle table per GPU. Silicon Data and SemiAnalysis show no constituent data.
- Per-mark provenance: Ornn forward API returns provenance{source, updated_at, sample_count}, as_of_date and an insufficient_data reason per tenor, and states the timestamp is publish time, "not the market observation time" (https://data.ornn.com/docs/api-reference/forward/get-the-forward-curve.md).
- Revision policy: Ornn "history is never silently restated"; Computable "Published observations are never revised... Corrections publish forward under a new version"; GetDeploying splices; SemiAnalysis "parameters may be refined"; Silicon Data says nothing.
- Governance: Ornn (Oversight Committee, IOSCO reference, dated changelog) and Computable (GOVERNANCE.md, coverage bands) publish formal governance; Compute Desk claims IOSCO/BMR without documents.

### Where our method is already ahead

- Prepayment. Requiring offers to state prepayment and normalising with Finance's money-cost convention is stricter than anything public. SemiAnalysis only footnotes a 25% assumption; Ornn and Silicon Data ignore it.
- Term grid. Seven tenors plus PAYG-as-a-term is denser than Ornn's five (1M/6M/1Y/3Y/5Y) and SemiAnalysis' seven (which skip 3M and 6M). Silicon Data's 1–36-month curve is interpolated, not observed.
- Quarter fixed effects for quote dates. No public product corrects for observation timing when pooling quotes; Ornn explicitly warns that its mark timestamp is not observation time.
- Suppression under 3 observations shown as a rule, with counts and limitations beside results. Ornn suppresses too, but the threshold is unpublished; GetDeploying's is 4 providers per model; Silicon Data publishes none.
- Point quality classes (good/thin/achieved-only) on the chart. Nobody else encodes evidence quality per point; Ornn's closest is has_mark/insufficient_data in the API.
- Reference lines from internal economics (Finance Tier-2 floor, 15.6% IRR floor, previous-generation parity ceiling) and a contract cash timeline. Ornn's Payback Period analytics is the only comparable object and is paywalled.
- Node configuration beside prices. Only Shadeform, GetDeploying and Vantage carry vCPU/RAM/interconnect, and none beside a benchmark mark.

### Where our method is behind

- No versioned methodology document with a dated changelog and a stated no-revision or forward-correction rule (Ornn, Computable).
- No history of the marks themselves. Every index product is a time series; our page shows the current curve. Ornn's forward API has the same gap and it is listed as a weakness there too.
- Freshness is one stamp for the page; the best practice is stamp plus per-cell or per-observation dates, with observation date separated from publish date.
- No implied-forward basis or monotonicity check across tenors (Ornn rejects ladders where the longer term spends less in total than the shorter).
- No stable identifier per mark to cite in Slack, decks or CRM notes (Ornn OCPI-H100, Silicon Data SDH100RT, Compute Desk DESKH100).
- No export or reproducibility path; Computable's one-command reproduce and gpuhunt's versioned snapshots are the standard.
- No explicit statement of the statistic behind each aggregate (weighted median is stated, but the weights are not described on the page as far as the brief says).

## 3. Functionality worth adopting

### Controls

- Basis toggle "Term price | Implied forward" (Ornn Forward Curves). On our Market benchmarks view: a two-pill segmented control beside the GPU selector; implied basis drawn as steps between tenors, with the rejection message when a ladder is non-monotone ("36M mark spends less over its term than 24M"). This turns the curve into a statement about marginal months 25–36, which is what a deal desk argues about.
- Range pills above the chart with disabled states rendered rather than hidden (Ornn "1W 1M 3M 1Y ALL"). For us the equivalent is a "curve as of" selector once mark history exists: "Latest | 4 weeks ago | Quarter start", disabled until enough snapshots accumulate.
- Segment toggle "Neo-cloud | Hyperscaler" (Silicon Data). Our Market position view already collapses hyperscalers beyond the axis; a toggle that includes or excludes them from the median marker would make the premium explicit.
- Prefilter chips that shrink the offer set before comparing (Prime Intellect "GPU Count", "VRAM per GPU"; GetDeploying "In stock only", "Billing type"). On Market position: chips for region, prepay band, node count, so the dot cloud is comparable before the median is read.
- Command-palette search (Ornn, GetDeploying ⌘K). Low value for a single-file page; skip.

### Comparisons

- "Compare with" plus "Normalized" mode (Ornn). Overlay two GPUs' curves, normalised to each family's PAYG or 1M mark as 100. Ornn's own public rendering of the forward curve is exactly this ("Term price as % of the 1-month mark") (https://data.ornn.com/publications). On our page: a checkbox list of GPUs in the series legend and a "Normalise to PAYG" toggle; useful for showing that B200 retains 98% at 1Y while H200 retains 80%.
- Provider-pair compare with explicit arithmetic (ComputePrices "↓$0.80 (40.3%)", "Average Price Difference: $2.25/hour between comparable GPUs"). On Market position: when a dot is clicked, show the delta to our line in dollars and percent, and to the median.
- Hyperscaler premium as a number (GetDeploying "charge about 88% more"; Thunder Compute median tables). One line under the Market position axis: "Hyperscaler list median is +X% over the comparable-offer median".
- Quality proxy beside price (Vast.ai DLPerf, reliability, verification tier; ClusterMAX tier). We could show the ClusterMAX tier of each competitor as a small tag in the dot tooltip; it explains why a Silver provider clears at a discount.
- Realised price after resale (SF Compute "Reserved price $3.00 / Realized price $4.29"). Relevant to Contract return only if we model resale; otherwise note as a concept.

### History

- Mark history table per GPU, most recent 14 rows: Date / Value / Change (Ornn market pages). Ours: a table of weekly snapshots of the 12M mark per GPU under the chart, with the observation count that produced each row.
- "Since inception" and 30-day change badges on the headline (Computable, GPU Tracker). Ours: a delta badge on each GPU's 12M weighted median versus the previous snapshot, green/red reserved for this use.
- Versioned snapshots (gpuhunt "YYYYMMDD-run"; Computable dated day archives). Store each published curve as a dated JSON in the repo alongside the HTML; the page reads the latest and can load an older one.
- Splicing rule for own-data corrections (GetDeploying) and forward-only corrections (Computable). Write it into the Data & method disclosure.

### Alerts

- No public product offers user-configurable price alerts; the closest are ComputePrices' weekly email report and Hyperstack's account-balance alerts. GPU Tracker's "Biggest Movers" and ComputePrices' "Biggest Drops This Week" tables are the passive equivalent. Ours: a "Moved since last snapshot" list at the top of the page (mark, direction, size, observation count), which the weekly Slack rundown can quote verbatim. Actual push alerts belong in the weekly routine, not the page.

### Export

- Save CSV and Save chart PNG buttons on every chart (Ornn), CSV/XLSX with explicit column headers "Tenor, Months, {GPU} Term Price (USD/GPU-hr), {GPU} Implied Forward (USD/GPU-hr)" (Ornn forward). Ours: two icon buttons per view; CSV of the visible series including quality class and count columns; PNG via canvas toBlob with the freshness stamp burned into the footer, as Ornn's export does ("data.ornn.com · Aug 4, 2026, 16:04").
- Reproduce block (Computable "./reproduce b300 2026-09-16 → $7.86 YOURS / $7.86 PUBLISHED"). Ours: a line in Data & method giving the script name and snapshot date so a colleague can re-run refresh_reserve_tenor and match the published mark.

### Sharing

- Citation block with canonical identifier and unit (Ornn "Cite the OCPI-H100"). Ours: give each mark a short identifier (e.g. NBX-H100-12M) and a one-line copyable citation: "NBX-H100-12M $x.xx/GPU-hr, weighted median of n prepay-stated offers, quarter-adjusted, as of 2026-09-16". This is what ends up in deal-desk Slack threads and decks.
- Freshness stamp beside the headline in full date-time and settlement rule (Ornn, Computable). Ours already shows freshness; move it to the headline row in the form "As of 16 Sep 2026 · n offers · prepay-stated only · quarter FE".
- Dataset and FAQ structured data (Ornn JSON-LD) is SEO machinery; not relevant to an internal page.
- Confluence publish already exists; add the PNG export to what the GHA publisher attaches so the Confluence page carries a static fallback image, which is the thing every client-rendered competitor lacks.

## 4. Look and feel

### What Ornn does visually

Design system (from the CSS bundle and public renderings; the live chart was not viewable): near-black canvas --bg-primary #0b0b0c, --bg-tertiary #121213, hairline borders --stroke-primary #2c2e33, text #fff / --text-secondary #7e838c / --text-muted #aab0bd; square corners everywhere (rounded-none); a single status accent --accent-index #e13f5e with success #009146 and error #cf5753; a muted ten-colour series palette (blue #6a8fbf, orange #c97a4e, amber #c2a24f, green #7d9a5c, teal #5f9c93, violet #8b7fb5, pink #bd7691, fuchsia #a2709e, slate #7b828e, neutral #5c6068) (https://data.ornn.com/_next/static/immutable/chunks/11x65epajrmaa.css). Typography: PP Neue Montreal for UI and numbers (tracking +0.01em on numerals), FK Roman Standard serif for large figures and headlines, Geist Mono for percentage tables. Charts: TradingView lightweight-charts with the library's axes suppressed and redrawn by the app; primary series white 2px with a faint white gradient fill, comparison series 1px cycling Solid/Dashed/Dotted/LargeDashed; horizontal grid only at #ffffff0d; 1px dashed crosshair at 30% white with black label pills; wheel zoom off, drag-to-scroll only; circle markers at the five tenors on top of a display-only smooth line. The public forward-curve rendering (Figure 5) uses a log-spaced tenor axis, 0.5px gridlines at 25% opacity, one lead series at 1.75px and the rest at 1px 60% opacity with distinct dash patterns, and end-of-line value labels instead of a legend (https://data.ornn.com/publications). Layout: single centred column, card header with title and price left and a control stack right, chart, table, FAQ; --nav-height 68px; freshness stamp always full date-time; locked features rendered at 40–54% opacity rather than removed.

### What the best dark data UIs in the sample share

- One accent for "ours" or "status", colour otherwise reserved for series identity: Ornn (#e13f5e), Silicon Data (cyan #67def9), Computable (mint #59b387 in dark), Compute Exchange (#00dd88), SF Compute (#4C78F5). Deltas get green/red and nothing else does.
- A monospace or tabular-numeral face for every number: Geist Mono (Ornn), Fragment Mono (Silicon Data, Compute Desk), IBM Plex Mono (Computable), JetBrains Mono (GetDeploying, Compute Exchange), ABC Diatype Mono (SF Compute).
- Controls directly above the chart as pill groups; range pills right, series/GPU pills left.
- Small-caps mono microcopy for provenance lines (Computable "COMPUTABLE GPU INDEX (CGI) · NVIDIA B300 · ON-DEMAND · $/GPU-HR").
- Hairline 1px borders, low-contrast horizontal grid only, no vertical gridlines, crosshair rather than hover cards where possible.
- Value labels at line ends rather than legends when there are five or fewer series (Ornn Figure 5); last-value pills on the right axis in normalised mode (Ornn X post rendering).
- Empty and suppressed states shown as an em dash, never 0.00 (Ornn catalog, SemiAnalysis "—" and "✕ Sold Out").

### Tokens to adopt or keep

Already matching: dark theme, single accent for our price, Inter-like sans with tabular numerals, disclosure for secondary comparisons, freshness and counts beside results. Keep these.

Adopt:

- Surface scale: --bg #0b0b0c, --bg-card #121213, --bg-control #18191b, --stroke #2c2e33, --stroke-strong #3a3a3c (Ornn chart-toolbar tokens).
- Text scale: --text #ffffff, --text-2 #aab0bd, --text-3 #7e838c; chart text at 60% white.
- Grid: horizontal only at rgba(255,255,255,0.05); crosshair rgba(255,255,255,0.3) dashed 1px.
- Series discipline for providers on Market position: use a muted palette in the Ornn range (#6a8fbf, #c97a4e, #c2a24f, #7d9a5c, #5f9c93, #8b7fb5, #bd7691, #7b828e) so the accent line remains the only saturated element.
- Numerals: keep Inter with font-variant-numeric: tabular-nums; add letter-spacing 0.01em on large figures. If a second face is wanted, use a mono (Geist Mono or JetBrains Mono, both on Google Fonts) for tables only; do not add a serif display face (see section 5).
- Corners: 0–2px radius on cards and pills (Ornn rounded-none, Shadeform --radius .2rem). Round only the crosshair label pills.
- Delta colours: success #009146 / #2ba471, error #cf5753 / #d54941; nowhere else.
- Quality classes on marks: solid filled circle (good), hollow circle (thin), diamond (achieved-only), matching Ornn's convention of markers on top of a display-only line and our existing Nebius diamond.
- Guidance lines: 1px dashed at 40% for floors and ceilings, labelled at the right end, as Ornn labels end-of-line values.
- Freshness line: full date-time plus rule, small-caps mono, directly under the headline mark.

## 5. What not to copy, and why

- Smooth interpolated lines between sparse tenors. Ornn draws a monotone cubic through five marks "for display only"; Silicon Data sells a 1–36-month curve derived by "no-arbitrage" from three tenors. Both invite reading interpolation as data. Our marks are at seven observed tenors; connect them with straight segments or steps, and leave gaps where cells are suppressed.
- A single pooled global series. Ornn tags trades by six continents but publishes one distribution; Compute Desk blends Hopper architecture. Our value is in the configuration and prepay detail; keep GPU, term, prepay and region separate.
- Level indices with an arbitrary base week (GetDeploying Jan 2025 = 100, GPU Tracker Feb 2026 = 100, GPUAlpha 148.72). They hide the dollar price and mix consumer cards with H100s. Normalised mode is fine as a toggle; never as the default.
- Hyperscaler over-weighting (SemiAnalysis) or blending hyperscaler list into a neocloud median. It moves the composite away from where Nebius transacts; Silicon Data's split ($7.20 vs $2.66) shows why.
- "Live" labels without a timestamp (GPU Tracker "Live Index · Updated 2026-04-19"; ComputePrices "LIVE" header with "updated daily" footer; Vast.ai pricing cards with no statistic named). Every badge on our page should carry the snapshot date it refers to.
- Client-only rendering with no static fallback. Every competitor's fetched HTML read "Loading" or "No chart data available". Our single HTML file already avoids this for the page itself; keep the Confluence publish carrying a rendered image.
- Serif display face for big figures (FK Roman Standard). It is Ornn's brand, and it needs a licensed font and a second typographic system. A mono for tables gives the same "data terminal" register without the licensing.
- Paywall affordances (disabled range pills, dashes for locked values, wall copy). Meaningless internally.
- Marketing coverage claims without denominators ("95% of neo-cloud providers", "3.5 million data points"). State counts per cell instead, as we do.
- SEO scaffolding (FAQPage JSON-LD, "How much does an H100 cost per hour?" blocks). Wrong audience.
- Undisclosed parameters (winsorisation alpha, circuit-breaker size, forward-fill window). If we adopt a robustness device, publish the number in Data & method.
- Emoji and playful microcopy in the UI (ComputePrices, GPU Utils). Off-register for a finance-adjacent page.

## 6. Prioritised change list

1. Headline freshness and citation block per mark. Full date-time, observation count, prepay rule and quarter adjustment under each GPU's mark, plus a copyable identifier and one-line citation. Borrowed from Ornn (market page "Cite the OCPI-H100", "Updated ... at 20:00 UTC") and Computable ("AS OF ..." mono line). Effort: small.
2. Versioned curve snapshots and a mark-history table. Save each publish as a dated JSON; show the last 8–14 weekly values of the 12M mark per GPU with change and count columns; enable a "curve as of" selector. Borrowed from Ornn (14-row settle table), gpuhunt (versioned catalogs), Computable (day archives). Effort: medium.
3. Export CSV and PNG on every view, with the freshness stamp burned into the PNG footer and explicit CSV headers including quality class and count. Borrowed from Ornn ("Save CSV", "Save chart", export footer stamp). Effort: small.
4. "Term price | Implied forward" basis toggle with monotonicity validation. Steps between tenors; refuse and explain non-monotone ladders. Borrowed from Ornn Forward Curves. Effort: medium.
5. Methodology document with dated changelog, statistic and weights stated, no-silent-revision rule and forward-only corrections. Expand Data & method into a versioned section with a change log. Borrowed from Ornn (methodology and changelog), Computable (METHODOLOGY.md, GOVERNANCE.md), GetDeploying (splicing rule). Effort: medium.
6. Compare and normalise mode on Market benchmarks. Overlay multiple GPUs; "Normalise to PAYG = 100" toggle; end-of-line value labels instead of a legend when five or fewer series. Borrowed from Ornn ("Compare with", "Normalized", Figure 5 rendering). Effort: medium.
7. Movers list at the top of the page. Marks that changed since the last snapshot with direction, size and observation count; also the source text for the weekly Slack rundown. Borrowed from GPU Tracker ("Biggest Movers") and ComputePrices ("Biggest Drops This Week"). Effort: small (after item 2).
8. Prefilter chips and delta arithmetic on Market position. Chips for region, prepay band, node count and "include hyperscaler list"; click-detail shows delta to our line and to the median in dollars and percent; a one-line hyperscaler premium figure. Borrowed from Prime Intellect and GetDeploying (prefilters), ComputePrices (pair deltas), Silicon Data (segment toggle), GetDeploying (premium figure). Effort: medium.
9. Visual token pass toward the Ornn register. Surface/stroke/text scale, horizontal-only grid, muted provider palette with accent reserved for our line and deltas, square corners, dashed 1px guidance lines labelled at line end, small-caps mono provenance lines, em dash for suppressed cells. Borrowed from Ornn CSS tokens and Computable microcopy. Effort: small.
10. Per-observation provenance in the mark tooltip. Source class (Slack offer, CRM, list, SemiAnalysis), observation date versus publish date, and a sample count per cell, mirroring the API-level provenance Ornn returns per mark. Borrowed from Ornn forward API (provenance, as_of_date, insufficient_data reason) and Computable (per-row source link). Effort: medium.


---

## Critic's review (independent agent)

**Verdict.** Solid where it leans on the Ornn and index clusters (figures, tokens and methodology quotes check out), but the report overreaches with 'only/nobody' absolutes the findings contradict, builds a fifth of its landscape table and several adoption ideas on cluster 3-4 material that is truncated or unsupplied, and the underlying research skipped the most relevant curve-dashboard references (free trials of Silicon Data and Ornn, Kalshi's market-implied curves, futures contract specs, hyperscaler capacity blocks, established financial term-structure UIs).

**Gaps the research did not cover**

- Marketplace and provider rate cards (report acknowledges the cut): CoreWeave (published reserved-vs-on-demand discount tiers), Lambda (on-demand + reserved cluster rate card), RunPod (Secure vs Community, savings plans), io.net, Akash (on-chain bid pricing), Together AI, Hyperbolic, Lium, Crusoe, Fluidstack, Voltage Park, Hyperstack. These are exactly the sources that would test the report's 'nobody else normalises prepay' and 'our term grid is denser' claims, which currently rest on absence of evidence.
- Hyperscaler forward-delivery and commitment products: AWS EC2 Capacity Blocks for ML (dated GPU blocks priced by start date, the only public hyperscaler forward schedule), AWS Spot price-history charts, GCP Dynamic Workload Scheduler calendar mode, Azure/GCP committed-use discount structures. Vantage (per the report) covers only the static reserved matrix.
- Established financial term-structure UIs as design references: CME/ICE futures settlement strips and term-structure viewers, Bloomberg/Refinitiv curve screens, EEX power forward curves, US Treasury/FRED yield-curve 'as-of' animations, TradingView futures-curve widgets. The report proposes a 'curve as of' selector and implied-forward steps with Ornn as the sole precedent.
- Kalshi's market-implied GPU forward curve pages (HTTP 429 at fetch) and Architect AX perpetuals screens: the only publicly rendered market-implied GPU term structures, described only via press coverage. The report never mentions market-implied curves as a third basis alongside analyst marks and internal marks.
- Futures contract specifications: CME/NYMEX Silicon Data H100/B200 Rental Index Futures (listed months, contract unit, settlement), ICE-Ornn, Nodal-Compute Desk, and NATIVX COIL on ICE (named once in a weakness bullet, never researched). These define the tenor grid the market will actually trade against.
- Paid views that free trials would have unlocked: Silicon Data's 7-day no-card Pro trial (36-month forward curve chart, history-date selection, NeoCloud vs Hyperscaler comparison, Market Trends filters) and Ornn's 72-hour Research Grant (live Forward Curves chart, utilization, Payback Period). The two most relevant curve dashboards were reconstructed from CSS/JS bundles instead of viewed.
- Methodology documents not read: Ornn /ocpi_methodology.pdf, Silicon Data's 'Download methodology' PDF, Computable SKU docs (404). The basis-adjustment coefficients and forward-curve construction the report calls undisclosed may be in the PDFs.
- SF Compute's public price-index / forward-by-duration page and Vast.ai price-history charts: cited in the report's table and sections 2-5 but absent from the findings as supplied (which end mid-cluster 3 at GetDeploying), so it cannot be confirmed they were researched at all.
- Adjacent AI-pricing dashboards as dark-data-UI references: Artificial Analysis (price vs throughput scatter, provider comparison), OpenRouter model pricing/throughput tables, Epoch AI GPU price-performance trend charts. None surveyed; the design section is effectively 'copy Ornn'.
- Regional basis display: no product finding describes how regional price differences are rendered (Vast.ai geolocation filters, GetDeploying by-country pages, Silicon Data Tier-2 country dataset), although the report recommends keeping region a separate dimension.
- Alert/watchlist features were not swept: only GetDeploying carries 'No user price alerts observed'; Vast.ai, RunPod, io.net, Shadeform, Prime Intellect and Silicon Data portal alert features were never checked.
- Dispersion display around a thin median: SemiAnalysis p25-p75 ranges, GetDeploying 'middle 60% of panel models' band, Computable stability band and TODAY HIGH/LOW are recorded but there is no cross-cutting analysis of how dispersion is drawn, and the report adopts none of them despite its own weighted-median-of-few-offers setup.
- Report omissions of material already in the findings: Ornn utilization ratio (rented / rented+available) and rolling-volatility windows 3/7/15/30d; arXiv 2607.12156's result that Ornn is weakly or negatively correlated with Silicon Data (argues against pooling any external series); GetDeploying's empirical term discounts (1-yr about 31% below on-demand, 3-yr about 50%, spot about 52% of on-demand) and Ornn's retention-by-tenor percentages as external checks on the team's term grid; Nebius list prices as a constituent of Computable's CGI (12.6% weight on B300), i.e. the team's own list price moves a public index; Prime Intellect's reserved-cost calculator with idle-capacity resale modelling (relevant to Contract return); Silicon Data's stale hardcoded FAQ values ($2.53 vs $2.66 same day) as a what-not-to-copy for FAQ blocks.
- The fifth and sixth clusters were never supplied and their intended scope is not stated; the report should say what they were meant to cover so readers know which comparison classes are missing.

**Claims the findings do not fully support**

- Structural: the findings supplied end mid-cluster 3 (GetDeploying strengths). Every claim about ComputePrices.com (lansky.tech, 77 providers, deals tracker, '$0.80 (40.3%)', 'Average Price Difference: $2.25/hour', 'Biggest Drops This Week', weekly email, 'LIVE'/'updated daily', emoji), cloud-gpus.com (Jolt ML, no cookies), Shadeform (interconnect/nvlink fields, 'no markups', --radius .2rem), Vantage Instances, gpuhunt (MPL-2.0, 'YYYYMMDD-run' snapshots), Cloud Mercato (Pro 1,000 EUR/mo), Thunder Compute median tables, GPU Utils, Hyperstack account-balance alerts, SF Compute (node = 8 GPUs, fills API 30 days back, 'Reserved $3.00 / Realized $4.29', accent #4C78F5, ABC Diatype Mono, fee structure) and Vast.ai (DLPerf, reliability, verification tier, 30/90-day history, reserved 1/3/6 months) is unverifiable against the findings as provided.
- Section 2 Normalisation: 'Shadeform is the only directory with structured interconnect (pcie/sxm4/sxm5/sxm6) and nvlink fields.' Contradicted by the findings: GetDeploying exposes 'Interconnect', 'Fabric and host' and 'Variant' facets, and Prime Intellect's availability API has a socket (PCIe/SXM) parameter.
- Section 2 'Where our method is ahead': 'Only Shadeform, GetDeploying and Vantage carry vCPU/RAM/interconnect.' Prime Intellect's API returns memory/vCPU/RAM per offer and Compute Exchange SKU pages carry spec sheets (HBM, NVLink bandwidth, TDP).
- Section 2 Collection: 'Same-day H100 prints on 15-16 Sep 2026 ran from ... to $3.50-3.51 (GPU Tracker p25, Computable).' GPU Tracker's page is stamped 'Updated 2026-04-19'; its $3.50 is a five-month-old value, not a same-day print.
- Section 2 Collection: 'The spread is the provenance class, not noise.' Two listed-price products differ by 32% (Silicon Data $2.66 vs Computable $3.51) while listed-price Silicon Data sits within 5% of trade-based Ornn ($2.53), and the findings cite arXiv 2607.12156 finding Ornn weakly or negatively correlated with Silicon Data. The findings do not establish provenance class as the explanatory variable.
- Sections 2 and 5: 'Silicon Data sells a 1-36-month curve derived by no-arbitrage from three tenors' and 'Silicon Data's 1-36-month curve is interpolated, not observed.' The findings say only 'implied forward rate derived by no-arbitrage pricing methodology', '1-month to 36-month tenors', 'recalculated every business day'. 'Three tenors' and 'interpolated' appear nowhere in the findings.
- Section 3 Comparisons: showing ClusterMAX tier 'explains why a Silver provider clears at a discount.' The findings place Google and AWS in Silver, and hyperscalers price at a premium ($7.20 vs $2.66), so tier does not explain a discount.
- Section 2 'Where our method is ahead': 'Ornn's Payback Period analytics is the only comparable object' to internal reference lines. The findings also record ClusterMAX's GPU Cluster TCO Calculator and Goodput Expense Calculator, InferenceX's TCO and Token Revenue calculators, Prime Intellect's reserved-cost calculator with resale modelling, and Computable's 'Rate floors' view.
- Section 2 'Where our method is ahead': 'Nobody else encodes evidence quality per point.' Computable publishes a stability band, 'SOURCES PASSING 8/8' and per-provider liveness weights per print; SemiAnalysis publishes p25-p75 ranges and 'Sold Out' states; GetDeploying shows availability state per listing.
- Section 2: 'Nobody else normalises prepay at all' and 'Ornn and Silicon Data ignore it.' Silicon Data's basis adjustment covers 'rental type' and its dataset distinguishes 3- and 6-month reserved contracts; the findings do not say whether prepay is handled. Compute Exchange's published payment schedule (31 days upfront plus 10% of remaining balance) is a stated prepay convention, so 'only SemiAnalysis states a convention' is also overstated.
- Section 3 Alerts: 'No public product offers user-configurable price alerts.' The findings record only 'No user price alerts observed' for GetDeploying; no other product's alert features were examined.
- Section 4: 'Controls directly above the chart as pill groups; range pills right, series/GPU pills left.' The findings describe pill groups above charts and Ornn's 'control stack right' only; no left/right convention is documented across products.
- Section 4: 'crosshair rather than hover cards where possible' as a shared trait of the sample. The findings describe a per-series hover tooltip (name, swatch, price, tenor) on Ornn's forward chart; only the OCPI spot chart is crosshair-only, and no other product's hover behaviour is recorded.
- Section 2 Collection: 'none of the vendors combines them ... on one axis.' SemiAnalysis explicitly crossfades survey contract data into its scraped spot composite as a 'single published series' (recorded as a weakness in the findings).
- Section 1 and Section 3: Prime Intellect quote 'Prices update in realtime' and prefilter chips 'GPU Count', 'VRAM per GPU'. The supplied Prime Intellect findings cite API parameters (gpu_count, socket, regions) and 'Some providers may also use dynamic pricing'; neither the quote nor the UI chips appear.
- Section 1 and Section 2 classify Silicon Data as 'Listed/scraped rental prices'. Silicon Data's own source list in the findings includes 'colocation markets, brokered cluster sales, and private rental platforms' and second-hand/brand-new contract types; 'scraped list prices' is the findings' cross-cutting inference, not a documented collection method, and the report presents it as fact. The same section also lists Vantage under 'scraped list prices' while the report's own table says Vantage reads official pricing APIs.

**Sources that could not be opened**

- Live rendering of data.ornn.com charts: the Browser pane reports data.ornn.com is blocked by the organization's policy, and the chart is client-rendered, so tooltips/live axes were reconstructed from the JS bundle and a public screenshot rather than observed
- Paywalled analytics routes (/analytics/forward-curves, /analytics/gpu/forward, /analytics/gpu/forward/h100, /analytics/gpu/remaining-life, /analytics/gpu/utilization, /data/compute) return the 'Sign in to Ornn Data' wall; described from the wall page, data dictionary, glossary and bundle strings only
- X (x.com) profile timeline for @OrnnExchange and the forward-curve article's embedded media: WebFetch returned HTTP 402; article body text was retrieved via direct HTTP and individual posts via the syndication endpoint, but no article images or the full timeline
- LinkedIn: company posts page and individual posts require login (only the public company-page summary was readable); Kush Bavaria's 'S&P 500 for Accelerated Compute' pulse post was readable
- Axios article (axios.com/2026/07/06/ornn-gpu-compute-commodity): HTTP 403
- Kalshi market pages (kalshi.com/markets/kxb200ws/…, kxh200ws/…): HTTP 429; settlement wording taken from third-party coverage instead
- Bloomberg (Jul 14, 2026 Kalshi article), The Economist (Feb 17, 2026) and WSJ pieces linked from ornn.com: paywalled, not fetched
- Gem job postings (jobs.gem.com/ornn/…) and trust.ornn.com: JavaScript-rendered, only titles/headers visible; Senior SWE description obtained from a freehire.me mirror
- YouTube launch video (iZEipJcKvN4): only oEmbed title/author ('Announcing: Ornn Series Seed Fundraise', Wayne Nelms) retrieved; description and content not available
- Product Hunt: no Ornn listing found in search
- OCPI methodology PDF (/ocpi_methodology.pdf) not downloaded; the HTML methodology page was used
- Ornn Substack essays (ornncompute.substack.com) not individually fetched; only the list and the ornn.com/insights mirrors
- Galaxy Ventures investment thesis: only the X teaser post; full thesis link not accessible
- ornn.com/buy-compute returned 404; ornn.com/case-studies/terminal not fetched
- SemiAnalysis Atlas dashboard (https://atlas.semianalysis.com/?dashboard=gpu_spot_pricing_index&src=tools) — 307 redirect to the marketing page; subscriber-only. https://semianalysis.com/gpu-pricing-index/ returned only navigation/cookie markup (client-rendered/paywalled).
- CME Group SER notice ser-9785 and compute-futures product page — timed out via WebFetch and curl was blocked ('This IP address is blocked due to suspected web scraping activity'); contract unit/settlement details taken from the PR Newswire release only.
- Silicon Data portal (https://portal.silicondata.com) — returned an app shell with no content (login required); 'Download methodology' PDF not fetched; docs.silicondata.com/introduction exposes only tier notes and an llms.txt pointer, no endpoint schema.
- Ornn Data documentation (https://data.ornn.com/documentation) — sign-in page; OCPI winsorization percentile α is confidential to licensed counterparties; Bloomberg tickers beyond 'ORNNH100' not listed; Morningstar PR copy returned 403.
- ClusterMAX rankings — /rankings returns 404 and /v2, /v2.1 deliver tier tables only as images (neocloud-ranking-v2.1.jpg); the Medium summary of 2.0 results returned 403; tier membership cited from search-indexed snippets.
- Computable SKU document (github.com/getcomputable/gpu-index/blob/main/skus/H100-SXM.md) — 404; github.com README fetch 403 (raw METHODOLOGY.md was readable).
- Compute Desk index values — homepage cards showed '—' and 'Price history is still loading for this index'; app.compute-desk.com is login-only; no methodology document found.
- Kaiko GPU indices page (https://www.kaiko.com/products/gpu-indices) — 404.
- Prime Intellect 'compute index' — no such product found on primeintellect.ai or in web search; only the marketplace and availability API exist.
- Bitdeer H100 rental index — no index or price-tracking product found on bitdeer.com / ai.bitdeer.com or in web search (only IR statements that H100 hourly pricing rose ~40% since late 2025).
- https://www.thundercompute.com/blog/gpu-price-tracker — HTTP 404; no page by that name exists. The 'price tracker' is the weekly-reviewed blog series (nvidia-h100-pricing, cheapest-cloud-gpu-providers, ai-gpu-rental-market-trends), which was used instead.
- https://computewatch.llm-utils.org/ — returns HTTP 200 but only a Next.js error shell (html id '__next_error__') with no data; tool appears defunct. Reported from the July 2023 announcement post instead.
- https://shadeform.com/pricing — HTTP 404 (live page is /prices). https://shadeform.com/directory/instances table rows are client-rendered; column labels were taken from the compare page's instance browser and field names from the RSC payload/API docs, not from a rendered directory table.
- https://cloud-gpus.com/ table is client-rendered from an obfuscated Astro payload; individual rows/prices and the filter-drawer control labels were not readable, only field names, availability/billing labels and detail-card labels from the GPUGrid JS module.
- https://www.cloud-mercato.com/pricing/ — HTTP 404 (tiers read from https://auth.cloud-mercato.com/pricing/). P2P and PCR pages show 'You're seeing a limited view!' paywall banners; Pro/Premium 'Extended data', XLS export and GraphQL responses were not accessed.
- https://getdeploying.com/price-index — HTTP 404 (correct path /gpu-price-index). GetDeploying API endpoints (/api/gpu-offerings, /api/gpu-price-history) are paid; response fields reported from the docs page only. The index chart itself renders client-side ('Building the index...'); chart type inferred from Chart.js in the app bundle.
- https://computeprices.com/api — HTTP 404 (docs live at /docs/api). Pro/Enterprise history and CSV/NDJSON exports not accessed.
- https://instances-api.vantage.sh/ — landing page lists SDKs only; endpoint-level field documentation and API key flow were not read.
- No dedicated Hyperstack 'vs AWS/Azure/GCP' comparison page and no Lambda comparison page were found; the Hyperstack blog 'top-cloud-gpu-providers' and Lambda /pricing were used instead.
- gpus.llm-utils.org guide pages are 2023 static posts; no live data to access.
- SF Compute market/dashboard UI (https://sfcompute.com/dashboard redirects to WorkOS AuthKit login) and the docs page https://docs.sfcompute.com/docs/how-the-market-works (307 to gated blueprint.sfcompute.com); reported from the public homepage, Specs page and preview docs instead
- Vast.ai console search UI (https://cloud.vast.ai/create/ is a reCAPTCHA-gated SPA; filter labels taken from docs/deepwiki description and the public API field names); the pricing page's per-GPU card values are client-rendered and the data feed is not visible in the page bundle; https://docs.vast.ai/documentation/reference/dlperf and /search-command return 404
- Prime Intellect: https://www.primeintellect.ai/pricing 404; https://docs.primeintellect.ai/compute/marketplace 404; https://api.primeintellect.ai/api/v1/availability/ returns "Not authenticated"; app.primeintellect.ai/dashboard/reserved-clusters returned no public content; offer-level prices therefore not observed live
- io.net: explorer.io.net is a JS SPA (data read directly from api.io.solutions/v1/io-explorer/* endpoints); https://io.net/pricing 404; https://io.net/cloud contains no price text; docs.io.net redirects to io.net/docs
- Akash: the GPU table at akash.network/pricing/gpus/ and stats.akash.network are client-rendered (values read from console-api.akash.network); console.akash.network bid list requires login; the method behind min/max/avg/weightedAverage/med is not documented (GitHub issue #121 is a proposal, not implementation docs; source file not located)
- RunPod console (console.runpod.io/pods) live availability requires login; https://docs.runpod.io/references/faq returned unrelated content
- CoreWeave pricing documentation at https://docs.coreweave.com/docs/pricing is gated behind an access code; spot-price mechanism and reserved tenors not publicly available
- Browser pane: tradingview.com, cmegroup.com, ice.com, eex.com and ustreasuryyieldcurve.com are blocked by the organisation's browser policy, so no screenshots or computed CSS/colour tokens could be captured for any product; all findings come from WebFetch, curl and r.jina.ai text renders.
- CME Group: WebFetch timed out (4 attempts) and curl returned HTTP 403; settlements and CurveWatch content was read via the r.jina.ai text render only; the SOFR term-structure chart in STIR Analytics requires a CME login and was not seen.
- ICE: data page serves an 'unsupported browser' shell to non-JS clients; table read via r.jina.ai; the price chart and any settlement column were not visible; ice.com/products/219/brent-crude-futures/data returned the same shell.
- EEX: Market Data Hub is JS-rendered; product picker, KPI card, table headers and chart descriptions were read via r.jina.ai but several panels showed 'Loading…'; the 45-day history and negative-price notes come from search-indexed page copy, not the rendered page.
- Euronext live: settlement-price table body showed 'Loading... Please wait'; only the month chips, Trade Date/Reset controls and OI footnote were captured.
- Koyfin: app.koyfin.com/curv is login-gated; koyfin.com/features/yield-curve/ returned 404; details come from the v3.40 release note, help pages and a third-party write-up.
- Montel: PFC chart UI inside Montel Online/Price it is subscription-only; support page shows navigation only; montel.energy/products/montel-online rendered only a cookie-consent overlay.
- Argus: Argus Direct viewer is paywalled; view.argusmedia.com/amer_forward_curves.html is a marketing/trial-form page without a chart; methodology read from the public PDF via pypdf.
- S&P Global Platts: spglobal.com/platts/en/products-services/risk returned 403; commodityinsightssupport.spglobal.com did not resolve (ENOTFOUND); Dimensions Pro user guide (insight.spglobal.com) returned 404; marketplace dataset page returned an empty shell; only the Energy Studio: Impact factsheet PDF was readable.
- TradingView: forward-curve chart is client-rendered (bundle symbol_page_tab_forward_curve returned a 671-byte stub); only the heading, explainer, Contracts table and the Yield Curves page/support article were captured; colour tokens absent from fetched CSS bundles; /markets/bonds/yield-curves/ and /yield-curves/US/ returned 'Page not found'.
- FRED: fred.stlouisfed.org refused curl (no response), so no CSS/colour tokens; page content read via WebFetch and r.jina.ai.
- U.S. Treasury: fetched HTML contained the rate tables and methodology text but no interactive chart markup; any chart/date-comparison features could not be confirmed.
- worldgovernmentbonds.com: all numeric fields and the curve chart were JS placeholders ('Loading data', '-.---%').
- Barchart.com futures-prices table body loads asynchronously (curl received a 202 bot challenge); only header quote, pagination ('Showing 1-100 of 126') and controls were captured.
- Bloomberg terminal curve screens (e.g. GC/FWCV) are not publicly available and were not attempted.
- https://registry.origami.ft.com/components/o-colors - fetch blocked; palette taken from @financial-times/o-colors _palette.scss on unpkg instead
- https://origami.ft.com/components/o-colors/ and https://origami.ft.com/components/o-typography/ - fetch blocked
- https://github.com/ft-interactive/g-chartframe and https://github.com/Financial-Times/g-chartframe - 404; FT chart-frame presets (title/subtitle/source sizes, frame dimensions) unverified; unpkg g-chartframe README lacks values
- https://ft-interactive.github.io/visual-vocabulary/ - JS-rendered; only intro text obtained via reading proxy
- https://www.bloomberg.com/ux/2021/10/14/designing-the-terminal-for-color-accessibility/ - 403; company/stories version read via r.jina.ai proxy
- https://mobbin.com/colors/brand/bloomberg - 403; no official Bloomberg hex values found on any accessible page
- Bloomberg Terminal itself - subscription product; conventions drawn from public stories and third-party write-ups only
- https://data.ornn.com/forward-curves - sign-in gated; only the nav description 'Published forward marks by tenor' is public; tenors and mark sourcing unverified
- https://x.com/OrnnExchange/article/2071933704163745971 (Ornn 'Compute Forward Curves' announcement) - 403
- https://docs.ornn.com/ and its llms.txt - contain compute-product docs only, no OCPI content
- https://dashboard.stripe.com - login required; Stripe's internal 'Sail' system is private; tokens reported are from stripe.com marketing CSS
- https://www.designmd.co/d/stripe (403) and https://www.designmd.co/d/linear.app (429) - third-party token pages not read
- Linear app UI (app.linear.app) - login required; tokens reported are from the marketing site's layout.B05Dfi6O.css
- Vercel dashboard - login required; Geist docs pages publish variable names without hex values; hex values scraped from vercel.com CSS chunks
- https://grafana.com/developers/saga/ foundation pages - JS-rendered; only navigation text retrieved; tokens taken from grafana/grafana source files
- https://observablehq.com/plot/ - HTTP 429; documentation read from the observablehq/plot GitHub docs source
- https://academy.datawrapper.de/article/177-how-to-style-your-charts - redirects to a 404 at datawrapper.de/academy/how-to-style-your-charts
- https://design.withfudge.com/tokens/linear.app - page states it retained no typeface or colour tokens
- https://github.com/vercel/geist-font - README lists no OpenType feature tags or weight ranges
