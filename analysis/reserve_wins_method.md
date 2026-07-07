# Reserve wins: methodology

What the "Reserve wins" section (Monday Slack thread + Confluence page) shows and
why, so the numbers can be defended and reproduced. Built 2026-07-07 (Koen +
Claude); rendered by diff.py from store/reserve_wins.csv; refreshed weekly
(Sunday 18:00 London) by the local scheduled task "refresh-reserve-wins".

## Definition
Rolling 30-day lowest / median / highest $/GPU-hour of Nebius' SIGNED reserve
deals, per GPU model and term bucket, with deal and GPU counts, and the median
compared to the matching Nebius committed list tier (512+ GPUs, 100% upfront).

## Source
`//home/dwh/nemax-prod/data/cdm/crm/deal_review_line_items_enriched`
(CRM deal-review line items). Price field: `unit_price_calculated` ($/GPU-hr).
GPU model: `gpu_model_canonical`. Quantity: `resource_quantity`.

## Inclusion rules (and why)
1. `close_utc_dttm` within the last 30 days AND deal stage contains "won"
   — only NEWLY CLOSED deals. Grandfathered contracts and long-running deals
   never enter; the metric is "what the market pays us NOW", not book average.
2. `autorenewal != 'Yes'` — automated renewals carry legacy rates forward at
   unchanged prices; they are not market evidence. (First pull: 3 H200
   autorenewals at $2.00 would have dragged the H200 read from -2% vs list to
   -10%.) Renegotiated renewals close as normal deals and stay included.
3. `consumption_type_slug` contains "reserve" — reserve/committed only, no PAYG
   line items.
4. Term buckets from consumption start/end: <=8mo / ~1yr (9-18mo) / 2yr+ (>18mo).
   Terms are never blended: a 3yr price is structurally lower than a 6mo price,
   and each bucket compares to its own list tier (9mo / 12mo / 24mo).
5. Price sanity bounds: 0.2 < unit_price_calculated < 20 (drops obvious unit
   errors, e.g. monthly or total amounts landing in the hourly field).
6. Median is per LINE ITEM (not GPU-weighted); deal count = distinct deals;
   GPUs = sum of resource_quantity. A single mega-deal therefore shifts the
   GPU column, not the median.

## Confidentiality
Aggregates only, never customer names or deal-level rows: reserve contracts
commonly carry price-confidentiality clauses, and the Slack channel + Confluence
page audience is wider than CRM deal permissions. Deal-level detail lives in
HubSpot for those with access.

## Known limitations
- `unit_price_calculated` is a CRM-derived field; validate surprising values
  against a real contract (open item: B200 short-term median ~$2.45, 2026-07-07).
- Deals closed now with consumption starting months out are included by design
  (they ARE current market prices), so wins can pre-date fleet availability.
- 30-day windows on few deals are noisy: read deal counts, not just medians.
- Refreshed weekly; the page shows a warning when the CSV is older than 14 days.
