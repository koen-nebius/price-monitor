"""Command-line entry point. Network execution always requires --execute."""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from .coverage import coverage_report
from .pipeline import MarketEventPipeline, ingest_fixture
from .registry import SourceRegistry
from .store import AppendOnlyStore
from .tavily import TavilyAdapter, TavilyConfig


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = PACKAGE_DIR / "config" / "source_registry.json"
DEFAULT_TAVILY = PACKAGE_DIR / "config" / "tavily.example.json"
DEFAULT_FIXTURE = PACKAGE_DIR / "fixtures" / "public_market_rows.json"


def _config(path: Path) -> TavilyConfig:
    with path.open() as handle:
        return TavilyConfig.from_dict(json.load(handle))


def _plan(registry: SourceRegistry, mode: str) -> list[dict]:
    rows = []
    for source in registry.all():
        if mode == "daily" and source.daily_extract:
            rows.append(
                {
                    "source_id": source.source_id,
                    "operation": "extract",
                    "official_domains": list(source.official_domains),
                    "urls": list(source.known_urls),
                }
            )
        elif mode == "weekly" and source.weekly_discovery:
            rows.append(
                {
                    "source_id": source.source_id,
                    "operation": "search+extract+crawl",
                    "official_domains": list(source.official_domains),
                    "queries": list(source.discovery_queries),
                    "crawl_roots": list(source.crawl_roots),
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-registry")

    plan = sub.add_parser("plan")
    plan.add_argument("--mode", choices=("daily", "weekly"), required=True)

    fixture = sub.add_parser("ingest-fixture")
    fixture.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    fixture.add_argument("--state-dir", type=Path, required=True)

    coverage = sub.add_parser("coverage")
    coverage.add_argument("--state-dir", type=Path, required=True)

    review_queue = sub.add_parser("review-queue")
    review_queue.add_argument("--state-dir", type=Path, required=True)
    review_queue.add_argument("--output", type=Path)

    promote = sub.add_parser("promote")
    promote.add_argument("--state-dir", type=Path, required=True)
    promote.add_argument("--semantic-key", required=True)
    promote.add_argument("--expected-version-id", required=True)
    promote.add_argument("--reviewer", required=True)
    promote.add_argument("--review-note", required=True)
    promote.add_argument("--confidence", choices=("low", "medium", "high"))

    run = sub.add_parser("run")
    run.add_argument("--mode", choices=("daily", "weekly"), required=True)
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--raw-dir", type=Path)
    run.add_argument("--tavily-config", type=Path, default=DEFAULT_TAVILY)
    run.add_argument("--source-id", action="append")
    run.add_argument("--execute", action="store_true")
    run.add_argument("--fail-on-source-error", action="store_true")

    args = parser.parse_args(argv)
    registry = SourceRegistry.load(args.registry)

    if args.command == "validate-registry":
        print(json.dumps({"status": "ok", "sources": len(registry)}, indent=2))
        return 0
    if args.command == "plan":
        print(json.dumps(_plan(registry, args.mode), indent=2))
        return 0
    if args.command == "ingest-fixture":
        with args.fixture.open() as handle:
            payload = json.load(handle)
        counts = ingest_fixture(AppendOnlyStore(args.state_dir), payload["records"])
        print(json.dumps(counts, indent=2))
        return 0
    if args.command == "coverage":
        metrics = coverage_report(AppendOnlyStore(args.state_dir).current())
        print(json.dumps([metric.to_dict() for metric in metrics], indent=2))
        return 0
    if args.command == "review-queue":
        output = args.output or args.state_dir / "review_queue.jsonl"
        count = AppendOnlyStore(args.state_dir).export_review_queue(output)
        print(json.dumps({"status": "ok", "candidates": count, "output": str(output)}, indent=2))
        return 0
    if args.command == "promote":
        result = AppendOnlyStore(args.state_dir).promote_candidate(
            semantic_key=args.semantic_key,
            expected_version_id=args.expected_version_id,
            reviewer=args.reviewer,
            review_note=args.review_note,
            confidence=args.confidence,
        )
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "run":
        if not args.execute:
            print(json.dumps(_plan(registry, args.mode), indent=2))
            print("Dry plan only. Add --execute to call registered public Tavily sources.")
            return 0
        pipeline = MarketEventPipeline(
            registry,
            TavilyAdapter(_config(args.tavily_config)),
            AppendOnlyStore(args.state_dir, raw_dir=args.raw_dir),
        )
        summary = pipeline.run(args.mode, source_ids=set(args.source_id or []))
        print(json.dumps(dataclasses.asdict(summary), indent=2))
        return 2 if args.fail_on_source_error and summary.errors else 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
