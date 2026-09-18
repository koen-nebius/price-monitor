# Competition coverage changes — 18 September 2026

Implemented locally: retain more public offers, expose collection gaps, and
prepare an opt-in Prime Intellect availability pilot. These changes have not
been pushed, merged or published to Slack/Confluence.

## Retained public offers

Current public source inputs were retrieved and parsed on 18 September:

| Collector | Retained offers | Eligible under publication rules | Remaining references |
| --- | ---: | ---: | --- |
| CoreWeave | 25 | 25 | None from this direct page sample |
| Hyperstack | 15 | 9 | Six starting-from reservation tariffs with unspecified terms |
| Verda | 68 | 56 | Twelve confidential-compute variants, separate from the standard cohort |

CoreWeave now preserves Europe and North America, instance GPU count and RTX
host-memory variants. Hyperstack retains each printed SKU/tier. Verda retains
each shape/rental type; an unreported region stays unknown. Published per-GPU
tariffs do not establish an eight-GPU host. The four ambiguous CoreWeave RTX
ComputePrices rows remain inspectable references, excluded from comparisons.

These counts describe observations from inspected source payloads, not independent
competitors, stock, transactions or a complete market census. Price arithmetic,
offer identity and configuration/term boundaries are covered by regressions.

## Coverage output

Both daily Confluence generators now include an expandable table by competitor,
GPU, provider-native region and purchase type, plus unobserved parts of the
tracked panel and collector health. Machine-readable output is `coverage.json`
in each store. Own Nebius records are excluded from competitor counts; duplicate
feeds retain one supplier identity. Sources can share an underlying rate card.

Fresh prices, dated or unqualified references, reported unavailability, unknown
signals and failed/partial/cached reads remain distinct. Capacity evidence keeps
its signal type. An unobserved cell does not mean sold out or not offered. The
panel is a collection target, not a claim that every provider sells every GPU.

Create a separate local preview from saved runs, without fetching or publishing:

```sh
python3 scripts/build_coverage.py --output-dir ./work/coverage-review
```

The preview retains each source run's completion date. It does not pretend that
the saved snapshots contain the newly collected offers or represent a new run.

## Capacity retrieval repairs

- AWS Capacity Blocks preserves the configured 16 region/SKU checks for one
  instance, 24 hours, within a 14-day horizon. Access/account/rate-limit failures
  stop further requests. Pagination and total requests are bounded; incomplete
  reads cannot establish earliest availability or sellout. Missing keys,
  dependencies, source failure and partial scope are reported separately.
- Voltage Park validates the response envelope and required integer counts.
  A successful empty response produces unknown inventory, not zero stock.
  One live check returned HTTP 200 with no locations. That is source access
  verification, not recovery of usable GPU inventory.
- Structured health reaches the manifest and coverage table. Partial results
  cannot overwrite a complete cache or display as a fully live source.

The exact AWS error from the saved morning run was not retained. Local AWS
credentials and boto3 were unavailable, so the repair's live authenticated
retrieval remains unverified; pagination/error handling is fixture-tested.

## Prime Intellect pilot

The [pilot contract and commands](primeintellect_pilot.md) document authenticated
GPU and multi-node catalogue retrieval, raw-page persistence and review output.
Public API documentation/OpenAPI were inspected and fixture tests pass.
No Prime credential was found in the inspected tracker environment; no live
authenticated request was made. The inspected schema does not establish the
multi-GPU price denominator sufficiently for production comparison. Pilot rows
remain quarantined and cannot affect benchmark medians or capacity headlines.

The next validation step is an existing authorized Availability Read credential
and a source-backed reconciliation of single-GPU, multi-GPU and multi-node prices,
including resource surcharges and spot semantics. No account, reservation or
subscription was created.

## Validation and release

Regression coverage includes region/configuration retention, ambiguous units
and terms, supplier/feed identity, unknown versus zero inventory, pagination,
partial source health, safe caches and offline report regeneration. Scheduled
workflows run the new relevant checks and persist coverage outputs.

Raw production snapshots and history remain unchanged. A local preview and
passing tests do not establish scheduler execution or destination publication.
