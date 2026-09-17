"""Massed stock semantics: catalogue != regional stock != cluster capacity."""
import unittest
import os
import runpy
from unittest.mock import patch

from capacity import config as capacity_config
from capacity.config import PRICE_JOIN_PEERS, SIGNAL_CLASS
from capacity.diff import _key
from capacity.insights import live_reads
from capacity.fetchers import massedcompute
from schema import PriceRecord

NOW = "2026-09-17T12:00:00+00:00"


def entry(regions, capacity=True, gpus=8, consumption_type="on_demand"):
    return {
        "instance_type": {"specs": {"gpus": gpus}},
        "regions_with_capacity_available": regions,
        "capacity_available": capacity,
        "test_consumption_type": consumption_type,
    }


def normalized(sku, item):
    """Stock tests isolate the pricing adapter's separately tested normalizer."""
    if sku == "unsupported":
        return None
    count = item["instance_type"]["specs"]["gpus"]
    return PriceRecord(
        provider="massedcompute", gpu_model="H100", gpu_count=count,
        instance_type=sku, region="global",
        consumption_type=item.get("test_consumption_type", "on_demand"),
        price_per_hour_usd=count * 3.0, price_per_gpu_hour_usd=3.0,
    )


class MassedCapacityTests(unittest.TestCase):
    def setUp(self):
        self.normalizer = patch.object(massedcompute, "normalize_offer", side_effect=normalized)
        self.normalizer.start()
        self.addCleanup(self.normalizer.stop)

    def parse(self, inventory):
        return massedcompute.parse({"gpu_inventory": inventory}, NOW)

    def test_listed_skus_without_regions_never_become_available(self):
        rows = self.parse({
            "zero": entry([], 0),
            "false": entry([], False),
            "positive": entry([], 12),
            "boolean": entry([], True),
            "missing": entry(None),
        })
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r.state == "unknown" and r.region == "unreported" for r in rows))
        self.assertFalse(live_reads(rows, "H100"))
        self.assertEqual(rows[-1].metric_value, None)

    def test_region_and_sku_identity_survives_without_cluster_rollup(self):
        rows = self.parse({
            "h100-1": entry([{"name": "us-east"}, "us-east", {"id": "us-west"}], gpus=1),
            "h100-8": entry([], False, gpus=8),
        })
        self.assertEqual([(r.instance_type, r.region, r.state) for r in rows], [
            ("h100-1", "us-east", "available"),
            ("h100-1", "us-west", "available"),
            ("h100-8", "unreported", "unknown"),
        ])
        self.assertEqual(len({_key(r) for r in rows}), 3)
        self.assertTrue(all(r.region != "global" for r in rows))
        self.assertFalse(live_reads(rows, "H100"))
        self.assertNotIn("massedcompute", PRICE_JOIN_PEERS)

    def test_available_eight_gpu_sku_still_does_not_claim_multinode_stock(self):
        row = self.parse({"h100-8": entry(["us-east"], True)})[0]
        self.assertEqual((row.state, row.metric_type, row.metric_value), ("available", "binary", 1.0))
        self.assertIn("8-GPU SKU", row.detail)
        self.assertIn("multi-node availability not established", row.detail)
        self.assertEqual((row.instance_type, row.fetched_at, row.data_source),
                         ("h100-8", NOW, "official_api"))
        self.assertFalse(live_reads([row], "H100"))

    def test_conflicting_or_malformed_capacity_never_becomes_available(self):
        inventory = {str(i): entry(["us-east"], flag)
                     for i, flag in enumerate([False, 0, -1, None, "false", {}, float("nan")])}
        rows = self.parse(inventory)
        self.assertTrue(all(r.state == "unknown" and r.metric_value is None for r in rows))
        malformed = self.parse({"bad": entry([{"unexpected": "us-east"}])})
        self.assertEqual((malformed[0].state, malformed[0].region), ("unknown", "unreported"))

    def test_counts_are_not_multiplied_by_gpus_or_duplicated_across_regions(self):
        rows = self.parse({"h100-8": entry(["us-east", "us-west"], 100)})
        self.assertTrue(all(r.metric_type == "binary" and r.metric_value == 1 for r in rows))
        self.assertTrue(all("quantity" in r.detail for r in rows))
        self.assertTrue(all("capacity_available=100 (unit unverified)" in r.detail for r in rows))

    def test_spot_and_literal_global_region_do_not_leak_to_ondemand_rollup(self):
        rows = self.parse({"h100-spot": entry(["global"], consumption_type="spot")})
        self.assertEqual(rows[0].consumption_type, "spot")
        self.assertNotEqual(rows[0].region, "global")
        self.assertFalse(live_reads(rows, "H100"))

    def test_missing_stock_flag_can_use_explicit_regional_stock_list(self):
        item = entry(["us-east"])
        del item["capacity_available"]
        self.assertEqual(self.parse({"h100": item})[0].state, "available")

    def test_response_errors_do_not_become_sold_out_observations(self):
        with self.assertRaises(ValueError):
            massedcompute.parse({"gpu_inventory": []}, NOW)
        with patch.object(massedcompute, "fetch_inventory", side_effect=RuntimeError("secret detail")):
            with self.assertLogs(massedcompute.logger, level="ERROR") as log:
                self.assertEqual(massedcompute.fetch(), [])
            self.assertNotIn("secret detail", " ".join(log.output))
        with patch.object(massedcompute, "fetch_inventory", return_value={"gpu_inventory": {"h100": entry([], False)}}):
            self.assertEqual(len(massedcompute.fetch()), 1)

    def test_registry_and_supported_offer_filter(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("massedcompute", runpy.run_path(capacity_config.__file__)["PROVIDERS"])
        with patch.dict(os.environ, {"MASSED_COMPUTE_API_KEY": "synthetic-test-key"}, clear=True):
            self.assertIn("massedcompute", runpy.run_path(capacity_config.__file__)["PROVIDERS"])
        self.assertEqual(SIGNAL_CLASS["massedcompute"], "live")
        self.assertEqual(self.parse({"unsupported": entry(["us-east"])}), [])


class MassedObservedShapeTests(unittest.TestCase):
    def test_observed_integer_stock_shape_with_real_price_normalizer(self):
        # Sanitized 2026-09-17 inventory examples: specs have no GPU-count key.
        # Capacity quantity units remain unverified, even though the field is int.
        payload = {"gpu_inventory": {
            "gpu_1x_h100": {
                "instance_type": {
                    "name": "gpu_1x_h100", "description": "1x H100 (80GB)",
                    "price_cents_per_hour": 273,
                    "specs": {"vcpu_count": 20, "memory_gib": 128, "storage_gb": 1250},
                },
                "regions_with_capacity_available": [{"name": "us-central-3", "description": "Des Moines, IA"}],
                "capacity_available": 6,
            },
            "gpu_8x_b200_SXM6": {
                "instance_type": {
                    "name": "gpu_8x_b200_SXM6", "description": "8x B200 SXM6",
                    "price_cents_per_hour": 4346,
                    "specs": {"vcpu_count": 280, "memory_gib": 2800, "storage_gb": 6000},
                },
                "regions_with_capacity_available": [], "capacity_available": 0,
            },
        }}
        rows = massedcompute.parse(payload, NOW)
        self.assertEqual(len(rows), 2)
        h100 = next(r for r in rows if r.gpu_model == "H100")
        self.assertEqual((h100.instance_type, h100.region, h100.state),
                         ("gpu_1x_h100", "us-central-3", "available"))
        self.assertEqual((h100.metric_type, h100.metric_value), ("binary", 1.0))
        self.assertIn("capacity_available=6 (unit unverified)", h100.detail)
        b200 = next(r for r in rows if r.gpu_model == "B200")
        self.assertEqual((b200.region, b200.state), ("unreported", "unknown"))
        self.assertFalse(live_reads(rows, "H100"))
        self.assertFalse(live_reads(rows, "B200"))


if __name__ == "__main__":
    unittest.main()
