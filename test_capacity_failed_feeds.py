"""Read-only capacity fetches stay bounded and preserve unknown versus zero."""
import json
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from capacity.fetchers import aws_capacity_blocks as aws
from capacity.fetchers import voltage_park as voltage

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromisoformat(NOW.isoformat())


class ApiError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code, "Message": "private API detail must not be copied"}}


@contextmanager
def sdk(responses, *, one_check=False):
    client = Mock(spec=["describe_capacity_block_offerings"])
    if isinstance(responses, list):
        client.describe_capacity_block_offerings.side_effect = responses
    else:
        client.describe_capacity_block_offerings.return_value = responses
    factory, config = Mock(return_value=client), Mock(return_value=object())
    modules = {"boto3": types.SimpleNamespace(client=factory),
               "botocore": types.ModuleType("botocore"),
               "botocore.config": types.SimpleNamespace(Config=config)}
    with patch.dict(sys.modules, modules), patch.dict(aws.os.environ, {"AWS_ACCESS_KEY_ID": "test-only"}), \
            patch.object(aws, "datetime", Clock), patch.object(aws.time, "sleep") as sleep:
        if one_check:
            with patch.object(aws, "CB_REGIONS", ["us-east-1"]), \
                    patch.object(aws, "INSTANCE_GPU_MAP", {"p5.48xlarge": "H100"}):
                yield client, factory, config, sleep
        else:
            yield client, factory, config, sleep


def offering(day):
    return {"StartDate": f"2026-09-{day:02}T12:00:00+00:00", "UpfrontFee": "800", "CurrencyCode": "USD"}


class AwsCapacityBlocks(unittest.TestCase):
    def test_full_configured_scope_is_preserved_and_empty_search_is_scoped(self):
        with sdk({"CapacityBlockOfferings": []}) as (client, factory, config, sleep):
            rows = aws.fetch()
        self.assertEqual(len(rows), 16)
        self.assertEqual(client.describe_capacity_block_offerings.call_count, 16)
        self.assertEqual(factory.call_count, 4)
        self.assertEqual(aws.LAST_FETCH_HEALTH["completed_checks"], 16)
        self.assertEqual(aws.LAST_FETCH_HEALTH["unqueried_checks"], [])
        self.assertEqual(aws.LAST_FETCH_HEALTH["status"], "live")
        self.assertTrue(all(r.state == "sold_out" and r.metric_value is None for r in rows))
        self.assertTrue(all(r.product_scope == "capacity_block_1_instance_24h" for r in rows))
        self.assertTrue(all("1-instance 24h" in r.detail for r in rows))
        self.assertEqual(config.call_args.kwargs["retries"]["total_max_attempts"], 1)
        self.assertEqual(sleep.call_count, 15)
        self.assertTrue(all(c.kwargs["InstanceCount"] == 1 and c.kwargs["CapacityDurationHours"] == 24
                            for c in client.describe_capacity_block_offerings.call_args_list))

    def test_account_limit_stops_after_one_request_with_remaining_scope(self):
        with sdk([ApiError("CapacityBlockDescribeLimitExceeded")]) as (client, _, _, sleep):
            self.assertEqual(aws.fetch(), [])
        health = aws.LAST_FETCH_HEALTH
        self.assertEqual(client.describe_capacity_block_offerings.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(health["status"], "failed")
        self.assertEqual(health["failure_class"], "account_limit")
        self.assertEqual(health["planned_checks"], 16)
        self.assertEqual(health["failed_checks"], 1)
        self.assertEqual(len(health["unqueried_checks"]), 15)
        self.assertNotIn("private", str(health))

    def test_access_failure_preserves_earlier_success_without_claiming_full_coverage(self):
        with sdk([{"CapacityBlockOfferings": []}, ApiError("UnauthorizedOperation")]) as (client, _, _, _):
            rows = aws.fetch()
        self.assertEqual(len(rows), 1)
        self.assertEqual(client.describe_capacity_block_offerings.call_count, 2)
        self.assertEqual(aws.LAST_FETCH_HEALTH["status"], "partial")
        self.assertEqual(aws.LAST_FETCH_HEALTH["failure_class"], "access")
        self.assertEqual(len(aws.LAST_FETCH_HEALTH["unqueried_checks"]), 14)

    def test_throttle_is_distinct_from_account_describe_limit(self):
        with sdk([ApiError("RequestLimitExceeded")]):
            aws.fetch()
        self.assertEqual(aws.LAST_FETCH_HEALTH["failure_class"], "rate_limit")
        self.assertEqual(aws.LAST_FETCH_HEALTH["requests_attempted"], 1)

    def test_all_pages_are_used_for_earliest_offer(self):
        with sdk([{"CapacityBlockOfferings": [offering(25)], "NextToken": "next"},
                  {"CapacityBlockOfferings": [offering(19)]}], one_check=True) as (client, _, _, _):
            rows = aws.fetch()
        self.assertEqual(rows[0].metric_value, 1.0)
        self.assertEqual(rows[0].state, "available")
        self.assertEqual(client.describe_capacity_block_offerings.call_args_list[1].kwargs["NextToken"], "next")
        self.assertEqual(aws.LAST_FETCH_HEALTH["pages_fetched"], 2)
        self.assertTrue(aws.LAST_FETCH_HEALTH["pagination_complete"])

    def test_empty_first_page_is_not_sold_out_when_more_results_exist(self):
        with sdk([{"CapacityBlockOfferings": [], "NextToken": "next"},
                  {"CapacityBlockOfferings": [offering(19)]}], one_check=True):
            rows = aws.fetch()
        self.assertEqual(rows[0].state, "available")

    def test_unfinished_pagination_never_establishes_a_floor_or_sold_out(self):
        pages = [{"CapacityBlockOfferings": [offering(19)], "NextToken": str(i)} for i in range(3)]
        with sdk(pages, one_check=True) as (client, _, _, _):
            self.assertEqual(aws.fetch(), [])
        self.assertEqual(client.describe_capacity_block_offerings.call_count, 3)
        self.assertFalse(aws.LAST_FETCH_HEALTH["pagination_complete"])
        self.assertEqual(aws.LAST_FETCH_HEALTH["error_code"], "pagination_limit")

    def test_bad_schema_is_unknown_and_no_zero_fee_is_invented(self):
        for response in ({}, {"CapacityBlockOfferings": [{"UpfrontFee": "0"}]}):
            with self.subTest(response=response), sdk(response, one_check=True):
                self.assertEqual(aws.fetch(), [])
                self.assertEqual(aws.LAST_FETCH_HEALTH["failed_checks"], 1)
        with sdk({"CapacityBlockOfferings": [{"StartDate": offering(19)["StartDate"]}]}, one_check=True):
            rows = aws.fetch()
        self.assertNotIn("$0", rows[0].detail)

    def test_missing_credentials_preserve_planned_scope_without_requests(self):
        with patch.dict(aws.os.environ, {}, clear=True):
            self.assertEqual(aws.fetch(), [])
        self.assertEqual(aws.LAST_FETCH_HEALTH["status"], "pending")
        self.assertEqual(aws.LAST_FETCH_HEALTH["requests_attempted"], 0)
        self.assertEqual(len(aws.LAST_FETCH_HEALTH["unqueried_checks"]), 16)


def location(identifier="east", *, eth=0, ib=0):
    return {"id": identifier, "specs_per_node": {"gpu_model": "NVIDIA H100"},
            "gpu_count_ethernet": eth, "gpu_count_infiniband": ib}


def voltage_fetch(results, **changes):
    payload = {"results": results, "total_result_count": len(results), "has_next": False, "has_previous": False}
    payload.update(changes)
    with patch("fetchers._http.http_get", return_value=json.dumps(payload).encode()) as get:
        rows = voltage.fetch()
    get.assert_called_once_with(voltage.API, timeout=20, retries=1)
    return rows


class VoltagePark(unittest.TestCase):
    def test_empty_live_envelope_is_unknown_inventory_not_zero_stock(self):
        self.assertEqual(voltage_fetch([]), [])
        self.assertEqual(voltage.LAST_FETCH_HEALTH["status"], "empty")
        self.assertEqual(voltage.LAST_FETCH_HEALTH["completed_checks"], 1)
        self.assertIn("unknown, not zero", voltage.LAST_FETCH_HEALTH["reason"])

    def test_explicit_zero_counts_and_positive_counts_are_distinct(self):
        rows = voltage_fetch([location()])
        self.assertEqual((rows[0].state, rows[0].metric_value), ("sold_out", 0.0))
        rows = voltage_fetch([location(eth=8, ib=16), location("west", eth=8, ib=0)])
        self.assertEqual((rows[0].state, rows[0].metric_value), ("limited", 32.0))
        self.assertEqual(voltage.LAST_FETCH_HEALTH["parsed_locations"], 2)
        self.assertEqual(voltage.LAST_FETCH_HEALTH["status"], "live")

    def test_missing_invalid_or_negative_counts_never_become_zero(self):
        for bad in (None, "0", -1, True):
            with self.subTest(bad=bad):
                rows = voltage_fetch([location(eth=bad)])
                self.assertEqual((rows[0].state, rows[0].metric_value), ("unknown", None))
                self.assertEqual(voltage.LAST_FETCH_HEALTH["status"], "partial")

    def test_incomplete_or_duplicate_locations_do_not_become_a_global_count(self):
        rows = voltage_fetch([location(eth=8)], has_next=True, total_result_count=2)
        self.assertEqual((rows[0].state, rows[0].metric_value), ("unknown", None))
        self.assertFalse(voltage.LAST_FETCH_HEALTH["pagination_complete"])
        self.assertEqual(voltage.LAST_FETCH_HEALTH["requests_attempted"], 1)
        rows = voltage_fetch([location(eth=8), location(eth=8)])
        self.assertEqual((rows[0].state, rows[0].metric_value), ("unknown", None))
        self.assertEqual(voltage.LAST_FETCH_HEALTH["invalid_locations"], 1)

    def test_access_rate_limit_and_connection_failures_remain_separate(self):
        errors = [(HTTPError(voltage.API, 403, "Forbidden", {}, None), "http_403"),
                  (HTTPError(voltage.API, 429, "Rate limited", {}, None), "http_429"),
                  (URLError("connection unavailable"), "URLError")]
        for error, code in errors:
            with self.subTest(code=code), patch("fetchers._http.http_get", side_effect=error) as get:
                self.assertEqual(voltage.fetch(), [])
                self.assertEqual(voltage.LAST_FETCH_HEALTH["error_code"], code)
                self.assertEqual(voltage.LAST_FETCH_HEALTH["status"], "failed")
                get.assert_called_once()

    def test_schema_change_is_reported_not_treated_as_empty_inventory(self):
        with patch("fetchers._http.http_get", return_value=b'{"items": []}'):
            self.assertEqual(voltage.fetch(), [])
        self.assertEqual(voltage.LAST_FETCH_HEALTH["error_code"], "invalid_schema")
        self.assertEqual(voltage.LAST_FETCH_HEALTH["status"], "failed")


class DailyHealthIntegration(unittest.TestCase):
    def test_empty_voltage_inventory_is_not_live_and_never_reuses_prior_stock(self):
        from capacity import main
        envelope = {"results": [], "total_result_count": 0, "has_next": False, "has_previous": False}
        with patch("fetchers._http.http_get", return_value=json.dumps(envelope).encode()), \
                patch.object(main.store, "update_peer_cache") as update, \
                patch.object(main.store, "get_cached_records") as cache, \
                patch.object(main.store, "load_last_snapshot", return_value=[]), \
                patch.object(main, "write_artifacts"):
            manifest = main.run(["voltage_park"], test=True)
        update.assert_not_called()
        cache.assert_not_called()
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["live_provider_count"], 0)
        status = manifest["provider_status"]["voltage_park"]
        self.assertEqual(status["status"], "empty")
        self.assertIn("unknown, not zero", status["reason"])

    def test_partial_aws_results_preserve_scoped_evidence_without_overwriting_cache(self):
        from capacity import main
        with sdk([{"CapacityBlockOfferings": [offering(19)]}, ApiError("CapacityBlockDescribeLimitExceeded")]), \
                patch.object(main.store, "update_peer_cache") as update, \
                patch.object(main.store, "get_cached_records") as cache, \
                patch.object(main.store, "load_last_snapshot", return_value=[]), \
                patch.object(main, "write_artifacts") as render:
            manifest = main.run(["aws_capacity_blocks"], test=True)
        update.assert_not_called()
        cache.assert_not_called()
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["live_provider_count"], 0)
        self.assertEqual(manifest["record_count"], 1)
        status = manifest["provider_status"]["aws_capacity_blocks"]
        self.assertEqual(status["status"], "partial")
        self.assertEqual(status["completed_checks"], 1)
        self.assertEqual(status["planned_checks"], 16)
        self.assertEqual(len(status["unqueried_checks"]), 14)
        self.assertEqual(len(render.call_args.args[0]), 1)


if __name__ == "__main__":
    unittest.main()
