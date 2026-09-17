# Scaleway exact instance stock

Verified against the anonymous Scaleway Instance API on 2026-09-17.

## Observation contract

- Endpoint: `https://api.scaleway.com/instance/v1/zones/{zone}/products/servers/availability`
- One record represents one exact provider SKU in one zone. `instance_type` preserves the full SKU, including SXM versus non-SXM and instance size; `gpu_count` is GPUs per instance, not inventory.
- `product_scope=gpu_instance`, `metric_type=instance_stock_status`, `data_source=official_api`.
- `available`, `scarce`, `shortage` map to `available`, `limited`, `sold_out`. The provider enum remains in `detail`. Missing, malformed or unknown labels produce `unknown`, never an inferred zero.
- `metric_value` is null: the API publishes an ordinal stock label, not a quantity. `source_url` identifies the exact page and zone; `fetched_at` records the observation time.
- The signal does not establish customer quota, concurrent deployment quantity, topology or multi-node capacity. No global/model-level rollups are produced by the fetcher.

## Completeness and failure handling

The live Paris zone response contained 134 server SKUs: page 1 contained 100. The JSON has no total field; pagination metadata is in response headers. The fetcher requests explicit sequential pages until a short page, including an empty page for exact multiples. Repeated SKUs or 20 full pages invalidate that zone rather than silently truncate.

All nine configured zones must return complete, valid pages. Any zone request or critical schema failure discards the entire live snapshot so the pipeline can use a clearly stale, compatible cache. No missing zone or missing SKU becomes a sold-out observation or a removal alert. Legacy model-level cache and history are incompatible with this exact-SKU contract.

The live check completed all nine zones and returned 16 tracked GPU SKU/zone records in `fr-par-2` and `pl-waw-2`. Other zones returning no tracked GPUs do not establish that these SKUs are unavailable elsewhere.

## Price comparisons

The existing ComputePrices feed uses synthetic instance identifiers and a global region. It cannot be matched reliably to these exact Scaleway SKUs and zones. Listed prices remain visible, but Scaleway is excluded from the broad cheapest-bookable price calculation until exact commercial-offer identity is available. Its scoped observations also do not enter cluster tightness or provider-wide restock/sold-out claims.

## Validation

`python3 -m unittest test_scaleway_capacity -v`: 14 offline tests pass. Coverage includes separate small/large and SXM/non-SXM shapes, raw-label provenance, missing/malformed states, GPU-count parsing, additional pages, repeated-page/cap failure, discarded partial snapshots, static error logging and schema serialization.

At the later live check, `fr-par-2` reported `H100-SXM-2-80G=available` and `H100-SXM-8-80G=shortage`. These are dated observations, not durable capacity assertions. Downstream comparisons must never lend the smaller SKU's availability to the eight-GPU price.
