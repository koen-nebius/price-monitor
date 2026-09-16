"""Unit checks for forward_curve.py (stdlib unittest; run: python3 -m unittest test_forward_curve)."""
import csv
import json
import math
import tempfile
import unittest
from datetime import date
from pathlib import Path

import forward_curve as fc


class Helpers(unittest.TestCase):
    def test_bucket_months(self):
        self.assertIsNone(fc.bucket_months(0))
        self.assertIsNone(fc.bucket_months(""))
        self.assertEqual(fc.bucket_months(1), 3)
        self.assertEqual(fc.bucket_months(3), 3)
        self.assertEqual(fc.bucket_months(6), 6)
        self.assertEqual(fc.bucket_months(9), 12)
        self.assertEqual(fc.bucket_months(12), 12)
        self.assertEqual(fc.bucket_months(18), 18)
        self.assertEqual(fc.bucket_months(24), 24)
        self.assertEqual(fc.bucket_months(36), 36)
        self.assertEqual(fc.bucket_months(48), 60)
        self.assertEqual(fc.bucket_months(60), 60)

    def test_prepay_normalise_raises_price_for_prepaid_quotes(self):
        self.assertEqual(fc.prepay_normalise(5.0, 0, 12), 5.0)
        # Finance convention: 12m/100% = -3.4 %, 24m/100% = -6.8 % (grid says -6.97 %)
        self.assertAlmostEqual(fc.prepay_discount(12, 100), 0.0345)
        self.assertAlmostEqual(fc.prepay_discount(24, 100), 0.069)
        self.assertAlmostEqual(fc.prepay_discount(12, 50), 0.0345 * 0.75)
        self.assertAlmostEqual(fc.prepay_discount(12, 30), 0.0345 * 0.51)
        self.assertAlmostEqual(fc.prepay_normalise(5.0, 100, 12), 5.0 / (1 - 0.0345))
        self.assertGreater(fc.prepay_normalise(5.0, 30, 12), 5.0)
        self.assertEqual(fc.prepay_normalise(5.0, "bad", 12), 5.0)
        self.assertLessEqual(fc.prepay_discount(60, 100), fc.PREPAY_CAP)
        p0 = fc.prepay_normalise(4.55, 100, 36)
        self.assertAlmostEqual(fc.price_at_prepay(p0, 36, 100), 4.55)

    def test_grid_reference_picks_bucketed_tenor(self):
        grids = {"2026-09-07": {"segments": {"ai_native_above_512": {"B300": {"24": {"100": 6.69, "50": 7.15}, "36": {"100": 5.40}}}}}}
        ref = fc.grid_reference(grids, "B300", 24)
        self.assertEqual(ref["prices"][100], 6.69)
        self.assertEqual(ref["months"], 24)
        self.assertIsNone(fc.grid_reference(grids, "B300", 12))
        self.assertIsNone(fc.grid_reference(grids, "VR", 60))

    def test_weighted_median(self):
        self.assertEqual(fc.weighted_median([1, 2, 3], [1, 1, 1]), 2)
        self.assertEqual(fc.weighted_median([1, 2, 3], [1, 1, 10]), 3)


class IntelQuality(unittest.TestCase):
    def test_prepay_known(self):
        from intel_quality import prepay_known
        self.assertTrue(prepay_known({"prepay_pct": "25", "notes": ""}))
        self.assertTrue(prepay_known({"prepay_pct": "0", "notes": "3yr 0% prepay committed deal"}))
        self.assertTrue(prepay_known({"prepay_pct": "0", "notes": "monthly payment terms large cluster"}))
        self.assertFalse(prepay_known({"prepay_pct": "0", "notes": "3yr offer seen by Cursor; prepay unspecified"}))
        self.assertFalse(prepay_known({"prepay_pct": "0", "notes": "512xB300 3yr US Jan/Feb delivery"}))

    def test_classify_removes_confirmed_repeats_and_keeps_lookalikes_for_review(self):
        from intel_quality import classify, dedupe
        rows = [
            # seed row repeating the retrieved Oracle row: confirmed repeat, removed
            {"message_ts": "seed_20260528_01", "message_date": "2026-05-28", "gpu_model": "B200", "price_per_gpu_hour_usd": "3.68", "term_months": "36", "provider_name": "Oracle", "notes": "3yr 0% prepay committed deal"},
            {"message_ts": "1779927609.062", "message_date": "2026-05-28", "gpu_model": "B200", "price_per_gpu_hour_usd": "3.68", "term_months": "36", "provider_name": "Oracle", "notes": "3yr 0% prepay 512 GPUs"},
            # one Slack message, two providers, same price: two offers (BoostRun 20k US Q4 vs Nscale 15k EU Q1); both kept, flagged
            {"message_ts": "1780592471.289", "message_date": "2026-06-04", "gpu_model": "GB300", "price_per_gpu_hour_usd": "3.80", "term_months": "36", "provider_name": "BoostRun", "notes": "20k GPUs US Q4 3yr"},
            {"message_ts": "1780592471.289", "message_date": "2026-06-04", "gpu_model": "GB300", "price_per_gpu_hour_usd": "3.80", "term_months": "36", "provider_name": "Nscale", "notes": "15k GPUs EU Q1 3yr high $3/hr range"},
            # seed row anonymised the provider: same day, price, term, notes -> confirmed repeat
            {"message_ts": "seed_20260501_03", "message_date": "2026-05-01", "gpu_model": "GB300", "price_per_gpu_hour_usd": "3.50", "term_months": "36", "provider_name": "Undisclosed", "notes": "3yr 100% upfront UAE market"},
            {"message_ts": "1777630000.000", "message_date": "2026-05-01", "gpu_model": "GB300", "price_per_gpu_hour_usd": "3.50", "term_months": "36", "provider_name": "Mistral", "notes": "3yr 100% upfront UAE market"},
            # seed row naming a different provider than the retrieved row: kept, review
            {"message_ts": "seed_20260414_02", "message_date": "2026-04-14", "gpu_model": "B200", "price_per_gpu_hour_usd": "4.25", "term_months": "0", "provider_name": "AWS", "notes": "through reseller 25% down"},
            {"message_ts": "1776160000.000", "message_date": "2026-04-14", "gpu_model": "B200", "price_per_gpu_hour_usd": "4.25", "term_months": "0", "provider_name": "Lyceum", "notes": "AWS nodes German broker 25% down"},
            # same provider, price and term four days apart: confirmed repeat
            {"message_ts": "1787599134.249", "message_date": "2026-08-24", "gpu_model": "B300", "price_per_gpu_hour_usd": "4.90", "term_months": "36", "provider_name": "AWS", "notes": ""},
            {"message_ts": "1787900000.000", "message_date": "2026-08-28", "gpu_model": "B300", "price_per_gpu_hour_usd": "4.90", "term_months": "36", "provider_name": "AWS", "notes": "same quote re-posted"},
            # seed row keyed from the same day's message at the range midpoint, retrieved row at the lower bound: kept, review
            {"message_ts": "seed_20260313_01", "message_date": "2026-03-13", "gpu_model": "B200", "price_per_gpu_hour_usd": "2.85", "term_months": "24", "provider_name": "Undisclosed", "notes": "$2.80-$2.90 range multiyear with prepay"},
            {"message_ts": "1773433467.443", "message_date": "2026-03-13", "gpu_model": "B200", "price_per_gpu_hour_usd": "2.80", "term_months": "24", "provider_name": "Undisclosed", "notes": "multiyear $2.80-$2.90 range with prepay"},
            # same provider, same price and term, but the later row states a different prepayment: two quotes, review
            {"message_ts": "1788000000.000", "message_date": "2026-08-29", "gpu_model": "H200", "price_per_gpu_hour_usd": "3.10", "term_months": "12", "provider_name": "Lambda", "notes": "1yr 25% prepay"},
            {"message_ts": "1788100000.000", "message_date": "2026-08-30", "gpu_model": "H200", "price_per_gpu_hour_usd": "3.10", "term_months": "12", "provider_name": "Lambda", "notes": "1yr 50% prepay", "prepay_pct": "50"},
        ]
        rows[-2]["prepay_pct"] = "25"
        kept, removed, review = classify(rows)
        self.assertEqual({str(d["row"]["message_ts"]) for d in removed}, {"seed_20260528_01", "seed_20260501_03", "1787900000.000"})
        self.assertTrue(all(d.get("duplicate_provider") for d in removed))   # the report names the row it repeats
        self.assertEqual(len(kept), len(rows) - 3)
        self.assertEqual({str(d["row"]["message_ts"]) + ":" + d["row"]["provider_name"] for d in review},
                         {"1780592471.289:Nscale", "seed_20260414_02:AWS", "seed_20260313_01:Undisclosed", "1788100000.000:Lambda"})
        self.assertTrue(any(k["provider_name"] == "Nscale" for k in kept))   # never removed on a shared message alone
        kept2, removed2 = dedupe(rows)
        self.assertEqual((len(kept2), len(removed2)), (len(rows) - 3, 3))


class Build(unittest.TestCase):
    def _write(self, tmp, name, header, rows):
        p = Path(tmp) / name
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return p

    def test_suppression_and_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            intel = self._write(tmp, "intel.csv",
                ["message_ts", "message_date", "gpu_model", "price_per_gpu_hour_usd", "term_months",
                 "prepay_pct", "provider_type", "provider_name", "notes"],
                [["a", "2026-09-01", "B300", "4.0", "36", "0", "neocloud", "X", "3yr 0% prepay"],
                 ["b", "2026-08-15", "B300", "4.2", "36", "25", "neocloud", "Y", ""],
                 ["c", "2026-08-01", "B300", "4.4", "36", "0", "hyperscaler", "Z", "monthly payment terms"],
                 ["d", "2026-08-01", "H100", "2.0", "12", "0", "hyperscaler", "Z", "no prepay"],   # alone -> suppressed
                 ["e", "2026-08-01", "B300", "40.0", "36", "0", "neocloud", "junk", "0% down"],   # outside band
                 ["f", "2026-08-20", "B300", "3.0", "36", "0", "neocloud", "Q", "512 GPUs US delivery"],     # prepay unstated -> counted, not pooled
                 ["g", "2026-08-21", "B300", "3.05", "36", "0", "neocloud", "R", "1k GPUs EU"],
                 ["h", "2026-08-22", "B300", "3.1", "36", "0", "broker", "S", "broker offer, terms tbc"]])
            reserve = self._write(tmp, "reserve_tenor.csv",
                ["generated_date", "gpu", "tenor_months", "close_month", "deals", "lines", "gpus",
                 "price_lo", "price_med", "price_hi"],
                [["2026-09-15", "B300", "36", "2026-08-01", "3", "3", "512", "4.5", "4.8", "5.0"],
                 ["2026-09-15", "B300", "12", "2026-07-01", "1", "1", "64", "5.5", "5.5", "5.5"],     # a lone deal: counted, price withheld
                 ["2026-09-15", "B300", "12", "2026-08-01", "1", "1", "64", "5.7", "5.7", "5.7"]])
            history = self._write(tmp, "history.csv",
                ["snapshot_date", "provider", "gpu_model", "consumption_type", "region", "instance_type",
                 "gpu_count", "price_per_gpu_hour_usd", "price_per_hour_usd", "data_source", "source_type",
                 "confidence", "interconnect", "form_factor"],
                [["2026-09-14", "nebius", "B300", "committed_3yr", "", "", "8", "4.55", "", "", "", "", "", ""],
                 ["2026-09-14", "aws", "B300", "reserved_3yr", "", "", "8", "9.0", "", "", "", "", "", ""]])
            res = fc.build(date(2026, 9, 15), intel=intel, reserve=reserve, history=history, crm_asks=Path(tmp) / "none.csv", index=Path(tmp) / "none.csv", snapshot=Path(tmp) / "none.json",
                           contracts=Path(tmp) / "none.csv", grid=Path(tmp) / "none.json")
        b300_36 = next(e for e in res["marks"] if e["tier"] == "B300" and e["tenor_months"] == 36)
        self.assertTrue(b300_36["has_mark"])
        self.assertEqual(b300_36["n_obs"], 6)            # 3 stated offers + 3 signed deals; junk price excluded by the sanity band; unstated offers not pooled
        self.assertEqual(b300_36["n_bid"], 3)
        self.assertEqual(b300_36["n_bid_unstated"], 3)
        self.assertEqual(b300_36["n_all"], 9)
        self.assertEqual(b300_36["n_ask"], 1)
        self.assertEqual(b300_36["ask_deals"], 3)
        self.assertTrue(4.0 <= b300_36["mark"] <= 4.8)
        self.assertEqual(b300_36["confidence"], "good")   # 6 observations, 4 providers, all recent
        self.assertIsNone(b300_36["confidence_reason"])
        # two single-deal months in one quarter merge to a 2-deal aggregate: still under 3 -> counted, price withheld, never pooled
        b300_12 = next(e for e in res["marks"] if e["tier"] == "B300" and e["tenor_months"] == 12)
        self.assertEqual((b300_12["n_ask"], b300_12["ask_deals"], b300_12["n_ask_withheld"], b300_12["ask_deals_withheld"]), (0, 0, 1, 2))
        self.assertFalse(b300_12["has_mark"])
        withheld_rows = [o for o in res["observations"] if o["side"] == "ask" and o.get("withheld")]
        self.assertEqual(len(withheld_rows), 1)
        self.assertIsNone(withheld_rows[0]["price"])
        self.assertIsNone(withheld_rows[0]["p0"])
        self.assertEqual(withheld_rows[0]["deals"], 2)
        self.assertEqual(b300_36["mark"], b300_36["mark_known"])
        self.assertLessEqual(b300_36["mark_all"], b300_36["mark"])          # the ~$3 unstated offers can only pull a pooled figure down...
        self.assertLess(b300_36["bid_median_all"], b300_36["bid_median"])   # ...and do so for the offers-only median
        self.assertAlmostEqual(b300_36["bid_median_unstated"], 3.05, places=2)
        unstated_obs = [o for o in res["observations"] if o["side"] == "bid" and not o["known"]]
        self.assertEqual(len(unstated_obs), 3)
        self.assertEqual(b300_36["list_nebius"], 4.55)
        self.assertEqual(b300_36["list_hyperscaler_min"], 9.0)
        self.assertGreater(b300_36["spread_pct"], 0)
        h100_12 = next(e for e in res["marks"] if e["tier"] == "H100" and e["tenor_months"] == 12)
        self.assertFalse(h100_12["has_mark"])
        self.assertIsNone(h100_12["mark"])
        self.assertIn("insufficient_data", h100_12["reason"])
        # render paths do not blow up and the body converts to well-formed storage XML
        body = fc.render_confluence_body(res, with_images=True)
        self.assertIn("ri:attachment", body)
        from confluence_storage import to_storage, validate_xml
        self.assertIsNone(validate_xml(to_storage(body)))
        svg = fc.render_svg(res)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("B300", fc.render_view_html(res, svg))

    def test_aggregate_asks_merges_to_three_deals_and_withholds_the_rest(self):
        def ask(tier, tenor, bucket, month, deals, price):
            d = date.fromisoformat(month)
            return {"side": "ask", "tier": tier, "tenor": tenor, "months": float(tenor), "date": d, "quarter": fc.quarter_of(d),
                    "price_raw": price, "p0": price, "p_adj": price, "logp0": math.log(price), "prepay_pct": 0.0 if bucket == "postpaid" else 100.0,
                    "prepay_bucket": bucket, "known": True, "weight": float(min(deals, 3)), "deals": deals, "age_days": 30, "provider": "Nebius"}
        rows = [ask("H100", 12, "postpaid", "2026-07-15", 1, 2.4), ask("H100", 12, "postpaid", "2026-08-15", 1, 2.5), ask("H100", 12, "postpaid", "2026-09-15", 1, 2.6),   # -> one 3-deal aggregate
                ask("H100", 36, "postpaid", "2026-08-15", 1, 2.0), ask("H100", 36, "upfront", "2026-08-15", 2, 1.9),                                              # -> merged across buckets: 3 deals, 'mixed'
                ask("B200", 24, "postpaid", "2026-08-15", 2, 4.5)]                                                                                                # -> withheld
        out = fc.aggregate_asks(rows)
        h12 = [o for o in out if o["tier"] == "H100" and o["tenor"] == 12]
        self.assertEqual(len(h12), 1)
        self.assertEqual((h12[0]["deals"], h12[0]["weight"], h12[0].get("withheld", False)), (3, 3.0, False))
        self.assertEqual(h12[0]["p0"], 2.5)
        self.assertEqual(h12[0]["date_from"], date(2026, 7, 15))
        h36 = [o for o in out if o["tier"] == "H100" and o["tenor"] == 36]
        self.assertEqual(len(h36), 1)
        self.assertEqual((h36[0]["deals"], h36[0]["prepay_bucket"], h36[0]["prepay_pct"]), (3, "mixed", None))
        b24 = [o for o in out if o["tier"] == "B200"]
        self.assertEqual(len(b24), 1)
        self.assertTrue(b24[0]["withheld"])
        self.assertEqual(b24[0]["weight"], 0.0)

    def test_quarter_effects_are_fitted_without_unstated_offers(self):
        import math as _m
        header = ["message_ts", "message_date", "gpu_model", "price_per_gpu_hour_usd", "term_months", "prepay_pct", "provider_type", "provider_name", "notes"]
        def rows(unstated_q2_price):
            return [["a", "2026-09-01", "B300", "4.0", "36", "0", "neocloud", "X", "3yr 0% prepay"],
                    ["b", "2026-08-15", "B300", "4.2", "36", "25", "neocloud", "Y", ""],
                    ["c", "2026-08-01", "B300", "4.4", "36", "0", "hyperscaler", "Z", "monthly payment terms"],
                    ["d", "2026-05-20", "B300", "3.6", "36", "0", "neocloud", "Q2", "2yr 0% prepay"],          # stated, 2026Q2
                    ["e", "2026-05-21", "B200", "3.0", "36", "0", "neocloud", "Q2b", "no prepay"],
                    ["f", "2026-08-20", "B200", "3.5", "36", "0", "neocloud", "R", "0% down"],
                    ["u", "2026-05-22", "B300", str(unstated_q2_price), "36", "0", "neocloud", "U", "512 GPUs US delivery"]]  # unstated, 2026Q2
        results = []
        for price in (1.5, 6.0):
            with tempfile.TemporaryDirectory() as tmp:
                intel = self._write(tmp, "intel.csv", header, rows(price))
                res = fc.build(date(2026, 9, 15), intel=intel, reserve=Path(tmp) / "none.csv", history=Path(tmp) / "none.csv", crm_asks=Path(tmp) / "none.csv", index=Path(tmp) / "none.csv", snapshot=Path(tmp) / "none.json",
                               contracts=Path(tmp) / "none.csv", grid=Path(tmp) / "none.json")
            results.append(res)
        self.assertEqual(results[0]["quarter_effects_log"], results[1]["quarter_effects_log"])
        pub = lambda r: [(e["tier"], e["tenor_months"], e["mark"], e["confidence"]) for e in r["marks"] if e["has_mark"]]
        self.assertEqual(pub(results[0]), pub(results[1]))
        self.assertTrue(pub(results[0]))
        self.assertTrue(_m.isfinite(results[0]["quarter_effects_log"]["2026Q2"]))

    def test_quarter_effects_shift_old_quotes_up_in_a_rising_market(self):
        obs = []
        for q, (m, p) in {"2026Q1": (2, 3.0), "2026Q3": (8, 4.5)}.items():
            for tier in ("B300", "B200"):
                obs.append({"tier": tier, "tenor": 36, "quarter": q, "weight": 1.0,
                            "logp0": __import__("math").log(p)})
        eff = fc.quarter_effects(obs, date(2026, 9, 15))
        self.assertAlmostEqual(eff["2026Q3"], 0.0)
        self.assertLess(eff["2026Q1"], 0.0)


class OtherEvidenceClasses(unittest.TestCase):
    """Lost/open CRM asks, index rows and short-term market prices are references: labelled, thresholded, never pooled."""

    def test_crm_asks_aggregate_to_three_deals_and_withhold_the_rest(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "crm_asks.csv"
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["generated_date", "gpu", "tenor_months", "stage_class", "close_month", "prepay_bucket", "deals", "lines", "gpus", "price_lo", "price_med", "price_hi"])
                w.writerow(["2026-09-16", "B300", "36", "lost", "2026-07-01", "postpaid", "2", "2", "512", "4.5", "4.7", "4.9"])
                w.writerow(["2026-09-16", "B300", "36", "lost", "2026-08-01", "upfront", "2", "2", "256", "4.0", "4.2", "4.4"])
                w.writerow(["2026-09-16", "B300", "36", "proposal", "2026-09-01", "postpaid", "2", "2", "128", "5.4", "5.5", "5.6"])
                w.writerow(["2026-09-16", "B300", "36", "lost", "2025-01-01", "postpaid", "9", "9", "999", "1.0", "1.0", "1.0"])   # outside the window
            res = fc.load_crm_asks(p, as_of=date(2026, 9, 16))
        cell = res["cells"]["B300"][36]
        self.assertEqual(cell["lost"]["deals"], 4)
        self.assertFalse(cell["lost"]["withheld"])
        self.assertGreater(cell["lost"]["p0"], 4.2)          # the upfront month is lifted to the 0% basis
        self.assertLess(cell["lost"]["p0"], 4.9)
        self.assertTrue(cell["proposal"]["withheld"])
        self.assertNotIn("p0", cell["proposal"])
        self.assertEqual(res["withheld_cells"], 1)
        self.assertEqual(res["deals"], {"lost": 4, "proposal": 2})
        self.assertEqual(res["generated"], "2026-09-16")

    def test_index_rows_are_rebased_and_kept_out_of_observations(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "index_quotes.csv"
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["source", "as_of", "gpu_model", "tenor_months", "prepay_pct", "price_per_gpu_hour_usd", "stat", "notes"])
                w.writerow(["SemiAnalysis GPU Pricing Index (draft)", "2026-09-15", "GB300", "36", "25", "5.60", "median", ""])
                w.writerow(["SemiAnalysis GPU Pricing Index (draft)", "2026-09-15", "GB300", "48", "25", "5.50", "median", ""])
            idx = fc.load_index(p)
        self.assertEqual([e["tenor"] for e in idx["GB300"]], [36, 60])   # 48 months buckets to the 60-month column
        self.assertGreater(idx["GB300"][0]["p0"], 5.60)
        self.assertEqual(idx["GB300"][0]["price"], 5.60)
        self.assertNotIn("weight", idx["GB300"][0])

    def test_short_term_clearing_maps_to_payg_and_small_hosts_are_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            h, s = Path(td) / "history.csv", Path(td) / "latest.json"
            with open(h, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["snapshot_date", "provider", "gpu_model", "consumption_type", "region", "instance_type", "gpu_count", "price_per_gpu_hour_usd"])
                w.writerow(["2026-09-15", "sfcompute", "H100", "spot", "us", "node", "8", "1.95"])
                w.writerow(["2026-09-16", "sfcompute", "H100", "spot", "us", "node", "8", "1.90"])
                w.writerow(["2026-09-16", "aws", "H100", "spot", "us", "p5", "8", "3.00"])            # hyperscaler spot is not an exchange price
            s.write_text(json.dumps([{"provider": "vast", "gpu_model": "H100", "consumption_type": "reserved_short", "gpu_count": 2,
                                      "price_per_gpu_hour_usd": 1.05, "region": "Montana, US", "fetched_at": "2026-09-16T07:00:00+00:00"}]))
            st = fc.load_short_term(h, s, as_of=date(2026, 9, 16))
        kinds = {e["kind"]: e for e in st["H100"]}
        self.assertEqual(set(kinds), {"clearing", "marketplace"})
        self.assertEqual((kinds["clearing"]["price"], kinds["clearing"]["tenor"], kinds["clearing"]["stale"]), (1.9, 0, False))
        self.assertEqual((kinds["marketplace"]["tenor"], kinds["marketplace"]["cluster_class"]), (3, False))


if __name__ == "__main__":
    unittest.main()
