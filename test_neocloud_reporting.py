"""Report-level evidence boundaries and complete Lambda tier rendering."""
import csv
from dataclasses import replace
from datetime import date
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import diff
import history
from coverage_report import (build_price_coverage, build_capacity_coverage,
                             build_priority_coverage, render_priority_coverage)
from confluence_storage import to_storage, validate_xml
from fetchers.lambda_labs import _parse_one_click_clusters
from test_lambda_offer_coverage import cluster_html, NOW
from test_coverage_report import offer
from capacity.schema import AvailabilityRecord


class NeocloudReports(unittest.TestCase):
    def test_all_cluster_tiers_survive_render_and_history_quantity_semantics(self):
        rows, _ = _parse_one_click_clusters(cluster_html(), NOW)
        body = diff._build_short_term_reserved_section(rows)
        for row in rows:
            self.assertIn(f'${row.price_per_gpu_hour_usd:.2f}', body)
        self.assertIn('256+ GPUs', body)
        self.assertIn('256 GPUs', body)
        self.assertEqual(body.count('2 weeks – 1 year'), 6)
        self.assertEqual(body.count('<tr>'), 7)
        validate_xml(to_storage(body))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.csv'
            path.write_text(','.join(history.COLUMNS) + '\n')
            with patch.object(history, 'HISTORY_CSV', path):
                history.append_records(rows, date(2026, 9, 18))
            with path.open() as stream:
                saved = list(csv.DictReader(stream))
            self.assertTrue(saved)
            self.assertTrue(all(r['term_min_days'] == '14' and r['term_max_days'] == '365' for r in saved))
            b200 = next(r for r in saved if r['gpu_model'] == 'B200')
            self.assertEqual(b200['gpu_count_relation'], 'minimum')

    def test_price_feeds_count_once_and_quotes_remain_separate(self):
        rows = [offer(fetched_at=NOW), offer(provider='cp_coreweave', fetched_at=NOW,
                    source_feed='computeprices', source_observed_at=NOW)]
        quotes = {'as_of': NOW, 'providers': [{'provider': 'coreweave', 'statuses': {
            'qualified_asking_price': 2, 'qualified_signed_deal': 1, 'expired': 3}}]}
        report = build_price_coverage(rows, NOW, quote_report=quotes)
        core = report['priority_neoclouds']['providers'][0]
        self.assertEqual(core['fresh_price_cells'], 1)
        self.assertEqual(core['qualified_asking_prices'], 2)
        self.assertEqual(core['qualified_signed_deals'], 1)
        self.assertEqual(core['quotes_requiring_review'], 3)
        self.assertIsNone(core['direct_instance_availability_cells'])

    def test_footprints_stale_rows_and_partial_sources_are_not_live_instance_coverage(self):
        row = AvailabilityRecord('lambda', 'H100', 'us', 'on_demand', 'available',
                                 'instance_launchability', instance_type='h100-8', fetched_at=NOW,
                                 product_scope='on_demand_instance', gpu_count=8, data_source='official_api')
        caps = [row, replace(row, region='footprint', metric_type='listed_offering'),
                replace(row, region='old', fetched_at='2026-09-01T00:00:00Z'),
                replace(row, region='unknown', state='unknown')]
        prices = build_price_coverage([], NOW)
        cap_report = build_capacity_coverage(caps, NOW, {'lambda': {'status': 'live'}})
        report = build_priority_coverage(prices, capacity_report=cap_report)
        lam = next(p for p in report['providers'] if p['provider'] == 'lambda')
        self.assertEqual(lam['direct_instance_availability_cells'], 1)
        self.assertEqual(lam['footprint_cells'], 1)
        partial = build_capacity_coverage(caps, NOW, {'lambda': {'status': 'partial'}})
        checked = build_priority_coverage(prices, capacity_report=partial)
        self.assertEqual(checked['providers'][1]['direct_instance_availability_cells'], 0)
        for source_status in ('empty', 'unknown', None):
            health = {'lambda': {'status': source_status}} if source_status else {}
            checked = build_priority_coverage(prices, capacity_report=build_capacity_coverage([row], NOW, health))
            self.assertEqual(checked['providers'][1]['direct_instance_availability_cells'], 0)
        unscoped = build_capacity_coverage([replace(row, product_scope='')], NOW, {'lambda': {'status': 'live'}})
        self.assertEqual(build_priority_coverage(prices, capacity_report=unscoped)['providers'][1]['direct_instance_availability_cells'], 0)
        validate_xml(to_storage(render_priority_coverage(report)))

    def test_undated_reference_is_not_fresh_and_different_run_clocks_survive(self):
        prices = build_price_coverage([offer(provider='lambda', source_feed='skypilot', fetched_at=NOW)], NOW)
        earlier = '2026-09-17T12:00:00Z'
        capacity = build_capacity_coverage([], earlier)
        report = build_priority_coverage(prices, capacity_report=capacity)
        self.assertEqual(report['providers'][1]['fresh_price_cells'], 0)
        self.assertNotEqual(report['pricing_as_of'], report['capacity_as_of'])

    def test_catalogue_only_and_partial_health_never_claim_all_sources_live(self):
        html = diff._run_health_line({'crusoe': {'status': 'catalogue_only'}, 'lambda': {'status': 'partial'}})
        self.assertNotIn('all sources live', html)
        self.assertIn('catalogue refreshed', html)
        self.assertIn('lambda partial', html)


if __name__ == '__main__':
    unittest.main()
