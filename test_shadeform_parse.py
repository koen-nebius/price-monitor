"""Offer-level Shadeform normalization without network access."""
import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from fetchers import shadeform

NOW = '2026-09-18T12:00:00+00:00'


def offer(**changes):
    item = {
        'cloud': 'verda', 'shade_instance_type': 'B300x8', 'cloud_instance_type': 'gpu_8x_b300',
        'configuration': {'gpu_type': 'B300', 'num_gpus': 8, 'interconnect': 'sxm6',
                          'nvlink': True, 'vcpus': 240, 'memory_in_gb': 2040,
                          'storage_in_gb': 4096, 'vram_per_gpu_in_gb': 288},
        'hourly_price': 6164,
        'availability': [{'region': 'fin-01', 'display_name': 'Finland',
                          'rental_type': 'on_demand', 'available': True}],
    }
    item.update(changes)
    return item


class ShadeformParseTests(unittest.TestCase):
    def test_preserves_every_size_sku_and_native_region_with_storage(self):
        first = offer(availability=[
            {'region': 'fin-01', 'display_name': 'Finland', 'rental_type': 'on_demand', 'available': True},
            {'region': 'fin-02', 'display_name': 'Finland', 'rental_type': 'on_demand', 'available': False},
            {'region': 'fin-03', 'display_name': 'Finland', 'rental_type': 'on_demand'},
        ])
        one_gpu = offer(shade_instance_type='B300x1', cloud_instance_type='gpu_1x_b300', hourly_price=799)
        one_gpu['configuration']['num_gpus'] = 1
        records = shadeform.parse([first, one_gpu], NOW)
        self.assertEqual(len(records), 4)
        eight = {r.region: r for r in records if r.gpu_count == 8}
        self.assertEqual(set(eight), {'fin-01', 'fin-02', 'fin-03'})
        self.assertEqual([eight[r].available for r in ['fin-01', 'fin-02', 'fin-03']], [True, False, None])
        self.assertAlmostEqual(eight['fin-01'].price_per_gpu_hour_usd, 7.705)
        self.assertAlmostEqual(eight['fin-01'].price_per_hour_usd, 61.64)
        self.assertEqual(eight['fin-01'].storage_gb, 4096)
        self.assertEqual((eight['fin-01'].vcpu, eight['fin-01'].ram_gb), (240, 2040))
        self.assertEqual((eight['fin-01'].form_factor, eight['fin-01'].interconnect), ('SXM', 'NVLink'))
        self.assertTrue(any(r.gpu_count == 1 and r.price_per_hour_usd == 7.99 for r in records))
        self.assertEqual(len({r.offer_id for r in records}), 4)
        for r in records:
            self.assertEqual((r.source_feed, r.source_observed_at, r.parser_version),
                             ('shadeform', NOW, 'aggregator-offers-1'))
            self.assertEqual(r.data_source, 'aggregator')

    def test_spot_and_od_have_separate_prices_states_and_identity(self):
        item = offer(availability=[
            {'region': 'fin-01', 'rental_type': 'on_demand', 'available': False, 'hourly_price': 'ignored'},
            {'region': 'fin-01', 'rental_type': 'spot', 'available': True, 'hourly_price': '26.76'},
        ])
        records = {r.consumption_type: r for r in shadeform.parse([item], NOW)}
        self.assertEqual(set(records), {'on_demand', 'spot'})
        self.assertAlmostEqual(records['on_demand'].price_per_hour_usd, 61.64)
        self.assertAlmostEqual(records['spot'].price_per_hour_usd, 26.76)
        self.assertAlmostEqual(records['spot'].price_per_gpu_hour_usd, 3.345)
        self.assertFalse(records['on_demand'].available)
        self.assertTrue(records['spot'].available)
        self.assertNotEqual(records['on_demand'].offer_id, records['spot'].offer_id)

    def test_spot_only_response_never_fabricates_od_offer(self):
        item = offer(availability=[{'region': 'fin-01', 'rental_type': 'spot',
                                    'available': True, 'hourly_price': '16.00'}])
        records = shadeform.parse([item], NOW)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].consumption_type, 'spot')
        self.assertEqual(records[0].region, 'fin-01')
        item['availability'][0]['hourly_price'] = 'not a number'
        self.assertEqual(shadeform.parse([item], NOW), [])

    def test_empty_availability_retains_only_unknown_catalogue_reference(self):
        for availability in [None, [], {}, [None], [{'region': 'fin-01', 'available': True}]]:
            with self.subTest(availability=availability):
                record, = shadeform.parse([offer(availability=availability)], NOW)
                self.assertEqual(record.region, 'unknown')
                self.assertIsNone(record.available)
                self.assertEqual(record.price_basis, 'aggregator_catalogue')
        record, = shadeform.parse([offer(availability=[{'display_name': 'Finland',
                                                      'available': True, 'rental_type': 'on_demand'}])], NOW)
        self.assertEqual(record.region, 'unknown')
        self.assertIsNone(record.available)

    def test_nested_configuration_wins_over_conflicting_legacy_fields(self):
        item = offer(gpu_type='H100', num_gpus=1, storage_in_gb=1)
        record, = shadeform.parse([item], NOW)
        self.assertEqual((record.gpu_model, record.gpu_count, record.storage_gb), ('B300', 8, 4096))
        legacy = copy.deepcopy(item)
        legacy.update(legacy.pop('configuration'))
        flattened, = shadeform.parse([legacy], NOW)
        self.assertEqual(record.to_dict(), flattened.to_dict())
        item['configuration']['num_gpus'] = None
        self.assertEqual(shadeform.parse([item], NOW), [])

    def test_identity_survives_repricing_stock_time_and_order_changes(self):
        original = offer()
        first, = shadeform.parse([original], NOW)
        changed = copy.deepcopy(original)
        changed['hourly_price'] = 7200
        changed['availability'][0]['available'] = False
        changed['availability'][0]['display_name'] = 'Translated display label'
        second, = shadeform.parse([changed], '2026-09-19T00:00:00Z')
        self.assertEqual(first.offer_id, second.offer_id)
        for config in [{'shade_instance_type': 'other'}, {'cloud_instance_type': 'other'},
                       {'availability': [{'region': 'other', 'rental_type': 'on_demand'}]}]:
            modified = copy.deepcopy(original)
            modified.update(config)
            record, = shadeform.parse([modified], NOW)
            self.assertNotEqual(record.offer_id, first.offer_id)
        modified = copy.deepcopy(original)
        modified['configuration']['storage_in_gb'] = 8192
        record, = shadeform.parse([modified], NOW)
        self.assertNotEqual(record.offer_id, first.offer_id)
        records = shadeform.parse([original, changed], NOW)
        self.assertEqual(len(records), 2)  # conflicting quotes are not reduced to the cheapest
        self.assertEqual(len(shadeform.parse([original, original], NOW)), 1)

    def test_variants_are_retained_without_merging_nvl_and_sxm(self):
        sxm, nvl = offer(), offer()
        sxm['configuration']['gpu_type'] = 'H100'
        nvl['configuration']['gpu_type'] = 'H100_nvl'
        records = shadeform.parse([sxm, nvl], NOW)
        self.assertEqual([r.gpu_model for r in records], ['H100', 'H100'])
        self.assertEqual([r.gpu_variant for r in records], ['H100', 'H100_nvl'])
        self.assertEqual([r.form_factor for r in records], ['SXM', 'NVL'])
        self.assertEqual(len({r.offer_id for r in records}), 2)

    def test_invalid_gpu_count_and_prices_never_get_coerced_into_offers(self):
        for value in [True, False, 0, -1, 1.5, 'NaN', 'Infinity', None]:
            item = offer()
            item['configuration']['num_gpus'] = value
            self.assertEqual(shadeform.parse([item], NOW), [], value)
        for value in [True, 0, -1, 'NaN', 'Infinity', None]:
            self.assertEqual(shadeform.parse([offer(hourly_price=value)], NOW), [], value)
        for value in ['false', 1, 0, None]:
            item = offer()
            item['availability'][0]['available'] = value
            record, = shadeform.parse([item], NOW)
            self.assertIsNone(record.available)

    def test_resale_pool_and_untracked_families_stay_excluded(self):
        other = offer()
        other['configuration']['gpu_type'] = 'A100_80G'
        self.assertEqual(shadeform.parse([offer(cloud='excesssupply'), other, None], NOW), [])

    def test_fetch_requires_existing_key_and_sends_it_only_to_mocked_api(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(shadeform.urllib.request, 'urlopen') as network:
            self.assertEqual(shadeform.fetch(), [])
            network.assert_not_called()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'instance_types': [offer()]}).encode()
        with patch.dict(os.environ, {'SHADEFORM_API_KEY': 'synthetic-key'}, clear=True), \
                patch.object(shadeform.urllib.request, 'urlopen', return_value=response) as network:
            records = shadeform.fetch()
        self.assertEqual(len(records), 1)
        request = network.call_args.args[0]
        self.assertEqual(request.full_url, shadeform.API_URL)
        self.assertEqual(request.get_header('X-api-key'), 'synthetic-key')
        self.assertEqual(records[0].source_observed_at, records[0].fetched_at)
        self.assertNotIn('synthetic-key', json.dumps(records[0].to_dict()))


if __name__ == '__main__':
    unittest.main()
