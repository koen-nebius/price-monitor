"""Publication is successful only after validation and destination read-back."""
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from capacity import notify_confluence as publisher
from confluence_storage import to_storage


class CapacityPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, filename in [('META_FILE', 'page.json'), ('BODY_FILE', 'body.html'),
                               ('MANIFEST_FILE', 'manifest.json'), ('RECEIPT_FILE', 'receipt.json')]:
            context = patch.object(publisher, name, self.root / filename)
            context.start()
            self.addCleanup(context.stop)
        env = patch.dict(os.environ, {'CONFLUENCE_EMAIL': 'test@example.invalid',
                                     'CONFLUENCE_API_TOKEN': 'SENSITIVE_TEST_TOKEN'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        publisher.MANIFEST_FILE.write_text(json.dumps({'run_date': datetime.now(timezone.utc).date().isoformat()}))
        publisher.META_FILE.write_text(json.dumps({'page_id': '123'}))
        publisher.BODY_FILE.write_text('<h2>Capacity</h2><p>Observed stock only</p>')

    def receipt(self):
        return json.loads(publisher.RECEIPT_FILE.read_text())

    def responses(self, actual_body=None, actual_version=8, title='Keep live title'):
        body = to_storage(publisher.BODY_FILE.read_text())
        return [{'title': 'Keep live title', 'version': {'number': 7}},
                {'version': {'number': 8}},
                {'title': title, 'version': {'number': actual_version},
                 'body': {'storage': {'value': body if actual_body is None else actual_body}}}]

    def test_preserves_title_verifies_readback_and_writes_secret_free_receipt(self):
        with patch.object(publisher, '_request', side_effect=self.responses()) as request:
            self.assertEqual(publisher.main(['--strict']), 0)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['GET', 'PUT', 'GET'])
        self.assertEqual(request.call_args_list[1].args[3]['title'], 'Keep live title')
        receipt = self.receipt()
        self.assertTrue(receipt['ok'])
        self.assertEqual(receipt['stage'], 'verified')
        self.assertTrue(receipt['pages']['123']['raw_body_equal'])
        self.assertEqual(receipt['pages']['123']['verification'], 'canonical_storage_xml')
        self.assertNotIn('SENSITIVE_TEST_TOKEN', publisher.RECEIPT_FILE.read_text())
        self.assertNotIn('test@example.invalid', publisher.RECEIPT_FILE.read_text())

    def test_strict_rejects_missing_credentials_id_body_and_invalid_xml_before_network(self):
        cases = [('credentials', lambda: os.environ.pop('CONFLUENCE_API_TOKEN')),
                 ('id', lambda: publisher.META_FILE.write_text('{}')),
                 ('body', lambda: publisher.BODY_FILE.write_text('')),
                 ('xml', lambda: publisher.BODY_FILE.write_text('<table><tr></table>'))]
        for name, mutate in cases:
            with self.subTest(case=name):
                publisher.META_FILE.write_text('{"page_id":"123"}')
                publisher.BODY_FILE.write_text('<p>valid</p>')
                os.environ['CONFLUENCE_API_TOKEN'] = 'SENSITIVE_TEST_TOKEN'
                mutate()
                with patch.object(publisher, '_request') as request:
                    self.assertEqual(publisher.main(['--strict']), 1)
                request.assert_not_called()
                self.assertFalse(self.receipt()['ok'])
                self.assertFalse(self.receipt()['write_attempted'])

    def test_stale_run_is_blocked_until_explicit_force_and_date_preserved(self):
        publisher.MANIFEST_FILE.write_text('{"run_date":"2000-01-01"}')
        with patch.object(publisher, '_request') as request:
            self.assertEqual(publisher.main(['--strict']), 1)
        request.assert_not_called()
        with patch.object(publisher, '_request', side_effect=self.responses()):
            self.assertEqual(publisher.main(['--strict', '--force']), 0)
        self.assertEqual(self.receipt()['run_date'], '2000-01-01')
        self.assertTrue(self.receipt()['forced'])

    def test_network_error_is_nonzero_and_does_not_log_error_details_or_retry_write(self):
        with patch.object(publisher, '_request', side_effect=URLError('SENSITIVE_TEST_TOKEN')) as request:
            self.assertEqual(publisher.main(['--strict']), 1)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('SENSITIVE_TEST_TOKEN', publisher.RECEIPT_FILE.read_text())
        self.assertFalse(self.receipt()['write_attempted'])

    def test_modified_content_version_or_title_fails_without_second_put(self):
        for kwargs in [dict(actual_body='<p>Different content</p>'), dict(actual_version=9), dict(title='Renamed')]:
            with self.subTest(kwargs=kwargs), patch.object(publisher, '_request', side_effect=self.responses(**kwargs)) as request:
                self.assertEqual(publisher.main(['--strict']), 1)
                self.assertEqual(sum(call.args[0] == 'PUT' for call in request.call_args_list), 1)
                self.assertFalse(self.receipt()['ok'])
                self.assertEqual(self.receipt()['stage'], 'read_back')
                self.assertTrue(self.receipt()['write_attempted'])

    def test_server_macro_metadata_does_not_false_fail_content_verification(self):
        publisher.BODY_FILE.write_text('<span data-type="status" data-color="green">Available</span>')
        body = to_storage(publisher.BODY_FILE.read_text())
        actual = body.replace('ac:schema-version="1"', 'ac:schema-version="2" ac:macro-id="server-generated"')
        with patch.object(publisher, '_request', side_effect=self.responses(actual_body=actual)):
            self.assertEqual(publisher.main(['--strict']), 0)
        self.assertFalse(self.receipt()['pages']['123']['raw_body_equal'])
        self.assertTrue(self.receipt()['pages']['123']['ok'])
        self.assertNotEqual(publisher.canonical_storage(actual), publisher.canonical_storage(actual.replace('Available', 'Absent')))
        self.assertNotEqual(publisher.canonical_storage(actual), publisher.canonical_storage(actual + '<table/>'))

    def test_server_named_entities_match_without_weakening_content_checks(self):
        expected = ('<h2>Capacity &#8212; observations</h2><table><tbody><tr>'
                    '<td><a href="https://example.invalid/source">Source</a></td>'
                    '<td>&#8805;8&#160;GPUs</td></tr></tbody></table>')
        actual = expected.replace('&#8212;', '&mdash;').replace('&#8805;', '&ge;').replace('&#160;', '&nbsp;')
        publisher.BODY_FILE.write_text(expected)
        with patch.object(publisher, '_request', side_effect=self.responses(actual_body=actual)):
            self.assertEqual(publisher.main(['--strict']), 0)
        self.assertFalse(self.receipt()['pages']['123']['raw_body_equal'])
        self.assertEqual(publisher.canonical_storage(expected), publisher.canonical_storage(actual))
        changed = [actual.replace('&ge;8', '&ge;4'),
                   actual.replace('https://example.invalid/source', 'https://example.invalid/different'),
                   actual.replace('<td>&ge;', '<th>&ge;').replace('GPUs</td>', 'GPUs</th>'),
                   actual + '<table/>']
        for body in changed:
            with self.subTest(body=body):
                self.assertNotEqual(publisher.canonical_storage(expected), publisher.canonical_storage(body))
                with patch.object(publisher, '_request', side_effect=self.responses(actual_body=body)):
                    self.assertEqual(publisher.main(['--strict']), 1)

    def test_unknown_named_entity_is_rejected_and_does_not_erase_text(self):
        actual = '<p>Observed &unknown_capacity_entity; stock</p>'
        with self.assertRaises(ET.ParseError):
            publisher.canonical_storage(actual)
        with patch.object(publisher, '_request', side_effect=self.responses(actual_body=actual)) as request:
            self.assertEqual(publisher.main(['--strict']), 1)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['GET', 'PUT', 'GET'])
        self.assertFalse(self.receipt()['ok'])
        self.assertEqual(self.receipt()['stage'], 'read_back')

    def test_verify_only_reads_current_body_once_and_never_publishes(self):
        body = to_storage(publisher.BODY_FILE.read_text())
        response = {'title': 'Keep live title', 'version': {'number': 45},
                    'body': {'storage': {'value': body}}}
        with patch.object(publisher, '_request', return_value=response) as request:
            self.assertEqual(publisher.main(['--strict', '--verify-only']), 0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], 'GET')
        self.assertIn('/123?expand=version,body.storage', request.call_args.args[1])
        receipt = self.receipt()
        self.assertTrue(receipt['ok'])
        self.assertFalse(receipt['write_attempted'])
        self.assertEqual(receipt['verification_mode'], 'verify_only')
        self.assertEqual(receipt['stage'], 'verified')
        self.assertIn('verified_at', receipt)
        self.assertNotIn('published_at', receipt)
        self.assertEqual(receipt['pages']['123']['version'], 45)
        self.assertEqual(receipt['pages']['123']['verification'], 'canonical_storage_xml')
        self.assertTrue(receipt['pages']['123']['raw_body_equal'])
        self.assertNotIn('SENSITIVE_TEST_TOKEN', publisher.RECEIPT_FILE.read_text())

    def test_verify_only_mismatch_never_repairs_or_retries_a_write(self):
        response = {'title': 'Keep live title', 'version': {'number': 45},
                    'body': {'storage': {'value': '<p>Different content</p>'}}}
        with patch.object(publisher, '_request', return_value=response) as request:
            self.assertEqual(publisher.main(['--strict', '--verify-only']), 1)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['GET'])
        receipt = self.receipt()
        self.assertFalse(receipt['ok'])
        self.assertFalse(receipt['write_attempted'])
        self.assertEqual(receipt['verification_mode'], 'verify_only')

    def test_verify_only_rejects_missing_body_or_invalid_metadata_without_write(self):
        body = to_storage(publisher.BODY_FILE.read_text())
        invalid = [{'title': 'Keep live title', 'version': {'number': 45}},
                   {'title': '', 'version': {'number': 45}, 'body': {'storage': {'value': body}}},
                   {'title': 'Keep live title', 'version': {'number': '45'}, 'body': {'storage': {'value': body}}}]
        for response in invalid:
            with self.subTest(response=response), patch.object(publisher, '_request', return_value=response) as request:
                self.assertEqual(publisher.main(['--strict', '--verify-only']), 1)
                self.assertEqual([call.args[0] for call in request.call_args_list], ['GET'])
                self.assertFalse(self.receipt()['ok'])
                self.assertFalse(self.receipt()['write_attempted'])

    def test_verify_only_keeps_stale_input_gate_and_dry_run_write_free(self):
        publisher.MANIFEST_FILE.write_text('{"run_date":"2000-01-01"}')
        with patch.object(publisher, '_request') as request:
            self.assertEqual(publisher.main(['--strict', '--verify-only']), 1)
        request.assert_not_called()
        self.assertFalse(self.receipt()['write_attempted'])
        publisher.RECEIPT_FILE.unlink()
        publisher.MANIFEST_FILE.write_text(json.dumps({'run_date': datetime.now(timezone.utc).date().isoformat()}))
        with patch.object(publisher, '_request') as request:
            self.assertEqual(publisher.main(['--strict', '--verify-only', '--dry-run']), 0)
        request.assert_not_called()
        self.assertFalse(publisher.RECEIPT_FILE.exists())

    def test_dry_run_validates_without_credentials_network_or_receipt(self):
        os.environ.pop('CONFLUENCE_API_TOKEN')
        with patch.object(publisher, '_request') as request:
            self.assertEqual(publisher.main(['--strict', '--dry-run']), 0)
        request.assert_not_called()
        self.assertFalse(publisher.RECEIPT_FILE.exists())


if __name__ == '__main__':
    unittest.main()
