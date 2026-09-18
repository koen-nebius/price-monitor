# Neocloud coverage changes — 18 September 2026

The tracker now retains more CoreWeave, Lambda and Crusoe evidence while keeping price, quote, commercial-plan and availability claims separate.

## Source coverage

| Supplier | Added or corrected evidence | Limit |
|---|---|---|
| CoreWeave | Quote-required regional GPU products and four commercial plans, including Flex Reservations | Plan terms do not establish a tariff, GPU applicability or currently bookable capacity |
| Lambda | All six published 1-Click Cluster price tiers; two longer-term quote options; explicit quantity and duration bounds | The 256+ B200 tier is a minimum quantity; the H100 256 tier is exact. Unknown region and physical host size remain unknown |
| Crusoe | Public per-GPU tariffs, quote-only GPU/purchase-type entries and documented SKU/location footprint | Public tariffs lack a priced VM configuration. Footprint does not establish stock. Authenticated capacity remains paused |
| Aggregators | Lambda and Crusoe offers retained alongside direct observations | Matching requires configuration, location, commercial terms and source dates. Agreement can reflect the same underlying source |

Public validation on 18 September found six CoreWeave quote-required regional offers and four plan records; nine Lambda on-demand rows, six priced cluster tiers and two quote-only rows; three numeric Crusoe reference tariffs, seven quote-only entries and 19 exact SKU/location listings. These are observations within the tracked GPU scope, not a complete market census. Numeric Crusoe references stay outside benchmark prices.

Sources: [CoreWeave pricing](https://www.coreweave.com/pricing), [CoreWeave capacity plans](https://www.coreweave.com/coreweave-capacity-plans), [Lambda pricing](https://lambda.ai/pricing), [Lambda instances](https://lambda.ai/instances), [Crusoe pricing](https://www.crusoe.ai/cloud/pricing), [Crusoe VM documentation](https://docs.crusoecloud.com/compute/virtual-machines/overview/index.html).

## Quote ledger contract

The existing field-intelligence CSV and Confluence inbox remain the ingestion path. Existing nine-column inbox rows still work. To supply extra fields, include an explicit CSV header. Headers may reorder fields; the merger preserves all existing columns and atomically extends the saved schema.

The original fields are `message_ts`, `message_date`, `gpu_model`, `price_per_gpu_hour_usd`, `term_months`, `prepay_pct`, `provider_type`, `provider_name`, `notes`, plus `prepay_known`.

Optional scope fields are `quote_id`, `quote_status`, `source_url`, `source_observed_at`, `expires_on`, `instance_type`, `gpu_variant`, `region`, `gpu_count`, `gpu_count_relation`, `delivery_start`, `delivery_end`, `currency` and `tax_basis`.

- Dates use `YYYY-MM-DD`. Source observation and message/reporting date remain distinct.
- `quote_status` is `asking_price`, `signed_deal` or `unknown`; quantity relation is `exact`, `minimum` or `unknown`.
- A price remains USD per GPU-hour. Non-USD evidence requires a documented conversion before entering that numeric field.
- `prepay_known=1` requires an explicit amount. Missing prepayment, monthly billing and unrelated zero percentages do not establish zero upfront payment.
- A qualified record needs known evidence URL, configuration, native region, quantity, term, currency/tax basis, prepayment and delivery window, plus a source observation within the 90-day review window. Asking prices also need unexpired validity. Qualification establishes field completeness, not independent validation or account availability.
- Expired records and explicit signed deals do not enter asking-price floors or the forward-curve bid. Signed observations remain separately inspectable. Older unclassified intelligence retains its existing reference treatment and is not retrospectively labelled as a quote or a signed deal.
- Repeated `message_ts` entries remain append-only deduplication. Corrections must be recorded as a new dated source observation; the merger does not rewrite the evidence behind an existing message.

## Outputs and operational behavior

`catalogue.json` holds unpriced products, unscoped tariffs and commercial plans. `quote_coverage.json` holds scoped quote evidence and missing-field reasons. Both feed separate report sections. The run manifest retains configuration-level price cross-check results and collector component health.

Coverage now shows fresh price cells, quote-only cells, qualified asking and signed observations, and review gaps for the three priority suppliers. The combined coverage preview adds dated, scoped direct-instance availability separately from footprint. A cell is one supplier/GPU/native-region/purchase-type combination; repeated feeds count once. These counts are not market coverage percentages or inventory quantities.

Partial Lambda collections do not overwrite the last complete price cache. Successful catalogue-only collections do not resurrect obsolete numeric prices from cache. SkyPilot rows with no upstream timestamp remain undated references even when downloaded successfully. Crusoe public documentation runs independently of paused authenticated capacity.

Daily snapshots retain the complete offer ladder. The historical trend CSV remains a selected-cheapest-offer summary, now retaining quantity and duration semantics for short reservations. It must not be used as the complete configuration catalogue.

Offline report rebuilding uses the saved retrieval timestamp, reads evidence from the selected store, excludes later observations and preserves the source files. Rebuilding artifacts does not imply publication or successful scheduler execution.

## Validation and release state

Public source parsing, offline fixtures, pipeline integration, quote CSV migration, freshness and scope exclusions, and Confluence-compatible XML have been checked locally. No authenticated provider request, reservation or provisioning was made. Production snapshots are unchanged. GitHub deployment and destination publication remain separate from this local implementation.
