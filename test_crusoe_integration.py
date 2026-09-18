"""Crusoe API quantities remain exact observations, not fleet/cluster claims."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from capacity import insights, render
from capacity.config import PRICE_JOIN_PEERS
from capacity.diff import compute_diff
from capacity.schema import AvailabilityRecord


NOW = "2026-09-17T12:00:00+00:00"


def quantity(value=5, sku="h100-80gb-sxm-ib.8x", region="us-east1-a"):
    return AvailabilityRecord(
        provider="crusoe", gpu_model="H100", region=region,
        consumption_type="on_demand", state="available" if value else "sold_out",
        metric_type="provider_quantity", metric_value=value, instance_type=sku,
        detail=f"API quantity {value} (provider units); num_slices=1; "
               "account quota, reservation eligibility and multi-node stock not established",
        fetched_at=NOW, source_url="https://docs.crusoecloud.com/api/",
        data_source="official_api",
    )


def footprint():
    return AvailabilityRecord(
        provider="crusoe", gpu_model="H100", region="global",
        consumption_type="on_demand", state="available",
        metric_type="listed_offering", metric_value=3,
        detail="offered in 3 zones (footprint, not live stock)",
        data_source="web_scrape", fetched_at=NOW,
    )


class CrusoeIntegrationTests(unittest.TestCase):
    def test_classification_follows_each_record_and_survives_cache_roundtrip(self):
        doc, api = footprint(), quantity()
        self.assertEqual(insights.signal_class(doc), "footprint")
        self.assertEqual(insights.signal_class(api), "live")
        for row in [doc, api]:
            restored = AvailabilityRecord.from_dict(json.loads(json.dumps(row.to_dict())))
            self.assertEqual(insights.signal_class(restored), insights.signal_class(row))
        self.assertEqual(insights.signal_class(replace(api, data_source="web_scrape")), "footprint")
        self.assertEqual(insights.signal_class(replace(api, metric_type="listed_offering")), "footprint")

    def test_exact_quantities_never_create_cluster_or_global_gtm_claims(self):
        rows = [quantity(5), quantity(8, sku="h100-80gb-sxm-ib.1x"), footprint()]
        self.assertEqual(insights.live_reads(rows, "H100"), [])
        self.assertIsNone(insights.tightness(rows, "H100"))
        self.assertEqual(insights.gtm_claims(rows, []), {"ammo": [], "expired": []})
        self.assertIsNone(insights.agg_state(rows, "crusoe", "H100"))
        # Even a provider location literally named global is not a rollup.
        global_qty = quantity(8, region="global")
        self.assertFalse(insights.live_reads([global_qty], "H100"))
        self.assertIsNone(insights.agg_state([global_qty], "crusoe", "H100"))

    def test_existing_verified_global_stock_behavior_is_preserved(self):
        legacy = replace(quantity(), provider="hyperstack", region="global",
                         metric_type="regions_with_capacity", instance_type="")
        reads = insights.live_reads([legacy, quantity()], "H100")
        self.assertEqual([r["provider"] for r in reads], ["hyperstack"])
        self.assertFalse(reads[0]["cluster_ok"])
        self.assertEqual(insights.node_summary([legacy], "H100")["checked"], [])
        self.assertEqual(insights.tightness([legacy, quantity()], "H100")["n"], 1)

    def test_crusoe_cannot_win_broad_price_bookability_join(self):
        self.assertNotIn("crusoe", PRICE_JOIN_PEERS)
        prices = [
            {"provider": "crusoe", "gpu_model": "H100", "consumption_type": "on_demand", "price_per_gpu_hour_usd": .01},
            {"provider": "cp_hyperstack", "gpu_model": "H100", "consumption_type": "on_demand", "price_per_gpu_hour_usd": 4},
        ]
        legacy = replace(quantity(), provider="hyperstack", region="global",
                         metric_type="regions_with_capacity", instance_type="")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            path.write_text(json.dumps(prices))
            with patch.object(insights, "PRICING_SNAPSHOT", path):
                joined = insights.price_join([quantity(), legacy])["H100"]
        self.assertEqual(joined["cheapest_listed"]["provider"], "Hyperstack")
        self.assertEqual(joined["cheapest_bookable"]["provider"], "Hyperstack")

    def test_exact_changes_mean_quantity_moves_not_provider_restock(self):
        old, new = quantity(0), quantity(5)
        changes = compute_diff([new], [old])
        self.assertEqual(len(changes), 1)
        description = render._describe_change(changes[0], [new])
        self.assertIn("h100-80gb-sxm-ib.8x us-east1-a", description)
        self.assertIn("API quantity 0 → 5", description)
        self.assertIn("this SKU/location only", description)
        self.assertNotIn("footprint", description)
        self.assertNotIn("restocked", description)
        self.assertFalse(insights.provider_transitions([new], [old]))
        self.assertFalse(insights.evaluate_triggers([new], [old], changes))
        self.assertFalse(insights.gtm_claims([new], changes)["expired"])

    def test_docs_upgrade_is_not_an_inventory_transition(self):
        api = quantity(0)
        doc = replace(api, metric_type="listed_offering", data_source="web_scrape",
                      metric_value=3, state="available")
        self.assertEqual(compute_diff([api], [doc]), [])
        self.assertEqual(compute_diff([doc], [api]), [])
        self.assertFalse(insights.provider_transitions([api], [footprint()]))

    def test_rendering_keeps_alternative_shapes_and_units_separate(self):
        rows = [quantity(5), quantity(8, sku="h100-80gb-sxm-ib.1x")]
        table = render._crusoe_quantity_table(rows)
        self.assertIn("h100-80gb-sxm-ib.8x", table)
        self.assertIn("h100-80gb-sxm-ib.1x", table)
        self.assertIn("<td>5</td>", table)
        self.assertIn("<td>8</td>", table)
        self.assertNotIn("<td>13</td>", table)
        self.assertIn("not GPU counts", table)
        self.assertIn("quantities are not summed", table)
        self.assertIn("num_slices=1", table)
        manifest = {"provider_status": {"crusoe": {"status": "live"}}}
        page = render.render_confluence(rows, [], manifest, [])
        self.assertIn("API quantity (exact SKU/location)", page)
        _, thread = render.render_slack(rows, [], manifest, [])
        self.assertNotIn("Crusoe API — exact instance/location quantities", thread)
        self.assertNotIn("quantity 5", thread)
        self.assertNotIn("quantity 8", thread)
        self.assertIn("not GPU counts", page)
        self.assertIn("h100-80gb-sxm-ib.1x", page)
        self.assertIn("Complete observations", thread)

    def test_cached_docs_and_failed_api_render_without_promoting_footprint(self):
        doc = footprint()
        cached = {"provider_status": {"crusoe": {"status": "cached"}}}
        page = render.render_confluence([doc], [], cached, [])
        self.assertNotIn("<h2>Crusoe API", page)
        self.assertIn("3 zones", page)
        self.assertIn(">Listed</span>", page)
        self.assertNotIn(">In stock</span>", page)
        failed = {"provider_status": {"crusoe": {"status": "failed"}}}
        failed_page = render.render_confluence([], [], failed, [])
        self.assertIn("failed", failed_page)

    def test_cached_api_section_discloses_age_and_observed_time(self):
        rows = [replace(quantity(), fetched_at="2026-09-15T13:00:00+00:00")]
        manifest = {"provider_status": {"crusoe": {
            "status": "cached", "cache_age_hours": 47}}}
        page = render.render_confluence(rows, [], manifest, [])
        section = page.split("<h2>Crusoe API", 1)[1].split("<h2>Offering Footprint", 1)[0]
        self.assertIn("Cached observation; not refreshed this run (cache age 47h)", section)
        self.assertIn("<th>Observed at</th>", section)
        self.assertIn("2026-09-15 13:00:00 UTC", section)
        _, thread = render.render_slack(rows, [], manifest, [])
        self.assertIn("Cached observation; not refreshed this run (cache age 47h)", thread)
        self.assertIn("Observed: 2026-09-15 13:00:00 UTC", thread)

    def test_failed_or_mixed_timestamp_reads_are_not_presented_as_fresh(self):
        rows = [quantity(), replace(quantity(sku="h100.1x"), fetched_at="2026-09-16T10:00:00Z")]
        manifest = {"provider_status": {"crusoe": {"status": "failed"}}}
        _, thread = render.render_slack(rows, [], manifest, [])
        self.assertIn("Fetch failed; observations not refreshed this run", thread)
        self.assertIn("observed 2026-09-17 12:00:00 UTC", thread)
        self.assertIn("observed 2026-09-16 10:00:00 UTC", thread)


if __name__ == "__main__":
    unittest.main()
