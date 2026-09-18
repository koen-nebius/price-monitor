"""Aggregator coverage survives source assembly, comparison and publication."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import diff
from comparability import enrich_comparability, is_public_benchmark_eligible
from confluence_storage import to_storage, validate_xml
from report_freshness import publication_records
from schema import PriceRecord
from source_priority import canonicalize_provider_sources

AS_OF = "2026-09-18T12:00:00Z"


def offer(provider="cp_coreweave", price=3.0, **extra):
    values = dict(provider=provider, gpu_model="H100", gpu_count=8,
                  instance_type="h100-sxm-8", region="us-east-1", consumption_type="on_demand",
                  price_per_hour_usd=price * 8, price_per_gpu_hour_usd=price,
                  fetched_at=AS_OF, source_observed_at=AS_OF, source_feed="computeprices",
                  data_source="aggregator", offer_id="h100-8-us", form_factor="SXM",
                  source_url="https://example.invalid/offer", parser_version="aggregator-offers-1")
    values.update(extra)
    return PriceRecord(**values)


class AggregatorIntegrationTests(unittest.TestCase):
    def test_coreweave_offers_survive_direct_presence_and_count_once(self):
        direct = offer("coreweave", 6, source_feed="", offer_id="", data_source="web_scrape")
        raw = [direct, offer(), offer(price=4, region="eu-west-1", offer_id="h100-8-eu")]
        rows = canonicalize_provider_sources(raw)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r.provider for r in rows}, {"coreweave"})
        self.assertEqual(raw[1].provider, "cp_coreweave")
        position = diff._position_for_tier(rows, "H100", {"on_demand"}, "on-demand")
        self.assertEqual(position["total_peers"], 1)
        self.assertEqual(position["cheapest_peer"], 3)
        self.assertIn("via ComputePrices", diff._build_executive_table(rows))

    def test_source_and_offer_ids_prevent_diff_collisions(self):
        rows = canonicalize_provider_sources([
            offer(offer_id="secure", offer_variant="Secure"),
            offer(price=2, offer_id="community", offer_variant="Community"),
            offer("sf_coreweave", source_feed="shadeform", offer_id="third-offer")])
        self.assertEqual(len({diff.record_key(r) for r in rows}), 3)
        changes = diff.compute_diff([], rows)
        self.assertEqual(len(changes), 3)
        self.assertEqual(len(diff._publication_diffs(changes, rows)), 3)

    def test_aggregator_price_change_is_not_provider_repricing(self):
        old = canonicalize_provider_sources([offer(fetched_at="2026-09-17T12:00:00Z")])[0]
        new = replace(old, price_per_gpu_hour_usd=2, price_per_hour_usd=16, fetched_at=AS_OF)
        with patch.object(diff, "_recent_price_levels", return_value={}):
            changes = diff.compute_diff([old], [new])
        self.assertEqual(changes[0].change_type, "aggregator_update")
        self.assertEqual(diff._group_significant_moves(changes), [])
        self.assertIn("aggregator_update", diff._secondary_changes_html(changes))
        self.assertEqual(diff._publication_diffs(changes, [new]), changes)

    def test_source_switch_is_not_repricing(self):
        old = offer("coreweave", source_feed="", offer_id="", data_source="web_scrape")
        new = canonicalize_provider_sources([offer(price=2)])[0]
        self.assertEqual({d.change_type for d in diff.compute_diff([old], [new])}, {"added", "removed"})

    def test_new_fetch_does_not_refresh_stale_upstream_quote(self):
        stale = offer(source_observed_at="2026-09-01T12:00:00Z")
        eligible, notices = publication_records([stale], AS_OF)
        self.assertEqual(eligible, [])
        self.assertIn("aggregator source update", notices[0])
        self.assertEqual(stale.fetched_at, AS_OF)

    def test_upstream_iso_fractional_precision_is_not_misclassified_invalid(self):
        for fraction in ("1", "12", "1234", "12345", "123456789"):
            row = offer(source_observed_at=f"2026-09-18T10:00:48.{fraction}+00:00")
            self.assertEqual(len(publication_records([row], AS_OF)[0]), 1)

    def test_missing_dates_and_unavailable_offers_remain_reference_only(self):
        rows = [offer(source_observed_at=""), offer(available=False)]
        self.assertEqual(publication_records(rows, AS_OF)[0], [])
        self.assertTrue(all(not is_public_benchmark_eligible(r) for r in rows))
        page = diff._aggregator_offers_html(rows, AS_OF)
        self.assertIn("Source date unknown", page)
        self.assertIn("Reported unavailable", page)
        self.assertIn("$3.0000", page)

    def test_aggregator_cannot_inherit_direct_only_cluster_assumptions(self):
        row = canonicalize_provider_sources([offer("sf_crusoe", source_feed="shadeform",
                                                  gpu_count=1, node_gpus=1)])[0]
        enrich_comparability([row])
        self.assertEqual(row.node_gpus, 1)
        unknown = offer(form_factor="unknown")
        enrich_comparability([unknown])
        self.assertEqual(unknown.form_factor, "unknown")
        sliced = offer(gpu_count=1, node_gpus=8)
        self.assertFalse(diff._is_cluster_peer(sliced))

    def test_conflicting_source_observations_are_retained_but_not_compared(self):
        raw = [offer(price=3), offer(price=4)]
        rows = canonicalize_provider_sources(raw)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not r.comparison_eligible for r in rows))
        self.assertEqual(publication_records(rows, AS_OF)[0], [])
        self.assertTrue(all(r.comparison_eligible for r in raw))

    def test_nebius_marketplace_listing_cannot_replace_our_own_price_anchor(self):
        rows = canonicalize_provider_sources([
            offer("sf_nebius", price=1, source_feed="shadeform"),
            offer("nebius", price=5, source_feed="", offer_id="", data_source="official_api")])
        eligible, notices = publication_records(rows, AS_OF)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].price_per_gpu_hour_usd, 5)
        self.assertFalse(is_public_benchmark_eligible(rows[0]))
        self.assertIn("own-price anchor", notices[0])
        self.assertIn("$1.0000", diff._aggregator_offers_html(rows, AS_OF))

    def test_complete_confluence_artifact_has_offer_evidence_and_valid_storage(self):
        rows = canonicalize_provider_sources([offer(offer_variant="Secure & Dedicated"),
                    offer(price=2, offer_id="unavailable", available=False)])
        with patch.object(diff, "_load_intel", return_value=[]), \
             patch.object(diff, "_load_reserve_wins", return_value=([], None, True)):
            page = diff.format_confluence_table(rows, "September 18, 2026")
        self.assertIn("Aggregator offers by configuration, region and term", page)
        self.assertIn("Secure &amp; Dedicated", page)
        self.assertIn("computeprices", page)
        self.assertIn("Reported unavailable", page)
        validate_xml(to_storage(page))


if __name__ == "__main__":
    unittest.main()
