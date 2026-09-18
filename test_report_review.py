"""Publication review: corrected bases, dated inputs and separated evidence."""
from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import diff
import report_freshness as freshness
from confluence_storage import to_storage, validate_xml
from schema import PriceRecord, DiffEntry


def offer(provider='lambda', gpu='H100', price=4.0, tier='on_demand', **extra):
    values = dict(provider=provider, gpu_model=gpu, gpu_count=8, instance_type=provider+'-h100-8',
                  region='us-east-1', consumption_type=tier, price_per_hour_usd=price*8,
                  price_per_gpu_hour_usd=price, fetched_at='2026-09-17T01:00:00Z',
                  data_source='official_api', source_url='https://example.invalid/prices', parser_version='same')
    values.update(extra)
    return PriceRecord(**values)


class ReportReviewTests(unittest.TestCase):
    def test_cached_together_bad_product_is_excluded_using_observation_date(self):
        row = offer('together', price=1.99, instance_type='together-hgx-h100', region='global',
                    data_source='web_scrape', fetched_at='2026-09-16T23:00:00Z')
        original = row.to_dict()
        eligible, notices = freshness.publication_records([row], '2026-09-17')
        self.assertEqual(eligible, [])
        self.assertTrue(any('mislabeled on-demand' in text for text in notices))
        self.assertEqual(row.to_dict(), original)

    def test_date_label_for_today_does_not_expire_current_inputs_early(self):
        now = datetime(2026, 9, 18, 1, 30, tzinfo=timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz else now.replace(tzinfo=None)
        row = offer(fetched_at='2026-09-16T03:00:00Z')  # 46.5h old at the run
        with patch.object(freshness, 'datetime', Clock):
            actual, _ = freshness.publication_records([row], now)
            labelled, _ = freshness.publication_records([row], 'September 18, 2026')
        self.assertEqual(actual, labelled)
        self.assertEqual(len(actual), 1)

    def test_real_azure_move_is_measured_after_the_denominator_correction(self):
        old = offer('azure', gpu='RTX6000', price=1.0, gpu_count=1, price_per_hour_usd=1,
                    instance_type='Standard_NC36ds_XL_RTXPRO6000BSE_v6', fetched_at='2026-09-16T01:00:00Z')
        new = replace(old, gpu_count=.25, price_per_hour_usd=1.1, price_per_gpu_hour_usd=4.4,
                      fetched_at='2026-09-17T01:00:00Z')
        with patch.object(diff, '_recent_price_levels', return_value={}):
            changes = diff.compute_diff([old], [new])
        moves = [x for x in changes if x.change_type == 'price_change']
        self.assertEqual(len(moves), 1)
        self.assertAlmostEqual(moves[0].old_price, 4.0)
        self.assertAlmostEqual(moves[0].new_price, 4.4)
        self.assertAlmostEqual(moves[0].delta_pct, 10.0)
        self.assertEqual(old.price_per_gpu_hour_usd, 1.0)

    def test_expired_committed_reference_never_becomes_a_current_comparison(self):
        rows = [offer('nebius', price=1.23, tier='committed_1yr'), offer('aws', price=4, tier='reserved_1yr')]
        with patch('config.NEBIUS_COMMITTED_PRICES_VERIFIED_DATE', '2026-06-23'):
            eligible, notices = freshness.publication_records(rows, '2026-09-17')
            with patch.object(diff, '_load_intel', return_value=[]), patch.object(diff, '_load_reserve_wins', return_value=([], None, True)):
                page = diff.format_confluence_table(rows, '2026-09-17')
        self.assertEqual([r.provider for r in eligible], ['aws'])
        self.assertTrue(any('verified 2026-06-23' in n for n in notices))
        self.assertNotIn('$1.23', page)
        self.assertIn('committed reference expired', page)

    def test_spot_reservations_and_exchange_do_not_share_an_auction_floor(self):
        rows = [offer('nebius', price=3, tier='preemptible'), offer('aws', price=2, tier='spot'),
                offer('aws_capacity_blocks', price=.75, tier='reserved_short', price_basis='published_capacity_block_rate'),
                offer('sfcompute', price=.50, tier='spot')]
        with patch.object(diff, '_load_reserve_wins', return_value=([], None, False)):
            page = diff.format_spot_auction_page(rows, '2026-09-17')
        interruptible, references = page.split('<h2>Short-term reservations and exchange observations</h2>', 1)
        self.assertIn('$2.00', interruptible)
        self.assertNotIn('$0.50', interruptible)
        self.assertNotIn('$0.75', interruptible)
        self.assertIn('$0.50', references)
        self.assertIn('$0.75', references)
        self.assertNotIn('Auction planning floor', page)
        self.assertNotIn('Net market floor', page)

    def test_page_preserves_dates_and_details_behind_expands(self):
        rows = [offer('nebius', price=4), offer('lambda', price=5)]
        with patch.object(diff, '_load_intel', return_value=[]), patch.object(diff, '_load_reserve_wins', return_value=([], None, False)):
            page = diff.format_confluence_table(rows, '2026-09-17')
        self.assertIn('2026-09-17T01:00:00Z', page)
        self.assertIn('official_api', page)
        self.assertGreaterEqual(page.count('data-type="expand"'), 5)
        self.assertIn('not the verification date of every input', page)
        storage = to_storage(page)
        self.assertIsNone(validate_xml(storage))
        tree = ET.fromstring('<root xmlns:ac="http://atlassian.com/content" xmlns:ri="http://atlassian.com/resource/identifier">'+storage+'</root>')
        self.assertEqual(len(tree.findall('table')), 1)

    def test_slack_move_output_is_bounded_with_all_detail_linked(self):
        records, changes = [], []
        for gpu in ['H100', 'H200', 'B200', 'B300', 'GB200', 'GB300', 'RTX6000', 'L40S']:
            row = offer('aws', gpu=gpu, price=5, instance_type='aws-'+gpu)
            records.append(row)
            changes.append(DiffEntry(row.provider, gpu, row.region, row.consumption_type,
                                     row.instance_type, 'price_change', 4, 5, 25))
        summary = diff.format_slack_summary(changes, '2026-09-17', 'https://example.invalid/page', records=records)
        with patch.object(diff, '_load_reserve_wins', return_value=([], None, False)):
            thread = diff.format_slack_message(changes, '2026-09-17', 'https://example.invalid/page', records=records)
        self.assertEqual(summary.count('→'), 2)
        self.assertEqual(thread.count('→'), 6)
        self.assertIn('6 further change groups', summary)
        self.assertIn('https://example.invalid/page', summary)
        self.assertIn('https://example.invalid/page', thread)
        self.assertLess(len(summary), 1500)
        self.assertLess(len(thread), 2500)

    def test_static_provider_scope_and_availability_claims_are_absent(self):
        regional = diff._build_hyperscaler_tables([offer('aws'), offer('nebius')])
        rtx = diff._build_rtx_section([offer('nebius', gpu='RTX6000'), offer('azure', gpu='RTX6000', price=6)])
        for unverified in ['availability by GPU', 'US regions only currently', 'partial-upfront capacity reservation',
                           'no US discount', 'OCI price-list API']:
            self.assertNotIn(unverified, regional)
        self.assertNotIn("hyperscalers don't offer this card", rtx)
        self.assertIn('not a complete market census', rtx)


if __name__ == '__main__':
    unittest.main()
