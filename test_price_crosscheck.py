"""Configuration matching never turns coverage differences into parse accusations."""
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import textwrap
import unittest
from unittest.mock import patch

from schema import PriceRecord
from price_crosscheck import compare_price_observations
from source_priority import canonical_provider, canonicalize_provider_sources
from fetchers import computeprices, gpuhunt

NOW = "2026-09-18T12:00:00Z"


def direct(**changes):
    values = dict(provider="lambda", gpu_model="H100", gpu_count=8,
                  instance_type="gpu_8x_h100_sxm5", region="us-east-1",
                  consumption_type="on_demand", price_per_hour_usd=31.92,
                  price_per_gpu_hour_usd=3.99, fetched_at=NOW, data_source="official_api",
                  vcpu=208, ram_gb=1800, form_factor="SXM", node_gpus=8,
                  source_url="https://lambda.ai/pricing", gpu_variant="H100")
    values.update(changes)
    return PriceRecord(**values)


def reference(**changes):
    values = dict(provider="sf_lambdalabs", data_source="aggregator", source_feed="shadeform",
                  source_observed_at=NOW, offer_id="shadeform:exact-sku")
    values.update(changes)
    return replace(direct(), **values)


class ConfigurationCrosschecks(unittest.TestCase):
    def check(self, primary=None, ref=None):
        return compare_price_observations([primary or direct()], [ref or reference()], NOW)

    def test_same_exact_sku_agrees_without_independence_claim(self):
        result = self.check()
        self.assertEqual(result["summary"], {"matched_agreement": 1})
        comparison = result["comparisons"][0]
        self.assertFalse(comparison["independent_confirmation"])
        self.assertEqual(comparison["reference"]["source"], "shadeform")
        self.assertEqual(comparison["reference"]["offer_id"], "shadeform:exact-sku")
        self.assertEqual(result["warnings"], [])

    def test_exact_disagreement_is_neutral_and_does_not_mutate_confidence(self):
        primary = direct(data_source="web_scrape")
        ref = reference(price_per_gpu_hour_usd=4.5, price_per_hour_usd=36)
        originals = primary.to_dict(), ref.to_dict()
        result = self.check(primary, ref)
        self.assertEqual(result["summary"], {"matched_disagreement": 1})
        self.assertAlmostEqual(result["comparisons"][0]["delta_pct"], (4.5 - 3.99) / 3.99 * 100)
        self.assertIn("cause unverified", result["warnings"][0])
        self.assertNotIn("wrong", result["warnings"][0])
        self.assertEqual((primary.to_dict(), ref.to_dict()), originals)
        self.assertEqual(primary.confidence, "high")

    def test_lambda_one_gpu_pcie_329_is_not_eight_gpu_sxm_399(self):
        ref = reference(instance_type="gpu_1x_h100_pcie", gpu_count=1, node_gpus=1,
                        gpu_variant="H100 PCIe", form_factor="PCIe", price_per_hour_usd=3.29,
                        price_per_gpu_hour_usd=3.29)
        result = self.check(ref=ref)
        self.assertEqual(result["summary"], {"not_comparable": 1})
        self.assertIn("different GPUs per priced configuration", result["comparisons"][0]["reasons"])
        self.assertEqual(result["warnings"], [])

    def test_mismatched_material_dimensions_never_flag_a_price_disagreement(self):
        cases = [dict(instance_type="another-sku"), dict(region="us-west-1"),
                 dict(form_factor="PCIe"), dict(offer_variant="Community Cloud"),
                 dict(consumption_type="spot"), dict(ram_gb=3600), dict(node_gpus=16),
                 dict(vcpu=104), dict(storage_gb=1000), dict(price_basis="account_catalog"),
                 dict(gpu_count_relation="minimum"), dict(term_min_days=14, term_max_days=90)]
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.check(ref=reference(price_per_hour_usd=8, price_per_gpu_hour_usd=1, **changes))
                self.assertEqual(result["summary"], {"not_comparable": 1})
                self.assertEqual(result["warnings"], [])

    def test_missing_region_host_and_source_time_are_not_comparable(self):
        for changes in [dict(region="global"), dict(region="unspecified"), dict(source_observed_at=""),
                        dict(source_observed_at="2026-09-18"), dict(form_factor="unknown"),
                        dict(ram_gb=None), dict(consumption_type="reserved_unknown")]:
            with self.subTest(changes=changes):
                self.assertEqual(self.check(ref=reference(**changes))["summary"], {"not_comparable": 1})

    def test_stale_retrieval_or_upstream_update_cannot_be_refreshed_by_other_clock(self):
        for changes in [dict(source_observed_at="2026-09-01T12:00:00Z"),
                        dict(fetched_at="2026-09-01T12:00:00Z"),
                        dict(source_observed_at="2026-09-19T12:00:00Z")]:
            with self.subTest(changes=changes):
                result = self.check(ref=reference(**changes))
                self.assertEqual(result["summary"], {"not_comparable": 1})
                self.assertEqual(result["warnings"], [])

    def test_exact_committed_terms_compare_without_merging_different_tenors(self):
        primary = direct(consumption_type="reserved_1yr")
        same = reference(consumption_type="committed_12mo", commitment_months=12)
        self.assertEqual(self.check(primary, same)["summary"], {"matched_agreement": 1})
        self.assertEqual(self.check(primary, replace(same, commitment_months=24))["summary"], {"not_comparable": 1})

    def test_shared_host_configuration_can_match_without_a_shared_sku(self):
        cp = reference(provider="cp_lambda-labs", source_feed="computeprices", offer_id="computeprices:x",
                       instance_type="lambda-h100-8x")
        self.assertEqual(self.check(ref=cp)["summary"], {"matched_agreement": 1})
        self.assertEqual(self.check(ref=replace(cp, vcpu=None, ram_gb=None))["summary"], {"not_comparable": 1})

    def test_unknown_cp_synthetic_name_does_not_prove_provider_sku_identity(self):
        primary = direct(vcpu=None, ram_gb=None)
        cp = reference(provider="cp_lambda-labs", source_feed="computeprices", vcpu=None, ram_gb=None)
        self.assertEqual(self.check(primary, cp)["summary"], {"not_comparable": 1})

    def test_same_source_and_ambiguous_direct_price_are_not_confirmation(self):
        self.assertEqual(self.check(ref=reference(source_feed="official_api"))["summary"], {"not_comparable": 1})
        ambiguous = replace(direct(), price_per_hour_usd=32, price_per_gpu_hour_usd=4)
        result = compare_price_observations([direct(), ambiguous], [reference()], NOW)
        self.assertEqual(result["summary"], {"not_comparable": 1})
        self.assertEqual(result["warnings"], [])

    def test_invalid_price_denominator_never_enters_gap_calculation(self):
        for changes in [dict(price_per_hour_usd=float("inf")), dict(gpu_count=float("nan")),
                        dict(price_per_hour_usd=1), dict(price_per_gpu_hour_usd=0)]:
            self.assertEqual(self.check(ref=reference(**changes))["summary"], {"not_comparable": 1})


class ProviderAliases(unittest.TestCase):
    def test_name_and_separator_variants_share_supplier_identity(self):
        for canonical, names in {
            "lambda": ["cp_lambda-labs", "cp_lambda_labs", "cp_lambdalabs", "sf_lambdalabs", "Lambda Labs"],
            "crusoe": ["cp_crusoe", "sf_crusoe", "Crusoe Cloud"],
            "coreweave": ["cp_core-weave", "sf_coreweave", "CoreWeave"],
            "cp_voltage": ["cp_voltage-park", "sf_voltage_park", "Voltage Park"],
            "massedcompute": ["cp_massed-compute", "sf_massedcompute", "Massed Compute"],
            "cp_scaleway": ["cp_scaleway", "sf_scaleway", "Scaleway"],
        }.items():
            for name in names:
                self.assertEqual(canonical_provider(name), canonical, name)

    def test_canonicalization_retains_source_offers_without_mutating_raw(self):
        a, b = reference(), reference(provider="cp_lambda_labs", source_feed="computeprices", offer_id="computeprices:b")
        result = canonicalize_provider_sources([a, b])
        self.assertEqual(len(result), 2)
        self.assertEqual({r.provider for r in result}, {"lambda"})
        self.assertEqual({r.source_feed for r in result}, {"computeprices", "shadeform"})
        self.assertEqual(a.provider, "sf_lambdalabs")
        self.assertEqual(b.provider, "cp_lambda_labs")


class FullSourceParsers(unittest.TestCase):
    def test_computeprices_crosscheck_retains_shapes_terms_dates_and_regions(self):
        base = {"provider": "Lambda Labs", "provider_slug": "lambda-labs", "gpu": "H100 SXM",
                "gpu_count": 8, "max_gpus_per_node": 8, "total_hourly_usd": 31.92,
                "price_per_hour_usd": 3.99, "pricing_type": "on_demand", "region": "us-east-1",
                "last_updated": "2026-09-01T00:00:00Z"}
        items = [base, {**base, "gpu": "H100 PCIe", "gpu_count": 1, "total_hourly_usd": 3.29},
                 {**base, "region": "us-west-1"}, {**base, "pricing_type": "spot"}]
        with patch.dict(computeprices.os.environ, {"COMPUTEPRICES_API_KEY": "test-only"}), \
             patch.object(computeprices, "GPU_SLUGS", ["h100"]), \
             patch.object(computeprices.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"data": items}).encode())):
            rows = computeprices.fetch_crosscheck()
        self.assertEqual(len(rows), 4)
        self.assertEqual({r.gpu_count for r in rows}, {1, 8})
        self.assertEqual({r.source_observed_at for r in rows}, {"2026-09-01T00:00:00Z"})
        self.assertEqual(len({r.offer_id for r in rows}), 4)

    def test_gpuhunt_preserves_exact_skus_regions_and_prices_without_minima(self):
        base = {"instance_name": "gpu_8x_h100_sxm5", "location": "us-east-1", "gpu_name": "H100",
                "gpu_count": "8", "price": "31.92", "spot": "False", "cpu": "208", "memory": "1800"}
        source_rows = [base, {**base, "instance_name": "gpu_1x_h100_pcie", "gpu_count": "1", "price": "3.29"},
                       {**base, "location": "us-west-1"}, {**base, "spot": "True"}]
        rows = gpuhunt.parse_offers(source_rows, "lambda", NOW, NOW, "https://example.invalid/catalog.zip")
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r.offer_id for r in rows}), 4)
        result = compare_price_observations([direct()], rows, NOW)
        self.assertEqual(result["summary"], {"matched_agreement": 1, "not_comparable": 3})
        self.assertEqual(result["warnings"], [])

    def test_gpuhunt_missing_timestamp_remains_unknown_and_numeric_failures_reject(self):
        base = {"instance_name": "gpu_8x_h100_sxm5", "gpu_name": "H100", "gpu_count": "8", "price": "31.92", "spot": "False"}
        for changes in [{"gpu_count": "nan"}, {"price": "inf"}, {"price": "-2"}, {"spot": "unknown"}]:
            self.assertEqual(gpuhunt.parse_offers([{**base, **changes}], "lambda", NOW), [])
        row = gpuhunt.parse_offers([base], "lambda", NOW)[0]
        self.assertEqual(row.source_observed_at, "")
        self.assertEqual(row.region, "unspecified")

    def test_gpuhunt_opaque_variant_metadata_is_retained_not_assumed_equivalent(self):
        raw = {"instance_name": "gpu_8x_h100_sxm5", "gpu_name": "H100", "gpu_count": "8",
               "price": "31.92", "spot": "False", "location": "us-east-1", "cpu": "208", "memory": "1800",
               "flags": "community_cloud", "provider_data": '{"host":"variant-b"}'}
        row = gpuhunt.parse_offers([raw], "lambda", NOW, NOW)[0]
        self.assertIn("community_cloud", row.offer_variant)
        self.assertIn("variant-b", row.offer_variant)
        result = compare_price_observations([direct()], [row], NOW)
        self.assertEqual(result["summary"], {"not_comparable": 1})

    def test_gpuhunt_fetch_crosscheck_never_calls_family_minimum(self):
        row = {"instance_name": "gpu_8x_h100_sxm5", "gpu_name": "H100", "gpu_count": "8", "price": "31.92", "spot": "False"}
        with patch.object(gpuhunt, "load_catalog", return_value=[row]), \
             patch.object(gpuhunt, "parse_min_on_demand", side_effect=AssertionError("family minimum used")):
            result = gpuhunt.fetch_crosscheck({"lambdalabs": "lambda"})
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], PriceRecord)

    def test_daily_crosscheck_wiring_preserves_confidence_and_audits_nonmatches(self):
        # Execute the real bounded orchestrator block with both fetches mocked;
        # no collection, cache, snapshot, artifact or external writes can run.
        code = Path(__file__).with_name("main.py").read_text()
        start = code.index("    # ── Exact-configuration price cross-checks")
        end = code.index("    # ── Write canonical outputs", start)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
        primary = direct(data_source="web_scrape")
        mismatched = reference(gpu_count=1, node_gpus=1, price_per_hour_usd=3.29,
                               price_per_gpu_hour_usd=3.29, form_factor="PCIe")
        namespace = {"accepted_records": [primary], "warnings": [], "datetime": Clock,
                     "timezone": timezone, "logger": logging.getLogger("crosscheck-test"),
                     "is_qualified_catalogue_reference": lambda row: False}
        with patch.object(computeprices, "fetch_crosscheck", return_value=[mismatched]), \
             patch.object(gpuhunt, "fetch_crosscheck", return_value=[reference(source_feed="gpuhunt")]):
            exec(compile(textwrap.dedent(code[start:end]), "main-crosscheck", "exec"), namespace)
        self.assertEqual(namespace["warnings"], [])
        self.assertEqual(namespace["crosscheck_reports"]["computeprices"]["summary"], {"not_comparable": 1})
        self.assertEqual(namespace["crosscheck_reports"]["gpuhunt"]["summary"], {"matched_agreement": 1})
        self.assertEqual(primary.confidence, "high")


if __name__ == "__main__":
    unittest.main()
