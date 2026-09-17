# Massed Compute inventory source

The daily price (01:23 UTC) and capacity (02:23 UTC) workflows use the repository
Actions secret `MASSED_COMPUTE_API_KEY`. Neither fetcher registers without a
nonblank key. The separate **Check Massed inventory (read only)** workflow tests
the saved secret without publishing pages, posting messages or changing resources.

The client calls only `gpu_inventory_list` on the official MCP endpoint. No
provisioning, account-balance or billing call is made. Request errors are sanitized
and redirects are refused to prevent forwarding the bearer token to another host.
Use a read-only provider token where supported. A tool being advertised by MCP is
not proof that the token can execute it; the monitor does not test write access.

Prices are **account catalogue** rates. Public retail/wholesale parity is not
established. Cents per configuration-hour are divided by 100, then by the explicit
GPU count in the SKU. Single-GPU, eight-GPU and Spot offers remain separate raw
records. Generic H100 stays form-factor unknown; SXM or NVLink does not establish
InfiniBand or multi-node availability. A rate has unspecified regional applicability
because the response lists stock regions, not region-specific prices.

Capacity records retain SKU and observed region. A listed price is not stock.
An empty region list means no regional availability reported, not verified physical
zero capacity. The integer `capacity_available` is not interpreted as a GPU or node
quantity because its unit is unverified. No global or cluster-availability rollup is
emitted. No provider/GPU-only price-to-stock join is enabled for this source.

Live plausible direct prices replace Massed aggregator rows only for covered GPU
and commercial tiers. If live retrieval fails, aggregator fallback is retained.
Massed is an additional alternative, not automatically an enterprise-median peer.
Account catalogue rows also stay out of the public RTX market statistic; they remain
visible with their basis in the all-provider market sweep.
The existing price history remains cheapest-per-provider/GPU/tier; exact configuration
detail is preserved in daily snapshots and peer cache, not in that aggregate history.

Sources: [official MCP overview](https://vm-docs.massedcompute.com/docs/mcp/overview),
[tool reference](https://vm-docs.massedcompute.com/docs/mcp/tools),
[inventory API](https://massedcompute.com/products/inventory-api/).

Verify locally without credentials: `python3 -m unittest discover -p 'test_massedcompute*.py'`.
After saving the secret, verify on GitHub with the read-only workflow before treating
the source as operational. Normal scheduled publication is a separate delivery stage.
