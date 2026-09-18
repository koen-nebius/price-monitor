"""Offline regeneration must preserve retrieval evidence and never imply delivery."""
from contextlib import ExitStack
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import rebuild_reports as rebuild
from schema import DiffEntry


def record(day="2026-09-18", price=4.0, provider="together", gpu="H100"):
    return {"provider": provider, "gpu_model": gpu, "gpu_count": 8,
            "instance_type": f"{provider}-hgx-{gpu.lower()}", "region": "global",
            "consumption_type": "on_demand", "price_per_hour_usd": price * 8,
            "price_per_gpu_hour_usd": price, "data_source": "web_scrape",
            "fetched_at": f"{day}T07:00:00+00:00"}


def fixture(folder, day="2026-09-18", records=None):
    folder.mkdir(parents=True, exist_ok=True)
    records = records or [record(day)]
    manifest = {"run_date": day, "started_at": f"{day}T06:58:00+00:00",
                "completed_at": f"{day}T07:01:00+00:00", "status": "partial",
                "record_count": len(records), "raw_record_count": len(records),
                "provider_status": {"together": {"status": "live", "record_count": 1},
                                    "sfcompute": {"status": "cache", "cache_age_hours": 500}},
                "provider_freshness": {"sfcompute": {"status": "cached", "age_hours": 500}},
                "failed_providers": [], "stale_providers": ["sfcompute"],
                "warnings": ["original retrieval warning"], "diff_count": 5, "post_thread": True,
                "generated_outputs": {"slack_message": True}, "delivery_receipt": "old receipt unchanged"}
    (folder / "last_snapshot.json").write_text(json.dumps(records))
    (folder / "run_manifest.json").write_text(json.dumps(manifest))
    (folder / "history.csv").write_text("snapshot_date,provider,gpu_model,consumption_type,price_per_gpu_hour_usd\n")
    return manifest


def render_stubs(stack):
    for name in ("format_slack_summary", "format_slack_message", "format_confluence_table", "format_spot_auction_page"):
        stack.enter_context(patch.object(rebuild.renderer, name, return_value=f"{name}: source report"))
    stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")))
    stack.enter_context(patch.object(rebuild.renderer, "compute_diff", return_value=[]))


class OfflineReportRebuildTests(unittest.TestCase):
    def test_rebuild_preserves_retrieval_and_raw_files_and_adds_separate_artifact_clock(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            folder = Path(directory) / "store"
            original = fixture(folder)
            protected = {name: (folder / name).read_bytes() for name in ["last_snapshot.json", "history.csv"]}
            render_stubs(stack)
            output = rebuild.rebuild_reports(folder, discover_git=False, generated_at="2026-09-20T12:00:00+00:00")
            for key in ("run_date", "started_at", "completed_at", "status", "provider_status", "provider_freshness",
                        "record_count", "raw_record_count", "failed_providers", "stale_providers", "warnings", "delivery_receipt"):
                self.assertEqual(output[key], original[key], key)
            self.assertEqual(output["artifacts_generated_at"], "2026-09-20T12:00:00+00:00")
            self.assertFalse(output["post_thread"])
            self.assertFalse(output["artifact_generation"]["comparison_available"])
            self.assertIn("comparison was not regenerated", (folder / "slack_message.txt").read_text())
            self.assertEqual(set(output["artifact_generation"]["artifacts"]),
                             {"slack_message.txt", "slack_thread.txt", "confluence_body.html", "spot_auction_body.html", "report_diff_2026-09-18.json", "coverage.json", "quote_coverage.json"})
            for name, raw in protected.items(): self.assertEqual((folder / name).read_bytes(), raw)
            self.assertNotIn("delivered", output["artifact_generation"])

    def test_saved_catalogue_and_intel_are_rebuilt_at_original_clock_without_refresh_or_input_writes(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            folder = Path(directory) / "store"
            original = fixture(folder)
            offers = [
                {"provider": "lambda", "product_id": "dated-quote-product", "gpu_model": "H100",
                 "price_status": "quote_required", "source_url": "https://lambda.ai/pricing",
                 "observed_at": "2026-09-01T06:00:00+00:00", "retrieved_at": "2026-09-18T06:00:00+00:00"},
                {"provider": "coreweave", "product_id": "future-product", "gpu_model": "B300",
                 "price_status": "quote_required", "source_url": "https://www.coreweave.com/pricing",
                 "observed_at": "2026-09-19T06:00:00+00:00", "retrieved_at": "2026-09-19T06:00:00+00:00"},
            ]
            (folder / "catalogue.json").write_text(json.dumps({"as_of": "2026-09-19", "offers": offers}))
            quote = {"message_ts": "1.0", "message_date": "2026-09-17", "gpu_model": "H100",
                     "price_per_gpu_hour_usd": "3.00", "term_months": "12", "prepay_pct": "0",
                     "prepay_known": "1", "provider_type": "neocloud", "provider_name": "lambda", "notes": "",
                     "quote_id": "source-quote", "quote_status": "asking_price", "source_url": "https://example.com/quote",
                     "source_observed_at": "2026-09-16", "expires_on": "2026-09-19", "instance_type": "gpu_8x_h100_sxm5",
                     "gpu_variant": "H100 SXM", "region": "us-west-3", "gpu_count": "8", "gpu_count_relation": "exact",
                     "delivery_start": "2026-09-20", "delivery_end": "2027-09-20", "currency": "USD", "tax_basis": "excluded"}
            future = {**quote, "message_ts": "2.0", "message_date": "2026-09-19", "quote_id": "future-quote"}
            with (folder / "intel.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(quote))
                writer.writeheader(); writer.writerows([quote, future])
            protected = {name: (folder / name).read_bytes() for name in
                         ("catalogue.json", "intel.csv", "last_snapshot.json", "history.csv")}
            render_stubs(stack)
            original_intel = rebuild.renderer.INTEL_CSV
            coverage = stack.enter_context(patch.object(rebuild, "build_price_coverage", wraps=rebuild.build_price_coverage))
            observed_paths = []
            def confluence(*args, **kwargs):
                observed_paths.append(rebuild.renderer.INTEL_CSV)
                from offer_catalogue import render_catalogue
                from quote_evidence import render_quote_report
                return render_catalogue(kwargs["catalogue_report"]) + render_quote_report(kwargs["quote_report"])
            rebuild.renderer.format_confluence_table.side_effect = confluence
            result = rebuild.rebuild_reports(folder, discover_git=False, generated_at="2026-10-01T12:00:00+00:00")
            self.assertEqual(observed_paths, [folder.resolve() / "intel.csv"])
            self.assertEqual(rebuild.renderer.INTEL_CSV, original_intel)
            quote_report = json.loads((folder / "quote_coverage.json").read_text())
            self.assertEqual(quote_report["as_of"], original["completed_at"])
            self.assertEqual(len(quote_report["observations"]), 1)
            self.assertEqual(quote_report["observations"][0]["quote_id"], "source-quote")
            self.assertEqual(quote_report["observations"][0]["status"], "qualified_asking_price")
            self.assertEqual(quote_report["observations"][0]["source_observed_at"], "2026-09-16")
            catalogue = rebuild.renderer.format_confluence_table.call_args.kwargs["catalogue_report"]
            self.assertEqual(catalogue["as_of"], original["completed_at"])
            self.assertEqual(len(catalogue["offers"]), 1)
            self.assertEqual(catalogue["offers"][0]["freshness"], "dated")
            self.assertEqual(catalogue["offers"][0]["observed_at"], offers[0]["observed_at"])
            self.assertEqual(coverage.call_args.kwargs["quote_report"], quote_report)
            self.assertEqual(coverage.call_args.kwargs["catalogue_offers"], catalogue["offers"])
            html = (folder / "confluence_body.html").read_text()
            self.assertIn("dated-quote-product", html)
            self.assertIn("source-quote", html)
            self.assertNotIn("future-product", html)
            self.assertNotIn("future-quote", html)
            self.assertTrue(result["generated_outputs"]["quote_coverage"])
            inputs = result["artifact_generation"]["supplementary_inputs"]
            self.assertEqual(inputs["catalogue"]["later_records_excluded"], 1)
            for name in ("catalogue", "field_intelligence"):
                self.assertTrue(inputs[name]["present"])
                self.assertEqual(len(inputs[name]["sha256"]), 64)
            for name, before in protected.items():
                self.assertEqual((folder / name).read_bytes(), before)

    def test_missing_supplementary_inputs_produce_empty_views_without_reading_global_intel(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            folder = Path(directory) / "store"
            original = fixture(folder)
            render_stubs(stack)
            result = rebuild.rebuild_reports(folder, discover_git=False)
            quote_report = json.loads((folder / "quote_coverage.json").read_text())
            self.assertEqual(quote_report["observations"], [])
            self.assertEqual(quote_report["as_of"], original["completed_at"])
            args = rebuild.renderer.format_confluence_table.call_args.kwargs
            self.assertEqual(args["catalogue_report"]["offers"], [])
            self.assertEqual(args["quote_report"], quote_report)
            self.assertFalse((folder / "catalogue.json").exists())
            self.assertFalse((folder / "intel.csv").exists())
            inputs = result["artifact_generation"]["supplementary_inputs"]
            self.assertFalse(inputs["catalogue"]["present"])
            self.assertFalse(inputs["field_intelligence"]["present"])

    def test_bad_catalogue_fails_before_writing_any_generated_artifact(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            folder = Path(directory) / "store"
            fixture(folder)
            (folder / "catalogue.json").write_text('{"offers": "invalid"}')
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            render_stubs(stack)
            with self.assertRaisesRegex(ValueError, "offer list"):
                rebuild.rebuild_reports(folder, discover_git=False)
            self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, before)

    def test_previous_run_must_be_coherent_and_precede_current(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            current, previous = Path(directory) / "current", Path(directory) / "previous"
            fixture(current)
            old_manifest = fixture(previous, day="2026-09-17", records=[record("2026-09-17", 3.99)])
            render_stubs(stack)
            result = rebuild.rebuild_reports(current, previous_snapshot=previous / "last_snapshot.json",
                                              previous_manifest=previous / "run_manifest.json", discover_git=False)
            self.assertTrue(result["artifact_generation"]["comparison_available"])
            self.assertEqual(result["artifact_generation"]["comparison_baseline"]["run_date"], "2026-09-17")
            old_manifest["record_count"] = 500
            (previous / "run_manifest.json").write_text(json.dumps(old_manifest))
            with self.assertRaisesRegex(ValueError, "count"):
                rebuild.rebuild_reports(current, previous_snapshot=previous / "last_snapshot.json",
                                        previous_manifest=previous / "run_manifest.json", discover_git=False)

    def test_three_skus_from_one_provider_do_not_trigger_coordinated_market_thread(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            current, previous = Path(directory) / "current", Path(directory) / "previous"
            gpus = ["H100", "H200", "B200"]
            fixture(current, records=[record(gpu=gpu) for gpu in gpus])
            fixture(previous, day="2026-09-17", records=[record("2026-09-17", 3, gpu=gpu) for gpu in gpus])
            render_stubs(stack)
            rebuild.renderer.compute_diff.return_value = [DiffEntry(
                provider="together", gpu_model=gpu, region="global", consumption_type="on_demand",
                instance_type=f"together-hgx-{gpu.lower()}", change_type="price_change",
                old_price=3, new_price=4, delta_pct=33.33) for gpu in gpus]
            result = rebuild.rebuild_reports(current, previous_snapshot=previous / "last_snapshot.json",
                                              previous_manifest=previous / "run_manifest.json", discover_git=False)
            self.assertEqual(result["significant_moves"], 3)
            self.assertFalse(result["post_thread"])

    def test_verified_source_restatement_triggers_correction_detail_without_market_move(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            current, previous = Path(directory) / "current", Path(directory) / "previous"
            fixture(current, day="2026-09-17", records=[record("2026-09-17", 3.99)])
            fixture(previous, day="2026-09-16", records=[record("2026-09-16", 1.99)])
            render_stubs(stack)
            rebuild.renderer.compute_diff.return_value = [DiffEntry(
                provider="together", gpu_model="H100", region="global", consumption_type="on_demand",
                instance_type="together-hgx-h100", change_type="restatement",
                old_price=1.99, new_price=3.99, delta_pct=100.5)]
            result = rebuild.rebuild_reports(current, previous_snapshot=previous / "last_snapshot.json",
                                              previous_manifest=previous / "run_manifest.json", discover_git=False)
            self.assertTrue(result["post_thread"])
            self.assertEqual(result["significant_moves"], 0)
            self.assertEqual(result["artifact_generation"]["diff_types"], {"restatement": 1})

    def test_multiple_aggregators_cannot_trigger_coordinated_provider_thread(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            current, previous = Path(directory) / "current", Path(directory) / "previous"
            providers = ["cp_hyperstack", "cp_together-ai", "cp_oracle"]
            fixture(current, records=[record(provider=p) for p in providers])
            fixture(previous, day="2026-09-17", records=[record("2026-09-17", 3, provider=p) for p in providers])
            render_stubs(stack)
            rebuild.renderer.compute_diff.return_value = [DiffEntry(
                provider=p, gpu_model="H100", region="global", consumption_type="on_demand",
                instance_type=f"{p}-hgx-h100", change_type="price_change", old_price=3, new_price=4, delta_pct=33.33)
                for p in providers]
            result = rebuild.rebuild_reports(current, previous_snapshot=previous / "last_snapshot.json",
                                              previous_manifest=previous / "run_manifest.json", discover_git=False)
            self.assertEqual(result["significant_moves"], 0)
            self.assertFalse(result["post_thread"])

    def test_stale_source_cannot_become_a_price_move_in_regenerated_report(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            current, previous = Path(directory) / "current", Path(directory) / "previous"
            stale = record("2026-08-01", 8, provider="sfcompute")
            fixture(current, records=[record(), stale])
            fixture(previous, day="2026-09-17", records=[record("2026-09-17"), {**stale, "price_per_gpu_hour_usd": 1}])
            render_stubs(stack)
            rebuild.renderer.compute_diff.return_value = [DiffEntry(
                provider="sfcompute", gpu_model="H100", region="global", consumption_type="on_demand",
                instance_type="sfcompute-hgx-h100", change_type="price_change", old_price=1, new_price=8, delta_pct=700)]
            result = rebuild.rebuild_reports(current, previous_snapshot=previous / "last_snapshot.json",
                                              previous_manifest=previous / "run_manifest.json", discover_git=False)
            self.assertEqual(result["diff_count"], 0)
            self.assertEqual(result["artifact_generation"]["suppressed_ineligible_diff_records"], 1)
            self.assertTrue(any("sfcompute" in n for n in result["comparison_exclusions"]))

    def test_git_discovery_uses_snapshot_and_manifest_from_same_earlier_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root, day="2026-09-17", records=[record("2026-09-17")])
            calls = []
            def fake_git(repo, *args):
                calls.append(args)
                if args[0] == "log": return b"new\nold\n"
                if args[1] == "new:store/run_manifest.json": return b'{"run_date":"2026-09-18"}'
                if args[1] == "old:store/run_manifest.json": return (root / "run_manifest.json").read_bytes()
                if args[1] == "old:store/last_snapshot.json": return (root / "last_snapshot.json").read_bytes()
                raise AssertionError(args)
            with patch.object(rebuild, "_git", side_effect=fake_git):
                previous = rebuild.previous_git_run(root, "2026-09-18")
            self.assertEqual(previous[1]["run_date"], "2026-09-17")
            self.assertIn("git:old:", previous[2]["source"])
            self.assertNotIn(("show", "new:store/last_snapshot.json"), calls)


if __name__ == "__main__":
    unittest.main()
