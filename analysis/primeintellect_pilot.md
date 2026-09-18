# Prime Intellect availability pilot

Status on 2026-09-18: implemented for opt-in, read-only collection and offline
review. **Not registered in the production price or capacity pipelines.** No
Prime Intellect credential was present in the inspected environment or existing
price-monitor `.env`; authenticated retrieval has not been tested. Public docs
and the public OpenAPI were retrieved; fixtures are documentation-derived, not
current market observations.

## Verified source contract

- [GPU availability](https://docs.primeintellect.ai/api-reference/availability/get-gpu-availability):
  `GET https://api.primeintellect.ai/api/v1/availability/gpus`.
- [Multinode availability](https://docs.primeintellect.ai/api-reference/availability/get-multinode-availability):
  `GET https://api.primeintellect.ai/api/v1/availability/multi-node`.
- [Availability guide](https://docs.primeintellect.ai/api-reference/check-gpu-availability)
  specifies Bearer authentication with **Availability → Read** permission.
- Both return `items` and `totalCount`. Pages start at 1 and contain up to 100
  items. The collector requires complete pages and a stable total; repeated
  configurations, changing totals, truncated pages and the page limit produce
  an explicit incomplete result.
- [Public OpenAPI](https://api.primeintellect.ai/openapi.json), retrieved
  2026-09-18, SHA-256
  `04c5f1e04082f2c7c2c4dad6c42eff89e70b9638b974a90cc284b52300df5ff7`:
  `GpuAvailability`, `Prices`, `CustomSpecValue`, `AvailabilityResponse`.

The API distinguishes the underlying `provider` from the collection feed.
`cloudId` must be combined with the datacenter and configuration. Both endpoint
responses use the same configuration schema; that does not establish how many
nodes are simultaneously launchable. `gpuCount` describes the configuration,
not stock quantity. Stock is a label: Available, Low, Medium, High or Unavailable.
The pilot retains the label, never converts it into inventory, and keeps endpoint
scope in its identity.

The schema establishes hours for prices, prepaid time and resource surcharges;
GB for memory/storage; Mbps for internet speed; Gbps for interconnect; and minutes
for provisioning time. `prepaidTime` is prepaid **hours**, followed by standard
hourly billing, not a commitment duration in months. `isSpot` describes whether
spot provisioning is supported, not whether `prices.onDemand` is a spot price.

`prices.onDemand` belongs to secure cloud and `communityPrice` to community
cloud. Preserve currency and variable-price flags. Default storage/CPU/memory
can incur separate charges; retain `defaultIncludedInPrice` and `pricePerUnit`.
The examined schema does not explicitly settle the GPU-versus-configuration
denominator for multi-GPU/multinode prices. The guide's one-GPU example is
insufficient to establish it. No source observation timestamp is supplied.

## Integration boundary

`fetchers.primeintellect.collect(token, endpoints, ...)` makes only the two
availability GET requests. It returns review observations and an audit, with a
callback for exact raw-page persistence. It rejects redirects and unexpected
hosts/paths. No account, billing, provisioning, reservation, signup or purchase
API is used. Error diagnostics contain controlled codes, not response bodies,
headers or credentials.

Every review observation retains source feed, provider, cloud ID, configuration,
endpoint scope, datacenter, region, country, security tier, resource specs,
network, stock label, raw prices and prepaid hours. Its stable offer identity
excludes prices and stock. Actual retrieval time is separate from the absent
source observation timestamp; an offline replay invents neither.

All rows remain quarantined (`comparison_eligible=false`), with explicit reasons.
The shared `PriceRecord` requires a USD/GPU-hour price, so the pilot emits **zero
PriceRecords** while the denominator and commercial treatment remain unresolved.
It must not influence market medians, bookability joins or Slack claims. No
changes to configuration, workflows or the shared schema are needed for this
pilot. Live access alone will not automatically activate production coverage.

Before production integration, verify authenticated complete retrieval, compare
single-GPU and multi-GPU quotes against the vendor UI or an explicit unit
definition, establish surcharge/spot treatment, and validate multinode topology
separately. Then add a reviewed mapping and production evidence/freshness tests.

## Run locally

Use a **new directory outside production stores**. The CLI has no default output
directory and refuses to overwrite an existing run. It writes exact raw page
bytes, `offers_for_review.json` and `audit.json` only to that selected directory.

Offline documentation-derived fixture (no network):

```sh
python3 scripts/primeintellect_pilot.py \
  --input-json tests/fixtures/primeintellect_documented_single_node.json \
  --endpoint single_node --output-dir ./work/primeintellect-fixture-review
```

The fixture follows the guide's H100 example. Its `totalCount` is reduced to one
and its image list shortened to make a complete synthetic page. It is not a
saved live response, and its price must never be presented as current.

With an existing authorized Availability → Read credential in `PRIME_API_KEY`:

```sh
python3 scripts/primeintellect_pilot.py --live \
  --output-dir ./work/primeintellect-live-review
```

`--token-env` accepts another environment-variable **name**, never the token
itself. Missing access produces an authentication-blocked audit and exit code 2
without a network request. Successful collection or replay returns exit code 0;
incomplete collection returns 2. Neither means production readiness.

Regression command:

```sh
python3 -m unittest test_primeintellect_pilot
```
