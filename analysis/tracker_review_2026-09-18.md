# Tracker publication corrections, 18 September 2026

The daily outputs now lead with observed changes and source exceptions. Full provider evidence remains expandable on the existing Confluence pages. Daily Slack artifacts contain a short parent and bounded supporting reply; delivery remains with the existing sender.

## Evidence and comparison rules

- One eight-GPU node is a separate question from aggregate free GPUs and multi-node capacity. RunPod's Low eight-GPU signal is positive. Exact Lambda and Scaleway eight-GPU configurations are included; Hyperstack aggregates and stale or unscoped observations remain unknown. Unknown providers are outside the checked denominator. Changes in coverage are shown separately.
- Azure fractional-GPU normalization errors in the documented 25 August–16 September observations are corrected on copies using the preserved instance price. Together's documented 3–16 September preemptible observations mislabeled on-demand are excluded from on-demand comparisons. Raw snapshots and history are preserved. A real price change concurrent with a denominator correction uses the corrected denominator.
- Legacy AWS Capacity Block constants no longer appear as fresh API prices. The separately scraped published-rate feed is the sole current reference. It does not prove an available booking.
- Current price comparisons require a source observation within 48 hours. The expired Nebius committed list is withheld until reverified. Historical CRM win aggregates retain their extraction date and original window; they do not become current because the page was regenerated.
- Interruptible offers, short-term reservations and negotiated term deals remain separate. No combined auction floor is inferred. Aggregator changes remain evidence, not provider-confirmed repricing alerts.
- CRM loss labels do not establish customer acceptance or willingness to pay. HubSpot and Salesforce medians remain separate with their own dates and populations. Finance grids and economics retain their separately documented source basis.

## Rebuild and publication

`scripts/rebuild_reports.py` reconstructs pricing artifacts from a paired accepted snapshot and retrieval manifest. It discovers an earlier coherent run in local Git history, or reports that no comparison is available. It records generation time and hashes separately and never refreshes raw observations or delivery receipts.

`python3 -m capacity.rebuild` similarly renders saved observations, with an explicit previous snapshot for comparisons. The strict capacity publisher validates freshness and XML, preserves the live title, and verifies the stored page after one write.

The existing daily schedules and destinations remain in place. Regression checks run before daily generation and on pull requests. A manual page-publication workflow publishes the already committed pricing, spot and capacity artifacts and rebuilds the benchmark pages; it does not post to Slack.
