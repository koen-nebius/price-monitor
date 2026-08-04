"""Extract, normalize, deduplicate, diff, and record source health."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .coverage import coverage_report
from .models import MarketEvent, SourceHealthEvent, stable_hash
from .normalize import PublicDocumentNormalizer, normalize_fixture_row
from .registry import SourceRegistry, SourceSpec
from .store import AppendOnlyStore
from .tavily import RawDocument, TavilyAdapter


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    sources: int
    documents: int
    candidates: int
    new_events: int
    changed_events: int
    unchanged_events: int
    errors: int


class MarketEventPipeline:
    def __init__(
        self,
        registry: SourceRegistry,
        adapter: TavilyAdapter,
        store: AppendOnlyStore,
        normalizer: PublicDocumentNormalizer | None = None,
    ):
        self.registry = registry
        self.adapter = adapter
        self.store = store
        self.normalizer = normalizer or PublicDocumentNormalizer()

    def run(self, mode: str, *, source_ids: set[str] | None = None) -> RunSummary:
        if mode not in {"daily", "weekly"}:
            raise ValueError("mode must be daily or weekly")
        now = datetime.now(timezone.utc)
        run_id = f"{mode}-{now.strftime('%Y%m%dT%H%M%SZ')}-{stable_hash(now.isoformat())[:6]}"
        totals = {
            "sources": 0,
            "documents": 0,
            "candidates": 0,
            "new_events": 0,
            "changed_events": 0,
            "unchanged_events": 0,
            "errors": 0,
        }
        for source in self.registry.all():
            if source_ids and source.source_id not in source_ids:
                continue
            if mode == "daily" and not source.daily_extract:
                continue
            if mode == "weekly" and not source.weekly_discovery:
                continue
            totals["sources"] += 1
            stats = self._run_source(source, mode, run_id, now)
            for key in totals:
                if key != "sources":
                    totals[key] += stats.get(key, 0)
        summary = RunSummary(run_id=run_id, **totals)
        current = self.store.current()
        coverage_population = [
            event
            for event in current
            if event.event_type != "source_health" or event.run_id == summary.run_id
        ]
        metrics = [metric.to_dict() for metric in coverage_report(coverage_population)]
        self.store.append_coverage_snapshot(
            run_id=summary.run_id,
            mode=mode,
            metrics=metrics,
        )
        return summary

    def _run_source(self, source: SourceSpec, mode: str, run_id: str, now: datetime) -> dict:
        documents: list[RawDocument] = []
        discovered = 0
        error_code = ""
        try:
            if mode == "daily":
                documents.extend(self.adapter.extract(source, list(source.known_urls)))
            else:
                search_documents: list[RawDocument] = []
                for query in source.discovery_queries:
                    search_documents.extend(self.adapter.search(source, query))
                discovered = len({document.url for document in search_documents})
                urls = sorted({document.url for document in search_documents})
                extracted = self.adapter.extract(source, urls) if urls else []
                documents.extend(extracted or search_documents)
                for root in source.crawl_roots:
                    documents.extend(self.adapter.crawl(source, root))
        except Exception as exc:
            error_code = type(exc).__name__

        unique: dict[tuple[str, str], RawDocument] = {}
        for document in documents:
            unique[(document.url, document.content_hash())] = document
        documents = list(unique.values())

        candidates: list[MarketEvent] = []
        for document in documents:
            self.store.append_raw(document)
            candidates.extend(self.normalizer.normalize(source, document))

        deduped = {}
        for event in candidates:
            deduped[(event.semantic_key(), event.content_hash())] = event
        candidates = list(deduped.values())

        change_counts = {"new": 0, "changed": 0, "unchanged": 0}
        for event in candidates:
            change = self.store.record(event)
            change_counts[change["change_type"]] += 1

        status = "error" if error_code and not documents else "partial" if error_code else "ready"
        source_url = (
            source.known_urls[0]
            if source.known_urls
            else source.crawl_roots[0]
            if source.crawl_roots
            else f"https://{source.official_domains[0]}"
        )
        health = SourceHealthEvent(
            source_id=source.source_id,
            source_url=source_url,
            provider=source.provider,
            observed_at=now.isoformat(),
            event_date=now.date().isoformat(),
            evidence_text=(
                f"{mode} public-source run: {len(documents)} documents, "
                f"{len(candidates)} review candidates."
            ),
            confidence="high",
            review_status="verified",
            extraction_method="pipeline_health",
            run_id=run_id,
            status=status,
            operation=mode,
            documents_discovered=discovered,
            documents_extracted=len(documents),
            candidate_events=len(candidates),
            new_events=change_counts["new"],
            changed_events=change_counts["changed"],
            unchanged_events=change_counts["unchanged"],
            failed_urls=1 if error_code else 0,
            error_code=error_code,
        )
        self.store.record(health)
        return {
            "documents": len(documents),
            "candidates": len(candidates),
            "new_events": change_counts["new"],
            "changed_events": change_counts["changed"],
            "unchanged_events": change_counts["unchanged"],
            "errors": 1 if error_code else 0,
        }


def ingest_fixture(store: AppendOnlyStore, rows: Iterable[dict]) -> dict[str, int]:
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    for row in rows:
        change = store.record(normalize_fixture_row(row))
        counts[change["change_type"]] += 1
    return counts
