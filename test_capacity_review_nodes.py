"""Regressions for scoped eight-GPU evidence and decision-useful output."""
import unittest
from dataclasses import replace
from xml.etree import ElementTree

from capacity import insights, render
from capacity.schema import AvailabilityRecord

NOW = '2026-09-18T07:32:00+00:00'


def row(provider, state='available', **kw):
    defaults = dict(provider=provider, gpu_model='H100', region='global',
                    consumption_type='on_demand', state=state,
                    metric_type='stock_status_label', fetched_at=NOW,
                    data_source='official_api', source_url='https://example.test/source')
    defaults.update(kw)
    return AvailabilityRecord(**defaults)


def runpod(label='Low'):
    return row('runpod', 'limited' if label in {'Low', 'none'} else 'available',
               detail=f'stock 1x: High, 8x: {label}' if label != 'none' else
               '1x stock High, but no 8-GPU (cluster) stock',
               metric_value=1 if label == 'Low' else 0, instance_type='H100-SXM')


def scaleway(count=8, state='available'):
    return row('scaleway', state, region='fr-par-2', metric_type='instance_stock_status',
               gpu_count=count, instance_type=f'H100-SXM-{count}-80G', product_scope='gpu_instance')


def lambda_node(state='sold_out'):
    return row('lambda', state, gpu_count=8, instance_type='gpu_8x_h100_sxm5',
               product_scope='on_demand_instance', metric_type='launchable_regions',
               metric_value=0 if state == 'sold_out' else 1)


class CapacityReviewTests(unittest.TestCase):
    def test_low_eight_gpu_stock_is_positive_and_not_one_gpu_only(self):
        r = runpod()
        self.assertTrue(insights.live_reads([r], 'H100')[0]['cluster_ok'])
        self.assertEqual(insights.node_observation(r), 'available')
        self.assertEqual(insights.node_observation(runpod('none')), 'absent')
        self.assertEqual(render._normalize_partial('RunPod', r.detail), r.detail)
        page = render.render_confluence([r], [], {}, [])
        self.assertNotIn('no cluster-scale stock', page)
        self.assertNotIn('Safe-to-quote', page)
        self.assertNotIn('1x only', page)
        self.assertEqual(insights.gtm_claims([r], []), {'ammo': [], 'expired': []})

    def test_exact_eight_gpu_signals_include_vm_providers_without_claiming_multinode(self):
        rows = [runpod(), scaleway(), lambda_node(), scaleway(2),
                row('hyperstack', 'limited', metric_type='stock_level', metric_value=8,
                    detail='stock in 3 regions, best 8+'),
                row('voltage_park', 'sold_out', data_source='aggregator', metric_type='binary')]
        view = insights.node_summary(rows, 'H100')
        self.assertEqual({r['provider'] for r in view['available']}, {'runpod', 'scaleway'})
        self.assertEqual({r['provider'] for r in view['absent']}, {'lambda'})
        self.assertEqual({r['provider'] for r in view['unknown']}, {'hyperstack', 'voltage_park'})
        self.assertEqual(len(view['checked']), 3)
        parent, _ = render.render_slack(rows, [], {}, [])
        self.assertIn('H100 2/3 (+2 unknown)', parent)
        self.assertIn('multi-node capacity unverified', parent)
        self.assertNotIn('hold', parent.lower())

    def test_inference_aggregates_and_small_vms_never_become_eight_gpu_stock(self):
        inference = row('together', metric_type='inference_replicas', gpu_count=8,
                        product_scope='dedicated_inference', metric_value=100)
        hyper = row('hyperstack', metric_type='stock_level', metric_value=100)
        self.assertEqual(insights.node_reads([inference], 'H100'), [])
        for r in [hyper, scaleway(1), replace(lambda_node('available'), gpu_count=1)]:
            view = insights.node_summary([r], 'H100')
            self.assertFalse(view['checked'])
            self.assertEqual(len(view['unknown']), 1)

    def test_cached_positive_is_unknown_and_missing_coverage_is_not_sellout(self):
        old = [runpod(), scaleway()]
        status = {'provider_status': {'runpod': {'status': 'cached', 'cache_age_hours': 24}}}
        view = insights.node_summary([runpod()], 'H100', status)
        self.assertFalse(view['checked'])
        changes, coverage = insights.node_changes([runpod()], old, status)
        self.assertEqual(changes, [])
        self.assertEqual(set(coverage[0]['lost']), {'runpod', 'scaleway'})
        parent, thread = render.render_slack([runpod()], [], status, old)
        self.assertIn('Coverage changed', parent)
        self.assertIn('current availability unknown', thread)
        self.assertNotIn('sold out', parent)

    def test_added_provider_or_changed_configuration_does_not_look_like_restock(self):
        changes, coverage = insights.node_changes([scaleway(), runpod()], [scaleway()])
        self.assertEqual(changes, [])
        self.assertEqual(coverage[0]['added'], ['runpod'])
        changes, coverage = insights.node_changes([replace(scaleway(), state='sold_out', region='fr-par-3')], [scaleway()])
        self.assertFalse(changes)
        self.assertEqual(coverage[0]['changed_scope'], ['scaleway'])

    def test_compact_slack_retains_changes_and_exceptions_not_static_inventory(self):
        records = [replace(scaleway(), instance_type=f'H100-SXM-8-{n}') for n in range(40)]
        manifest = {'provider_status': {'scaleway': {'status': 'live'}}}
        parent, thread = render.render_slack(records, [], manifest, records)
        self.assertLess(len(parent), 800)
        self.assertLess(len(thread), 600)
        self.assertNotIn('H100-SXM-8-39', thread)
        page = render.render_confluence(records, [], manifest, records)
        self.assertIn('H100-SXM-8-39', page)
        self.assertIn('ac:name="expand"', page)
        self.assertLess(page.index('Eight-GPU node availability'), page.index('Provider evidence'))
        self.assertIn(NOW[:10], page)
        self.assertIn('https://example.test/source', page)
        ElementTree.fromstring('<root xmlns:ac="urn:ac">' + page + '</root>')

    def test_generic_diff_keeps_raw_scope_and_never_claims_restocked_nodes(self):
        from capacity.schema import CapacityDiffEntry
        rows = [row('hyperstack', 'limited', metric_type='stock_level', metric_value=8,
                    detail='stock in 1 region, best 8+'), runpod()]
        for current, scope in [(rows[0], 'aggregate GPU count'), (rows[1], '1x/8x stock labels')]:
            change = CapacityDiffEntry(current.provider, 'H100', 'global', 'on_demand',
                                       'state_change', old_state='sold_out', new_state='limited',
                                       instance_type=current.instance_type)
            text = render._describe_change(change, [current])
            self.assertIn(scope, text)
            self.assertIn('zero reported → limited signal', text)
            self.assertIn(current.detail, text)
            self.assertNotIn('sold out', text)
            self.assertNotIn('restocked', text)

    def test_matched_explicit_absence_to_positive_is_a_node_change(self):
        changes, coverage = insights.node_changes([runpod()], [runpod('none')])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['old'], 'absent')
        self.assertEqual(changes[0]['new'], 'available')
        self.assertEqual(coverage, [])

class CapacityRebuildTests(unittest.TestCase):
    def test_saved_run_dates_and_inputs_preserved_without_network(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from capacity.rebuild import rebuild
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, manifest = root / 'snapshot.json', root / 'manifest.json'
            snapshot.write_text(json.dumps([runpod().to_dict()]))
            manifest.write_text(json.dumps({'run_date': '2026-09-18',
                                           'completed_at': NOW,
                                           'provider_status': {'runpod': {'status': 'live'}}}))
            original = snapshot.read_bytes(), manifest.read_bytes()
            with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                result = rebuild(snapshot, manifest, root / 'preview')
            self.assertEqual(result['comparison'], 'baseline only')
            self.assertEqual(original, (snapshot.read_bytes(), manifest.read_bytes()))
            self.assertIn('2026-09-18', (root / 'preview/slack_message.txt').read_text())
            self.assertIn('Baseline:', (root / 'preview/slack_message.txt').read_text())


if __name__ == '__main__':
    unittest.main()
