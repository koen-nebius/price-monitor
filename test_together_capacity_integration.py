"""Dedicated-inference replicas must never masquerade as raw GPU cluster stock."""
import json
import os
import runpy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from capacity import config, insights, render
from capacity.schema import AvailabilityRecord, CapacityDiffEntry

NOW = "2026-09-17T12:00:00+00:00"


def inference(value=1, count=1, sku="h100-1", relation="RELATION_EQ", region="us-east"):
    state = "unknown" if value is None else "sold_out" if value == 0 else "limited" if value <= 2 else "available"
    return AvailabilityRecord(
        "together", "H100", region, "on_demand", state, "inference_replicas", value,
        detail="Dedicated-inference headroom for this exact configuration only",
        instance_type=sku, fetched_at=NOW, source_url="https://api.together.ai/v2/public/inference-instance-types",
        data_source="official_api", product_scope="dedicated_inference",
        gpu_count=count, quantity_relation=relation,
    )


def legacy(source="official_api", state="sold_out"):
    return AvailabilityRecord(
        "together", "H100", "global", "on_demand", state, "regions_with_capacity", 0,
        detail="no region with headroom", fetched_at=NOW, data_source=source,
    )


class TogetherCapacityIntegrationTests(unittest.TestCase):
    def test_classification_requires_product_scope_and_exact_metric(self):
        row = inference()
        self.assertEqual(insights.signal_class(row), "inference")
        for changed in (legacy(), legacy("aggregator"), replace(row, product_scope=""),
                        replace(row, metric_type="stock_level"), replace(row, data_source="aggregator")):
            self.assertEqual(insights.signal_class(changed), "unverified_scope")
            self.assertFalse(insights.is_together_inference(changed))
        restored = AvailabilityRecord.from_dict(json.loads(json.dumps(row.to_dict())))
        self.assertEqual(restored.product_scope, "dedicated_inference")
        self.assertEqual(insights.signal_class(restored), "inference")

    def test_small_positive_inference_is_not_global_sellout_or_cluster_stock(self):
        rows = [inference(1), inference(2, sku="h100-8", count=8), inference(0, region="eu-west")]
        self.assertFalse(insights.live_reads(rows, "H100"))
        self.assertIsNone(insights.tightness(rows, "H100"))
        self.assertFalse(insights.provider_transitions(rows, [legacy()]))
        self.assertFalse(insights.evaluate_triggers(rows, [legacy()], []))
        self.assertEqual(insights.gtm_claims(rows, []), {"ammo": [], "expired": []})
        # A region literally named global cannot sneak into a cluster rollup.
        self.assertIsNone(insights.agg_state([inference(10, region="global")], "together", "H100"))

    def test_legacy_global_and_aggregator_cache_cannot_enter_insights(self):
        rows = [legacy("official_api", "available"), legacy("aggregator", "sold_out")]
        self.assertFalse(insights.live_reads(rows, "H100"))
        self.assertIsNone(insights.agg_state(rows, "together", "H100"))
        self.assertFalse(insights.provider_transitions(rows, [legacy()]))
        change = CapacityDiffEntry("together", "H100", "global", "on_demand", "state_change",
                                   old_state="sold_out", new_state="available")
        self.assertEqual(insights.gtm_claims(rows, [change]), {"ammo": [], "expired": []})
        manifest = {"provider_status": {"together": {"status": "cached", "cache_age_hours": 47}}}
        page = render.render_confluence(rows, [change], manifest, [legacy()])
        self.assertIn("legacy Together observations have unverified product scope", page)
        self.assertNotIn("no region with headroom", page)
        matrix = page.split("<h2>Live-Stock Matrix</h2>", 1)[1].split("<h2>", 1)[0]
        self.assertNotIn("Together", matrix)
        _, thread = render.render_slack(rows, [change], manifest, [legacy()])
        self.assertIn("legacy observations excluded", thread)
        self.assertNotIn("Together AI H100: sold out", thread)
        self.assertNotIn("Together AI H100: in stock", thread)

    def test_inference_does_not_make_together_gpu_price_bookable(self):
        self.assertNotIn("together", config.PRICE_JOIN_PEERS)
        prices = [{"provider": "together", "gpu_model": "H100", "consumption_type": "on_demand",
                   "price_per_gpu_hour_usd": .01},
                  {"provider": "lambda", "gpu_model": "H100", "consumption_type": "on_demand",
                   "price_per_gpu_hour_usd": 4}]
        lambda_row = replace(legacy(state="available"), provider="lambda")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            path.write_text(json.dumps(prices))
            with patch.object(insights, "PRICING_SNAPSHOT", path):
                joined = insights.price_join([inference(10), lambda_row])["H100"]
        self.assertEqual(joined["cheapest_listed"]["provider"], "Lambda")
        self.assertEqual(joined["cheapest_bookable"]["provider"], "Lambda")

    def test_rendering_preserves_exact_lower_bound_zero_and_unknown(self):
        rows = [inference(0), inference(5, 8, "h100-8", "RELATION_GTE"),
                inference(None, None, "h100-unspecified", "", region="unreported")]
        table = render._together_inference_table(rows)
        self.assertIn("0 replicas (exact)", table)
        self.assertIn("≥5 replicas (lower bound)", table)
        self.assertIn("unknown replica headroom", table)
        self.assertIn("<td>Unreported</td>", table)
        self.assertIn("h100-8", table)
        self.assertIn("<td>8</td>", table)
        self.assertNotIn("40 GPUs", table)
        self.assertIn("not raw GPU rental", table)
        self.assertIn("2026-09-17 12:00:00 UTC", table)

    def test_cached_inference_is_labelled_with_age_and_observed_time(self):
        row = replace(inference(1), fetched_at="2026-09-15T13:00:00Z")
        manifest = {"provider_status": {"together": {"status": "cached", "cache_age_hours": 47}}}
        page = render.render_confluence([row], [], manifest, [])
        table = page.split("<h2>Together AI", 1)[1].split("<h2>", 1)[0]
        self.assertIn("Cached observation; not refreshed this run (cache age 47h)", table)
        self.assertIn("2026-09-15 13:00:00 UTC", table)
        _, thread = render.render_slack([row], [], manifest, [])
        self.assertIn("Cached observation; not refreshed this run (cache age 47h)", thread)
        self.assertIn("Observed: 2026-09-15 13:00:00 UTC", thread)
        self.assertIn("1 GPU/replica", thread)
        self.assertIn("1 replica (exact)", thread)

    def test_configured_api_failure_is_failure_not_pending_or_aggregator(self):
        with patch.dict(os.environ, {"TOGETHER_API_KEY": "test-placeholder"}, clear=True):
            configured = runpy.run_path(config.__file__)["PENDING_ACTIVATION"]
        self.assertNotIn("together", configured)
        with patch.dict(os.environ, {}, clear=True):
            missing = runpy.run_path(config.__file__)["PENDING_ACTIVATION"]
        self.assertIn("together", missing)
        manifest = {"provider_status": {"together": {"status": "failed"}}}
        with patch.object(insights, "PENDING_ACTIVATION", configured), patch.object(render, "PENDING_ACTIVATION", configured):
            self.assertEqual(insights.freshness(manifest)["failed"], ["together"])
            page = render.render_confluence([], [], manifest, [])
        method_row = page.split("<td>Together AI</td>", 1)[1].split("</tr>", 1)[0]
        self.assertIn("<td>failed</td>", method_row)
        self.assertNotIn("pending", method_row)
        self.assertNotIn("Shadeform", method_row)


if __name__ == "__main__":
    unittest.main()
