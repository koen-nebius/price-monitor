"""Partial/empty API reads stay distinct from healthy or sold-out inventory."""
from contextlib import ExitStack
import os
import unittest
from unittest.mock import patch
from capacity import config, insights, main, render, store
from capacity.schema import AvailabilityRecord


class FetchHealthTests(unittest.TestCase):
    def run_feed(self, status, rows=None, cached=None, provider="voltage_park",
                 health_extra=None, error=None):
        def fetch(provider):
            if status is not None:
                main.FETCH_HEALTH[provider] = {
                    "status": status, "reason": "bounded source result",
                    "completed_checks": 1, "planned_checks": 16,
                    **(health_extra or {}),
                }
            if error:
                raise error
            return rows or []
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "_fetch_provider", side_effect=fetch))
            cache = stack.enter_context(patch.object(store, "get_cached_records", return_value=(cached or [], 2)))
            update = stack.enter_context(patch.object(store, "update_peer_cache"))
            stack.enter_context(patch.object(store, "load_last_snapshot", return_value=[]))
            stack.enter_context(patch.object(main, "write_artifacts"))
            save = stack.enter_context(patch.object(store, "save_snapshot"))
            history = stack.enter_context(patch.object(store, "append_history"))
            manifest = stack.enter_context(patch.object(store, "save_run_manifest"))
            result = main.run(providers=[provider], test=True)
            save.assert_not_called()
            history.assert_not_called()
            manifest.assert_not_called()
        return result, cache, update

    def test_empty_success_does_not_replay_cached_positive_stock(self):
        old = AvailabilityRecord("voltage_park", "H100", "us", "on_demand", "available", "binary", 1)
        result, cache, update = self.run_feed("empty", cached=[old])
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(result["provider_status"]["voltage_park"]["status"], "empty")
        self.assertEqual(result["status"], "partial")
        cache.assert_not_called()
        update.assert_not_called()

    def test_partial_rows_are_retained_without_overwriting_complete_cache(self):
        row = AvailabilityRecord("voltage_park", "H100", "us", "on_demand", "unknown", "binary")
        result, cache, update = self.run_feed("partial", [row])
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["live_provider_count"], 0)
        self.assertEqual(result["provider_status"]["voltage_park"]["planned_checks"], 16)
        self.assertEqual(result["failed_providers"], ["voltage_park"])
        self.assertEqual(result["status"], "partial")
        cache.assert_not_called()
        update.assert_not_called()

    def test_failed_read_reason_survives_cache_fallback(self):
        old = AvailabilityRecord("voltage_park", "H100", "us", "on_demand", "available", "binary", 1)
        result, cache, update = self.run_feed("failed", cached=[old])
        status = result["provider_status"]["voltage_park"]
        self.assertEqual(status["status"], "cached")
        self.assertEqual(status["reason"], "bounded source result")
        self.assertEqual(result["status"], "partial")
        self.assertIn("bounded source result", render._provider_read_freshness("voltage_park", result))
        update.assert_not_called()

    def test_partial_and_empty_are_degraded_with_scope_visible(self):
        for state, label in (("partial", "partial:"), ("empty", "empty catalogue:")):
            with self.subTest(state=state):
                result, _, _ = self.run_feed(state, health_extra={"unqueried_checks": ["region-b"]})
                fresh = insights.freshness(result)
                self.assertEqual(fresh[state], ["voltage_park"])
                text, html = render._fresh_line(result)
                self.assertIn('data-color="yellow"', html)
                self.assertIn(label, text)
                self.assertIn(label, html)
                self.assertNotIn("all 0 live", text)
                detail = render._provider_read_freshness("voltage_park", result)
                self.assertIn("bounded source result", detail)
                self.assertIn("1/16 checks completed", detail)
                self.assertIn("1 checks unqueried", detail)

    def test_explicit_aws_failure_is_not_hidden_by_pending_activation(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(config, "PENDING_ACTIVATION", {"aws_capacity_blocks"}):
            result, _, update = self.run_feed("failed", provider="aws_capacity_blocks", health_extra={
                "reason": "account describe limit reached", "error_code": "CapacityBlockDescribeLimitExceeded",
            })
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_providers"], ["aws_capacity_blocks"])
        fresh = insights.freshness(result)
        self.assertEqual(fresh["failed"], ["aws_capacity_blocks"])
        self.assertEqual(fresh["pending"], [])
        text, html = render._fresh_line(result)
        self.assertIn('data-color="yellow"', html)
        self.assertIn("down:", text)
        self.assertNotIn("awaiting access", text)
        detail = render._provider_read_freshness("aws_capacity_blocks", result)
        self.assertIn("Fetch failed", detail)
        self.assertIn("account describe limit reached", detail)
        self.assertIn("CapacityBlockDescribeLimitExceeded", detail)
        update.assert_not_called()

    def test_explicit_pending_is_distinct_from_failure(self):
        result, cache, update = self.run_feed("pending", provider="aws_capacity_blocks")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_providers"], [])
        text, html = render._fresh_line(result)
        self.assertIn('data-color="yellow"', html)
        self.assertIn("no active checks", text)
        self.assertIn("1 awaiting access", text)
        self.assertIn("API access pending", render._provider_read_freshness("aws_capacity_blocks", result))
        cache.assert_not_called()
        update.assert_not_called()

    def test_legacy_pending_requires_actual_missing_credentials(self):
        for environment, expected in (({}, "pending"), ({"HYPERSTACK_API_KEY": "test-placeholder"}, "failed")):
            with self.subTest(expected=expected), patch.dict(os.environ, environment, clear=True), \
                    patch.object(config, "PENDING_ACTIVATION", {"hyperstack"}):
                result, cache, update = self.run_feed(None, provider="hyperstack")
            self.assertEqual(result["provider_status"]["hyperstack"]["status"], expected)
            if expected == "pending":
                cache.assert_not_called()
            else:
                cache.assert_called_once_with("hyperstack")
            update.assert_not_called()

    def test_failed_health_cannot_promote_returned_rows_or_replace_cache(self):
        unsafe = AvailabilityRecord("voltage_park", "H100", "us", "on_demand", "available", "binary", 1)
        old = AvailabilityRecord("voltage_park", "H100", "us", "on_demand", "sold_out", "binary", 0)
        result, cache, update = self.run_feed("failed", rows=[unsafe], cached=[old])
        self.assertEqual(result["provider_status"]["voltage_park"]["status"], "cached")
        self.assertEqual(result["live_provider_count"], 0)
        self.assertEqual(result["record_count"], 1)
        cache.assert_called_once_with("voltage_park")
        update.assert_not_called()
        result, _, update = self.run_feed("failed", rows=[unsafe])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["record_count"], 0)
        update.assert_not_called()

    def test_exception_cannot_be_downgraded_to_pending(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(config, "PENDING_ACTIVATION", {"hyperstack"}):
            result, _, update = self.run_feed("pending", provider="hyperstack", error=RuntimeError("private body"))
        health = result["provider_status"]["hyperstack"]
        self.assertEqual(health["status"], "failed")
        self.assertEqual(health["error_code"], "RuntimeError")
        self.assertNotIn("private body", str(result))
        update.assert_not_called()

    def test_no_provider_checks_cannot_render_all_live(self):
        text, html = render._fresh_line({"provider_status": {}})
        self.assertEqual(text, "Feeds: no active checks")
        self.assertIn("No active checks", html)
        self.assertIn('data-color="yellow"', html)


if __name__ == "__main__":
    unittest.main()
