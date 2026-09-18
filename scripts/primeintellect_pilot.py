#!/usr/bin/env python3
"""Read-only, opt-in Prime Intellect pilot with explicitly selected audit output."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fetchers import primeintellect as pi  # noqa: E402


def _write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--live", action="store_true", help="Opt in to authenticated GET availability calls")
    source.add_argument("--input-json", type=Path, help="Replay one saved API response; makes no network calls")
    parser.add_argument("--output-dir", required=True, type=Path, help="New audit directory; no production store default")
    parser.add_argument("--endpoint", choices=["both", *pi.ENDPOINTS], default="both")
    parser.add_argument("--token-env", default="PRIME_API_KEY", help="Environment variable name; never pass a token as an argument")
    parser.add_argument("--max-pages", type=int, default=100)
    args = parser.parse_args(argv)
    if args.input_json and args.endpoint == "both":
        parser.error("--input-json requires --endpoint single_node or multi_node")
    output = args.output_dir.resolve()
    for production in (ROOT / "store", ROOT / "capacity" / "store"):
        if output == production or production in output.parents:
            parser.error("pilot output must be outside production stores")
    if output.exists():
        parser.error("--output-dir must be a new directory to preserve prior audit runs")
    if not 1 <= args.max_pages <= 1000:
        parser.error("--max-pages must be between 1 and 1000")
    raw_fixture = args.input_json.read_bytes() if args.input_json else None
    output.mkdir(parents=True)
    generated_at = datetime.now(timezone.utc).isoformat()

    def persist(endpoint, page, raw, metadata):
        name = f"raw_{endpoint}_page_{page:04}.json"
        with (output / name).open("xb") as stream:
            stream.write(raw)
        metadata["raw_file"] = name

    if args.live:
        token = os.environ.get(args.token_env, "").strip()
        endpoints = tuple(pi.ENDPOINTS) if args.endpoint == "both" else (args.endpoint,)
        audit = pi.collect(token, endpoints, max_pages=args.max_pages, on_page=persist)
    else:
        metadata = {"fetched_at": None, "sha256": hashlib.sha256(raw_fixture).hexdigest(),
                    "bytes": len(raw_fixture), "origin": "caller_supplied_replay"}
        persist(args.endpoint, 1, raw_fixture, metadata)
        try:
            payload = pi.decode_payload(raw_fixture)
            items, total = pi.validate_page(payload)
            rows = [pi.audit_offer(item, args.endpoint, row_index=index) for index, item in enumerate(items)]
            complete = len(items) == total
            audit = {"status": "replayed_for_review", "offers": rows,
                     "endpoints": {args.endpoint: {"complete": complete, "observed_rows": len(items),
                                                   "total_count": total, "pages": [metadata]}}}
        except (ValueError, UnicodeDecodeError):
            audit = {"status": "invalid_replay", "offers": [], "reason": "invalid_response_schema"}
        audit.update(source_feed=pi.SOURCE_FEED, parser_version=pi.PARSER_VERSION,
                     mode="offline_replay", live_attempted=False, production_eligible=False,
                     price_record_count=0, blocked_stage="authenticated_live_validation")
        audit["quarantine_counts"] = dict(Counter(reason for row in audit["offers"] for reason in row["quarantine_reasons"]))
    audit["artifacts_generated_at"] = generated_at
    offers = audit.pop("offers")
    _write_json(output / "offers_for_review.json", offers)
    _write_json(output / "audit.json", audit)
    # No prices, credentials, HTTP response bodies or source-specific errors on stdout.
    print(json.dumps({"status": audit["status"], "live_attempted": audit["live_attempted"],
                      "observed_offers": len(offers), "production_eligible": False,
                      "blocked_stage": audit["blocked_stage"]}))
    return 0 if audit["status"] in {"collected_for_review", "replayed_for_review"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
