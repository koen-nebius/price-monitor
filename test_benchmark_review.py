"""Review regressions: source medians, CRM interpretation and compact publication."""
import csv
import json
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest.mock import patch

import forward_curve as fc
from confluence_storage import to_storage, validate_xml


def cohorts():
    return [
        {"gpu": "B300", "tenor_months": 3, "outcome": "won", "source": "hubspot",
         "opps": 6, "lines": 7, "gpus": 352, "med": 3.50, "p25": 3.40, "p75": 4.49,
         "window_from": "2026-01-01", "window_to": "2026-08-10", "generated": "2026-09-16"},
        {"gpu": "B300", "tenor_months": 3, "outcome": "won", "source": "salesforce",
         "opps": 3, "lines": 3, "gpus": 296, "med": 6.90, "p25": 6.25, "p75": 7.38,
         "window_from": "2026-08-10", "window_to": "2026-09-17", "generated": "2026-09-17"},
    ]


def result_fixture():
    return {
        "as_of": "2026-09-18", "method_version": fc.METHOD_VERSION,
        "n_observations": {"bid": 0, "ask": 0, "ask_deals": 0},
        "sources": {"intel_latest_quote": "2026-09-11", "reserve_tenor_generated": "2026-09-12",
                    "grid_version": "2026-09-07", "list_snapshot": "2026-09-15"},
        "marks": [], "shape": [{"tier": tier} for tier in fc.TIERS],
        "params": {"segment": "test"}, "quarter_effects_log": {}, "cohorts": cohorts(),
        "ask_paths": [{"gpu": "B300", "tenor_months": 12, "outcome": "lost_capacity",
                       "window_from": "2024-10-15", "window_to": "2026-08-10", "generated": "2026-09-14",
                       "first_ask_med": 5.0, "final_med": 4.8, "deals": 2, "share_revised": 0.5}],
    }


class BenchmarkPublicationReview(unittest.TestCase):
    def test_expired_manual_committed_list_is_withheld_without_replacing_finance_grid(self):
        columns = ['snapshot_date', 'provider', 'gpu_model', 'gpu_count', 'instance_type', 'region',
                   'consumption_type', 'price_per_hour_usd', 'price_per_gpu_hour_usd', 'data_source']
        rows = [['2026-09-18', 'nebius', 'B300', 8, 'B300', 'global', 'committed_3yr', 36.4, 4.55, 'manual'],
                ['2026-09-18', 'aws', 'B300', 8, 'p6-b300', 'us-east-1', 'reserved_3yr', 72, 9, 'official_api']]
        with tempfile.TemporaryDirectory() as tmp:
            path, grid, missing = Path(tmp)/'history.csv', Path(tmp)/'grid.json', Path(tmp)/'missing.csv'
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream); writer.writerow(columns); writer.writerows(rows)
            grid.write_text(json.dumps({'grids': {'2026-09-07': {'segments': {'ai_native_above_512': {
                'B300': {'36': {'100': 5.4, '50': 6.0}}}}}}}))
            with patch('config.NEBIUS_COMMITTED_PRICES_VERIFIED_DATE', '2026-06-23'):
                result = fc.build(date(2026, 9, 18), history=path, grid=grid, intel=missing,
                                  reserve=missing, contracts=missing, crm_asks=missing, index=missing, snapshot=missing)
            with patch('config.NEBIUS_COMMITTED_PRICES_VERIFIED_DATE', '2026-09-17'):
                fresh = fc.load_list(path, as_of=date(2026, 9, 18))
        cell = next(r for r in result['marks'] if r['tier'] == 'B300' and r['tenor_months'] == 36)
        self.assertIsNone(cell['list_nebius'])
        self.assertEqual(cell['list_hyperscaler_min'], 9)
        self.assertEqual((cell['grid_100'], cell['grid_50']), (5.4, 6.0))
        self.assertEqual(fresh[('B300', 36)]['nebius'], 4.55)
        self.assertFalse(result['sources']['nebius_committed_reference_eligible'])
        self.assertEqual(result['sources']['nebius_committed_reference_verified'], '2026-06-23')
        self.assertIn('excluded from current list comparisons', fc.render_confluence_body(result))
        self.assertIsNone(next(r for r in fc.view_payload(result)['marks']
                               if r['tier'] == 'B300' and r['tenor_months'] == 36)['list_nebius'])

    def test_marks_history_is_normalized_to_lf_only_when_outputs_are_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            history = out/'marks_history.csv'
            history.write_bytes(b'as_of,tier\r\n2026-09-10,B300\r\n')
            result = result_fixture()
            with patch.object(fc, 'OUT_DIR', out), patch.object(fc, 'BODY_HTML', out/'body.html'), \
                 patch.object(fc, 'render_png', return_value=False), patch.object(fc, 'render_svg', return_value='<svg/>'):
                fc.write_outputs(result, quiet=True)
            self.assertNotIn(b'\r\n', history.read_bytes())
            self.assertIn(b'2026-09-10,B300', history.read_bytes())

    def test_historical_payg_reference_excludes_mislabeled_together_prices(self):
        # The bad preemptible observation must not become an artificial PAYG dip.
        columns = ['snapshot_date', 'provider', 'gpu_model', 'gpu_count', 'instance_type', 'region',
                   'consumption_type', 'price_per_hour_usd', 'price_per_gpu_hour_usd', 'data_source']
        bad = ['2026-09-16', 'together', 'H100', 8, 'together-hgx-h100', 'global',
               'on_demand', 15.92, 1.99, 'web_scrape']
        peer = ['2026-09-16', 'lambda', 'H100', 8, 'lambda-h100-8', 'us-east-1',
                'on_demand', 32, 4, 'official_api']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history.csv'
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream); writer.writerow(columns); writer.writerows([bad, peer])
            raw = path.read_bytes()
            reference = fc.load_on_demand(path, realised=Path(tmp)/'missing.csv')['H100']
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(reference['peer_od_median'], 4.0)
        self.assertEqual(reference['peer_od_n'], 1)
        self.assertEqual([r['provider'] for r in reference['peer_list']], ['lambda'])

    def test_retired_aws_constants_do_not_enter_committed_benchmark_references(self):
        columns = ['snapshot_date', 'provider', 'gpu_model', 'gpu_count', 'instance_type', 'region',
                   'consumption_type', 'price_per_hour_usd', 'price_per_gpu_hour_usd', 'data_source']
        retired = ['2026-09-18', 'aws', 'B300', 8, 'p6-b300.48xlarge', 'us-east-1',
                   'capacity_block', 93.6, 11.7, 'official_api']
        valid = ['2026-09-18', 'aws', 'B300', 8, 'p6-b300.48xlarge', 'us-east-1',
                 'reserved_1yr', 120, 15, 'official_api']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history.csv'
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream); writer.writerow(columns); writer.writerows([retired, valid])
            raw = path.read_bytes()
            references = fc.load_list(path)
            self.assertEqual(path.read_bytes(), raw)
        self.assertNotIn(('B300', 3), references)
        self.assertEqual(references[('B300', 12)]['hyperscaler_min'], 15)

    def test_source_medians_are_not_turned_into_a_pooled_price(self):
        body = fc._render_cohorts(cohorts())
        self.assertIn('$3.50', body)
        self.assertIn('$6.90', body)
        self.assertNotIn('$4.63', body)  # previous arithmetic average of medians
        self.assertIn('(6; 7; 352)', body)  # opportunity count is distinct from line count
        self.assertIn('(3; 3; 296)', body)
        self.assertIn('HubSpot', body)
        self.assertIn('Salesforce', body)
        self.assertIn('2026-01-01 to before 2026-08-10', body)
        self.assertIn('2026-08-10 to 2026-09-17', body)
        self.assertIn('input generated 2026-09-16', body)
        self.assertIn('input generated 2026-09-17', body)

    def test_loss_classes_do_not_assert_acceptance_or_ceiling(self):
        result = result_fixture()
        for row, outcome in zip(result['cohorts'], ['lost_capacity', 'lost_price_or_competitor']):
            row['outcome'] = outcome
        body = fc.render_confluence_body(result)
        self.assertIn('lost: capacity', body)
        self.assertIn('lost: price or competitor', body)
        self.assertIn('does not establish price acceptance', body)
        self.assertIn('does not establish a willingness-to-pay ceiling', body)
        self.assertIn('same last priced snapshot', body)
        for unsupported in ['accepted, lost', 'customer accepted our price', 'stronger than any quote',
                            'the price the customer had accepted', 'competitor: a ceiling']:
            self.assertNotIn(unsupported, body)

    def test_loader_retains_the_line_count_and_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'cohorts.csv'
            with path.open('w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['gpu', 'tenor_months', 'outcome', 'source', 'opps', 'lines', 'gpus',
                                 'price_p25', 'price_med', 'price_p75', 'window_from', 'window_to', 'generated_date'])
                writer.writerow(['B300', 3, 'won', 'hubspot', 6, 7, 352, 3.4, 3.5, 4.49,
                                 '2026-01-01', '2026-08-10', '2026-09-16'])
            loaded = fc.load_cohorts(path)
        self.assertEqual(loaded, [cohorts()[0]])

    def test_overview_keeps_dates_visible_and_details_expandable(self):
        body = fc.render_confluence_body(result_fixture(), with_images=True)
        storage = to_storage(body)
        self.assertIsNone(validate_xml(storage))
        ac = '{http://atlassian.com/content}'
        tree = ET.fromstring('<root xmlns:ac="http://atlassian.com/content" '
                             'xmlns:ri="http://atlassian.com/resource/identifier">'+storage+'</root>')
        visible = ''.join(''.join(child.itertext()) for child in tree
                          if not (child.tag == ac+'structured-macro' and child.get(ac+'name') == 'expand'))
        for date in ['2026-09-18', '2026-09-11', '2026-09-12', '2026-09-07', '2026-09-15',
                     '2026-09-16', '2026-09-17', '2026-09-14']:
            self.assertIn(date, visible)
        self.assertIn('daily build does not refresh every input', visible)
        self.assertIn('2-opportunity/deal minimum', visible)
        self.assertIn('require at least 3 and 3 deals', visible)
        self.assertEqual(len(tree.findall('table')), 1)  # the overview contains only the mark matrix
        expands = [child for child in tree if child.tag == ac+'structured-macro' and child.get(ac+'name') == 'expand']
        self.assertEqual(len(expands), 6)
        all_details = ' '.join(''.join(e.itertext()) for e in expands)
        for evidence in ['HubSpot', 'Salesforce', '$3.50', '$6.90', '$5.00', '$4.80',
                         'Finance', 'Public contracts', 'Unstated terms', 'Prepay normalisation']:
            self.assertIn(evidence, all_details)

    def test_source_metadata_is_escaped(self):
        row = dict(cohorts()[0], source='CRM & archive', generated='<unknown>')
        body = fc._render_cohorts([row])
        self.assertIn('CRM &amp; archive', body)
        self.assertIn('&lt;unknown&gt;', body)
        self.assertIsNone(validate_xml(to_storage(body)))

    def test_interactive_references_are_portable_without_changing_raw_provenance(self):
        result = result_fixture()
        local = '/Users/researcher/Claude PM/gb300-pricing/scripts/build_final_v4.py:455'
        result['perf'] = {'_source': local, '_extracted': '2026-09-15',
                          'pairs': [{'sku': 'B300', 'versus': 'GB300', 'sources': [local]}]}
        result['sources']['working_reference'] = '/private/tmp/review/source.json'
        raw = json.dumps(result, sort_keys=True)
        rendered = fc.render_view_html(result)
        self.assertIn('gb300-pricing/scripts/build_final_v4.py:455', rendered)
        self.assertNotIn('/Users/', rendered)
        self.assertNotIn('/private/tmp', rendered)
        self.assertEqual(json.dumps(result, sort_keys=True), raw)
        payload = fc.view_payload(result)
        self.assertEqual(payload['as_of'], '2026-09-18')
        self.assertEqual(payload['perf']['_extracted'], '2026-09-15')
        self.assertEqual(payload['perf']['pairs'][0]['sources'],
                         ['gb300-pricing/scripts/build_final_v4.py:455'])

    def test_interactive_source_medians_and_script_syntax(self):
        node = shutil.which('node')
        if not node:
            fallback = Path('/Users/koenbrormann/.nvm/versions/node/v22.22.0/bin/node')
            node = str(fallback) if fallback.exists() else None
        if not node:
            self.skipTest('Node is needed to execute the interactive formatter')
        template = fc.TEMPLATE.read_text()
        script = re.search(r'<script>(.*?)</script>', template, re.S).group(1)
        syntax = subprocess.run([node, '--check'], input=script, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        helper = template[template.index('  function cohortReferenceText('):template.index('  function renderExtras(')]
        runtime = ('const TENORS=[3,6,12,18,24,36,60]; '
                   'const fmt=x=>"$"+x.toFixed(2), n0=x=>String(x), esc=x=>String(x);\n' + helper +
                   '\nprocess.stdout.write(cohortReferenceText('+json.dumps(cohorts())+'));')
        rendered = subprocess.run([node], input=runtime, text=True, capture_output=True)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        for value in ['$3.50', '$6.90', 'HubSpot', 'Salesforce', '6 opportunities; 7 lines',
                      '2026-01-01 to before 2026-08-10', '2026-08-10 to 2026-09-17',
                      'input 2026-09-16', 'input 2026-09-17']:
            self.assertIn(value, rendered.stdout)
        self.assertNotIn('$4.63', rendered.stdout)
        for view in ['Market benchmarks', 'Market position', 'Contract return']:
            self.assertIn('>'+view+'</button>', template)
        self.assertIn('establishes neither willingness to pay nor a competitor price', template)
        for unsupported in ['accepted, lost', 'customer took our price', 'upper bound on what those customers would pay',
                            'upper bound for that customer']:
            self.assertNotIn(unsupported, template)


if __name__ == '__main__':
    unittest.main()
