"""
Competitor Storage Pricing — sibling Confluence page builder.

Renders config.STORAGE_PRICES (the curated, source-verified table — see the
rationale there) into store/storage_body.html. Published daily by the GHA run
via scripts/publish_page.py (upsert by title), NOT by the CCR routine — storage
list prices move ~quarterly, and keeping this page's publish path inside the
data build avoids touching the pinned posting-routine prompt.

Separate page by design (2026-08-15, Koen): different decision domain, units
($/GiB-month, $/GiB-RAM-hr) and audience than the GPU page; sibling-page
pattern per the spot/auction precedent.
"""
from datetime import datetime, timezone

from config import STORAGE_PRICES, STORAGE_PRICES_VERIFIED

_UPPER = {"aws", "gcp", "ovh"}


def _prov(p: str) -> str:
    return p.upper() if p in _UPPER else p.title()


def _money(row) -> str:
    cur = "€" if row.get("currency") == "EUR" else "$"
    return f'{cur}{row["price"]:.4f}'.rstrip("0").rstrip(".")


def _egress(row) -> str:
    e = row.get("egress")
    if e is None:
        return "—"
    return "free" if e == 0 else f'${e:.3f}'.rstrip("0").rstrip(".")


def _table(rows, headers, cells) -> str:
    html = ['<table data-layout="default"><tbody>', headers]
    for r in sorted(rows, key=lambda x: x["price"]):
        html.append(cells(r))
    html.append('</tbody></table>')
    return "\n".join(html)


def format_storage_page(run_date: str) -> str:
    html = [
        f'<p><em>Competitor storage and memory-component pricing, verified against each '
        f'provider\'s own page/API on <strong>{STORAGE_PRICES_VERIFIED}</strong>. This is a '
        f'curated benchmark, not a live scrape: storage list prices move roughly quarterly, '
        f'and a daily drift check flags any source that no longer matches (see methodology '
        f'below). EUR prices shown in EUR — no silent conversion. '
        f'Sibling pages: GPU Competitor Pricing — Daily Overview · Competitor Spot &amp; '
        f'Auction Pricing.</em></p>',

        '<h2>Object Storage — standard/hot tier</h2>',
        _table(STORAGE_PRICES["object"],
               '<tr><th>Provider</th><th>Service</th><th>$/GiB-mo</th><th>Egress $/GB</th><th>Note</th></tr>',
               lambda r: (f'<tr><td><strong>{_prov(r["provider"])}</strong></td><td>{r["name"]}</td>'
                          f'<td>{_money(r)}</td><td>{_egress(r)}</td><td>{r.get("note", "")}</td></tr>')),
        '<p><em>Egress is the real differentiator: a single full read-out of stored data costs '
        '~4 months of storage at AWS ($0.09/GB) vs zero at R2/OVH/CoreWeave and $0.015 at '
        'Nebius. Position: Nebius object ($0.0147) undercuts every hyperscaler.</em></p>',

        '<h2>Block / Network SSD</h2>',
        _table(STORAGE_PRICES["block"],
               '<tr><th>Provider</th><th>Service</th><th>$/GiB-mo</th><th>Included performance</th></tr>',
               lambda r: (f'<tr><td><strong>{_prov(r["provider"])}</strong></td><td>{r["name"]}</td>'
                          f'<td>{_money(r)}</td><td>{r.get("note", "")}</td></tr>')),
        '<p><em>Position: Nebius Network SSD ($0.071) is below EBS gp3 / Azure Premium SSD v2 '
        '($0.080) — on standalone storage list prices Nebius is competitive-to-cheap. The '
        'like-for-like caveat lives on the GPU page: competitors bundle local NVMe scratch '
        'into GPU node prices; Nebius bills storage separately.</em></p>',

        '<h2>Shared / Parallel Filesystems (GPU-cluster relevant)</h2>',
        _table(STORAGE_PRICES["shared_fs"],
               '<tr><th>Provider</th><th>Service</th><th>$/GiB-mo</th><th>Note</th></tr>',
               lambda r: (f'<tr><td><strong>{_prov(r["provider"])}</strong></td><td>{r["name"]}</td>'
                          f'<td>{_money(r)}</td><td>{r.get("note", "")}</td></tr>')),

        '<h2>Memory (RAM) as a priced component</h2>',
        _table(STORAGE_PRICES["ram"],
               '<tr><th>Provider</th><th>Basis</th><th>$/GiB-RAM-hr</th><th>Note</th></tr>',
               lambda r: (f'<tr><td><strong>{_prov(r["provider"])}</strong></td><td>{r["name"]}</td>'
                          f'<td>{_money(r)}</td><td>{r.get("note", "")}</td></tr>')),
        '<p><em>Only Nebius publishes a standalone RAM rate; hyperscaler figures are derived '
        'from same-vCPU instance-family deltas and marked as estimates. Managed in-memory '
        'services (Valkey/Redis-class) are benchmarked separately on the Managed Database '
        'Pricing page.</em></p>',

        '<h2>Methodology</h2>',
        '<p>Curated table, source URL per row (in the repo config). A daily automated drift '
        'check re-fetches the machine-readable sources and flags mismatches in the pipeline '
        'run manifest; confirmed changes update this table with a new verification date. '
        'Storage price CHANGES also surface as a line in the daily #competitor-pricing '
        'digest. Units: binary GiB per month; AWS bills GB=2^30 bytes so published rates '
        'compare 1:1.</p>',

        f'<p><em>Data date {run_date} · table verified {STORAGE_PRICES_VERIFIED} · generated '
        f'{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} by the price-monitor '
        f'pipeline.</em></p>',
    ]
    return "\n".join(html)
