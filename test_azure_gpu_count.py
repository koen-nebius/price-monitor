"""Offline Azure VM-to-GPU normalization regression tests.

GPU quantities follow Microsoft's NC RTX PRO 6000 BSE v6 accelerator tables.
Prices below are fixtures, not claims about current regional Azure prices.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -B test_azure_gpu_count.py
"""
import copy
import json
import unittest
from unittest.mock import patch

from config import GPU_MAP

ORIGINAL_AZURE_CONFIG = copy.deepcopy(GPU_MAP['azure'])
from fetchers import azure
from schema import PriceRecord
from comparability import enrich_comparability
from diff import _best_price, _is_cluster_peer

NOW = '2026-09-06T00:00:00+00:00'


def meter(instance, price, **changes):
    item = {
        'armSkuName': instance, 'armRegionName': 'eastus',
        'type': 'Consumption', 'skuName': instance.removeprefix('Standard_'),
        'productName': 'Virtual Machines NC RTX PRO 6000 BSE v6 Series',
        'retailPrice': price, 'unitOfMeasure': '1 Hour',
    }
    item.update(changes)
    return item


def records(instance, gpu_model, spec, price=1.24, items=None):
    response = json.dumps({'Items': items if items is not None else [meter(instance, price)]})
    with patch.object(azure, 'http_get', return_value=response):
        return azure._fetch_instance(instance, gpu_model, spec, ['eastus'], NOW)


class AzureGpuQuantityTests(unittest.TestCase):
    def test_documented_rtx_sizes_override_wrong_whole_gpu_config(self):
        # Quarter, half, whole and two-GPU VMs; both documented memory variants.
        cases = [
            ('NC36ds', 1, .25, 1.24, 4.96),
            ('NC72ds', 2, .5, 2.68, 5.36),
            ('NC144ds', 4, 1, 5.50, 5.50),
            ('NC288ds', 8, 2, 11.00, 5.50),
            ('NC24lds', 1, .25, 1.24, 4.96),
            ('NC36lds', 1, .25, 1.24, 4.96),
            ('NC72lds', 2, .5, 2.68, 5.36),
            ('NC144lds', 4, 1, 5.50, 5.50),
            ('NC288lds', 8, 2, 11.00, 5.50),
        ]
        for size, stale_count, quantity, vm_price, gpu_price in cases:
            with self.subTest(size=size):
                instance = f'Standard_{size}_xl_RTXPRO6000BSE_v6'
                spec = {'gpu_count': stale_count, 'vcpu': 36, 'ram_gb': 132}
                result = records(instance, 'RTX6000', spec, vm_price)
                self.assertEqual(len(result), 1)
                r = result[0]
                self.assertEqual(r.instance_type, instance)
                self.assertEqual(r.gpu_count, quantity)
                self.assertEqual(r.price_per_hour_usd, vm_price)
                self.assertAlmostEqual(r.price_per_gpu_hour_usd, gpu_price)
                self.assertAlmostEqual(r.price_per_gpu_hour_usd * r.gpu_count, vm_price)
                self.assertEqual(spec['gpu_count'], stale_count)

    def test_registry_uses_same_corrected_quantity_without_mutating_config(self):
        self.assertEqual(GPU_MAP['azure'], ORIGINAL_AZURE_CONFIG)
        expected = {'NC36ds': .25, 'NC36lds': .25, 'NC72ds': .5, 'NC144ds': 1, 'NC288ds': 2}
        for size, quantity in expected.items():
            key = f'Standard_{size}_xl_RTXPRO6000BSE_v6'
            self.assertEqual(azure._ALL_AZURE_TYPES[key][1]['gpu_count'], quantity)

    def test_non_rtx_family_quantities_are_preserved(self):
        for instance, model, quantity in [
            ('Standard_NC40ads_H100_v5', 'H100', 1),
            ('Standard_ND96isr_H100_v5', 'H100', 8),
            ('Standard_ND96isr_H200_v5', 'H200', 8),
            ('Standard_ND128isr_NDR_GB200_v6', 'GB200', 4),
        ]:
            with self.subTest(instance=instance):
                r = records(instance, model, {'gpu_count': quantity}, 32)[0]
                self.assertEqual(r.gpu_count, quantity)
                self.assertEqual(r.price_per_gpu_hour_usd, 32 / quantity)

    def test_ambiguous_or_conflicting_rtx_labels_are_omitted(self):
        for instance, model in [
            ('Standard_NC36_RTXPRO6000BSE_v6', 'RTX6000'),
            ('Standard_NC36ds_xl_RTXPRO6000BSE_v7', 'RTX6000'),
            ('Standard_NC36ds_xl_RTXPRO6000BSE_v6-extra', 'RTX6000'),
            ('RTX PRO 6000 quarter GPU', 'RTX6000'),
            ('Standard_ND96isr_H100_v5', 'RTX6000'),
            ('Standard_NC36ds_xl_RTXPRO6000BSE_v6', 'H100'),
        ]:
            with self.subTest(instance=instance, model=model), patch.object(azure, 'http_get') as http:
                self.assertEqual(azure._fetch_instance(instance, model, {'gpu_count': 1}, ['eastus'], NOW), [])
                http.assert_not_called()

    def test_missing_or_wrong_api_sku_cannot_borrow_requested_gpu_quantity(self):
        requested = 'Standard_NC36ds_xl_RTXPRO6000BSE_v6'
        missing = meter(requested, 1.24)
        missing.pop('armSkuName')
        wrong = meter('Standard_NC144ds_xl_RTXPRO6000BSE_v6', 5.50)
        self.assertEqual(records(requested, 'RTX6000', {'gpu_count': 1}, items=[missing, wrong]), [])

    def test_reservations_and_spot_keep_both_hourly_and_gpu_bases(self):
        instance = 'Standard_NC36ds_xl_RTXPRO6000BSE_v6'
        rows = [
            meter(instance, 1.24),
            meter(instance, .75, skuName='NC36 Spot'),
            meter(instance, .30, skuName='NC36 Low Priority'),
            meter(instance, 2190, type='Reservation', reservationTerm='1 Year'),
            meter(instance, 6570, type='Reservation', reservationTerm='3 Years'),
            meter(instance, .10, productName='Virtual Machines Windows'),
        ]
        got = {r.consumption_type: r for r in records(instance, 'RTX6000', {'gpu_count': 1}, items=rows)}
        self.assertEqual(set(got), {'on_demand', 'spot', 'low_priority', 'reserved_1yr', 'reserved_3yr'})
        for kind, hourly, per_gpu in [('on_demand', 1.24, 4.96), ('spot', .75, 3),
                                      ('low_priority', .30, 1.2), ('reserved_1yr', .25, 1),
                                      ('reserved_3yr', .25, 1)]:
            self.assertEqual(got[kind].gpu_count, .25)
            self.assertAlmostEqual(got[kind].price_per_hour_usd, hourly)
            self.assertAlmostEqual(got[kind].price_per_gpu_hour_usd, per_gpu)

    def test_downstream_json_enrichment_and_selection_keep_fractions(self):
        instance = 'Standard_NC36ds_xl_RTXPRO6000BSE_v6'
        original = records(instance, 'RTX6000', {'gpu_count': 1})[0]
        restored = PriceRecord.from_dict(json.loads(json.dumps(original.to_dict())))
        enrich_comparability([restored])
        self.assertEqual(restored.gpu_count, .25)
        self.assertEqual(restored.node_gpus, .25)
        self.assertAlmostEqual(restored.price_per_gpu_hour_usd, 4.96)
        self.assertFalse(_is_cluster_peer(restored))
        whole = records('Standard_NC144ds_xl_RTXPRO6000BSE_v6', 'RTX6000', {'gpu_count': 4}, 5.5)[0]
        selected = _best_price([restored, whole], 'RTX6000', 'on_demand', tiers=['hyperscaler'])
        self.assertIs(selected, restored)
        self.assertAlmostEqual(selected.price_per_gpu_hour_usd * selected.gpu_count, 1.24)

    def test_invalid_configured_non_rtx_quantities_are_omitted(self):
        for quantity in [None, 0, -1, True, float('nan'), float('inf'), '1/4']:
            with self.subTest(quantity=quantity), patch.object(azure, 'http_get') as http:
                self.assertEqual(azure._fetch_instance('Standard_NC40ads_H100_v5', 'H100',
                                                     {'gpu_count': quantity}, ['eastus'], NOW), [])
                http.assert_not_called()


if __name__ == '__main__':
    unittest.main()
