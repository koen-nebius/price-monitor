"""Offline Massed inventory contract tests, using the observed API shape."""
import copy
import unittest
from unittest.mock import patch

from comparability import enrich_comparability
from fetchers import massedcompute
from schema import PriceRecord

NOW = "2026-09-17T12:00:00+00:00"


def offer(sku="gpu_8x_b200_SXM6", description="8x B200 SXM6", cents=4346):
    return {
        "instance_type": {
            "name": sku, "description": description, "price_cents_per_hour": cents,
            "specs": {"vcpu_count": 280, "memory_gib": 2800, "storage_gb": 6000},
        },
        "regions_with_capacity_available": [{"name": "us-central-3", "description": "Des Moines, IA"}],
        "capacity_available": 0,
    }


class MassedPriceTests(unittest.TestCase):
    def normalize(self, sku="gpu_8x_b200_SXM6", description="8x B200 SXM6", cents=4346):
        return massedcompute.normalize_offer(sku, offer(sku, description, cents), NOW)

    def test_real_b200_instance_cents_and_specs(self):
        row = self.normalize()
        self.assertEqual((row.gpu_model, row.gpu_count, row.instance_type), ("B200", 8, "gpu_8x_b200_SXM6"))
        self.assertAlmostEqual(row.price_per_hour_usd, 43.46)
        self.assertAlmostEqual(row.price_per_gpu_hour_usd, 5.4325)
        self.assertEqual((row.vcpu, row.ram_gb, row.storage_gb), (280, 2800, 6000))
        self.assertEqual((row.form_factor, row.interconnect, row.node_gpus), ("SXM", "unknown", 8))
        self.assertEqual((row.region, row.price_basis, row.data_source), ("unspecified", "account_catalog", "official_api"))
        self.assertEqual((row.fetched_at, row.parser_version), (NOW, "massedcompute-price-1.0"))

    def test_real_b300_and_spot_normalization(self):
        b300 = self.normalize("gpu_8x_b300_SXM6", "8x B300 SXM6", 5280)
        self.assertAlmostEqual(b300.price_per_gpu_hour_usd, 6.60)
        spot = self.normalize("gpu_4x_h200_nvl_nvlink_spot", "4x H200 NVL (141GB) NVLink [Spot]", 1375)
        self.assertEqual((spot.gpu_model, spot.gpu_count, spot.form_factor, spot.consumption_type), ("H200", 4, "NVL", "spot"))
        self.assertAlmostEqual(spot.price_per_gpu_hour_usd, 3.4375)
        self.assertEqual(spot.interconnect, "unknown")

    def test_form_factors_and_unknown_survive_enrichment(self):
        examples = [
            ("gpu_1x_h100", "1x H100 (80GB)", "unknown"),
            ("gpu_1x_H100_SXM5", "1x H100 SXM5 (80GB)", "SXM"),
            ("gpu_1x_h100_nvl", "1x H100 NVL", "NVL"),
            ("gpu_1x_h100_pcie", "1x H100 PCIe", "PCIe"),
            ("gpu_1x_l40s", "1x L40S (48GB)", "PCIe"),
            ("gpu_1x_pro_6000_blackwell", "1x RTX PRO 6000 Blackwell (96GB)", "PCIe"),
        ]
        for sku, description, form in examples:
            with self.subTest(sku=sku):
                row = enrich_comparability([self.normalize(sku, description, 273)])[0]
                self.assertEqual((row.form_factor, row.interconnect), (form, "unknown"))

    def test_exact_sku_storage_variants_and_spot_are_not_deduplicated(self):
        sku = "gpu_1x_pro_6000_blackwell"
        low = sku + "_low_storage"
        spot = "gpu_1x_h100_spot"
        payload = {"gpu_inventory": {
            sku: offer(sku, "1x RTX PRO 6000 Blackwell (96GB)", 219),
            low: offer(low, "1x RTX PRO 6000 Blackwell (96GB)", 219),
            spot: offer(spot, "1x H100 (80GB) [Spot]", 140),
        }}
        payload["gpu_inventory"][low]["instance_type"]["specs"]["storage_gb"] = 100
        rows = massedcompute.parse(payload, NOW)
        self.assertEqual([r.instance_type for r in rows], [sku, low, spot])
        self.assertEqual([r.storage_gb for r in rows], [6000, 100, 6000])
        self.assertEqual(rows[-1].consumption_type, "spot")

    def test_gpu_models_use_tokens_and_skip_cpu_or_untracked_hardware(self):
        for sku in ["cpu_mini_amd_epyc", "gpu_1x_6000_ada", "gpu_1x_pro_4500_blackwell", "gpu_1x_l40", "gpu_1x_a100", "gpu_1x_gh200", "gpu_1x_h1000"]:
            with self.subTest(sku=sku):
                self.assertIsNone(self.normalize(sku, "", 100))
        self.assertEqual(self.normalize("gpu_8x_gb200", "8x GB200", 10000).gpu_model, "GB200")
        self.assertEqual(self.normalize("gpu_8x_gb300", "8x GB300", 10000).gpu_model, "GB300")

    def test_count_mismatch_invalid_counts_and_identity_are_rejected(self):
        for sku, description in [
            ("gpu_0x_h100", "0x H100"), ("gpu_-1x_h100", "1x H100"),
            ("gpu_1.5x_h100", "1x H100"), ("gpu_8x_h100", "4x H100"),
            ("gpu_8x_h100", "8× H200"), ("gpu_8x_h100_sxm5", "8x H100 PCIe"),
        ]:
            with self.subTest(sku=sku, description=description):
                self.assertIsNone(self.normalize(sku, description, 100))
        for key, value in [("gpus", 4), ("gpu_count", 8.5), ("gpus", True)]:
            item = offer()
            item["instance_type"]["specs"][key] = value
            self.assertIsNone(massedcompute.normalize_offer("gpu_8x_b200_SXM6", item))
        item = offer()
        item["instance_type"]["name"] = "gpu_4x_b200_SXM6"
        self.assertIsNone(massedcompute.normalize_offer("gpu_8x_b200_SXM6", item))

    def test_invalid_prices_and_malformed_entries_do_not_produce_records(self):
        for cents in [0, -1, None, True, "4346", float("nan"), float("inf"), {}, []]:
            with self.subTest(cents=cents):
                self.assertIsNone(self.normalize(cents=cents))
        for item in [None, [], {}, {"instance_type": None}]:
            self.assertIsNone(massedcompute.normalize_offer("gpu_8x_b200_SXM6", item))
        for payload in [None, {}, {"gpu_inventory": []}]:
            with self.assertRaises(ValueError):
                massedcompute.parse(payload, NOW)

    def test_unknown_terms_do_not_become_ondemand(self):
        for term in ["reserved", "committed", "preemptible"]:
            self.assertIsNone(self.normalize("gpu_8x_h100_" + term, "8x H100", 1000))

    def test_stock_does_not_change_price_region_or_catalogue_coverage(self):
        original = offer()
        changed = copy.deepcopy(original)
        changed["capacity_available"] = 100
        changed["regions_with_capacity_available"] = []
        self.assertEqual(massedcompute.normalize_offer("gpu_8x_b200_SXM6", original),
                         massedcompute.normalize_offer("gpu_8x_b200_SXM6", changed))

    def test_optional_schema_fields_roundtrip_and_legacy_defaults(self):
        row = self.normalize()
        self.assertEqual(PriceRecord.from_dict(row.to_dict()), row)
        legacy = row.to_dict()
        del legacy["price_basis"]
        del legacy["storage_gb"]
        old = PriceRecord.from_dict(legacy)
        self.assertEqual(old.price_basis, "")
        self.assertIsNone(old.storage_gb)

    def test_fetch_is_read_only_and_sanitizes_errors(self):
        with patch.object(massedcompute, "fetch_inventory", return_value={"gpu_inventory": {"gpu_8x_b200_SXM6": offer()}}) as fetch:
            self.assertEqual(len(massedcompute.fetch()), 1)
            fetch.assert_called_once_with()
        with patch.object(massedcompute, "fetch_inventory", side_effect=RuntimeError("secret detail")):
            with self.assertLogs(massedcompute.logger, level="ERROR") as log:
                self.assertEqual(massedcompute.fetch(), [])
            self.assertNotIn("secret detail", " ".join(log.output))


if __name__ == "__main__":
    unittest.main()
