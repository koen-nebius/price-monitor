"""Lambda instance launchability must not become cluster stock or bookable pricing."""
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


def instance(count=1, regions=("us-east-1",), sku=None):
    sku = sku or f"gpu_{count}x_h100_sxm5"
    state = "unknown" if regions is None else "available" if regions else "sold_out"
    summary = AvailabilityRecord(
        "lambda", "H100", "global", "on_demand", state, "launchable_regions",
        None if regions is None else len(regions), instance_type=sku,
        detail="Exact on-demand instance launchability only", fetched_at=NOW,
        source_url="https://cloud.lambda.ai/api/v1/instance-types",
        data_source="official_api", product_scope="on_demand_instance", gpu_count=count,
        parser_version="lambda-instance-capacity-2.0",
    )
    return [summary] + [replace(summary, region=region, state="available",
                               metric_type="instance_launchability", metric_value=1)
                        for region in regions or ()]


def legacy(source="official_api", state="available"):
    return AvailabilityRecord("lambda", "H100", "global", "on_demand", state,
                              "regions_with_capacity", 1, data_source=source,
                              detail="Misleading old fleet-wide stock claim", fetched_at=NOW)


def change(row, old="sold_out", new="available"):
    return CapacityDiffEntry(row.provider, row.gpu_model, row.region, row.consumption_type,
                             "state_change", old_state=old, new_state=new,
                             instance_type=row.instance_type)


class LambdaCapacityIntegrationTests(unittest.TestCase):
    def test_classification_requires_exact_official_instance_scope(self):
        rows = instance()
        for row in rows:
            self.assertTrue(insights.is_lambda_instance(row))
            self.assertEqual(insights.signal_class(row), "instance")
        row = rows[0]
        for changed in (legacy(), legacy("aggregator"), replace(row, product_scope=""),
                        replace(row, gpu_count=None), replace(row, gpu_count=True),
                        replace(row, gpu_count=0), replace(row, instance_type=""),
                        replace(row, metric_type="instance_launchability"),
                        replace(row, data_source="aggregator")):
            self.assertFalse(insights.is_lambda_instance(changed))
            self.assertEqual(insights.signal_class(changed), "unverified_scope")

    def test_one_and_eight_gpu_instances_never_prove_cluster_stock(self):
        for count in (1, 8):
            rows = instance(count)
            self.assertEqual(insights.live_reads(rows, "H100"), [])
            self.assertIsNone(insights.tightness(rows, "H100"))
            self.assertIsNone(insights.agg_state(rows, "lambda", "H100"))
            self.assertEqual(insights.provider_transitions(rows, [legacy(state="sold_out")]), [])
            self.assertEqual(insights.evaluate_triggers(rows, [legacy(state="sold_out")], []), [])
            self.assertEqual(insights.gtm_claims(rows, [change(rows[0])]), {"ammo": [], "expired": []})

    def test_mixed_shapes_keep_their_own_regions_and_do_not_sum(self):
        rows = instance(1, ("us-east-1",)) + instance(8, ("us-west-1", "eu-south-1"))
        views = render._lambda_instance_views(rows)
        by_shape = {row.instance_type: text for row, text in views}
        self.assertEqual(by_shape, {"gpu_1x_h100_sxm5": "us-east-1",
                                   "gpu_8x_h100_sxm5": "eu-south-1, us-west-1"})
        table = render._lambda_instance_table(rows)
        self.assertIn("<td>1</td>", table)
        self.assertIn("<td>8</td>", table)
        self.assertNotIn("9 GPUs", table)
        self.assertNotIn("17 GPUs", table)
        self.assertIn("not 1ClickClusters stock", table)
        self.assertIn("single node", table)

    def test_explicit_empty_unknown_and_partial_unknown_remain_distinct(self):
        empty = instance(8, ())
        unknown = instance(1, None)
        self.assertEqual(render._lambda_instance_views(empty)[0][1], "none reported")
        self.assertEqual(render._lambda_instance_views(unknown)[0][1], "complete availability unknown")
        partial = unknown + [replace(unknown[0], region="us-east-1", metric_type="instance_launchability",
                                     metric_value=1, state="available")]
        self.assertEqual(render._lambda_instance_views(partial)[0][1],
                         "complete availability unknown; reported regions: us-east-1")
        table = render._lambda_instance_table(empty + unknown)
        self.assertIn("none reported", table)
        self.assertIn("complete availability unknown", table)

    def test_legacy_cache_excluded_from_matrix_gtm_and_generic_changes(self):
        rows = [legacy(), legacy("aggregator", "sold_out")]
        diff = [change(rows[0])]
        manifest = {"provider_status": {"lambda": {"status": "cached", "cache_age_hours": 47}}}
        self.assertEqual(insights.live_reads(rows, "H100"), [])
        self.assertEqual(insights.gtm_claims(rows, diff), {"ammo": [], "expired": []})
        page = render.render_confluence(rows, diff, manifest, [])
        matrix = page.split("<h2>Live-Stock Matrix</h2>")[1].split("<h2>")[0]
        self.assertNotIn("Lambda", matrix)
        self.assertIn("legacy Lambda observations have unverified product scope", page)
        self.assertNotIn("Misleading old fleet-wide stock claim", page)
        _, thread = render.render_slack(rows, diff, manifest, [])
        self.assertIn("legacy observations excluded", thread)
        self.assertNotIn("Lambda H100: sold out", thread)
        self.assertNotIn("Lambda H100: in stock", thread)

    def test_listed_price_stays_but_no_lambda_shape_is_misjoined_as_bookable(self):
        prices = [{"provider": "lambda", "gpu_model": "H100", "consumption_type": "on_demand",
                   "price_per_gpu_hour_usd": 2, "instance_type": "different-cheapest-shape"},
                  {"provider": "hyperstack", "gpu_model": "H100", "consumption_type": "on_demand",
                   "price_per_gpu_hour_usd": 4}]
        hyperstack = replace(legacy(), provider="hyperstack")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            path.write_text(json.dumps(prices))
            with patch.object(insights, "PRICING_SNAPSHOT", path):
                for rows in (instance(1), instance(8), [legacy()]):
                    result = insights.price_join(rows)["H100"]
                    self.assertEqual(result["cheapest_listed"]["provider"], "Lambda")
                    self.assertIsNone(result["cheapest_bookable"])
                result = insights.price_join(instance(8) + [hyperstack])["H100"]
                self.assertEqual(result["cheapest_bookable"]["provider"], "Hyperstack")

    def test_scoped_changes_keep_instance_context(self):
        rows = instance(8, ())
        diff = [change(rows[0], "available", "sold_out")]
        description = render._describe_change(diff[0], rows)
        self.assertIn("gpu_8x_h100_sxm5", description)
        self.assertIn("none reported", description)
        self.assertIn("not 1ClickClusters stock", description)
        _, thread = render.render_slack(rows, diff, {}, [])
        self.assertIn("Changes apply only to the named instance and region", thread)
        self.assertNotIn("Lambda H100: in stock → sold out", thread)
        self.assertNotIn("datacenter-level change", thread)

    def test_cached_views_show_original_utc_observation_and_cache_age(self):
        rows = [replace(row, fetched_at="2026-09-15T14:00:00+02:00") for row in instance(8)]
        manifest = {"provider_status": {"lambda": {"status": "cached", "cache_age_hours": 47}}}
        page = render.render_confluence(rows, [], manifest, [])
        section = page.split("<h2>Lambda —")[1].split("<h2>")[0]
        self.assertIn("2026-09-15 12:00:00 UTC", section)
        self.assertIn("Cached observation; not refreshed this run (cache age 47h)", section)
        _, thread = render.render_slack(rows, [], manifest, [])
        self.assertIn("Observed: 2026-09-15 12:00:00 UTC", thread)
        self.assertIn("Cached observation; not refreshed this run (cache age 47h)", thread)
        self.assertIn("8 GPUs/instance", thread)

    def test_failure_without_rows_stays_visible(self):
        manifest = {"provider_status": {"lambda": {"status": "failed"}}}
        with patch.object(render, "PENDING_ACTIVATION", set()):
            page = render._lambda_instance_table([], manifest)
            _, thread = render.render_slack([], [], manifest, [])
        self.assertIn("Fetch failed; observations not refreshed this run", page)
        self.assertIn("No verified exact-instance observations available", page)
        self.assertIn("Fetch failed; observations not refreshed this run", thread)

    def test_configured_failure_is_not_pending_activation(self):
        with patch.dict(os.environ, {"LAMBDA_API_KEY": "test-placeholder"}, clear=True):
            configured = runpy.run_path(config.__file__)["PENDING_ACTIVATION"]
        self.assertNotIn("lambda", configured)
        with patch.dict(os.environ, {}, clear=True):
            missing = runpy.run_path(config.__file__)["PENDING_ACTIVATION"]
        self.assertIn("lambda", missing)
        manifest = {"provider_status": {"lambda": {"status": "failed"}}}
        with patch.object(render, "PENDING_ACTIVATION", missing):
            self.assertIn("API access pending; no fresh observations",
                          render._lambda_instance_table([], manifest))
        with patch.object(insights, "PENDING_ACTIVATION", configured), patch.object(render, "PENDING_ACTIVATION", configured):
            self.assertEqual(insights.freshness(manifest)["failed"], ["lambda"])
            page = render.render_confluence([], [], manifest, [])
        method = page.split("<td>Lambda</td>")[-1].split("</tr>")[0]
        self.assertIn("<td>failed</td>", method)
        self.assertNotIn("pending", method)


if __name__ == "__main__":
    unittest.main()
