"""Vultr source selection and daily-orchestrator wiring; no network or writes."""
import io
import json
import unittest
from unittest.mock import patch

from schema import PriceRecord
from source_priority import exclude_superseded_vultr


def offer(provider, price=3.5):
    return PriceRecord(provider, "B200", 8, "node", "unspecified", "on_demand", price * 8, price)


class VultrIntegrationTests(unittest.TestCase):
    def test_direct_catalogue_replaces_unqualified_aggregator_and_preserves_others(self):
        direct = offer("vultr", 8.5)
        direct.price_basis = "public_catalog_ondemand_disabled"
        other = offer("coreweave", 8.6)
        self.assertEqual(exclude_superseded_vultr([
            offer("cp_vultr"), direct, offer("sf_vultr"), other,
        ]), [direct, other])

    def test_failed_direct_does_not_revive_known_misclassified_cache(self):
        other = offer("coreweave", 8.6)
        rows = [offer(p) for p in ("cp_vultr", "cp_vultr-cloud", "cp_vultr_cloud", "sf_vultr")]
        self.assertEqual(exclude_superseded_vultr(rows + [other]), [other])

    def test_live_aggregator_vultr_is_not_a_second_payg_observation(self):
        from fetchers import computeprices
        payload = {"data": [{"provider": "Vultr", "gpu": "b200", "gpu_count": 8,
                             "total_hourly_usd": 28, "pricing_type": "on_demand"}]}
        with patch.object(computeprices.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            self.assertEqual(computeprices._fetch_slug("b200", None, "2026-09-17", set()), [])

    def test_registered_daily_dispatcher_calls_direct_fetcher_without_credentials(self):
        import config
        from main import _fetch_provider
        from fetchers import vultr
        self.assertIn("vultr", config.PROVIDERS)
        self.assertNotIn("vultr", config.PROVIDER_TIERS["enterprise_gpu_cloud"])
        expected = [offer("vultr", 8.5)]
        with patch.object(vultr, "fetch", return_value=expected) as fetch:
            self.assertEqual(_fetch_provider("vultr"), expected)
            fetch.assert_called_once_with()

    def test_restricted_catalogue_cannot_reappear_as_public_history_anchor(self):
        from history import _cheapest_per_combo
        restricted = offer("vultr", 8.5)
        restricted.price_basis = "public_catalog_ondemand_disabled"
        public = offer("vultr", 9.0)
        public.price_basis = "public_catalog"
        self.assertEqual(list(_cheapest_per_combo([restricted, public]).values()), [public])
        self.assertEqual(_cheapest_per_combo([restricted]), {})


if __name__ == "__main__":
    unittest.main()
