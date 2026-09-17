"""Exact Scaleway VM stock must not become cluster stock or GPU-level bookability."""
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from capacity import insights, render
from capacity.diff import compute_diff
from capacity.schema import AvailabilityRecord, CapacityDiffEntry

NOW = "2026-09-17T12:00:00+00:00"


def observation(count=8, state="limited", zone="fr-par-2", **changes):
    raw = {"available": "available", "limited": "scarce", "sold_out": "shortage", "unknown": "new-enum"}[state]
    return replace(AvailabilityRecord(
        "scaleway", "H100", zone, "on_demand", state, "instance_stock_status",
        instance_type=f"H100-{count}-88G", gpu_count=count, product_scope="gpu_instance",
        data_source="official_api", fetched_at=NOW, detail=f"availability={raw}; exact SKU/zone only",
        source_url=f"https://api.scaleway.com/instance/v1/zones/{zone}/products/servers/availability",
        parser_version="scaleway-instance-stock-2.0",
    ), **changes)


def legacy(source="official_api", state="available"):
    return observation(state=state, region="global", gpu_count=None, instance_type="",
                       product_scope="", metric_type="stock_status_label", data_source=source,
                       metric_value=2, detail="Misleading old fleet-wide stock claim")


class ScalewayScopeTests(unittest.TestCase):
    def test_classification_requires_exact_official_sku_zone_and_count(self):
        row = observation()
        self.assertTrue(insights.is_scaleway_instance(row))
        self.assertEqual(insights.signal_class(row), "instance_stock")
        for invalid in [legacy(), legacy("aggregator"), replace(row, product_scope=""),
                        replace(row, gpu_count=None), replace(row, gpu_count=True),
                        replace(row, gpu_count=0), replace(row, instance_type=""),
                        replace(row, metric_type="stock_status_label"),
                        replace(row, region="global"), replace(row, region=""),
                        replace(row, data_source="aggregator")]:
            with self.subTest(invalid=invalid):
                self.assertFalse(insights.is_scaleway_instance(invalid))
                self.assertEqual(insights.signal_class(invalid), "unverified_scope")

    def test_every_shape_and_legacy_source_is_excluded_from_fleet_inferences(self):
        for row in [observation(1, "available"), observation(2, "available"), observation(8),
                    observation(8, "sold_out"), legacy(), legacy("aggregator")]:
            rows = [row]
            old = [replace(row, state="sold_out")]
            change = CapacityDiffEntry(row.provider, row.gpu_model, row.region, row.consumption_type,
                                       "state_change", old_state="sold_out", new_state="available",
                                       instance_type=row.instance_type)
            self.assertEqual(insights.live_reads(rows, "H100"), [])
            self.assertIsNone(insights.tightness(rows, "H100"))
            self.assertIsNone(insights.agg_state(rows, "scaleway", "H100"))
            self.assertEqual(insights.provider_transitions(rows, old), [])
            self.assertEqual(insights.evaluate_triggers(rows, old, [change]), [])
            self.assertEqual(insights.gtm_claims(rows, [change]), {"ammo": [], "expired": []})

    def test_listing_price_stays_independent_even_with_eight_gpu_available(self):
        prices = [{"provider": "cp_scaleway", "gpu_model": "H100", "consumption_type": "on_demand",
                   "price_per_gpu_hour_usd": 2, "instance_type": "unknown-shape"},
                  {"provider": "hyperstack", "gpu_model": "H100", "consumption_type": "on_demand",
                   "price_per_gpu_hour_usd": 4}]
        path = Mock()
        path.read_text.return_value = json.dumps(prices)
        with patch.object(insights, "PRICING_SNAPSHOT", path):
            for row in [observation(2, "available"), observation(8, "available"), legacy(), legacy("aggregator")]:
                result = insights.price_join([row])["H100"]
                self.assertEqual(result["cheapest_listed"]["provider"], "Scaleway")
                self.assertEqual(result["cheapest_listed"]["price"], 2)
                self.assertFalse(result["cheapest_listed"]["sold_out"])
                self.assertIsNone(result["cheapest_bookable"])
            control = replace(legacy(), provider="hyperstack")
            result = insights.price_join([observation(), control])["H100"]
            self.assertEqual(result["cheapest_bookable"]["provider"], "Hyperstack")

    def test_render_retains_different_vm_sizes_zone_enum_and_time(self):
        rows = [observation(2, "available"), observation(8),
                observation(8, "sold_out", "fr-par-3"), legacy("aggregator")]
        manifest = {"provider_status": {"scaleway": {"status": "live"}}}
        page = render.render_confluence(rows, [], manifest, [])
        section = page.split("<h2>Scaleway —")[1].split("<h2>")[0]
        for expected in ["H100-2-88G", "H100-8-88G", "<td>2</td>", "<td>8</td>",
                         "fr-par-2", "fr-par-3", "available", "scarce", "shortage", NOW[:10],
                         "multi-node", "listed prices remain independent"]:
            self.assertIn(expected, section)
        matrix = page.split("<h2>Live-Stock Matrix</h2>")[1].split("<h2>")[0]
        self.assertNotIn("Scaleway", matrix)
        self.assertNotIn("Misleading old fleet-wide stock claim", page)
        _, thread = render.render_slack(rows, [], manifest, [])
        self.assertIn("H100-2-88G · 2 GPUs/instance · fr-par-2: available", thread)
        self.assertIn("H100-8-88G · 8 GPUs/instance · fr-par-2: scarce", thread)
        self.assertIn("H100-8-88G · 8 GPUs/instance · fr-par-3: shortage", thread)
        self.assertIn("legacy or aggregator observations excluded", thread)

    def test_exact_state_change_cannot_be_described_as_fleet_change(self):
        old, new = observation(state="available"), observation(state="sold_out")
        changes = compute_diff([new], [old])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].instance_type, old.instance_type)
        description = render._describe_change(changes[0], [new])
        self.assertIn("H100-8-88G", description)
        self.assertIn("fr-par-2", description)
        self.assertIn("available → sold_out", description)
        self.assertIn("shortage", description)
        self.assertIn("single-instance status only", description)
        _, thread = render.render_slack([new], changes, {}, [old])
        self.assertIn("Changes apply only to the named instance and zone", thread)
        self.assertNotIn("Scaleway H100: in stock → sold out", thread)
        self.assertNotIn("datacenter-level change", thread)

    def test_cached_and_missing_sources_do_not_look_current_or_zero(self):
        manifest = {"provider_status": {"scaleway": {"status": "cached", "cache_age_hours": 20}}}
        table = render._scaleway_instance_table([observation()], manifest)
        self.assertIn("Cached observation; not refreshed this run (cache age 20h)", table)
        self.assertIn("2026-09-17 12:00:00 UTC", table)
        manifest["provider_status"]["scaleway"] = {"status": "failed"}
        table = render._scaleway_instance_table([], manifest)
        self.assertIn("Fetch failed", table)
        self.assertIn("No verified exact-instance observations available", table)


if __name__ == "__main__":
    unittest.main()
