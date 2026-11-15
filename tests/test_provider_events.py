import hashlib
import hmac
import unittest

from automation.provider_events import (
    classify_telegram_business_update,
    classify_whatsapp_batch,
    parse_max_update,
    parse_telegram_update,
    parse_whatsapp_update,
    verify_max_secret,
    verify_meta_challenge,
    verify_meta_signature,
    verify_telegram_secret,
)


class AuthenticationTests(unittest.TestCase):
    def test_telegram_and_max_shared_secret_is_required_and_case_insensitive_header(self):
        for verify, header in (
            (verify_telegram_secret, 'X-Telegram-Bot-Api-Secret-Token'),
            (verify_max_secret, 'X-Max-Bot-Api-Secret'),
        ):
            with self.subTest(header=header):
                self.assertTrue(verify({header.lower(): 'example-secret'}, 'example-secret'))
                self.assertFalse(verify({header: 'wrong'}, 'example-secret'))
                self.assertFalse(verify({}, 'example-secret'))
                self.assertFalse(verify({header: ''}, ''))
                self.assertFalse(verify({header: 'x'}, None))
                self.assertFalse(verify({header: 'x', header.lower(): 'x'}, 'x'))
                self.assertFalse(verify({header: 'é'}, 'e'))

    def test_meta_hmac_is_over_unchanged_raw_bytes(self):
        body = b'{"object":"whatsapp_business_account"}'
        signature = 'sha256=' + hmac.new(b'app-secret', body, hashlib.sha256).hexdigest()
        headers = {'x-hub-signature-256': signature}
        self.assertTrue(verify_meta_signature(body, headers, 'app-secret'))
        self.assertFalse(verify_meta_signature(body + b' ', headers, 'app-secret'))
        self.assertFalse(verify_meta_signature(body, headers, 'wrong'))
        self.assertFalse(verify_meta_signature(body, headers, ''))
        self.assertFalse(verify_meta_signature(body.decode(), headers, 'app-secret'))
        self.assertFalse(verify_meta_signature(body, {}, 'app-secret'))
        self.assertFalse(verify_meta_signature(body, {'X-Hub-Signature-256': 'sha1=' + signature[7:]}, 'app-secret'))
        self.assertFalse(verify_meta_signature(body, {'X-Hub-Signature-256': 'sha256=bad'}, 'app-secret'))
        self.assertFalse(verify_meta_signature(body, {'X-Hub-Signature-256': signature,
                                                        'x-hub-signature-256': signature}, 'app-secret'))

    def test_challenge_requires_subscribe_and_matching_nonempty_token(self):
        self.assertEqual(verify_meta_challenge('subscribe', 'verify', 'verify', '90210'), '90210')
        self.assertIsNone(verify_meta_challenge('unsubscribe', 'verify', 'verify', '90210'))
        self.assertIsNone(verify_meta_challenge('subscribe', 'wrong', 'verify', '90210'))
        self.assertIsNone(verify_meta_challenge('subscribe', '', '', '90210'))
        self.assertIsNone(verify_meta_challenge('subscribe', 'verify', 'verify', ''))
        self.assertIsNone(verify_meta_challenge('subscribe', 'verify', 'verify', None))


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.message = {'message_id': 10, 'chat': {'id': 101, 'type': 'private'},
                        'from': {'id': 101, 'is_bot': False}, 'text': 'Synthetic question'}

    def test_private_text_and_account_is_from_configuration(self):
        update = {'update_id': 44, 'message': dict(self.message, account_id='untrusted')}
        expected = {'platform': 'telegram', 'account_id': 'configured-1', 'event_id': '44',
                    'provider_message_id': '10', 'chat_id': '101', 'text': 'Synthetic question'}
        self.assertEqual(parse_telegram_update(update, 'configured-1'), [expected])
        other = parse_telegram_update(update, 'configured-2')[0]
        self.assertEqual(other['account_id'], 'configured-2')
        with self.assertRaises(ValueError):
            parse_telegram_update(update, '')

    def test_business_binding_and_outgoing_or_unsupported_are_ignored(self):
        business = dict(self.message, business_connection_id='business-1')
        update = {'update_id': 45, 'business_message': business}
        self.assertEqual(parse_telegram_update(update, 'one'), [])
        self.assertEqual(parse_telegram_update(update, 'one', expected_business_connection_id='other'), [])
        events = parse_telegram_update(update, 'one', expected_business_connection_id='business-1')
        self.assertEqual(events[0]['business_connection_id'], 'business-1')
        outgoing = dict(business, **{'from': {'id': 999, 'is_bot': False}})
        self.assertEqual(parse_telegram_update({'update_id': 46, 'business_message': outgoing}, 'one',
                                               expected_business_connection_id='business-1'), [])
        self.assertEqual(parse_telegram_update(update, 'one', expected_business_connection_id='business-1',
                                               own_user_id=101), [])
        self.assertEqual(parse_telegram_update({'update_id': 47, 'edited_business_message': business}, 'one'), [])
        bot = dict(self.message, **{'from': {'id': 101, 'is_bot': True}})
        self.assertEqual(parse_telegram_update({'update_id': 48, 'message': bot}, 'one'), [])
        media = dict(self.message, photo=[{'file_id': 'example'}])
        self.assertEqual(parse_telegram_update({'update_id': 49, 'message': media}, 'one'), [])
        self.assertEqual(parse_telegram_update({'update_id': 50, 'message': dict(self.message, text=None)}, 'one'), [])
        self.assertEqual(parse_telegram_update({'update_id': 51, 'message': self.message,
                                                 'business_message': business}, 'one'), [])


class MaxTests(unittest.TestCase):
    def setUp(self):
        self.message = {'sender': {'user_id': 101, 'is_bot': False},
                        'recipient': {'chat_id': 202, 'chat_type': 'dialog', 'user_id': 999},
                        'body': {'mid': 'max-event', 'text': 'Test text', 'attachments': None}}

    def test_private_message_and_media_bot_and_other_updates(self):
        update = {'update_type': 'message_created', 'message': self.message}
        result = parse_max_update(update, 'max-account')
        self.assertEqual(result, [{'platform': 'max', 'account_id': 'max-account',
                                   'event_id': 'max-event', 'chat_id': '202', 'text': 'Test text'}])
        self.assertEqual(parse_max_update(update, 'max-account', own_user_id=101), [])
        bot = dict(self.message, sender={'user_id': 999, 'is_bot': True})
        self.assertEqual(parse_max_update({'update_type': 'message_created', 'message': bot}, 'a'), [])
        missing_flag = dict(self.message, sender={'user_id': 101})
        self.assertEqual(parse_max_update({'update_type': 'message_created', 'message': missing_flag}, 'a'), [])
        media = dict(self.message, body={'mid': 'm2', 'text': 'caption', 'attachments': [{'type': 'image'}]})
        self.assertEqual(parse_max_update({'update_type': 'message_created', 'message': media}, 'a'), [])
        self.assertEqual(parse_max_update({'update_type': 'message_edited', 'message': self.message}, 'a'), [])
        self.assertEqual(parse_max_update({'update_type': 'message_created', 'message': None}, 'a'), [])
        group = dict(self.message, recipient={'chat_id': 202, 'chat_type': 'chat'})
        self.assertEqual(parse_max_update({'update_type': 'message_created', 'message': group}, 'a'), [])


class WhatsAppTests(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {'from': '101', 'id': 'wamid.1', 'type': 'text', 'text': {'body': 'First'}},
            {'from': '102', 'id': 'wamid.2', 'type': 'text', 'text': {'body': 'Second'}},
            {'from': '103', 'id': 'wamid.3', 'type': 'image', 'image': {'caption': 'skip'}},
            {'from': '15550001111', 'id': 'wamid.self', 'type': 'text', 'text': {'body': 'self'}},
        ]
        self.payload = {'object': 'whatsapp_business_account', 'entry': [
            {'changes': [{'field': 'messages', 'value': {
                'metadata': {'phone_number_id': 'phone-1', 'display_phone_number': '+1 555 000 1111'},
                'messages': self.messages,
            }}]},
            {'changes': [{'field': 'messages', 'value': {
                'metadata': {'phone_number_id': 'phone-2'},
                'messages': [{'from': '104', 'id': 'wamid.4', 'type': 'text', 'text': {'body': 'Other'}}],
            }}]},
        ]}

    def test_batched_messages_and_account_separation(self):
        rows = parse_whatsapp_update(self.payload, 'configured-a', expected_phone_number_id='phone-1')
        self.assertEqual([r['event_id'] for r in rows], ['wamid.1', 'wamid.2'])
        self.assertTrue(all(r['account_id'] == 'configured-a' for r in rows))
        rows_b = parse_whatsapp_update(self.payload, 'configured-b', expected_phone_number_id='phone-2')
        self.assertEqual(rows_b, [{'platform': 'whatsapp', 'account_id': 'configured-b',
                                  'event_id': 'wamid.4', 'chat_id': '104', 'text': 'Other'}])
        self.assertEqual(parse_whatsapp_update(self.payload, 'configured-a'), [])
        self.assertEqual(parse_whatsapp_update(self.payload, 'configured-a', expected_phone_number_id='nope'), [])
        self.assertEqual([r['event_id'] for r in parse_whatsapp_update(
            self.payload, 'configured-a', expected_phone_number_id='phone-1', own_phone_number='101')], ['wamid.2'])

    def test_malformed_and_status_only_updates_are_ignored(self):
        for value in (None, [], {'object': 'whatsapp_business_account', 'entry': [None, {'changes': [None]}]},
                      {'object': 'other', 'entry': self.payload['entry']},
                      {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages',
                       'value': {'metadata': {'phone_number_id': 'phone-1'}, 'statuses': [{'id': 'x'}]}}]}]}):
            with self.subTest(value=value):
                self.assertEqual(parse_whatsapp_update(value, 'a', expected_phone_number_id='phone-1'), [])


class TelegramClassificationTests(unittest.TestCase):
    def setUp(self):
        self.bindings = {'conn-1': 'account-1'}
        self.message = {'message_id': 77, 'business_connection_id': 'conn-1',
                        'chat': {'id': 101, 'type': 'private'},
                        'from': {'id': 101, 'is_bot': False}, 'text': 'Question'}

    def test_known_message_retains_message_id_separately_from_update_id(self):
        result = classify_telegram_business_update({'update_id': 500, 'business_message': self.message}, self.bindings)
        self.assertEqual(result['kind'], 'message')
        self.assertEqual(result['account_id'], 'account-1')
        self.assertEqual(result['events'][0]['event_id'], '500')
        self.assertEqual(result['events'][0]['provider_message_id'], '77')
        self.assertEqual(result['events'][0]['business_connection_id'], 'conn-1')

    def test_unknown_connection_is_not_ignored(self):
        update = {'update_id': 501, 'business_message': dict(self.message, business_connection_id='conn-new')}
        self.assertEqual(classify_telegram_business_update(update, self.bindings),
                         {'kind': 'unknown_connection', 'event_type': 'business_message',
                          'connection_id': 'conn-new'})
        missing = {'update_id': 501, 'business_message': dict(self.message, business_connection_id=None)}
        self.assertEqual(classify_telegram_business_update(missing, self.bindings)['kind'], 'malformed')

    def test_connection_rights_and_unsupported_edits_deletes_are_exposed(self):
        update = {'update_id': 502, 'business_connection': {'id': 'conn-1', 'is_enabled': False,
                                                             'rights': {'can_reply': True}}}
        result = classify_telegram_business_update(update, self.bindings)
        self.assertEqual(result['kind'], 'connection_change')
        self.assertIs(result['enabled'], False)
        self.assertIs(result['can_reply'], True)
        unknown = {'update_id': 503, 'business_connection': {'id': 'conn-other', 'is_enabled': True}}
        self.assertEqual(classify_telegram_business_update(unknown, self.bindings)['kind'], 'unknown_connection')
        for kind in ('edited_business_message', 'deleted_business_messages'):
            unsupported = {'update_id': 504, kind: {'business_connection_id': 'conn-1'}}
            self.assertEqual(classify_telegram_business_update(unsupported, self.bindings)['kind'], 'unsupported')
            unsupported[kind]['business_connection_id'] = 'missing'
            self.assertEqual(classify_telegram_business_update(unsupported, self.bindings)['kind'], 'unknown_connection')

    def test_skipped_and_malformed_are_distinct(self):
        image = dict(self.message, text=None, photo=[{'file_id': 'example'}])
        self.assertEqual(classify_telegram_business_update({'update_id': 505, 'business_message': image}, self.bindings)['kind'], 'ignored')
        missing_from = dict(self.message)
        del missing_from['from']
        self.assertEqual(classify_telegram_business_update({'update_id': 506, 'business_message': missing_from}, self.bindings)['kind'], 'malformed')
        self.assertEqual(classify_telegram_business_update({'update_id': 507, 'message': {}}, self.bindings)['kind'], 'ignored')
        self.assertEqual(classify_telegram_business_update({'update_id': 508, 'business_message': self.message,
                                                              'business_connection': {}}, self.bindings)['kind'], 'malformed')
        for other in ('edited_message', 'callback_query', 'poll', 'subscription', 'stopped_message_generation'):
            combined = {'update_id': 509, 'business_message': self.message, other: {}}
            with self.subTest(other=other):
                self.assertEqual(classify_telegram_business_update(combined, self.bindings)['kind'], 'malformed')
                self.assertEqual(parse_telegram_update(combined, 'account-1', expected_business_connection_id='conn-1'), [])
        unknown_field = {'update_id': 510, 'business_message': self.message, 'new_update_type': {}}
        self.assertEqual(classify_telegram_business_update(unknown_field, self.bindings)['kind'], 'malformed')
        with self.assertRaises(ValueError):
            classify_telegram_business_update({'update_id': 1}, {' ': 'account'})


class WhatsAppBatchClassificationTests(unittest.TestCase):
    @staticmethod
    def change(phone, messages):
        metadata = {} if phone is None else {'phone_number_id': phone}
        return {'field': 'messages', 'value': {'metadata': metadata, 'messages': messages}}

    def test_routes_all_configured_accounts_and_reports_unknown_or_missing(self):
        def text(msg_id, sender):
            return {'type': 'text', 'id': msg_id, 'from': sender, 'text': {'body': 'Example'}}
        changes = [self.change('phone-1', [text('one', '11'), {'type': 'image', 'id': 'pic', 'from': '11'}]),
                   self.change('phone-2', [text('two', '22')]),
                   self.change('phone-3', [text('unknown', '33')]),
                   self.change('phone-3', [text('unknown-2', '33')]),
                   self.change(None, [text('missing', '44')]),
                   self.change('phone-1', [{'type': 'text', 'id': 'bad', 'from': '11', 'text': {}}]),
                   {'field': 'other', 'value': {}}, None]
        payload = {'object': 'whatsapp_business_account', 'entry': [{'changes': changes}]}
        result = classify_whatsapp_batch(payload, {'phone-1': 'A', 'phone-2': 'B'})
        self.assertEqual([(e['event_id'], e['account_id']) for e in result['events']], [('one', 'A'), ('two', 'B')])
        self.assertEqual(result['unknown_phone_number_ids'], ['phone-3'])
        self.assertEqual(result['missing_phone_number_ids'], 1)
        self.assertEqual(result['malformed_items'], 2)
        self.assertEqual(result['unsupported_messages'], 1)
        self.assertEqual(result['ignored_changes'], 1)

    def test_status_only_and_empty_value_are_distinct(self):
        def payload(value):
            return {'object': 'whatsapp_business_account', 'entry': [{'changes': [
                {'field': 'messages', 'value': {'metadata': {'phone_number_id': 'phone-1'}, **value}}]}]}
        settings = {'phone-1': 'account-1'}
        status = {'id': 'wamid.example', 'status': 'delivered'}
        only_status = classify_whatsapp_batch(payload({'statuses': [status, status]}), settings)
        self.assertEqual(only_status['status_events'], 2)
        self.assertEqual(only_status['malformed_items'], 0)
        self.assertEqual(only_status['events'], [])
        empty = classify_whatsapp_batch(payload({}), settings)
        self.assertEqual(empty['status_events'], 0)
        self.assertEqual(empty['malformed_items'], 1)
        for value in ({'statuses': []}, {'messages': []}, {'statuses': None}):
            with self.subTest(value=value):
                self.assertEqual(classify_whatsapp_batch(payload(value), settings)['malformed_items'], 1)
        invalid = classify_whatsapp_batch(payload({'statuses': [None, {'id': 'x'}]}), settings)
        self.assertEqual(invalid['malformed_items'], 2)
        self.assertEqual(invalid['status_events'], 0)
        mixed = classify_whatsapp_batch(payload({'statuses': [status], 'messages': [
            {'id': 'received', 'from': '101', 'type': 'text', 'text': {'body': 'Hi'}}]}), settings)
        self.assertEqual(mixed['status_events'], 1)
        self.assertEqual([event['event_id'] for event in mixed['events']], ['received'])

    def test_fail_closed_for_bad_config_and_classify_malformed_containers(self):
        for binding in (None, [], {'': 'account'}, {'phone': ''}):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                classify_whatsapp_batch({}, binding)
        self.assertEqual(classify_whatsapp_batch(None, {})['malformed_items'], 1)
        payload = {'object': 'whatsapp_business_account', 'entry': [None, {'changes': [None, {'field': 'messages', 'value': None}]}]}
        self.assertEqual(classify_whatsapp_batch(payload, {})['malformed_items'], 3)
        with self.assertRaises(ValueError):
            classify_whatsapp_batch(payload, {}, own_phone_numbers=[])


if __name__ == '__main__':
    unittest.main()
