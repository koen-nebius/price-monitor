# Reserve wins: methodology

What the "Reserve wins" section (Monday Slack thread + Confluence page) shows and
why, so the numbers can be defended and reproduced. Built 2026-07-07 (Koen +
Claude); unit/snapshot/model fixes 2026-08-15; rendered by diff.py from
store/reserve_wins.csv; refreshed weekly (Sunday 18:00 London) by the local
scheduled task "refresh-reserve-wins".

## Definition
Rolling 30-day lowest / median / highest $/GPU-hour of Nebius' SIGNED reserve
deals, per GPU model and term bucket, with deal and GPU counts, and the median
compared to the matching Nebius committed list tier (512+ GPUs, 100% upfront).

## Source
`//home/dwh/nemax-prod/data/cdm/crm/deal_review_line_items_enriched`
(CRM deal-review line items). Price field: `unit_price_calculated` — its meaning
depends on `unit` (see rule 5). GPU model: `gpu_model_canonical` (with quirks,
see rule 6). Quantity: `resource_quantity` (GPUs on gpu-unit lines, RACKS on
rack-unit lines).

The table stores a FULL SNAPSHOT of every deal per review meeting
(`overview_status` = 'Historical overview' ~88k rows / 'Previous overview' /
'Actual overview' ~900 rows). Only `overview_status = 'Actual overview'` is
read; without that filter every line item counts once per meeting and
medians/GPU sums inflate ~2-3x (bug fixed 2026-08-15 — CSVs before that date
overstate GPU counts).

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
5. Unit awareness (added 2026-08-15): `unit` = 'gpu'/'gpus' lines are already
   $/GPU-hr and GPU counts. `unit` = 'rack'/'racks' lines (all GB200/GB300
   NVL72 deals, future Vera Rubin) carry a PER-RACK hourly price (~$170-440)
   and rack counts; they are converted — price / gpus_per_rack, quantity *
   gpus_per_rack — BEFORE bounds and bucketing. gpus_per_rack comes from the
   'GPU-NN' pattern in product_name, falling back to 72 for NVL72/GB-family.
   Before this fix, rack prices failed the <$20 bound and were silently
   dropped: the CSV showed ZERO GB300 wins ever despite 12 closed-won GB300
   reserve deals. Other units (TiB, gib, vcpu/ram, fip, ...) are non-GPU line
   items and excluded.
6. Model normalization (added 2026-08-15): `gpu_model_canonical` is the string
   'H100 SXM' for H100 deals — normalized to 'H100' (the old exact-match list
   silently excluded every H100 deal). Lines with NULL model (Vera Rubin,
   ad-hoc products) get the model derived from product_name as fallback.
   Tracked set: H100, H200, B200, B300, GB200, GB300, VeraRubin.
7. Price sanity bounds: 0.2 < $/GPU-hr < 20, applied AFTER per-rack -> per-GPU
   conversion (drops obvious unit errors, e.g. monthly or total amounts
   landing in the hourly field).
8. Median is per LINE ITEM (not GPU-weighted); deal count = distinct deals;
   GPUs = sum of converted GPU quantity. A single mega-deal therefore shifts
   the GPU column, not the median. Multi-tranche deals (same deal, staggered
   consumption starts) keep one line per tranche by design.
9. No silent caps: every weekly run also executes a drop-accounting query and
   reports line-item counts per drop reason (model unmapped / unit unknown /
   price out of bounds / model outside tracked set). The GB300 rack bug
   survived 5+ weeks precisely because exclusions were silent.

## Confidentiality
Aggregates only, never customer names or deal-level rows: reserve contracts
commonly carry price-confidentiality clauses, and the Slack channel + Confluence
page audience is wider than CRM deal permissions. Deal-level detail lives in
HubSpot for those with access.

## Known limitations
- `unit_price_calculated` is a CRM-derived field; validate surprising values
  against a real contract (open item: B200 short-term median ~$2.45, 2026-07-07).
- CSVs generated before 2026-08-15 are not comparable: GPU sums were inflated
  by review-meeting snapshots, H100 deals were missing (model-string mismatch),
  and rack-priced GB200/GB300 deals were missing entirely.
- GB300 reference bands for sanity checks: commercial wins ~$5.15-6.05/GPU-hr,
  hyperscaler mega-deals ~$2.4-3.5 (vs committed list ~$5.19-6.10).
- Deals closed now with consumption starting months out are included by design
  (they ARE current market prices), so wins can pre-date fleet availability.
- 30-day windows on few deals are noisy: read deal counts, not just medians.
- Refreshed weekly; the page shows a warning when the CSV is older than 14 days.
