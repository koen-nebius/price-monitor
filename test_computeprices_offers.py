"""ComputePrices offer identity and provenance regressions; no network or writes."""
import io
import json
import unittest
from unittest.mock import patch

from fetchers import computeprices as cp


NOW = "2026-09-18T12:00:00+00:00"


def offer(**changes):
    row = {
        "provider": "CoreWeave", "provider_slug": "coreweave",
        "gpu": "H100 SXM", "gpu_count": 8, "max_gpus_per_node": 8,
        "total_hourly_usd": 40.0, "price_per_hour_usd": 5.0,
        "pricing_type": "on_demand", "commitment_months": None,
        "region": "us-east-1", "variant": "Dedicated", "available": None,
        "source_url": "https://www.coreweave.com/pricing",
        "last_updated": "2026-09-17T10:15:30Z",
    }
    row.update(changes)
    return row


class ComputePricesOfferTests(unittest.TestCase):
    def test_coreweave_retained_with_source_identity(self):
        row = cp.parse([offer()], NOW)[0]
        self.assertEqual((row.provider, row.source_feed), ("cp_coreweave", "computeprices"))
        self.assertEqual(row.source_url, "https://www.coreweave.com/pricing")
        self.assertEqual(row.parser_version, "aggregator-offers-1")
        self.assertEqual((row.gpu_variant, row.offer_variant), ("H100 SXM", "Dedicated"))
        self.assertEqual(row.form_factor, "SXM")
        self.assertTrue(row.offer_id.startswith("computeprices:"))

    def test_regions_sizes_gpu_variants_and_offering_tiers_stay_distinct(self):
        items = [offer(), offer(region="eu-west-1"), offer(variant="Shared"),
                 offer(gpu="H100 PCIe"), offer(gpu="H100 NVL"),
                 offer(gpu_count=1, total_hourly_usd=6), offer(region=None)]
        rows = cp.parse(items, NOW)
        self.assertEqual(len(rows), 7)
        self.assertEqual(len({r.offer_id for r in rows}), 7)
        self.assertEqual({r.region for r in rows}, {"us-east-1", "eu-west-1", "unspecified"})
        self.assertEqual({r.form_factor for r in rows}, {"SXM", "PCIe", "NVL"})

    def test_offer_id_excludes_price_stock_and_observation_time(self):
        rows = cp.parse([offer(), offer(total_hourly_usd=44, available=False,
                                       last_updated=NOW)], NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].offer_id, rows[1].offer_id)
        self.assertNotEqual(rows[0].price_per_gpu_hour_usd, rows[1].price_per_gpu_hour_usd)
        self.assertEqual(len(cp.parse([offer(), offer()], NOW)), 1)

    def test_stale_observation_is_not_restamped_by_fetch(self):
        stale = "2026-06-01T02:03:04Z"
        row = cp.parse([offer(last_updated=stale)], NOW)[0]
        self.assertEqual(row.source_observed_at, stale)
        self.assertEqual(row.fetched_at, NOW)
        missing = cp.parse([offer(last_updated=None)], NOW)[0]
        self.assertEqual(missing.source_observed_at, "")

    def test_available_is_three_state_and_never_coerced_from_text(self):
        for raw, expected in [(True, True), (False, False), (None, None),
                              ("false", None), ("true", None), (0, None), (1, None)]:
            with self.subTest(raw=raw):
                self.assertIs(cp.parse([offer(available=raw)], NOW)[0].available, expected)

    def test_exact_commitments_are_not_rounded_up_to_year_buckets(self):
        months = [1, 3, 6, 9, 12, 18, 24, 30, 36, 48, 60]
        rows = cp.parse([offer(pricing_type="reserved", commitment_months=m) for m in months], NOW)
        self.assertEqual([r.commitment_months for r in rows], months)
        self.assertEqual(len({r.offer_id for r in rows}), len(months))
        self.assertEqual([r.consumption_type for r in rows], [
            "committed_1mo", "committed_3mo", "committed_6mo", "committed_9mo",
            "reserved_1yr", "committed_18mo", "committed_2yr", "committed_30mo",
            "reserved_3yr", "committed_4yr", "committed_60mo"])

    def test_unknown_reserved_terms_never_become_on_demand(self):
        for raw in [None, 0, -1, 1.5, True, "twelve"]:
            with self.subTest(raw=raw):
                row = cp.parse([offer(pricing_type="reserved", commitment_months=raw)], NOW)[0]
                self.assertEqual(row.consumption_type, "reserved_unknown")
                self.assertIsNone(row.commitment_months)
        for raw in [None, "", "unknown", "contact_sales"]:
            with self.subTest(raw=raw):
                self.assertEqual(cp.parse([offer(pricing_type=raw)], NOW), [])

    def test_gh200_and_known_product_exclusions_do_not_return(self):
        self.assertEqual(cp.parse([offer(gpu="GH200")], NOW), [])
        for provider in ["Vultr", "Modal", "Hinode", "Genesis Cloud", "Together AI"]:
            with self.subTest(provider=provider):
                self.assertEqual(cp.parse([offer(provider=provider)], NOW), [])

    def test_documented_h100_label_is_retained_without_inventing_form_factor(self):
        row = cp.parse([offer(gpu="H100 80GB")], NOW)[0]
        self.assertEqual((row.gpu_model, row.gpu_variant, row.form_factor),
                         ("H100", "H100 80GB", "unknown"))

    def test_prices_and_counts_must_be_finite_positive_documented_numbers(self):
        for count in [None, 0, -1, float("nan"), float("inf"), True, "8", 1.5]:
            with self.subTest(count=count):
                self.assertEqual(cp.parse([offer(gpu_count=count)], NOW), [])
        for price in [0, -1, float("nan"), float("inf"), True, "40"]:
            with self.subTest(price=price):
                self.assertEqual(cp.parse([offer(total_hourly_usd=price)], NOW), [])
                self.assertEqual(cp.parse([offer(price_per_hour_usd=price)], NOW), [])
                self.assertEqual(cp.parse([offer(total_hourly_usd=None, price_per_hour_usd=price)], NOW), [])
        self.assertEqual(cp.parse([offer(total_hourly_usd=None, price_per_hour_usd=None)], NOW), [])
        row = cp.parse([offer(total_hourly_usd=None, price_per_hour_usd=4.5)], NOW)[0]
        self.assertEqual((row.price_per_hour_usd, row.price_per_gpu_hour_usd), (36.0, 4.5))
        self.assertEqual(cp.parse([offer(gpu_count=8, total_hourly_usd=None,
                                        price_per_hour_usd=1e308)], NOW), [])

    def test_fetch_keeps_reserved_offer_above_cheaper_different_shape_and_region(self):
        items = [offer(gpu_count=1, total_hourly_usd=1, region="us-east-1"),
                 offer(pricing_type="reserved", commitment_months=12,
                       gpu_count=8, total_hourly_usd=32, region="eu-west-1"),
                 offer(gpu_count=8, total_hourly_usd=40, region="eu-west-1")]
        with patch.object(cp, "GPU_SLUGS", ["h100"]), \
             patch.dict(cp.os.environ, {"COMPUTEPRICES_API_KEY": "test-only"}), \
             patch.object(cp.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"data": items}).encode())):
            rows = cp.fetch()
        self.assertEqual(len(rows), 3)
        reserved = next(r for r in rows if r.consumption_type == "reserved_1yr")
        self.assertEqual((reserved.region, reserved.gpu_count, reserved.price_per_gpu_hour_usd),
                         ("eu-west-1", 8, 4.0))


if __name__ == "__main__":
    unittest.main()
