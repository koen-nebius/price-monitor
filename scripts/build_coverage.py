#!/usr/bin/env python3
"""Build a local coverage preview from saved snapshots, without fetching or publishing."""
import argparse
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from schema import PriceRecord
from capacity.schema import AvailabilityRecord
from coverage_report import (build_price_coverage, build_capacity_coverage, render_coverage,
                             build_priority_coverage, render_priority_coverage)
from offer_catalogue import render_catalogue
from quote_evidence import render_quote_report
from scripts.rebuild_reports import _supplementary_evidence


def read_snapshot(folder, cls, filename):
    manifest = json.loads((folder / "run_manifest.json").read_text())
    rows = json.loads((folder / filename).read_text())
    if not isinstance(rows, list) or len(rows) != manifest["record_count"]:
        raise ValueError(f"{folder}: snapshot count does not match run manifest")
    as_of = datetime.fromisoformat(manifest["completed_at"].replace("Z", "+00:00"))
    if as_of.date().isoformat() != manifest["run_date"]:
        raise ValueError(f"{folder}: manifest date does not match completion")
    return [cls.from_dict(r) for r in rows], manifest


def build(pricing_store, capacity_store, output_dir):
    if output_dir.resolve() in {pricing_store.resolve(), capacity_store.resolve()}:
        raise ValueError("Use a separate output directory; this command does not rewrite source stores")
    prices, pm = read_snapshot(pricing_store, PriceRecord, "last_snapshot.json")
    capacity, cm = read_snapshot(capacity_store, AvailabilityRecord, "last_snapshot.json")
    catalogue, quotes, _ = _supplementary_evidence(pricing_store, pm["completed_at"])
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "catalogue": catalogue, "quotes": quotes,
              "pricing": build_price_coverage(prices, pm["completed_at"], pm.get("provider_status"),
                                              catalogue_offers=catalogue["offers"], quote_report=quotes),
              "capacity": build_capacity_coverage(capacity, cm["completed_at"], cm.get("provider_status"))}
    report["priority_neoclouds"] = build_priority_coverage(report["pricing"], quotes, report["capacity"])
    html = ['<!doctype html><html lang="en"><meta charset="utf-8"><title>Competition coverage</title>',
            '<style>body{font:15px system-ui;color:#20252a;margin:32px;max-width:1600px}'
            'table{border-collapse:collapse;width:100%;font-size:13px;margin:16px 0 32px}'
            'td,th{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}'
            'th{background:#f2f4f6}h1{font-size:26px}summary{font-weight:600;cursor:pointer;padding:14px 0}'
            'p{max-width:1000px;line-height:1.5}details{margin:16px 0}</style>',
            '<h1>Competition coverage</h1><p>Saved-run review. Source dates are retained; '
            'this preview does not refresh prices, test access or publish to Confluence.</p>']
    html.append(render_priority_coverage(report["priority_neoclouds"]))
    html.append('<details><summary>Product catalogue and commercial plans</summary>' + render_catalogue(catalogue) + '</details>')
    html.append('<details><summary>Quote evidence and qualification gaps</summary>' + render_quote_report(quotes) + '</details>')
    for kind in ("pricing", "capacity"):
        part = report[kind]
        problems = [s for s in part["source_health"] if s.get("status") not in {"live", "catalogue_only"}]
        html.append(f'<h2>{kind.title()}</h2><p>Source run: {escape(part["as_of"])} · '
                    f'{part["observed_cells"]} observed cells · '
                    f'{len(problems)} collectors with a non-live status</p>')
        html.append('<details><summary>Coverage, unobserved areas and collector health</summary>' +
                    render_coverage(part) + '</details>')
    html.append('</html>')
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "coverage.json").write_text(json.dumps(report, indent=2) + "\n")
    (output_dir / "coverage.html").write_text("\n".join(html))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pricing-store", type=Path, default=ROOT / "store")
    parser.add_argument("--capacity-store", type=Path, default=ROOT / "capacity/store")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.pricing_store, args.capacity_store, args.output_dir)
    print(json.dumps({kind: {k: result[kind][k] for k in ("as_of", "observed_cells", "observations")}
                      for kind in ("pricing", "capacity")}, indent=2))
