# Crusoe authenticated capacity

The capacity monitor uses `GET https://api.cloud.crusoe.ai/v1/capacities`
when both `CRUSOE_ACCESS_KEY_ID` and `CRUSOE_SECRET_KEY` are configured.
Credentials belong in GitHub Actions repository secrets. They are injected
only into the capacity pipeline and the read-only verification workflow.
The `price-monitor` key created on 17 September 2026 expires on 16 December 2026.
Renew it before that date to avoid a stale-source fallback.

The client signs GET requests using Crusoe's HMAC authentication. It never
provisions resources or requests quota. The key itself is not restricted to
read-only by the console; the integration contains only a read endpoint.

## What the observation establishes

One row is one provider-reported configuration and location. `quantity` is
kept in the provider's units. `num_slices` describes the resource, while
`quota_type` is a quota category, not the account's remaining quota.
Alternative configurations may share capacity; their quantities must not
be added together. These records establish neither multi-node cluster
availability nor this account's ability to purchase a resource.
Identical duplicate observations are deduplicated. Conflicting quantities
or slice/quota contexts for the same configuration/location are marked
unconfirmed; the monitor does not choose the most favorable quantity.

Exact rows appear in a separate capacity table. They do not enter global
cluster tightness or a price/capacity join without a matching commercial
offer. Public price scraping remains separate and unchanged.

With neither credential set, the existing documentation scrape remains a
labelled offering footprint. A partial credential pair or API error fails
the live fetch and permits only the existing explicitly stale cache path;
it never substitutes documentation (fresh or cached) for an API observation.

## Verification and operation

`python3 -m unittest discover -p 'test_crusoe*.py'` covers signing, parsing,
failure handling, production dispatch and output classification.
`python3 scripts/check_crusoe.py` verifies the authenticated production
dispatcher and prints aggregate coverage only. It does not persist raw
responses, create resources, or publish to Slack or Confluence.

The dedicated GitHub verification workflow has read-only repository
permissions and runs on the integration branch and relevant main changes.
The existing daily capacity workflow includes both secrets and regression
tests. Deployment is complete only once these changes are on main; a
passing branch check alone does not establish a completed daily run.

## Primary references

- [Current API, capacities endpoint](https://docs.crusoecloud.com/api/)
- [Crusoe HMAC authentication](https://docs.cloud.crusoe.ai/reference/api/)

Documentation verified 17 September 2026.
