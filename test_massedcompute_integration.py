"""Offline wiring and fallback tests for the Massed inventory integration."""
import os
import subprocess
import sys
import unittest

from schema import PriceRecord
from source_priority import prefer_massed_direct


def offer(provider, gpu="B200", tier="on_demand", sku="test", price=5.43):
    return PriceRecord(provider, gpu, 8, sku, "unspecified", tier, price * 8, price)


class IntegrationTests(unittest.TestCase):
    def test_fresh_direct_replaces_only_covered_gpu_and_tier(self):
        direct = [offer("massedcompute", sku="one"), offer("massedcompute", sku="two")]
        uncovered = [offer("cp_massedcompute", "H200"), offer("cp_massedcompute", tier="spot")]
        rows = direct + uncovered + [offer("cp_massedcompute"), offer("sf_massedcompute")]
        self.assertEqual(prefer_massed_direct(rows, True), direct + uncovered)

    def test_live_failure_keeps_aggregator_not_duplicate_cached_direct(self):
        fallback = offer("cp_massedcompute")
        rows = [offer("massedcompute"), fallback, offer("sf_massedcompute")]
        self.assertEqual(prefer_massed_direct(rows, False), [fallback])

    def test_unsupported_direct_price_does_not_displace_fallback(self):
        fallback = offer("cp_massedcompute")
        self.assertEqual(prefer_massed_direct([offer("massedcompute", price=.01), fallback], True), [fallback])

    def test_missing_direct_does_not_drop_sole_shadeform(self):
        rows = [offer("sf_massedcompute")]
        self.assertEqual(prefer_massed_direct(rows, False), rows)

    def test_unrelated_provider_is_unchanged(self):
        rows = [offer("coreweave"), offer("cp_massedcompute")]
        self.assertEqual(prefer_massed_direct(rows, True), rows)

    def test_account_catalogue_label_survives_slack_renderers(self):
        from schema import DiffEntry
        from diff import format_slack_summary, format_slack_message, _provider_display
        change = DiffEntry("massedcompute", "B200", "unspecified", "on_demand",
                           "gpu_8x_b200_SXM6", "price_change", 5.0, 6.0, 20.0)
        for render in (format_slack_summary, format_slack_message):
            text = render([change], "2026-09-17", "https://example.invalid")
            self.assertIn("Massed Compute (account catalogue)", text)
        self.assertEqual(_provider_display("massedcompute"), "Massed Compute (account catalogue)")

    def test_account_catalogue_does_not_change_public_rtx_median(self):
        from diff import _rtx_market_stats
        account = offer("massedcompute", "RTX6000", price=1.0)
        account.price_basis = "account_catalog"
        public = [offer("nebius", "RTX6000", price=4.0),
                  offer("aws", "RTX6000", price=6.0),
                  offer("azure", "RTX6000", price=8.0)]
        self.assertEqual(_rtx_market_stats(public + [account]), _rtx_market_stats(public))

    def test_registration_is_optional_and_dispatchable(self):
        code = """
import config
from capacity import config as capacity_config
from main import PROVIDER_MODULES
enabled = bool(__import__('os').environ.get('MASSED_COMPUTE_API_KEY', '').strip())
assert ('massedcompute' in config.PROVIDERS) == enabled
assert ('massedcompute' in capacity_config.PROVIDERS) == enabled
assert PROVIDER_MODULES['massedcompute'] == 'massedcompute'
assert 'massedcompute' not in config.PROVIDER_TIERS['enterprise_gpu_cloud']
assert 'massedcompute' not in capacity_config.PRICE_JOIN_PEERS
"""
        for value in ("", "test-placeholder"):
            env = dict(os.environ, MASSED_COMPUTE_API_KEY=value)
            subprocess.run([sys.executable, "-c", code], env=env, check=True,
                           capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
