import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from market_events.coverage import coverage_report
from market_events.models import CompetitorOfferEvent
from market_events.normalize import PublicDocumentNormalizer, normalize_fixture_row
from market_events.registry import SourceSpec
from market_events.pipeline import ingest_fixture
from market_events.store import AppendOnlyStore
from market_events.tavily import RawDocument


ROOT = Path(__file__).resolve().parents[1]


def fixture_rows():
    with (ROOT / "fixtures" / "public_market_rows.json").open() as handle:
        return json.load(handle)["records"]


class ModelsStoreCoverageTests(unittest.TestCase):
    def test_fixture_normalizes_to_twelve_verified_events(self):
        events = [normalize_fixture_row(row) for row in fixture_rows()]
        self.assertEqual(len(events), 12)
        self.assertEqual(sum(event.event_type == "competitor_offer" for event in events), 6)
        self.assertEqual(sum(event.event_type == "capacity_announcement" for event in events), 6)
        self.assertTrue(all(event.review_status == "verified" for event in events))

    def test_price_change_requires_previous_price(self):
        with self.assertRaisesRegex(ValueError, "previous_price"):
            CompetitorOfferEvent(
                source_id="source",
                source_url="https://example.com/pricing",
                provider="Example",
                observed_at="2026-08-04T00:00:00+00:00",
                event_date="2026-08-04",
                evidence_text="H100 price changed.",
                gpu_model="H100",
                product_name="H100",
                consumption_type="PAYG",
                price_usd_per_gpu_hour=4.0,
                is_price_change=True,
            ).validate()

    def test_candidate_diff_ignores_refresh_timestamps_and_table_columns(self):
        source = SourceSpec(
            source_id="example",
            provider="Example",
            official_domains=("example.com",),
            event_kinds=("pricing", "capacity"),
        )
        first_document = RawDocument(
            source_id="example",
            url="https://example.com/pricing",
            title="Pricing",
            content=(
                "| H100 | $4.00 | $3.50 |\n"
                "A deployment includes 10,000 H100 GPUs"
            ),
            collected_at="2026-08-04T00:00:00+00:00",
            operation="extract",
        )
        refreshed_document = replace(
            first_document,
            collected_at="2026-08-05T00:00:00+00:00",
        )
        normalizer = PublicDocumentNormalizer()
        first = normalizer.normalize(source, first_document)
        refreshed = normalizer.normalize(source, refreshed_document)
        self.assertEqual(len(first), 2)
        offer = next(event for event in first if event.event_type == "competitor_offer")
        self.assertFalse(offer.is_price_change)
        self.assertIsNone(offer.previous_price_usd_per_gpu_hour)
        self.assertEqual(offer.price_usd_per_gpu_hour, 3.50)
        self.assertEqual(
            [event.semantic_key() for event in first],
            [event.semantic_key() for event in refreshed],
        )
        self.assertEqual(
            [event.content_hash() for event in first],
            [event.content_hash() for event in refreshed],
        )

    def test_explicit_from_to_price_change_and_gpu_model_number_guard(self):
        source = SourceSpec(
            source_id="example",
            provider="Example",
            official_domains=("example.com",),
            event_kinds=("pricing", "capacity"),
        )
        document = RawDocument(
            source_id="example",
            url="https://example.com/pricing",
            title="Pricing",
            content=(
                "H100 PAYG changed from $3.39 to $4.41 per GPU hour\n"
                "How long is the waiting period to reserve an NVIDIA A100 GPU?"
            ),
            collected_at="2026-08-04T00:00:00+00:00",
            operation="extract",
        )
        events = PublicDocumentNormalizer().normalize(source, document)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].is_price_change)
        self.assertEqual(events[0].previous_price_usd_per_gpu_hour, 3.39)
        self.assertEqual(events[0].price_usd_per_gpu_hour, 4.41)

    def test_append_only_store_dedupes_and_versions_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AppendOnlyStore(tmp)
            first = ingest_fixture(store, fixture_rows())
            second = ingest_fixture(store, fixture_rows())
            self.assertEqual(first, {"new": 12, "changed": 0, "unchanged": 0})
            self.assertEqual(second, {"new": 0, "changed": 0, "unchanged": 12})
            self.assertEqual(len(store.versions()), 12)
            self.assertEqual(len(store.current(include_health=False)), 12)

            event = normalize_fixture_row(fixture_rows()[0])
            changed = replace(event, price_usd_per_gpu_hour=3.30)
            diff = store.record(changed)
            self.assertEqual(diff["change_type"], "changed")
            self.assertIn("price_usd_per_gpu_hour", diff["changed_fields"])
            self.assertEqual(len(store.versions()), 13)
            old = [row for row in store.versions() if row["semantic_key"] == event.semantic_key()][0]
            self.assertEqual(old["price_usd_per_gpu_hour"], 3.26)

    def test_raw_documents_are_content_deduped_without_rewrite(self):
        document = RawDocument(
            source_id="source",
            url="https://example.com/pricing",
            title="Pricing",
            content="H100 $4.00",
            collected_at="2026-08-04T00:00:00+00:00",
            operation="extract",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = AppendOnlyStore(tmp)
            self.assertTrue(store.append_raw(document))
            self.assertFalse(store.append_raw(document))
            self.assertEqual(len(store.raw_documents()), 1)

    def test_raw_documents_can_be_kept_outside_committed_state(self):
        document = RawDocument(
            source_id="source",
            url="https://example.com/pricing",
            title="Pricing",
            content="Full public page body that should stay in an artifact.",
            collected_at="2026-08-04T00:00:00+00:00",
            operation="extract",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            raw_dir = root / "raw-artifact"
            store = AppendOnlyStore(state_dir, raw_dir=raw_dir)
            self.assertTrue(store.append_raw(document))
            self.assertFalse((state_dir / "raw_documents.jsonl").exists())
            self.assertTrue((raw_dir / "raw_documents.jsonl").exists())

    def test_review_queue_and_promotion_are_version_locked_and_append_only(self):
        candidate = replace(
            normalize_fixture_row(fixture_rows()[0]),
            review_status="candidate",
            confidence="low",
            extraction_method="public_page_regex_candidate",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AppendOnlyStore(root / "state")
            store.record(candidate)
            queue_path = root / "review_queue.jsonl"
            self.assertEqual(store.export_review_queue(queue_path), 1)
            queue = [json.loads(line) for line in queue_path.read_text().splitlines()]
            self.assertEqual(queue[0]["semantic_key"], candidate.semantic_key())
            self.assertNotIn("content", queue[0])
            self.assertEqual(
                queue[0]["candidate"]["price_usd_per_gpu_hour"],
                candidate.price_usd_per_gpu_hour,
            )
            self.assertNotIn("content", queue[0]["candidate"])

            result = store.promote_candidate(
                semantic_key=queue[0]["semantic_key"],
                expected_version_id=queue[0]["expected_version_id"],
                reviewer="pricing-reviewer",
                review_note="Matched the price and effective date to the official page.",
                confidence="high",
            )
            self.assertEqual(result["change"]["change_type"], "changed")
            self.assertIn("review_status", result["change"]["changed_fields"])
            self.assertEqual(len(store.versions()), 2)
            self.assertEqual(len(store.reviews()), 1)
            self.assertEqual(store.current(include_health=False)[0].review_status, "verified")
            self.assertEqual(store.export_review_queue(queue_path), 0)

            with self.assertRaisesRegex(ValueError, "refresh the review queue"):
                store.promote_candidate(
                    semantic_key=queue[0]["semantic_key"],
                    expected_version_id=queue[0]["expected_version_id"],
                    reviewer="pricing-reviewer",
                    review_note="Attempted stale review.",
                )

    def test_coverage_uses_applicability_not_wide_table_nulls(self):
        events = [normalize_fixture_row(row) for row in fixture_rows()]
        metrics = {(item.event_type, item.field): item for item in coverage_report(events)}
        term = metrics[("competitor_offer", "term_months")]
        self.assertEqual(term.applicable_count, 3)
        self.assertEqual(term.populated_count, 3)
        previous = metrics[("competitor_offer", "previous_price_usd_per_gpu_hour")]
        self.assertEqual(previous.applicable_count, 6)
        self.assertEqual(previous.coverage_pct, 100.0)
        gpu_quantity = metrics[("capacity_announcement", "announced_gpus")]
        self.assertEqual(gpu_quantity.applicable_count, 3)
        self.assertEqual(gpu_quantity.coverage_pct, 100.0)
        bundle = metrics[("competitor_offer", "bundle_status")]
        self.assertEqual(bundle.coverage_pct, 0.0)


if __name__ == "__main__":
    unittest.main()
