"""Offline regressions for exact Scaleway instance stock and pagination."""
import json
import unittest
from unittest.mock import Mock, patch

from capacity.fetchers import scaleway as fetcher
from capacity.schema import AvailabilityRecord

NOW = "2026-09-17T19:00:00+00:00"
ZONE = "fr-par-2"
SKU = "H100-SXM-8-80G"


def payload(rows=None):
    return {"servers": ({SKU: {"availability": "scarce"}} if rows is None else rows)}


def encoded(rows=None):
    return json.dumps(payload(rows)).encode()


class ScalewayParsingTests(unittest.TestCase):
    def test_small_available_instance_never_promotes_eight_gpu_or_pcie_stock(self):
        rows = fetcher.parse(payload({
            "H100-SXM-2-80G": {"availability": "available"},
            "H100-SXM-8-80G": {"availability": "shortage"},
            "H100-2-80G": {"availability": "scarce"},
            "B300-SXM-8-288G": {"availability": "shortage"},
        }), ZONE, NOW)
        self.assertEqual([(r.instance_type, r.gpu_count, r.state) for r in rows], [
            ("B300-SXM-8-288G", 8, "sold_out"),
            ("H100-2-80G", 2, "limited"),
            ("H100-SXM-2-80G", 2, "available"),
            ("H100-SXM-8-80G", 8, "sold_out"),
        ])
        self.assertTrue(all(r.region == ZONE for r in rows))
        self.assertTrue(all(r.metric_value is None for r in rows))
        self.assertEqual(len(rows), 4)

    def test_provenance_raw_enum_timestamp_and_quantity_caveat_are_preserved(self):
        url = fetcher.API.format(zone=ZONE) + "?per_page=100&page=2"
        row = fetcher.parse(payload(), ZONE, NOW, url)[0]
        self.assertEqual((row.product_scope, row.metric_type, row.data_source, row.parser_version),
                         ("gpu_instance", "instance_stock_status", "official_api", "scaleway-instance-stock-2.0"))
        self.assertEqual((row.source_url, row.fetched_at), (url, NOW))
        self.assertIn("Provider stock status: scarce", row.detail)
        self.assertIn("customer quota, stock quantity and multi-node availability not established", row.detail)
        self.assertEqual(AvailabilityRecord.from_dict(row.to_dict()), row)

    def test_only_explicit_shortage_establishes_sold_out(self):
        for info in [None, [], {}, {"availability": None}, {"availability": False},
                     {"availability": []}, {"availability": "unknown"},
                     {"availability": "Available"}, {"availability": "maintenance"},
                     {"availability": "private\nbody"}]:
            with self.subTest(info=info):
                row = fetcher.parse(payload({SKU: info}), ZONE)[0]
                self.assertEqual((row.state, row.metric_value), ("unknown", None))
                self.assertNotIn("private", row.detail)
        row = fetcher.parse(payload({SKU: {"availability": "maintenance"}}), ZONE)[0]
        self.assertIn("maintenance (unrecognized enum)", row.detail)

    def test_supported_model_boundaries_and_counts(self):
        supported = ["GB300-SXM-8-288G", "GB200-4-192G", "B300-SXM-8-288G", "B200-8-180G",
                     "H200-8-141G", "H100-1-80G", "L40S-8-48G", "RTX-PRO-6000-1-96G"]
        excluded = ["H1000-8-80G", "GH200-1-96G", "L4-1-24G", "RENDER-S", "DEV1-S"]
        rows = fetcher.parse(payload({sku: {"availability": "available"}
                                     for sku in supported + excluded}), ZONE)
        self.assertEqual({r.instance_type for r in rows}, set(supported))
        self.assertEqual({r.gpu_model for r in rows}, {"GB300", "GB200", "B300", "B200", "H200", "H100", "L40S", "RTX6000"})

    def test_malformed_recognized_gpu_count_fails_closed(self):
        for sku in ["H100-SXM", "H100-SXM-0-80G", "H100-SXM-01-80G", "H100-SXM-2147483648-80G"]:
            with self.subTest(sku=sku), self.assertRaisesRegex(fetcher.ScalewayParseError, "invalid GPU count"):
                fetcher.parse(payload({sku: {"availability": "available"}}), ZONE)

    def test_invalid_envelope_identity_zone_and_new_pagination_fail_statically(self):
        for data in [None, [], {}, {"servers": []}, {"servers": {}, "next": "private-token"},
                     {"servers": {}, "next_page_token": "private-token"},
                     {"servers": {"private\nbody": {}}}]:
            with self.subTest(data=data), self.assertRaises(fetcher.ScalewayParseError) as raised:
                fetcher.parse(data, ZONE)
            self.assertNotIn("private", str(raised.exception))
        with self.assertRaisesRegex(fetcher.ScalewayParseError, "zone is invalid"):
            fetcher.parse(payload(), "global")
        self.assertEqual(str(fetcher.ScalewayParseError("private-body")), "Scaleway stock schema validation failed")
        self.assertEqual(fetcher.parse(payload({}), ZONE), [])

    def test_parsing_order_is_deterministic(self):
        data = {"H100-SXM-8-80G": {"availability": "scarce"},
                "H100-1-80G": {"availability": "available"}}
        self.assertEqual(fetcher.parse(payload(data), ZONE),
                         fetcher.parse(payload(dict(reversed(list(data.items())))), ZONE))


class ScalewayFetchingTests(unittest.TestCase):
    def test_pages_include_later_gpu_skus_with_exact_page_sources(self):
        http = Mock(side_effect=[encoded({"DEV1-S": {}, SKU: {"availability": "scarce"}}),
                                 encoded({"B300-SXM-8-288G": {"availability": "shortage"}})])
        with patch.object(fetcher, "PAGE_SIZE", 2):
            rows = fetcher._fetch_zone(ZONE, NOW, http)
        self.assertEqual(len(rows), 2)
        self.assertEqual(http.call_count, 2)
        self.assertTrue(rows[0].source_url.endswith("?per_page=2&page=1"))
        self.assertTrue(rows[1].source_url.endswith("?per_page=2&page=2"))
        self.assertEqual({r.fetched_at for r in rows}, {NOW})
        for call in http.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": 30})

    def test_exact_page_multiple_terminates_on_explicit_empty_page(self):
        http = Mock(side_effect=[encoded(), encoded({})])
        with patch.object(fetcher, "PAGE_SIZE", 1):
            self.assertEqual(len(fetcher._fetch_zone(ZONE, NOW, http)), 1)
        self.assertEqual(http.call_count, 2)

    def test_ignored_pagination_and_page_cap_fail_instead_of_partial_snapshot(self):
        http = Mock(return_value=encoded())
        with patch.object(fetcher, "PAGE_SIZE", 1), self.assertRaisesRegex(fetcher.ScalewayParseError, "repeated a SKU"):
            fetcher._fetch_zone(ZONE, NOW, http)
        self.assertEqual(http.call_count, 2)
        with patch.object(fetcher, "PAGE_SIZE", 1), patch.object(fetcher, "MAX_PAGES", 1):
            with self.assertRaisesRegex(fetcher.ScalewayParseError, "page limit"):
                fetcher._fetch_zone(ZONE, NOW, Mock(return_value=encoded()))

    def test_failed_later_page_discards_entire_snapshot_even_with_other_good_zone(self):
        http = Mock(side_effect=[
            encoded(), RuntimeError("private-body"),
            encoded({"H100-1-80G": {"availability": "available"}}), encoded({}),
        ])
        with patch.object(fetcher, "ZONES", [ZONE, "nl-ams-1"]), patch.object(fetcher, "PAGE_SIZE", 1), \
                patch("fetchers._http.http_get", http), self.assertLogs(fetcher.logger, level="WARNING") as logs:
            rows = fetcher.fetch()
        self.assertEqual(rows, [])
        self.assertIn("incomplete snapshot (1/2 zones)", " ".join(logs.output))
        self.assertIn(ZONE, " ".join(logs.output))
        self.assertNotIn("private-body", " ".join(logs.output))

    def test_all_failed_zones_return_no_invented_sold_out_records(self):
        with patch.object(fetcher, "ZONES", [ZONE]), \
                patch("fetchers._http.http_get", side_effect=RuntimeError("private-body")), \
                self.assertLogs(fetcher.logger, level="WARNING") as logs:
            self.assertEqual(fetcher.fetch(), [])
        self.assertIn("incomplete snapshot (0/1 zones)", " ".join(logs.output))
        self.assertNotIn("private-body", " ".join(logs.output))

    def test_malformed_page_does_not_publish_partial_or_global_rows(self):
        with patch.object(fetcher, "ZONES", [ZONE]), \
                patch("fetchers._http.http_get", return_value=b'{"servers": []}'), \
                self.assertLogs(fetcher.logger, level="WARNING"):
            self.assertEqual(fetcher.fetch(), [])

    def test_complete_fetch_preserves_observation_time(self):
        with patch.object(fetcher, "ZONES", [ZONE]), \
                patch("fetchers._http.http_get", return_value=encoded()):
            rows = fetcher.fetch()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].region, ZONE)
        self.assertTrue(rows[0].fetched_at)


if __name__ == "__main__":
    unittest.main()
