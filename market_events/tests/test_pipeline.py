import tempfile
import unittest

from market_events.pipeline import MarketEventPipeline
from market_events.registry import SourceRegistry, SourceSpec
from market_events.store import AppendOnlyStore
from market_events.tavily import RawDocument


def document(operation="extract"):
    return RawDocument(
        source_id="digitalocean_official",
        url="https://www.digitalocean.com/blog/price-changes-gpus",
        title="GPU updates",
        content=(
            "H100 PAYG changed from $3.39 to $4.41 per GPU hour\n"
            "A future deployment includes 50,000 B300 GPUs and 240 MW of capacity"
        ),
        collected_at="2026-08-04T00:00:00+00:00",
        operation=operation,
        request_id="fixture-request",
    )


def source():
    return SourceSpec(
        source_id="digitalocean_official",
        provider="DigitalOcean",
        official_domains=("digitalocean.com",),
        known_urls=("https://www.digitalocean.com/blog/price-changes-gpus",),
        discovery_queries=("DigitalOcean GPU pricing availability capacity announcement",),
        crawl_roots=("https://www.digitalocean.com/blog",),
        event_kinds=("pricing", "capacity"),
    )


class DailyAdapter:
    def extract(self, source, urls):
        return [document("extract")]


class WeeklyAdapter:
    def __init__(self):
        self.calls = []

    def search(self, source, query):
        self.calls.append("search")
        return [document("search")]

    def extract(self, source, urls):
        self.calls.append("extract")
        return [document("extract")]

    def crawl(self, source, root):
        self.calls.append("crawl")
        return [document("crawl")]


class PipelineTests(unittest.TestCase):
    def test_daily_pipeline_creates_candidates_health_and_unchanged_diff(self):
        registry = SourceRegistry([source()])
        with tempfile.TemporaryDirectory() as tmp:
            store = AppendOnlyStore(tmp)
            pipeline = MarketEventPipeline(registry, DailyAdapter(), store)
            first = pipeline.run("daily")
            second = pipeline.run("daily")

            self.assertEqual(first.documents, 1)
            self.assertEqual(first.candidates, 2)
            self.assertEqual(first.new_events, 2)
            self.assertEqual(second.unchanged_events, 2)
            current = store.current()
            business = [event for event in current if event.event_type != "source_health"]
            health = [event for event in current if event.event_type == "source_health"]
            self.assertEqual(len(business), 2)
            self.assertEqual(len(health), 2)
            self.assertTrue(all(event.review_status == "candidate" for event in business))
            self.assertEqual(len(store.raw_documents()), 1)
            self.assertEqual(len(store.coverage_history()), 2)
            self.assertEqual(
                {row["mode"] for row in store.coverage_history()},
                {"daily"},
            )
            self.assertTrue(
                all(row["metrics"] for row in store.coverage_history())
            )
            latest_metrics = {
                (row["event_type"], row["field"]): row
                for row in store.coverage_history()[-1]["metrics"]
            }
            self.assertEqual(
                latest_metrics[("source_health", "documents_extracted")][
                    "applicable_count"
                ],
                1,
            )

    def test_weekly_pipeline_uses_search_extract_and_crawl(self):
        registry = SourceRegistry([source()])
        adapter = WeeklyAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            store = AppendOnlyStore(tmp)
            summary = MarketEventPipeline(registry, adapter, store).run("weekly")
            self.assertEqual(len(store.coverage_history()), 1)
        self.assertEqual(adapter.calls, ["search", "extract", "crawl"])
        self.assertEqual(summary.sources, 1)
        # Extract and crawl returned the same document, so content dedupe keeps one.
        self.assertEqual(summary.documents, 1)


if __name__ == "__main__":
    unittest.main()
