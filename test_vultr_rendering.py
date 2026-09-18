"""Catalogue tariffs must stay visible without becoming purchasable price claims."""
import unittest
from dataclasses import replace
from unittest.mock import patch

import diff
from comparability import (
    QUALIFIED_CATALOGUE_BASES, enrich_comparability,
    is_public_benchmark_eligible, is_qualified_catalogue_reference,
)
from schema import PriceRecord


def offer(provider="vultr", gpu="H100", basis="public_catalog_ondemand_disabled",
          tier="on_demand", price=0.75, count=8):
    return PriceRecord(provider, gpu, count, "vcg-h100-8", "unspecified", tier,
                       price * count, price, source_url="https://api.vultr.com/v2/plans-metal",
                       price_basis=basis, data_source="official_api")


class VultrRenderingTests(unittest.TestCase):
    def test_qualification_follows_record_not_provider(self):
        for basis in QUALIFIED_CATALOGUE_BASES:
            record = offer(basis=basis)
            self.assertTrue(is_qualified_catalogue_reference(record))
            self.assertFalse(is_public_benchmark_eligible(record))
        enabled = offer(basis="public_catalog")
        self.assertFalse(is_qualified_catalogue_reference(enabled))
        self.assertTrue(is_public_benchmark_eligible(enabled))
        self.assertFalse(is_public_benchmark_eligible(offer(basis="account_catalog")))
        self.assertTrue(is_public_benchmark_eligible(offer("lambda", basis="")))

    def test_unknown_direct_form_factor_remains_unknown(self):
        record = offer(basis="public_catalog")
        enrich_comparability([record])
        self.assertEqual((record.form_factor, record.interconnect), ("unknown", "unknown"))
        explicit = replace(record, form_factor="SXM", interconnect="InfiniBand")
        enrich_comparability([explicit])
        self.assertEqual((explicit.form_factor, explicit.interconnect), ("SXM", "InfiniBand"))
        peer = offer("crusoe", basis="")
        enrich_comparability([peer])
        # A provider/GPU name alone cannot establish Crusoe's source configuration.
        self.assertEqual((peer.form_factor, peer.interconnect), ("unknown", "unknown"))

    def test_restricted_tariff_does_not_win_price_or_peer_comparison(self):
        neb = offer("nebius", basis="", price=4)
        peer = offer("lambda", basis="", price=5)
        restricted = offer(price=.10)
        # Simulate future peer-group membership; qualification still wins.
        tiers = {"enterprise_gpu_cloud": ["lambda", "vultr"]}
        with patch.object(diff, "PROVIDER_TIERS", tiers):
            row = diff._position_for_tier([neb, peer, restricted], "H100", {"on_demand"}, "on_demand")
        self.assertEqual(row["total_peers"], 1)
        self.assertEqual(row["median_peer"], 5)
        self.assertIs(diff._best_price([peer, restricted], "H100", "on_demand"), peer)
        enabled = replace(restricted, price_basis="public_catalog")
        self.assertIs(diff._best_price([peer, enabled], "H100", "on_demand"), enabled)

    def test_restricted_rtx_and_spot_do_not_change_statistics(self):
        rows = [offer("nebius", "RTX6000", basis="", price=4),
                offer("aws", "RTX6000", basis="", price=6),
                offer("azure", "RTX6000", basis="", price=8)]
        restricted = offer(gpu="RTX6000", price=.10)
        self.assertEqual(diff._rtx_market_stats(rows + [restricted]), diff._rtx_market_stats(rows))
        spot = offer(basis="public_catalog_no_locations", tier="preemptible", price=.10)
        peer = offer("lambda", basis="", tier="preemptible", price=2)
        self.assertEqual(diff._representative_spot_floor([spot, peer], "H100"), ("lambda", 2, 1))

    def test_catalogue_changes_not_actionable_moves(self):
        old = replace(offer(price=1), fetched_at="2026-09-16T01:00:00Z")
        new = replace(offer(price=2), fetched_at="2026-09-17T01:00:00Z")
        with patch.object(diff, "_recent_price_levels", return_value={}):
            changes = diff.compute_diff([old], [new])
        self.assertEqual(changes[0].change_type, "catalog_reference_change")
        self.assertEqual(diff._group_significant_moves(changes), [])
        summary = diff.format_slack_summary(changes, "2026-09-17", "https://example.invalid")
        self.assertNotIn("Vultr", summary)
        self.assertNotIn("$1.00→$2.00", summary)
        self.assertNotIn("Vultr", diff._build_price_moves_section(changes))
        # Becoming eligible is not itself evidence of a market repricing.
        with patch.object(diff, "_recent_price_levels", return_value={}):
            transition = diff.compute_diff([old], [replace(new, price_basis="public_catalog")])
            public = diff.compute_diff([replace(old, price_basis="public_catalog")],
                                       [replace(new, price_basis="public_catalog")])
        self.assertEqual(transition[0].change_type, "catalog_reference_change")
        self.assertEqual(public[0].change_type, "price_change")
        self.assertEqual(public[0].delta_pct, 100)

    def test_reference_table_shows_full_instance_and_tier_without_median(self):
        row = offer(price=2.49)
        preemptible = replace(row, consumption_type="preemptible", price_basis="public_catalog_no_locations")
        table = diff._build_qualified_catalogue_section([row, preemptible])
        self.assertIn("Vultr / H100", table)
        self.assertIn("vcg-h100-8", table)
        self.assertIn("8 GPUs · $19.92/instance-hr", table)
        self.assertIn("$2.4900", table)
        self.assertIn("On-demand deployment disabled", table)
        self.assertIn("No deployment locations listed", table)
        self.assertIn("Spot / Preemptible (interruptible)", table)
        self.assertIn("Not listed", table)
        self.assertIn("do not establish live stock", table)
        self.assertNotIn("median", table)
        self.assertEqual(diff._build_qualified_catalogue_section([replace(row, price_basis="public_catalog")]), "")

    def test_confluence_and_slack_keep_reference_separate_from_ordinary_sweep(self):
        rows = [offer("nebius", basis="", price=4), offer(),
                offer(basis="public_catalog_no_locations", tier="preemptible")]
        self.assertNotIn("Vultr", diff._build_peer_tables(rows))
        public = replace(rows[1], price_basis="public_catalog", region="ewr")
        self.assertIn("Vultr", diff._build_peer_tables([rows[0], public]))
        page = diff.format_confluence_table(rows, "2026-09-17")
        self.assertIn("Catalogue prices — deployment restricted or unconfirmed", page)
        self.assertIn("Vultr / H100", page)
        self.assertNotIn("<td>Vultr", page.split("<h2>Catalogue prices — deployment restricted or unconfirmed</h2>")[0])
        thread = diff.format_slack_message([], "2026-09-17", "https://example.invalid", records=rows)
        # The daily Slack reply is delta-only; standing catalogue detail stays on the page.
        self.assertNotIn("Catalogue prices (deployment restricted or unconfirmed): Vultr", thread)
        self.assertNotIn("$0.75", thread)
        self.assertIn("Full change ledger", thread)
        self.assertIn('data-type="expand"', page)
        changes = diff.compute_diff([replace(rows[1], price_per_gpu_hour_usd=.5)], [rows[1]])
        summary = diff.format_slack_summary(changes, "2026-09-17", "https://example.invalid", records=rows)
        self.assertNotIn("→", summary)
        self.assertNotIn("$0.75", summary)

    def test_reference_preserves_observation_time_and_cached_refresh_status(self):
        row = replace(offer(), fetched_at="2026-09-15T14:00:00+02:00")
        for cache_status in ("cache", "cached"):
            status = {"vultr": {"status": cache_status, "cache_age_hours": 47}}
            page = diff.format_confluence_table([row], "2026-09-17", provider_status=status)
            section = page.split("<h2>Catalogue prices — deployment restricted or unconfirmed</h2>")[1].split("<h2>")[0]
            self.assertIn("2026-09-15 12:00:00 UTC", section)
            self.assertIn("Cached; not refreshed this run (cache age 47h)", section)
            self.assertNotIn("Refreshed this run", section)
        live = diff._build_qualified_catalogue_section([row], {"vultr": {"status": "live"}})
        self.assertIn("Refreshed this run", live)
        failed = diff._build_qualified_catalogue_section([row], {"vultr": {"status": "error"}})
        self.assertIn("Fetch failed; not refreshed this run", failed)

    def test_missing_observation_metadata_is_not_presented_as_fresh(self):
        for timestamp, expected in [("", "Observation time unreported"),
                                     ("invalid", "Observation time unreported"),
                                     ("2026-09-15T12:00:00", "Timezone unreported")]:
            section = diff._build_qualified_catalogue_section([replace(offer(), fetched_at=timestamp)])
            self.assertIn(expected, section)
            self.assertIn("Refresh status unreported", section)


if __name__ == "__main__":
    unittest.main()
