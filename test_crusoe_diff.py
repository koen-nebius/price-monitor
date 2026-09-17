"""Source changes must not masquerade as capacity changes."""
import unittest
from dataclasses import replace
from capacity.diff import compute_diff
from capacity.schema import AvailabilityRecord


class CrusoeDiffTests(unittest.TestCase):
    def row(self, **changes):
        base = AvailabilityRecord("crusoe", "H100", "us-east1-a", "on_demand",
                                  "available", "provider_quantity", 8,
                                  instance_type="h100.8x", data_source="official_api")
        return replace(base, **changes)

    def test_documentation_upgrade_is_not_restock(self):
        old = self.row(metric_type="listed_offering", data_source="web_scrape",
                       state="limited", metric_value=1)
        self.assertEqual(compute_diff([self.row()], [old]), [])

    def test_api_loss_to_documentation_is_not_capacity_drop(self):
        old = self.row()
        new = self.row(metric_type="listed_offering", data_source="web_scrape",
                       state="limited", metric_value=1)
        self.assertEqual(compute_diff([new], [old]), [])

    def test_exact_api_stock_change_is_kept(self):
        changes = compute_diff([self.row(state="sold_out", metric_value=0)], [self.row()])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].instance_type, "h100.8x")
        self.assertEqual(changes[0].region, "us-east1-a")


if __name__ == "__main__":
    unittest.main()
