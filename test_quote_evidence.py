"""Quote-evidence qualification and inbox migration, using temporary CSVs only."""
import csv
from datetime import date
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from intel_schema import INTEL_COLUMNS, BASE_COLUMNS, validate_row, is_expired
from intel_quality import classify
from quote_evidence import qualification, build_quote_report, load_rows, render_quote_report
from scripts import intel_inbox_merge as inbox

AS_OF = "2026-09-18T12:00:00Z"


def quote(**changes):
    row = dict(message_ts="1789128000.000001", message_date="2026-09-11", gpu_model="H100",
               price_per_gpu_hour_usd="2.50", term_months="12", prepay_pct="0",
               provider_type="neocloud", provider_name="CoreWeave", notes="No prepayment",
               prepay_known="1", quote_id="quote-1", quote_status="asking_price",
               source_url="https://example.invalid/evidence/quote-1", source_observed_at="2026-09-10",
               expires_on="2026-09-30", instance_type="h100-sxm-8", gpu_variant="H100 SXM",
               region="us-east-1", gpu_count="256", gpu_count_relation="exact",
               delivery_start="2026-10-01", delivery_end="2027-09-30", currency="USD", tax_basis="excludes tax")
    row.update(changes)
    return row


def csv_text(rows, columns=INTEL_COLUMNS, header=True):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    if header:
        writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


class QuoteQualification(unittest.TestCase):
    def test_complete_asking_quote_and_signed_deal_remain_separate(self):
        asking, signed = quote(), quote(quote_status="signed_deal", quote_id="signed-1", expires_on="")
        self.assertEqual(qualification(asking, AS_OF), ("qualified_asking_price", []))
        self.assertEqual(qualification(signed, AS_OF), ("qualified_signed_deal", []))
        report = build_quote_report([asking, signed], AS_OF)
        self.assertEqual({r["status"] for r in report["observations"]},
                         {"qualified_asking_price", "qualified_signed_deal"})
        self.assertIn("not independent verification or current stock", report["scope"])

    def test_expiry_boundary_and_unknown_expiry_do_not_become_live_quotes(self):
        row = quote(expires_on="2026-09-18")
        self.assertFalse(is_expired(row, date(2026, 9, 18)))
        self.assertTrue(is_expired(row, date(2026, 9, 19)))
        self.assertEqual(qualification(row, "2026-09-19T00:00:00Z")[0], "expired")
        status, reasons = qualification(quote(expires_on=""), AS_OF)
        self.assertEqual(status, "reference_only")
        self.assertIn("Quote expiry unknown", reasons)

    def test_reporting_date_never_fills_unknown_quote_date(self):
        report = build_quote_report([quote(source_observed_at="")], AS_OF)
        row = report["observations"][0]
        self.assertEqual(row["status"], "reference_only")
        self.assertEqual(row["source_observed_at"], "")
        self.assertEqual(row["reported_at"], "2026-09-11")
        self.assertTrue(any("observation date unknown" in reason for reason in row["reasons"]))

    def test_missing_scope_and_closing_status_remain_reference_only(self):
        for field, missing in [("region", "global"), ("gpu_count", ""), ("gpu_count_relation", "unknown"),
                               ("instance_type", ""), ("currency", ""), ("tax_basis", "unknown"),
                               ("delivery_start", ""), ("delivery_end", ""), ("source_url", ""),
                               ("term_months", "0"), ("quote_status", "unknown")]:
            with self.subTest(field=field):
                self.assertEqual(qualification(quote(**{field: missing}), AS_OF)[0], "reference_only")

    def test_no_invented_zero_prepayment(self):
        for notes in ["", "Monthly billing", "0% utilization", "Prepayment unknown; monthly billing"]:
            with self.subTest(notes=notes):
                report = build_quote_report([quote(prepay_known="", notes=notes)], AS_OF)
                row = report["observations"][0]
                self.assertEqual(row["status"], "reference_only")
                self.assertIsNone(row["prepay_pct"])
                self.assertIn("Prepayment unknown", row["reasons"])
        for notes in ["No prepayment", "0% upfront", "prepay: 0%"]:
            self.assertEqual(qualification(quote(prepay_known="", notes=notes), AS_OF)[0], "qualified_asking_price")
        row = build_quote_report([quote(prepay_known="0", notes="No prepayment")], AS_OF)["observations"][0]
        self.assertIsNone(row["prepay_pct"])

    def test_malformed_numbers_dates_and_links_fail_validation_without_crashing(self):
        cases = [{"term_months": "inf"}, {"term_months": "nan"}, {"term_months": "-0.5"},
                 {"prepay_pct": "inf"}, {"prepay_pct": "-0.5"}, {"prepay_pct": "100.5"},
                 {"prepay_pct": "", "prepay_known": "1"}, {"gpu_count": "1.5"}, {"gpu_count": "nan"},
                 {"gpu_count": True}, {"source_url": "https:///missing-host"},
                 {"source_url": "javascript:alert(1)"}, {"source_url": "https://user:pass@example.invalid"},
                 {"expires_on": "2026-09-01"}, {"delivery_end": "2026-01-01"},
                 {"source_observed_at": "yesterday"}, {"message_date": "wrong"},
                 {"source_observed_at": "2026-09-12"}, {"currency": "EUR"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertTrue(validate_row(quote(**changes)))
                self.assertEqual(qualification(quote(**changes), AS_OF)[0], "invalid")

    def test_historical_as_of_filters_before_deduplication(self):
        earlier = quote(prepay_known="", prepay_pct="0", notes="Payment terms not supplied")
        later = quote(message_ts="1789732800.000001", message_date="2026-09-19", prepay_pct="25", notes="25% upfront")
        report = build_quote_report([earlier, later], AS_OF)
        self.assertEqual(len(report["observations"]), 1)
        self.assertEqual(report["observations"][0]["reported_at"], "2026-09-11")
        self.assertIsNone(report["observations"][0]["prepay_pct"])

    def test_render_preserves_scope_tax_terms_and_clickable_evidence(self):
        report = build_quote_report([quote(gpu_count_relation="minimum", prepay_pct="25", quote_id="Q<&1")], AS_OF)
        text = render_quote_report(report)
        for value in ["256+ GPUs", "prepay 25%", "H100 SXM", "USD, excludes tax", "Source evidence", "Q&lt;&amp;1"]:
            self.assertIn(value, text)
        self.assertIn('href="https://example.invalid/evidence/quote-1"', text)
        invalid = build_quote_report([quote(source_url="javascript:alert(1)")], AS_OF)
        self.assertNotIn('href="javascript:', render_quote_report(invalid))

    def test_priority_coverage_keeps_asking_signed_and_review_counts_separate(self):
        from coverage_report import build_price_coverage, build_priority_coverage
        rows = [quote(), quote(quote_status="signed_deal", expires_on="", quote_id="signed"),
                quote(quote_id="expired", expires_on="2026-09-17"),
                quote(quote_id="undated", source_observed_at="")]
        quotes = build_quote_report(rows, AS_OF)
        report = build_priority_coverage(build_price_coverage([], AS_OF), quotes)
        provider = next(p for p in report["providers"] if p["provider"] == "coreweave")
        self.assertEqual(provider["qualified_asking_prices"], 1)
        self.assertEqual(provider["qualified_signed_deals"], 1)
        self.assertEqual(provider["quotes_requiring_review"], 2)
        self.assertEqual(provider["fresh_price_cells"], 0)
        self.assertIsNone(provider["direct_instance_availability_cells"])


class QuoteDeduplication(unittest.TestCase):
    def test_explicit_zero_prepay_does_not_merge_with_25_percent_offer(self):
        zero = quote(notes="", prepay_known="1", prepay_pct="0")
        upfront = quote(notes="", prepay_known="1", prepay_pct="25")
        kept, removed, review = classify([zero, upfront])
        self.assertEqual(len(kept), 2)
        self.assertEqual(removed, [])
        self.assertEqual(len(review), 1)

    def test_different_region_quantity_delivery_term_and_status_are_not_repeats(self):
        variants = [quote(), quote(region="eu-west-1"), quote(gpu_count="512"),
                    quote(delivery_start="2026-11-01"), quote(delivery_end="2027-10-31"),
                    quote(term_months="13"), quote(quote_status="signed_deal"),
                    quote(gpu_count_relation="minimum"), quote(gpu_variant="H100 PCIe")]
        kept, removed, _ = classify(variants)
        self.assertEqual(len(kept), len(variants))
        self.assertEqual(removed, [])

    def test_true_same_offer_repeat_is_removed_without_mutating_inputs(self):
        first = quote()
        repeat = quote(message_ts="1789214400.000001", message_date="2026-09-12")
        original = first.copy(), repeat.copy()
        kept, removed, _ = classify([repeat, first])
        self.assertEqual(kept, [first])
        self.assertEqual(len(removed), 1)
        self.assertEqual((first, repeat), original)


class IntelInboxMigration(unittest.TestCase):
    def test_merge_does_not_turn_monthly_billing_into_zero_prepayment(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "intel.csv"
            content = csv_text([quote(prepay_known="", notes="Monthly billing")])
            with patch.object(inbox, "INTEL_CSV", path), \
                 patch.object(inbox, "fetch_inbox_storage", return_value='<![CDATA[' + content + ']]>'):
                inbox.main()
            row = load_rows(path)[0]
            self.assertEqual(row["prepay_known"], "0")
            self.assertEqual(qualification(row, AS_OF)[0], "reference_only")

    def test_legacy_nine_columns_and_explicit_extended_header_parse(self):
        original = quote()
        old_columns = BASE_COLUMNS[:9]
        for header in (False, True):
            parsed = inbox.parse_rows(csv_text([original], old_columns, header).splitlines())
            self.assertEqual(parsed, [{key: original[key] for key in old_columns}])
        parsed = inbox.parse_rows(csv_text([original]).splitlines())
        self.assertEqual(parsed, [original])
        self.assertEqual(validate_row(parsed[0]), [])

    def test_multiline_notes_survive_cdata_and_csv_roundtrip(self):
        row = quote(notes='Observed <note>, "quoted"\nsecond line without commas')
        content = csv_text([row])
        lines = inbox.extract_csv_lines('<ac:plain-text-body><![CDATA[' + content + ']]></ac:plain-text-body>')
        parsed = inbox.parse_rows(lines)
        self.assertEqual(parsed, [row])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "intel.csv"
            inbox.append_preserving_schema(path, parsed)
            self.assertEqual(load_rows(path), [row])

    def test_nested_pre_code_is_not_extracted_twice(self):
        content = csv_text([quote()])
        lines = inbox.extract_csv_lines('<pre><code>' + content + '</code></pre>')
        self.assertEqual(len(inbox.parse_rows(lines)), 1)

    def test_old_csv_header_upgrades_atomically_and_preserves_custom_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "intel.csv"
            legacy = {key: quote()[key] for key in BASE_COLUMNS[:9]}
            legacy["reviewer_note"] = "retained"
            path.write_text(csv_text([legacy], BASE_COLUMNS[:9] + ["reviewer_note"]))
            addition = quote(message_ts="another-message")
            inbox.append_preserving_schema(path, [addition])
            with path.open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames[:9], BASE_COLUMNS[:9])
                self.assertTrue(set(INTEL_COLUMNS).issubset(reader.fieldnames))
                rows = list(reader)
            self.assertEqual(rows[0]["reviewer_note"], "retained")
            self.assertEqual(rows[0]["quote_status"], "")
            self.assertEqual({key: rows[1][key] for key in INTEL_COLUMNS}, addition)
            self.assertEqual(len(list(Path(temp).iterdir())), 1)

    def test_failed_atomic_replace_leaves_original_bytes_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "intel.csv"
            original = csv_text([quote()], BASE_COLUMNS[:9]).encode()
            path.write_bytes(original)
            with patch.object(inbox.os, "replace", side_effect=OSError("test failure")):
                with self.assertRaises(OSError):
                    inbox.append_preserving_schema(path, [quote()])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(temp).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
