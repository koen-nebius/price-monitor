"""Offline orchestration regressions for price, catalogue and fetch-health evidence.

Every provider/cross-check fetch is mocked. All output files use a temporary
directory; peer-cache, snapshot, history and position mutations are intercepted.
"""
from contextlib import ExitStack, contextmanager
import copy
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

import diff
import main
from fetchers import lambda_labs
from intel_schema import INTEL_COLUMNS
from schema import PriceRecord


def price(now, **changes):
    values = dict(provider="lambda", gpu_model="H100", gpu_count=8,
                  instance_type="gpu_8x_h100_sxm5", region="us-east-1",
                  consumption_type="on_demand", price_per_hour_usd=31.92,
                  price_per_gpu_hour_usd=3.99, fetched_at=now,
                  source_observed_at=now, source_url="https://lambda.ai/pricing",
                  data_source="official_api", form_factor="SXM", interconnect="IB")
    values.update(changes)
    return PriceRecord(**values)


def catalogue(now, **changes):
    values = dict(provider="crusoe", product_id="public-tariff-h100",
                  gpu_model="H100", region="unspecified", purchase_type="on_demand",
                  gpu_count=None, gpu_count_relation="unknown", price_status="published_unscoped",
                  price_per_gpu_hour_usd=3.90, currency="USD",
                  source_url="https://www.crusoe.ai/cloud/pricing",
                  observed_at=now, retrieved_at=now,
                  description="Public per-GPU tariff; purchasable configuration unreported",
                  commercial_terms={"price_column": "On-demand"})
    values.update(changes)
    return values


@contextmanager
def pipeline(feeds, caches=None, references=None, quote_rows=None):
    """Run the real orchestrator inside a fully isolated persistence boundary."""
    caches = caches or {}
    references = references or {}
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        target = Path(directory)
        if quote_rows:
            with (target / "intel.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=INTEL_COLUMNS)
                writer.writeheader()
                writer.writerows(quote_rows)

        def fake_fetch(provider):
            records, metadata = feeds[provider]
            main.FETCH_METADATA[provider] = copy.deepcopy(metadata)
            return copy.deepcopy(records)

        def write_json(filename, value):
            (target / filename).write_text(json.dumps(value, indent=2) + "\n")

        def save_snapshot(records, day):
            write_json(day.isoformat() + ".json", [r.to_dict() for r in records])

        def save_last(records):
            write_json("last_snapshot.json", [r.to_dict() for r in records])

        mocks = {}
        replacements = {
            "STORE_DIR": target,
            "_fetch_provider": fake_fetch,
            "get_cached_records": lambda p: copy.deepcopy(caches.get(p, [])),
            "get_cache_age_hours": lambda p: 8.0 if p in caches else None,
            "previous_snapshot_day": lambda: None,
            "load_last_snapshot": lambda: [],
            "load_snapshot": lambda day: [],
            "save_snapshot": save_snapshot,
            "save_last_snapshot": save_last,
            "save_run_manifest": lambda value: write_json("run_manifest.json", value),
        }
        for name, value in replacements.items():
            if name == "STORE_DIR":
                stack.enter_context(patch.object(main, name, value))
            else:
                mocks[name] = stack.enter_context(patch.object(main, name, side_effect=value))
        for name in ("update_peer_cache", "append_history_records"):
            mocks[name] = stack.enter_context(patch.object(main, name))
        for name in ("format_slack_summary", "format_slack_message", "format_confluence_table",
                     "format_spot_auction_page"):
            mocks[name] = stack.enter_context(patch.object(main, name, return_value="isolated report"))
        mocks["positions"] = stack.enter_context(patch("diff.record_position_history"))
        stack.enter_context(patch("test_consistency.check_cross_table_consistency", return_value=[]))
        stack.enter_context(patch("storage_page.format_storage_page", return_value="isolated storage"))
        for feed in ("computeprices", "gpuhunt"):
            mocks[feed] = stack.enter_context(patch(
                "fetchers." + feed + ".fetch_crosscheck", return_value=references.get(feed, [])))
        stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")))
        stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network forbidden")))
        # Preserve metadata across test cases and never retain a previous feed's
        # catalogue when a later fetch fails or produces no observations.
        stack.enter_context(patch.dict(main.FETCH_METADATA, {}, clear=True))
        with unittest.TestCase().assertLogs("main", level="INFO"):
            result = main.run(providers=list(feeds), test=True)
        yield result, target, mocks


class NeocloudPipelineHealth(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).isoformat()

    def test_catalogue_only_success_never_restores_legacy_crusoe_price_cache(self):
        old = price(self.now, provider="crusoe", gpu_count=1, instance_type="crusoe-h100",
                    price_per_hour_usd=3.90, price_per_gpu_hour_usd=3.90)
        metadata = {"health": {"status": "catalogue_only", "reason": "Configuration unreported"},
                    "catalogue": [catalogue(self.now)]}
        with pipeline({"crusoe": ([], metadata)}, caches={"crusoe": [old]}) as (result, target, mocks):
            self.assertEqual(result["records"], 0)
            self.assertEqual(result["manifest"]["provider_status"]["crusoe"]["status"], "catalogue_only")
            self.assertEqual(result["manifest"]["catalogue_record_count"], 1)
            self.assertEqual(json.loads((target / "last_snapshot.json").read_text()), [])
            mocks["get_cached_records"].assert_not_called()
            mocks["update_peer_cache"].assert_not_called()
            self.assertEqual(mocks["positions"].call_args.args[0], [])
            offer = json.loads((target / "catalogue.json").read_text())["offers"][0]
            self.assertEqual(offer["price_per_gpu_hour_usd"], 3.90)
            self.assertIsNone(offer["gpu_count"])
            self.assertFalse(offer["comparison_eligible"])

    def test_partial_lambda_keeps_observed_rows_without_overwriting_healthy_cache(self):
        row = price(self.now)
        metadata = {"health": {"status": "partial", "reason": "One-click cluster table unavailable",
                    "components": {"on_demand": {"status": "live"},
                                   "one_click_clusters": {"status": "failed", "record_count": 0}}}}
        with pipeline({"lambda": ([row], metadata)}, caches={"lambda": [row]}) as (result, target, mocks):
            self.assertEqual(result["records"], 1)
            self.assertEqual(result["manifest"]["status"], "partial")
            self.assertEqual(result["manifest"]["provider_status"]["lambda"]["status"], "partial")
            self.assertEqual(result["manifest"]["provider_status"]["lambda"]["components"],
                             metadata["health"]["components"])
            mocks["update_peer_cache"].assert_not_called()
            mocks["get_cached_records"].assert_not_called()
            self.assertEqual(mocks["append_history_records"].call_args.args[0][0].instance_type,
                             row.instance_type)

    def test_failed_and_empty_source_results_survive_missing_or_cached_delivery(self):
        for fetch_status in ("failed", "empty"):
            for has_cache in (False, True):
                with self.subTest(fetch_status=fetch_status, has_cache=has_cache):
                    health = {"status": fetch_status, "reason": "Official pricing response has no usable rows",
                              "attempted_pages": 1, "record_count": 0}
                    caches = {"lambda": [price(self.now)]} if has_cache else {}
                    with pipeline({"lambda": ([], {"health": health})}, caches=caches) as (result, target, mocks):
                        manifest = result["manifest"]
                        status = manifest["provider_status"]["lambda"]
                        self.assertEqual(status.get("fetch_status", status["status"]), fetch_status)
                        self.assertEqual(status["reason"], health["reason"])
                        self.assertEqual(status["attempted_pages"], 1)
                        self.assertIn("lambda", manifest["failed_providers"])
                        self.assertNotEqual(manifest["status"], "success")
                        self.assertNotEqual(manifest["provider_freshness"]["lambda"]["status"], "live")
                        mocks["update_peer_cache"].assert_not_called()
                        self.assertEqual(json.loads((target / "run_manifest.json").read_text()), manifest)

    def test_undated_skypilot_fetch_is_reference_only_and_never_live(self):
        raw = ("InstanceType,AcceleratorName,AcceleratorCount,vCPUs,MemoryGiB,Price,Region,SpotPrice\n"
               "gpu_8x_h100_sxm5,H100,8,208,1800,31.92,us-east-1,\n")
        records = lambda_labs._parse_skypilot_catalog(raw, self.now)
        health = {"status": "fallback", "reason": "Only undated SkyPilot references available"}
        with pipeline({"lambda": (records, {"health": health})}) as (result, target, mocks):
            self.assertEqual(result["records"], 1)  # provenance remains inspectable
            manifest = result["manifest"]
            self.assertEqual(manifest["provider_status"]["lambda"]["status"], "fallback")
            self.assertEqual(manifest["provider_freshness"]["lambda"]["status"], "fallback")
            self.assertEqual(manifest["comparison_record_count"], 0)
            self.assertTrue(manifest["comparison_exclusions"])
            mocks["update_peer_cache"].assert_not_called()
            self.assertEqual(mocks["positions"].call_args.args[0], [])
            coverage = json.loads((target / "coverage.json").read_text())
            self.assertEqual(coverage["cells"][0]["statuses"], {"reference_only": 1})
            self.assertEqual(coverage["cells"][0]["latest_observed_at"], "")
            stored = json.loads((target / "last_snapshot.json").read_text())[0]
            self.assertEqual(stored["source_observed_at"], "")
            self.assertFalse(stored["comparison_eligible"])

    def test_manifest_crosschecks_and_written_catalogue_quotes_match_renderer_inputs(self):
        day = self.now[:10]
        quote = dict(message_ts="fixture-quote", message_date=day, gpu_model="H100",
                     price_per_gpu_hour_usd="2.40", term_months="12", prepay_pct="",
                     provider_type="neocloud", provider_name="Crusoe", notes="Fixture only",
                     source_url="https://example.com/quotes/test", quote_status="asking_price",
                     source_observed_at=day, quote_id="offline-quote-1")
        reports = {feed: {"status": "complete", "summary": {"not_comparable": 1},
                         "comparisons": [{"reference": {"source": feed, "offer_id": feed + ":fixture"},
                                          "status": "not_comparable", "reasons": ["unknown SKU"]}],
                         "warnings": []} for feed in ("computeprices", "gpuhunt")}
        metadata = {"health": {"status": "catalogue_only"}, "catalogue": [catalogue(self.now)]}
        with patch("price_crosscheck.compare_price_observations", side_effect=list(reports.values())):
            with pipeline({"crusoe": ([], metadata)}, quote_rows=[quote]) as (result, target, mocks):
                manifest = json.loads((target / "run_manifest.json").read_text())
                self.assertEqual(manifest, result["manifest"])
                self.assertEqual(manifest["price_crosschecks"], reports)
                catalogue_report = json.loads((target / "catalogue.json").read_text())
                quote_report = json.loads((target / "quote_coverage.json").read_text())
                arguments = mocks["format_confluence_table"].call_args.kwargs
                self.assertEqual(arguments["catalogue_report"], catalogue_report)
                self.assertEqual(arguments["quote_report"], quote_report)
                self.assertEqual(catalogue_report["source_health"], manifest["provider_status"])
                self.assertEqual(quote_report["observations"][0]["quote_id"], "offline-quote-1")
                self.assertEqual(quote_report["observations"][0]["status"], "reference_only")
                self.assertEqual(quote_report["observations"][0]["source_url"], quote["source_url"])
                for name in ("catalogue", "quote_coverage", "coverage"):
                    self.assertTrue(manifest["generated_outputs"][name])

    def test_invalid_catalogue_is_visible_failure_and_does_not_create_evidence(self):
        broken = catalogue(self.now, source_url="")
        metadata = {"health": {"status": "catalogue_only"}, "catalogue": [broken]}
        with pipeline({"crusoe": ([], metadata)}) as (result, target, mocks):
            status = result["manifest"]["provider_status"]["crusoe"]
            self.assertEqual(status["fetch_status"], "failed")
            self.assertEqual(status["reason"], "Catalogue metadata failed validation")
            self.assertEqual(result["manifest"]["catalogue_record_count"], 0)
            self.assertEqual(json.loads((target / "catalogue.json").read_text())["offers"], [])
            mocks["update_peer_cache"].assert_not_called()

    def test_real_dispatcher_clears_old_catalogue_and_keeps_health_on_failure(self):
        from fetchers import crusoe

        def failure():
            crusoe.LAST_FETCH_HEALTH.update(status="failed", reason="Fixture retrieval failure")
            raise RuntimeError("Fixture failure")

        with patch.object(crusoe, "LAST_CATALOGUE_OFFERS", [catalogue(self.now)]), \
                patch.object(crusoe, "LAST_FETCH_HEALTH", {"status": "catalogue_only"}), \
                patch.object(crusoe, "fetch", side_effect=failure), \
                patch.dict(main.FETCH_METADATA, {}, clear=True), \
                patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")):
            with self.assertRaisesRegex(RuntimeError, "Fixture failure"):
                main._fetch_provider("crusoe")
            self.assertEqual(main.FETCH_METADATA["crusoe"]["catalogue"], [])
            self.assertEqual(main.FETCH_METADATA["crusoe"]["health"], {
                "status": "failed", "reason": "Fixture retrieval failure"})


class NeocloudEvidenceRendering(unittest.TestCase):
    def test_all_six_lambda_cluster_tiers_keep_prices_quantities_and_duration(self):
        tiers = [("HGX B200", "16", "9.86"), ("HGX B200", "64", "9.36"),
                 ("HGX B200", "256+", "8.87"), ("H100", "16", "6.16"),
                 ("H100", "64", "5.85"), ("H100", "256", "5.54")]
        source = "<table>" + "".join(
            f"<tr><th>NVIDIA {gpu}</th><td>2 weeks – 1 year</td><td>{count}</td>"
            f"<td>${rate}</td></tr>" for gpu, count, rate in tiers) + "</table>"
        stamp = datetime.now(timezone.utc).isoformat()
        records, _ = lambda_labs._parse_one_click_clusters(source, stamp)
        html = diff._build_short_term_reserved_section(records)
        table = ElementTree.fromstring("<root>" + html + "</root>").find("table")
        rendered = [row.findall("td") for row in table.findall("tbody/tr")[1:]]
        self.assertEqual(len(rendered), 6)
        actual = {(cells[0].text.rsplit(" ", 1)[-1], cells[1].text, cells[3].text) for cells in rendered}
        expected = {(gpu.replace("HGX ", ""), "$" + rate, count + " GPUs · 2 weeks – 1 year")
                    for gpu, count, rate in tiers}
        self.assertEqual(actual, expected)
        self.assertEqual(html.count(stamp), 6)
        self.assertEqual(html.count(lambda_labs.ONE_CLICK_URL), 6)

    def test_raw_legacy_crusoe_tariff_does_not_claim_an_instance_price(self):
        row = price(datetime.now(timezone.utc).isoformat(), provider="crusoe", gpu_count=1,
                    instance_type="crusoe-h100", price_per_hour_usd=3.90,
                    price_per_gpu_hour_usd=3.90, price_basis="", form_factor="", interconnect="")
        original = row.to_dict()
        html = diff._build_qualified_catalogue_section([row])
        self.assertIn("Configuration and minimum order unknown", html)
        self.assertNotIn("$3.90/instance-hr", html)
        self.assertNotIn("1 GPUs", html)
        self.assertIn("$3.9000", html)
        self.assertEqual(row.to_dict(), original)


if __name__ == "__main__":
    unittest.main()
