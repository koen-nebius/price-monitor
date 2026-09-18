# Aggregator pricing coverage — 18 September 2026

Aggregator observations are retained as a separate CoreWeave evidence feed.
The inspected ComputePrices CoreWeave entries relay the published CoreWeave
rate card; they do not establish private negotiated offers or bookable stock.
Dropping an entire provider at ingestion loses potentially useful regional
and configuration evidence, even when a direct public scraper exists.

## Implemented scope

- ComputePrices retains CoreWeave and preserves individual regions, GPU
  variants, GPU counts, commercial tiers and exact commitment months. It no
  longer keeps only the cheapest offer or deletes a reserved offer because a
  differently configured on-demand offer is cheaper. GH200 is not an H100
  observation; unknown reserved terms cannot become on-demand prices.
- Shadeform preserves each SKU, region and rental type. Its nested and legacy
  configuration formats are supported. On-demand cents and spot dollar amounts
  remain separate; a spot-only response cannot create an on-demand offer.
- Provider identity is separate from feed identity. A public rate-card source
  no longer suppresses all Shadeform offers from the same provider. Peer
  comparisons count a canonical supplier once. Known invalid Together relays
  and unqualified Vultr tariffs retain their product restrictions.
- Source update time, retrieval time, source URL, offer ID, availability and
  configuration survive persistence. New aggregator observations require a
  valid source date within the existing 48-hour publication window. Unavailable,
  stale, undated and conflicting observations remain inspectable as references.
- Confluence generation includes the full aggregator offer evidence, with
  source dates and comparison status. Aggregator updates and source switches
  cannot become confirmed provider repricing alerts. Own Nebius list anchors
  continue to use the direct source.
- Current PAYG references can use the full accepted snapshot, so retaining a
  cheap single-GPU offer does not erase an eligible eight-GPU offer. A one-GPU
  slice on an eight-GPU host is not promoted to a full-node offer.

## Live validation

One read-only request to the existing ComputePrices account on 18 September
returned 30 CoreWeave rows. Of these, 21 cover the tracker’s six matched GPU
families: H100, H200, B200, B300, L40S and RTX PRO 6000. They span Europe and
North America, with on-demand and spot entries. The previous collector excluded
all CoreWeave entries from this feed.

Six retained offers have 17 September source timestamps and meet the 48-hour
date gate at validation time. Fifteen have 13 September timestamps and remain
dated references. This is a source-freshness result, not proof of stock,
transaction prices or cluster configuration comparability. All six fresh rows
have unknown availability signals.

## Evidence and release boundary

A subsequent source-level reconciliation on 18 September checked all 30 API
prices against CoreWeave's public regional instance table. Prices reconcile
after per-GPU rounding, but this is not blanket configuration validation:
the four RTX PRO 6000 feed rows omit the host-memory variant. Their on-demand
and spot rates match different published host variants. These four rows now
remain reference-only, without guessing configuration from price. Two of the
six rows passing the timestamp gate are therefore excluded from comparisons.
The B300 VRAM metadata also differs between feed and provider table; matching
prices do not settle that metadata disagreement.

See [the next coverage implementation](competition_coverage_2026-09-18.md)
for direct collector repairs, the coverage tables and the opt-in Prime pilot.

- [ComputePrices API documentation](https://computeprices.com/docs/api)
- [ComputePrices OpenAPI schema](https://computeprices.com/api/v1/openapi.json)
- [Shadeform instance API documentation](https://docs.shadeform.ai/api-reference/instances/instances-types)

The public ComputePrices schema was retrieved and checked against the parser.
The live CoreWeave response was tested through normalization, provider identity
and freshness filtering. Offline regressions cover offer retention, source
switches, stale inputs, own-price isolation, history and Confluence generation.
Shadeform was tested with documented response fixtures; its existing key/access
gate remains unchanged.

Validation: 330 offline unit tests passed, cross-table consistency passed on
the 1,390-row saved snapshot, and all 11 Together parser fixtures passed.

No historical store files were rewritten, no new subscription was purchased,
and no live Confluence page or Slack message was published by this work. This
change is local until separately pushed and merged.
