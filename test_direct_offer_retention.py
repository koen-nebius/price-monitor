"""Regressions from provider response structure inspected 18 September 2026.

Reduced fixtures preserve responsive CoreWeave rows, Hyperstack's published
per-GPU/maximum-resource columns, and Verda's instance-type JSON contract.
All tests are offline; no provider or credential is contacted.
"""
import copy
import unittest

from fetchers import coreweave, hyperstack, verda
from comparability import enrich_comparability, is_public_benchmark_eligible

NOW = "2026-09-18T15:00:00+00:00"


def cw_row(sku, title, count=8, ram=2048, od="49.24", spot="19.71", cpu=128, storage="61.44"):
    # The actual page repeats the heading for desktop/mobile within ONE row.
    return f'''<div role="listitem" class="table-row-v2 w-dyn-item"><div class="table-grid">
      <h3 data-product="{sku}">{title}</h3><div>{ram}</div>
      <div><h3 data-product="{sku}">{title}</h3>
      <span>On-Demand Price: {"$"+od if od else ""} / Hour</span>
      <span>Spot Price: {"$"+spot if spot else "N/A"} / Hour</span>
      <span>Inference Single GPU Price: $0.01 / Hour</span></div>
      <div><div>{count}</div><div>GPU Count</div></div>
      <div><div>{cpu}</div><div>vCPUs</div></div>
      <div><div>{storage}</div><div>Local Storage (TB)</div></div>
      <div><div>{ram}</div><div>System RAM</div></div></div></div>'''


def verda_item(count=1, sku="1H100.30V", **changes):
    item = {"id": sku, "name": "H100 SXM5 80GB", "instance_type": sku,
            "gpu": {"number_of_gpus": count}, "cpu": {"number_of_cores": 30*count},
            "memory": {"size_in_gigabytes": 120*count},
            "storage": {"description": "dynamic"}, "currency": "usd",
            "price_per_hour": str(3*count), "spot_price": str(1.5*count)}
    item.update(changes)
    return item


class CoreWeaveRegionalOffers(unittest.TestCase):
    def test_real_regions_and_responsive_duplicates_do_not_collapse_distinct_prices(self):
        html = '<h5><strong>REGION: NORTH AMERICA</strong></h5>' + cw_row("hgx-h100", "NVIDIA HGX H100")
        html += '<h5>REGION: EUROPE</h5>' + cw_row("hgx-h100", "NVIDIA HGX H100", spot="19.51")
        rows = coreweave._parse_html(html, NOW)
        self.assertEqual(len(rows), 4)
        self.assertEqual({r.region for r in rows}, {"NORTH AMERICA", "EUROPE"})
        spots = {r.region: r.price_per_gpu_hour_usd for r in rows if r.consumption_type == "spot"}
        self.assertEqual(spots, {"NORTH AMERICA": 19.71/8, "EUROPE": 19.51/8})
        self.assertEqual(len({r.offer_id for r in rows}), 4)
        self.assertTrue(all(r.storage_gb == 61.44*1024 and r.vcpu == 128 for r in rows))

    def test_rtx_host_variants_are_identified_by_title_and_keep_matching_resources(self):
        sku = "nvidia-rtx-pro-6000-blackwell-server-edition"
        # Equal spot prices deliberately prove price cannot determine the host.
        html = '<h5>REGION: NORTH AMERICA</h5>'
        html += cw_row(sku, "NVIDIA RTX PRO 6000 Blackwell Server Edition (High Memory)",
                       ram=1024, od="20", spot="9.56", storage="7.68")
        html += cw_row(sku+"-standard-memory", "NVIDIA RTX PRO 6000 Blackwell Server Edition (Standard Memory)",
                       ram=512, od="", spot="9.56", storage="7.68")
        rows = coreweave._parse_html(html, NOW)
        self.assertEqual(len(rows), 3)
        self.assertEqual({(r.offer_variant, r.ram_gb, r.consumption_type) for r in rows},
                         {("High Memory", 1024, "on_demand"), ("High Memory", 1024, "spot"),
                          ("Standard Memory", 512, "spot")})
        self.assertTrue(all(r.gpu_model == "RTX6000" and r.storage_gb == 7.68*1024 for r in rows))
        self.assertTrue(all(r.comparison_eligible for r in rows))

    def test_no_inferred_region_or_rtx_variant_or_nvl72_count(self):
        rows = coreweave._parse_html(cw_row("nvidia-gb200-nvl72", "NVIDIA GB200 NVL72",
                                           count="4^1", ram=960, od="42", spot=""), NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].gpu_count, rows[0].region, rows[0].price_per_gpu_hour_usd), (4, "unknown", 10.5))
        unknown = coreweave._parse_html(cw_row("nvidia-rtx-pro-6000-blackwell-server-edition",
                                               "NVIDIA RTX PRO 6000 Blackwell Server Edition", od="20"), NOW)
        self.assertTrue(all(not r.comparison_eligible and "unspecified" in r.correction_reason for r in unknown))
        self.assertEqual(coreweave._parse_html(cw_row("hgx-h100", "NVIDIA HGX H100", count="1.5"), NOW), [])

    def test_identical_rows_dedup_but_different_configurations_and_reprices_survive(self):
        first = cw_row("hgx-h100", "NVIDIA HGX H100", spot="")
        other = cw_row("hgx-h100", "NVIDIA HGX H100", ram=4096, spot="")
        rows = coreweave._parse_html(first + first + other, NOW)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].offer_id, rows[1].offer_id)
        changed = coreweave._parse_html(first.replace("49.24", "50.00"), "2026-09-19T00:00:00Z")[0]
        self.assertEqual(rows[0].offer_id, changed.offer_id)


class HyperstackRateCards(unittest.TestCase):
    FIXTURE = '''On-Demand GPU Pricing
      GPU Model VRAM (GB) Max pCPUs per GPU Max RAM (GB) per GPU Pricing Per Hour
      NVIDIA H100 SXM 80 24 240 $3.20
      NVIDIA H100 NVLink 80 31 180 $2.60
      NVIDIA H100 80 28 180 $2.50
      NVIDIA RTX Pro 6000 SE 96 31 180 $1.85
      Reservation Pricing Starting from
      NVIDIA H100 SXM $2.72 Reserve here NVIDIA H100 $1.75 Reserve here
      Spot VM Pricing NVIDIA H100 PCIe $2.00 NVIDIA RTX Pro 6000 SE $1.48
      On-Demand CPU Pricing Unrelated NVIDIA H100 SXM $0.99
    '''

    def test_all_printed_variants_rental_types_and_max_allowances_survive(self):
        rows = hyperstack._parse_pricing(self.FIXTURE, NOW)
        self.assertEqual(len(rows), 8)
        h100_od = [r for r in rows if r.gpu_model == "H100" and r.consumption_type == "on_demand"]
        self.assertEqual({r.price_per_gpu_hour_usd for r in h100_od}, {3.2, 2.6, 2.5})
        self.assertEqual({r.instance_type for r in h100_od},
                         {"hyperstack-h100-sxm", "hyperstack-h100-nvlink", "hyperstack-h100"})
        self.assertTrue(all(r.gpu_count == r.node_gpus == 1 and r.region == "unknown" for r in rows))
        plain = next(r for r in h100_od if r.gpu_variant == "H100")
        self.assertEqual((plain.vcpu, plain.ram_gb), (28, 180))
        self.assertEqual(plain.price_basis, "per_gpu_rate_card_max_resources")
        enriched = enrich_comparability([plain])[0]
        self.assertEqual(enriched.form_factor, "unknown")
        self.assertFalse(any(r.price_per_gpu_hour_usd == .99 for r in rows))

    def test_unknown_reservation_terms_are_inspectable_references(self):
        rows = hyperstack._parse_pricing(self.FIXTURE, NOW)
        reserved = [r for r in rows if r.consumption_type.startswith("reserved")]
        self.assertEqual(len(reserved), 2)
        self.assertTrue(all(r.consumption_type == "reserved_unknown" and r.commitment_months is None for r in reserved))
        self.assertTrue(all(not is_public_benchmark_eligible(r) and "term" in r.correction_reason for r in reserved))
        self.assertEqual(len(hyperstack._parse_pricing(self.FIXTURE + self.FIXTURE, NOW)), 8)


class VerdaInstanceOffers(unittest.TestCase):
    def test_all_sizes_preserved_without_inferred_region_host_size_or_linear_prices(self):
        rows = verda.parse([verda_item(), verda_item(8, "8H100.240V", price_per_hour="20.8")], NOW)
        self.assertEqual(len(rows), 4)
        self.assertEqual({r.gpu_count for r in rows}, {1, 8})
        self.assertTrue(all(r.node_gpus == r.gpu_count and r.region == "unknown" for r in rows))
        od = {r.gpu_count: r.price_per_gpu_hour_usd for r in rows if r.consumption_type == "on_demand"}
        self.assertEqual(od, {1: 3, 8: 2.6})
        self.assertTrue(all(r.storage_gb is None for r in rows))

    def test_configuration_and_confidential_variant_are_retained_separately(self):
        normal = verda_item(name="RTX PRO 6000 96GB")
        cc = verda_item(name="RTX PRO 6000 CC 96GB", instance_type="1RTXCC")
        larger = copy.deepcopy(normal)
        larger["memory"]["size_in_gigabytes"] = 240
        larger["storage"]["size_in_gigabytes"] = 200
        rows = verda.parse([normal, normal, cc, larger], NOW)
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({r.offer_id for r in rows}), 6)
        self.assertEqual(sum(not r.comparison_eligible for r in rows), 2)
        self.assertEqual(sum(r.storage_gb == 200 for r in rows), 2)

    def test_malformed_numeric_fields_currency_and_independent_rental_prices(self):
        for count in [True, 0, -1, 1.5, "NaN", "Infinity", None]:
            self.assertEqual(verda.parse([verda_item(gpu={"number_of_gpus": count})], NOW), [])
        for currency in ["eur", "", None, 1]:
            self.assertEqual(verda.parse([verda_item(currency=currency)], NOW), [])
        for value in [True, "NaN", "Infinity", "-3", "oops"]:
            rows = verda.parse([verda_item(price_per_hour=value)], NOW)
            self.assertEqual([r.consumption_type for r in rows], ["spot"])

    def test_direct_reference_qualifications_survive_publication_copies(self):
        from price_corrections import correct_snapshot
        from report_freshness import publication_records
        rows = verda.parse([verda_item(name="RTX PRO 6000 CC 96GB")], NOW)
        rows += hyperstack._parse_pricing(HyperstackRateCards.FIXTURE, NOW)
        original = [r.to_dict() for r in rows]
        qualified = [r for r in rows if not r.comparison_eligible]
        self.assertEqual(len(qualified), 4)
        copied = correct_snapshot(qualified, include_excluded=True)
        self.assertEqual([r.to_dict() for r in copied], [r.to_dict() for r in qualified])
        self.assertTrue(all(a is not b for a, b in zip(copied, qualified)))
        eligible, notices = publication_records(rows, NOW)
        self.assertEqual(len(eligible), len(rows)-4)
        self.assertTrue(all(r.comparison_eligible for r in eligible))
        self.assertTrue(any("Confidential-compute" in message for message in notices))
        self.assertTrue(any("no published commitment term" in message for message in notices))
        self.assertEqual([r.to_dict() for r in rows], original)


if __name__ == "__main__":
    unittest.main()
