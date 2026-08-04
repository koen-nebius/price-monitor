# Public market-event enrichment

This package fills one real gap in the GPU price monitor: official public
evidence about competitor offer changes, bookability, bundle comparability, and
capacity announcements. It does **not** rebuild data the repository already has.

Existing sources remain authoritative for their own questions:

- `store/history.csv`: daily competitor price history.
- `store/last_snapshot.json`: current normalized offer snapshot.
- `store/intel.csv`: governed commercial and field-price evidence.
- `store/reserve_wins.csv`: signed Reserve outcomes by GPU and term.

Therefore the package should not create another wide "market and commercial
signals" table. Its eventual output is a narrow, typed stream that can enrich a
pricing opportunity or a market-change view. A standalone dashboard tab is only
justified after the verified event stream is broad and current enough to support
decisions.

## What is implemented

- A 15-provider official-domain registry in
  `config/source_registry.json`.
- Typed and validated `CompetitorOfferEvent`,
  `CapacityAnnouncementEvent`, and `SourceHealthEvent` records.
- Explicit offer states:
  - availability: `not_checked`, `listed_only`, `bookable_verified`,
    `unavailable`, `not_publicly_verifiable`, `stale`;
  - bundle: `not_checked`, `partial`, `normalized`, `not_comparable`.
- Tavily Search, Extract, and Crawl support with official-domain and registered-
  query enforcement.
- Daily known-page extraction and weekly official-domain discovery flows.
- Low-confidence regex candidates from public documents. Candidate extraction
  is deliberately not self-verifying.
- Append-only JSONL event versions, field-level changes, review decisions, and
  per-run coverage history.
- A version-locked human-review queue and explicit promotion command. Promotion
  appends a verified event version and review audit; it never rewrites the
  candidate.
- A separate raw-document location so full public page bodies can be retained
  briefly for review without entering Git history.
- Applicability-aware completeness metrics. For example, PAYG offers are not
  penalized for missing Reserve term, and announcements expressed in MW are not
  penalized for missing GPU count.
- Twelve verified fixtures copied from existing official-provider evidence rows.
  Internal secondary links were intentionally omitted.

## Public-only boundary

Tavily is only an extraction and discovery layer for public official domains.
The adapter rejects:

- non-HTTPS or non-registered domains;
- arbitrary search queries;
- registered sources not marked `public_only`;
- strings containing common internal Slack, Jira, Confluence, tenant, customer,
  account, or deal identifiers;
- configuration files containing an API key.

The API key is read only from `TAVILY_API_KEY`. Crawl requests set
`allow_external=false` and restrict `select_domains` to the official registry.

Do not send `intel.csv`, CRM records, quotes, signed rates, customer names, Slack
threads, Jira tickets, or Confluence content to Tavily. Those records should stay
in their approved internal connectors and be joined downstream by non-identifying
keys.

## Flow

```text
official source registry
        |
        +-- daily: Extract known pricing/product pages
        |
        +-- weekly: Search official domain -> Extract result URLs
                     + bounded Crawl of registered news roots
        |
        v
temporary raw_documents.jsonl (Actions artifact, 3-day retention)
        |
deterministic candidate normalizer
        |
semantic dedupe -> new / changed / unchanged diff
        |
append-only events.jsonl + changes.jsonl + coverage_history.jsonl
        |
review_queue.jsonl -> version-locked human verification
        |
append verified event version + reviews.jsonl decision
        |
approved DWH materialization and decision view
```

`bookable_verified` must come from a real availability check. A public pricing
page can establish `listed_only`, but Tavily cannot prove that capacity can be
purchased. Similarly, a provider announcement does not prove the announced
capacity is uncontracted or available to Nebius's target customers.

## Commands

The package targets Python 3.11+, matching the repository's GitHub workflow.

Validate all source domains and URLs without network access:

```bash
python3 -m market_events.cli validate-registry
```

Inspect the exact public requests a schedule would make:

```bash
python3 -m market_events.cli plan --mode daily
python3 -m market_events.cli plan --mode weekly
```

Seed a local append-only store from the verified public fixtures:

```bash
python3 -m market_events.cli ingest-fixture \
  --state-dir /tmp/gpu-market-events-state
```

Report completeness using applicable denominators:

```bash
python3 -m market_events.cli coverage \
  --state-dir /tmp/gpu-market-events-state
```

An actual public Tavily run requires both `TAVILY_API_KEY` and the explicit
`--execute` flag:

```bash
python3 -m market_events.cli run --mode daily \
  --state-dir /durable/state/market_events \
  --raw-dir /temporary/raw-market-documents \
  --execute \
  --fail-on-source-error
```

Without `--execute`, `run` prints the plan and does not call Tavily. Every
executed run appends an applicability-aware snapshot to
`coverage_history.jsonl`. `--fail-on-source-error` returns a non-zero status
after source-health, event, change, and coverage records have been persisted.

Export current candidates for human review:

```bash
python3 -m market_events.cli review-queue \
  --state-dir market_events/state \
  --output market_events/state/review_queue.jsonl
```

Each queue row contains `semantic_key` and `expected_version_id`. After checking
the official source URL and evidence, append a verified version with both values:

```bash
python3 -m market_events.cli promote \
  --state-dir market_events/state \
  --semantic-key 'competitor_offer:...' \
  --expected-version-id '...' \
  --reviewer 'pricing-reviewer' \
  --review-note 'Matched price, lane, region and effective date to the official page.' \
  --confidence high
```

Promotion fails if the candidate changed after the queue was exported. Refresh
the queue and review the new version instead. Reviewer notes and internal
evidence must never be passed to Tavily; the promote command is local-only and
makes no network request.

## Recurring GitHub workflow

`.github/workflows/market-events.yml` is self-contained and does not modify the
existing `.github/workflows/scrape.yml` flow. Once this branch is reviewed and
merged, it will:

1. Run the offline unit suite and validate all registry entries before any
   network request.
2. Fail closed if the repository secret `TAVILY` is absent. The secret is mapped
   to `TAVILY_API_KEY` and is never printed.
3. Extract registered known pages every day at 02:47 UTC.
4. On Sunday, additionally run registered official-domain Search, Extract, and
   bounded Crawl. Manual runs can select `daily`, `weekly`, or `both`.
5. Keep full raw page bodies under `${{ runner.temp }}` and upload them as a
   private Actions artifact with three-day retention.
6. Commit only these allowlisted files under `market_events/state/` when changed:
   `events.jsonl`, `changes.jsonl`, `coverage_history.jsonl`, `reviews.jsonl`,
   and `review_queue.jsonl`.

The job stays failed when any source run reports an error, while the `always()`
cleanup steps still preserve source-health state, the review queue, and any raw
artifact gathered before failure. No raw page body is allowlisted for commit.

## Publication gates

Do not add a standalone market-events dashboard tab until all of the following
are true for the chosen decision scope and freshness window:

1. The event is `verified`, not merely a regex candidate.
2. Source freshness and source-run status are visible.
3. Offer price, GPU, lane, configuration, region scope, and source URL are known.
4. Availability uses one of the explicit states rather than a null or the vague
   word `withheld`.
5. Bundle status is explicit. A comparison labelled `normalized` has the
   relevant GPU count, topology/interconnect, storage, CPU/RAM, and SLA fields.
6. Capacity announcements keep GPU quantity, MW, contracted status, and
   bookability separate. MW is never converted to sellable GPUs without an
   explicit configuration.
7. Applicability-aware coverage is high enough for the decision. A missing field
   that is not applicable is not a data-quality failure.

Before those gates pass, verified events should enrich the top pricing
opportunities as evidence links, not occupy an under-populated tab.

## Remaining integration gates

The recurring collector and review audit are implemented, but the data is not
yet approved for a standalone dashboard tab. The remaining sequence is:

1. Review the initial candidate backlog and establish reviewer ownership and an
   evidence freshness SLA.
2. Materialize verified current events, event history, source health, and
   coverage history to governed DWH tables.
3. Join those tables to `history.csv`, `intel.csv`, and `reserve_wins.csv` by
   non-identifying commercial dimensions. Do not copy those sources into this
   public event store or send them to Tavily.
4. First add verified evidence links and source freshness to the 3-5 priority
   pricing opportunities. Add a market tab only if verified coverage passes the
   publication gates above and the tab supports a distinct decision.

## Tests

The fixture suite performs no network calls:

```bash
python3 -m unittest discover -s market_events/tests -v
```

It covers official-domain enforcement, secret handling, registered-query
enforcement, Search/Extract/Crawl payloads, typed validation, fixture
normalization, append-only versioning, dedupe, field-level diffs,
applicability-aware coverage history, raw/state separation, version-locked human
promotion, daily/weekly orchestration, and the workflow safety contract.
