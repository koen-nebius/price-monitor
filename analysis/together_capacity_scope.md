# Together capacity scope

The existing Together feed queried dedicated-inference instance headroom but
rendered it as GPU-cluster stock. It also selected maximum headroom across
different instance sizes and could label a GPU sold out when only small
positive headroom remained. The corrected feed keeps the products separate.

## Dedicated inference

`GET https://api.together.ai/v2/public/inference-instance-types` is retained as
a read-only dedicated-inference feed. Each record retains exact instance ID,
GPU model, GPUs per replica, region, replica headroom, exact/lower-bound
relation and observation timestamp. It produces no global stock row.

An exact zero establishes zero headroom only for that inference configuration
and region. Positive exact values of one or two replicas remain limited.
Missing/invalid headroom, unknown relations and a zero lower bound are
unknown. Lower bounds are never used to calculate percentage stock changes.
Replica counts are never added across alternative configurations or converted
into a total supply of GPUs.

Inference records appear in their own report section. They do not enter GPU
cluster tightness, sales claims or the GPU-cluster price/bookability join.
Legacy Together and aggregator records with unverified product scope are also
excluded. Old aggregated caches cannot replace a failed inference read.

## GPU clusters

The documented read-only cluster discovery route is
`GET https://api.together.ai/v1/compute/regions`. Its documented response
contains supported instance types and driver versions by region. It provides
neither stock counts nor prices. The verification script probes account access
and reports aggregate coverage; reaching this endpoint does not establish
purchasable cluster capacity.

Sources checked 17 September 2026:

- [GPU clusters API guide](https://docs.together.ai/docs/gpu-clusters-api)
- [Cluster regions API](https://docs.together.ai/reference/clusters-list-regions)

## Verification and deployment

`python3 -m unittest discover -p 'test_together_capacity*.py'` exercises parsing,
scope, classification, rendering, stale-cache handling and persistence.
`python3 scripts/check_together_capacity.py` exercises the production dispatcher
with the existing GitHub Actions secret and separately probes the cluster
regions endpoint. It does not create resources, run inference, save raw
responses, or publish reports.

The normal capacity workflow runs these regressions before its existing daily
collection. A successful branch/main check is distinct from the next scheduled
collection and publication.
