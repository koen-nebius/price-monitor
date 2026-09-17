# Lambda availability: exact on-demand VM configurations

The previous monitor aggregated sizes by GPU model, chose the largest shape
present in the response, and interpreted missing region arrays as empty. This
could turn a one-GPU observation into a cluster claim or unreadable data into
a sellout. Regional rows also discarded alternative shapes.

The capacity fetcher now uses Lambda's authenticated, read-only
`GET https://cloud.lambda.ai/api/v1/instance-types` endpoint. Each exact SKU has
its explicit GPU count from `instance_type.specs.gpus`, a global summary of the
number of launchable regions, and one positive observation per listed region.
An explicit empty array means no region is reported for that SKU; missing or
malformed availability is unknown. Counts are regions or GPUs per VM, never
physical inventory or simultaneously deployable capacity.

The source is scoped to `on_demand_instance`. It does not establish 1-Click
Clusters availability, account quota, interconnect, or simultaneous deployment.
These rows are shown in their own Slack/Confluence section with exact SKU, GPU
count, provider region and observation time. They are excluded from generic
cluster tightness, market claims, and bookability derived from unmatched prices.
The independent Lambda pricing fetcher remains unchanged; listed prices remain
listed-price evidence.

Legacy aggregated cache and history rows are excluded from current inference.
A failed request can use only scoped, exact-SKU cached observations, with their
original timestamp and the existing 48-hour limit. Fresh unknown observations
replace older positive ones. Missing SKUs, unknown region lists, changed product
identity and the legacy migration do not create sellout or recovery alerts.
Region-count changes are not represented as percentage changes in stock.

The dedicated `lambda-capacity-check.yml` workflow runs offline regressions and
the production fetch dispatcher with the existing `LAMBDA_API_KEY`, without
launching instances or publishing. The scheduled capacity pipeline runs the
same regression suite before collecting. Passing this check verifies retrieval
and output scope; the next scheduled run separately establishes collection and
publication with refreshed data.

Sources checked 2026-09-17:

- [Lambda API reference](https://docs-api.lambda.ai/api/cloud)
- [On-demand instances](https://docs.lambda.ai/public-cloud/on-demand/)
- [Creating and managing instances, including quota](https://docs.lambda.ai/public-cloud/on-demand/creating-managing-instances/)
