"""Crusoe public tariff and SKU-footprint evidence cannot fabricate stock or configurations."""
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import crusoe_api
from capacity.fetchers import crusoe as footprint
from comparability import enrich_comparability, is_public_benchmark_eligible, is_crusoe_unscoped_reference
from fetchers import crusoe as pricing
from offer_catalogue import normalize_offers
from schema import PriceRecord

NOW = "2026-09-18T17:00:00+00:00"


def row(name, od="Contact sales", spot="Contact sales", tags="HGX"):
    return (f'<div class="pricing_gpu-item"><a><h4>{name}</h4><div class="pricing-tag">{tags}</div></a>'
            f'<div class="pricing-rich"><p>{od}</p></div><div class="pricing-rich"><p>{spot}</p></div></div>')


def table(rows, columns=("On-demand", "Current spot")):
    return ('<div class="pricing-gap"><div class="pricing-table-heading">'
            '<div class="pricing-heading">GPU model</div>'
            + ''.join(f'<div class="pricing-heading">{column}</div>' for column in columns)
            + '</div>' + ''.join(rows) + '</div>')


GPU_TABLE = table([
    row("NVIDIA GB200", tags="NVL72"), row("NVIDIA B200"),
    row("NVIDIA H200", "$4.29/GPU-hr"), row("NVIDIA H100", "$3.90/GPU-hr"),
    row("NVIDIA L40S", "$1.50/GPU-hr", tags="48GB"),
])
DOCS = '''<table><tr><th>Type</th><th>GPU</th><th>Zones</th></tr>
<tr><td><code>h100-80gb-sxm-ib.8x</code></td><td>8x NVIDIA H100 80GB SXM5</td><td>us-east1-a, eu-iceland1-a</td></tr>
<tr><td><code>l40s-48gb.1x</code></td><td>1x NVIDIA L40S 48GB PCIe</td><td>us-east1-a</td></tr>
<tr><td><code>l40s-48gb.8x</code></td><td>8x NVIDIA L40S 48GB PCIe</td><td>us-east1-a</td></tr>
<tr><td><code>gb200-186gb-nvl-4x</code></td><td>4x NVIDIA GB200 186GB NVL</td><td>eu-iceland1-a</td></tr></table>'''


class PublicTariffCatalogue(unittest.TestCase):
    def test_numeric_and_quote_only_columns_are_retained_without_price_records(self):
        self.assertEqual(pricing._parse_html(GPU_TABLE, NOW), [])
        offers = normalize_offers(pricing.LAST_CATALOGUE_OFFERS)
        self.assertEqual(len(offers), 10)
        numeric = [r for r in offers if r["price_status"] == "published_unscoped"]
        self.assertEqual({r["gpu_model"]: r["price_per_gpu_hour_usd"] for r in numeric},
                         {"H100": 3.9, "H200": 4.29, "L40S": 1.5})
        self.assertEqual({r["purchase_type"] for r in numeric}, {"on_demand"})
        quote = [r for r in offers if r["gpu_model"] in {"GB200", "B200"}]
        self.assertEqual(len(quote), 4)
        self.assertEqual({r["price_status"] for r in quote}, {"quote_required"})
        for offer in offers:
            self.assertIsNone(offer["gpu_count"])
            self.assertEqual(offer["gpu_count_relation"], "unknown")
            self.assertEqual(offer["region"], "unspecified")
            self.assertFalse(offer["comparison_eligible"])
            self.assertEqual(offer["availability"], "not_established")
            self.assertEqual(offer["observed_at"], NOW)
        self.assertEqual(pricing.LAST_FETCH_HEALTH["status"], "catalogue_only")

    def test_header_order_controls_price_scope_without_neighbor_context(self):
        html = '<p>Reserved and spot GPU options from $0.01/GPU-hr</p>' + table([
            row("NVIDIA H100", "$2.00/GPU-hr", "$4.00/GPU-hr")], columns=("Current spot", "On-demand"))
        pricing._parse_html(html, NOW)
        got = {r["purchase_type"]: r["price_per_gpu_hour_usd"] for r in pricing.LAST_CATALOGUE_OFFERS}
        self.assertEqual(got, {"spot": 2.0, "on_demand": 4.0})

    def test_other_products_and_unlabeled_prices_are_not_gpu_tariffs(self):
        html = '<h4>NVIDIA H100</h4><p>$99.00/GPU-hr</p>'
        self.assertEqual(pricing._parse_html(html, NOW), [])
        self.assertEqual(pricing.LAST_CATALOGUE_OFFERS, [])
        self.assertEqual(pricing.LAST_FETCH_HEALTH["status"], "failed")
        pricing._parse_html(table([row("NVIDIA H100", "$5.50", "$6.00")], columns=("Price per hour", "Input tokens")), NOW)
        self.assertEqual(pricing.LAST_CATALOGUE_OFFERS, [])

    def test_missing_price_column_is_partial_not_shifted_to_neighbor_purchase_type(self):
        html = table(['<div class="pricing_gpu-item"><h4>NVIDIA H100</h4>'
                      '<div class="pricing-rich">$2.00/GPU-hr</div></div>'])
        pricing._parse_html(html, NOW)
        self.assertEqual(pricing.LAST_CATALOGUE_OFFERS, [])
        self.assertEqual(pricing.LAST_FETCH_HEALTH["status"], "partial")

    def test_undated_manual_values_are_rejected_without_fresh_restatement(self):
        with patch.object(pricing, "MANUAL_PRICES", {("crusoe", "B300", "on_demand", "us-east"): 9.0}), \
                patch.object(pricing.urllib.request, "urlopen", return_value=io.BytesIO(GPU_TABLE.encode())):
            self.assertEqual(pricing.fetch(), [])
            self.assertEqual(pricing._manual_records([], NOW), [])
        self.assertEqual(pricing.LAST_FETCH_HEALTH["undated_manual_rates_rejected"], 1)
        self.assertIn("dated", pricing.LAST_FETCH_HEALTH["manual_warning"])
        self.assertEqual(len(pricing.LAST_CATALOGUE_OFFERS), 10)
        self.assertFalse(any(r["gpu_model"] == "B300" for r in pricing.LAST_CATALOGUE_OFFERS))

    def test_untracked_source_gpu_models_are_explicit_in_health(self):
        pricing._parse_html(GPU_TABLE + table([row("AMD MI355X"), row("NVIDIA A100")]), NOW)
        self.assertEqual(pricing.LAST_FETCH_HEALTH["excluded_gpu_models"], ["AMD MI355X", "NVIDIA A100"])


class NoInventedConfiguration(unittest.TestCase):
    def test_legacy_generic_tariff_cannot_keep_old_eight_gpu_fabric_promotion(self):
        legacy = PriceRecord("crusoe", "H100", 1, "crusoe-h100", "us-east", "on_demand", 3.9, 3.9,
                             data_source="web_scrape", node_gpus=8, form_factor="SXM", interconnect="InfiniBand")
        self.assertTrue(is_crusoe_unscoped_reference(legacy))
        self.assertFalse(is_public_benchmark_eligible(legacy))
        enrich_comparability([legacy])
        self.assertIsNone(legacy.node_gpus)
        self.assertEqual((legacy.form_factor, legacy.interconnect), ("unknown", "unknown"))
        self.assertEqual(legacy.price_basis, "public_gpu_tariff_configuration_unknown")
        self.assertFalse(legacy.comparison_eligible)

    def test_unknown_crusoe_sku_does_not_receive_model_based_fabric_or_node_inference(self):
        row = PriceRecord("crusoe", "H100", 1, "explicit-source-id", "unspecified", "on_demand", 3.9, 3.9)
        enrich_comparability([row])
        self.assertEqual(row.node_gpus, 1)
        self.assertEqual((row.form_factor, row.interconnect), ("unknown", "unknown"))

    def test_explicit_source_configuration_survives(self):
        row = PriceRecord("crusoe", "H100", 8, "h100-80gb-sxm-ib.8x", "us-east1-a", "on_demand", 32, 4,
                          node_gpus=8, form_factor="SXM", interconnect="InfiniBand", parser_version="direct-offers-1")
        enrich_comparability([row])
        self.assertEqual((row.gpu_count, row.node_gpus, row.form_factor, row.interconnect), (8, 8, "SXM", "InfiniBand"))
        self.assertTrue(is_public_benchmark_eligible(row))


class IndependentPublicFootprint(unittest.TestCase):
    def test_exact_sku_location_configurations_are_listings_only(self):
        rows = footprint._parse_public_footprint(DOCS, NOW)
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({(r.instance_type, r.region) for r in rows}), 5)
        self.assertEqual({r.metric_type for r in rows}, {"listed_offering"})
        self.assertEqual({r.state for r in rows}, {"unknown"})
        self.assertTrue(all(r.metric_value is None and r.region != "global" for r in rows))
        by_sku = {r.instance_type: r.gpu_count for r in rows}
        self.assertEqual(by_sku["l40s-48gb.1x"], 1)
        self.assertEqual(by_sku["l40s-48gb.8x"], 8)
        self.assertEqual(by_sku["gb200-186gb-nvl-4x"], 4)

    def test_paused_authenticated_path_stays_paused_while_public_collector_runs(self):
        with patch.object(crusoe_api, "CAPACITY_ACCESS_PAUSED", True), \
                patch.object(footprint, "credentials_configured") as credentials, \
                patch.object(footprint, "fetch_capacities") as authenticated, \
                patch("fetchers._http.http_get", return_value=DOCS.encode()) as public:
            self.assertEqual(footprint.fetch(), [])
            public.assert_not_called()
            rows = footprint.fetch_public_footprint()
        credentials.assert_not_called()
        authenticated.assert_not_called()
        public.assert_called_once_with(footprint.URL, timeout=25, retries=1)
        self.assertEqual(len(rows), 5)
        self.assertEqual(footprint.LAST_PUBLIC_FOOTPRINT_HEALTH["status"], "live")

    def test_missing_counts_or_locations_are_not_guessed(self):
        malformed = DOCS.replace('8x NVIDIA H100 80GB SXM5', 'NVIDIA H100').replace('us-east1-a</td>', 'unknown</td>')
        rows = footprint._parse_public_footprint(malformed, NOW)
        self.assertEqual({r.gpu_model for r in rows}, {"GB200"})
        self.assertEqual(footprint.LAST_PUBLIC_FOOTPRINT_HEALTH["status"], "partial")


class PublicCollectorIntegration(unittest.TestCase):
    def test_daily_dispatch_collects_public_docs_while_api_stays_paused(self):
        from capacity import config, insights, main, render
        self.assertIn("crusoe_public", config.PROVIDERS)
        with patch.object(crusoe_api, "CAPACITY_ACCESS_PAUSED", True), \
                patch.object(footprint, "fetch_capacities") as authenticated, \
                patch.object(footprint, "credentials_configured") as credentials, \
                patch("fetchers._http.http_get", return_value=DOCS.encode()) as public, \
                patch.object(main.store, "update_peer_cache") as update, \
                patch.object(main.store, "get_cached_records") as cache, \
                patch.object(main.store, "load_last_snapshot", return_value=[]), \
                patch.object(main, "write_artifacts") as artifacts:
            manifest = main.run(["crusoe", "crusoe_public"], test=True)
        authenticated.assert_not_called()
        credentials.assert_not_called()
        public.assert_called_once()
        cache.assert_not_called()
        self.assertEqual(update.call_args.args[0], "crusoe_public")
        self.assertEqual(manifest["provider_status"]["crusoe"]["status"], "paused")
        self.assertEqual(manifest["provider_status"]["crusoe_public"]["status"], "live")
        records = artifacts.call_args.args[0]
        self.assertEqual(len(records), 5)
        self.assertEqual(insights.node_reads(records, "H100", manifest), [])
        self.assertIsNone(insights.agg_state(records, "crusoe", "H100"))
        self.assertNotIn("crusoe", config.PRICE_JOIN_PEERS)
        page = render.render_confluence(records, [], manifest, [])
        self.assertIn("Crusoe public catalogue", page)
        self.assertIn("2 zones; 1 documented SKU", page)
        self.assertIn("Access paused", page)
        self.assertIn("h100-80gb-sxm-ib.8x", page)

    def test_public_cache_retains_source_time_and_separate_cache_health(self):
        from capacity import main, render
        observed = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
        cached = footprint._parse_public_footprint(DOCS, observed)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "peer_cache.json"
            path.write_text(json.dumps({"crusoe_public": {"fetched_at": observed,
                            "records": [r.to_dict() for r in cached]}}))
            original = path.read_bytes()
            with patch.object(main.store, "PEER_CACHE_FILE", path), \
                    patch.object(crusoe_api, "CAPACITY_ACCESS_PAUSED", True), \
                    patch.object(footprint, "fetch_capacities") as authenticated, \
                    patch("fetchers._http.http_get", side_effect=URLError("unavailable")), \
                    patch.object(main.store, "load_last_snapshot", return_value=[]), \
                    patch.object(main, "write_artifacts") as artifacts:
                manifest = main.run(["crusoe", "crusoe_public"], test=True)
            self.assertEqual(path.read_bytes(), original)
        authenticated.assert_not_called()
        records = artifacts.call_args.args[0]
        self.assertEqual({r.fetched_at for r in records}, {observed})
        status = manifest["provider_status"]["crusoe_public"]
        self.assertEqual(status["status"], "cached")
        self.assertEqual(status["cache_age_hours"], 8.0)
        self.assertEqual(manifest["provider_status"]["crusoe"]["status"], "paused")
        page = render.render_confluence(records, [], manifest, [])
        self.assertIn("Cached observation; not refreshed this run", page)
        self.assertIn("cache age 8h", page)
        self.assertIn(observed[:10], page)

    def test_public_collector_rejects_authenticated_and_legacy_global_cache(self):
        from capacity import main
        from capacity.schema import AvailabilityRecord
        incompatible = [AvailabilityRecord("crusoe", "H100", "global", "on_demand", "available", "listed_offering", 3),
                        AvailabilityRecord("crusoe", "H100", "us-east1-a", "on_demand", "available", "provider_quantity", 8,
                                           instance_type="h100-80gb-sxm-ib.8x", data_source="official_api")]
        with patch.object(crusoe_api, "CAPACITY_ACCESS_PAUSED", True), \
                patch("fetchers._http.http_get", side_effect=URLError("unavailable")), \
                patch.object(main.store, "get_cached_records", return_value=(incompatible, 4)), \
                patch.object(main.store, "load_last_snapshot", return_value=incompatible), \
                patch.object(main, "write_artifacts") as artifacts:
            manifest = main.run(["crusoe", "crusoe_public"], test=True)
        self.assertEqual(manifest["record_count"], 0)
        self.assertEqual(manifest["diff_count"], 0)
        self.assertEqual(manifest["provider_status"]["crusoe_public"]["status"], "failed")
        self.assertEqual(artifacts.call_args.args[0], [])


if __name__ == "__main__":
    unittest.main()
