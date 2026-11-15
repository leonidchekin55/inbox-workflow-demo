"""Pure, fail-closed webhook helpers. No I/O, network access or persistence.

Authentication and parsing are separate operations: callers must authenticate the
original bytes before decoding or using normalized events. Mapping one public
webhook endpoint to an authorized local account is the caller's responsibility.
A successful parse alone must never authorize storage, external processing or send.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any


# Bot API Update fields, verified against the official list. Unknown top-level
# fields fail closed, so a future Bot API update requires an explicit review.
_TELEGRAM_UPDATE_FIELDS = frozenset({
    'message', 'edited_message', 'channel_post', 'edited_channel_post',
    'business_connection', 'business_message', 'edited_business_message',
    'deleted_business_messages', 'guest_message', 'message_reaction',
    'message_reaction_count', 'inline_query', 'chosen_inline_result',
    'callback_query', 'widget_query', 'shared_widget_query', 'shipping_query',
    'pre_checkout_query', 'purchased_paid_media', 'poll', 'poll_answer',
    'my_chat_member', 'chat_member', 'chat_join_request', 'chat_boost',
    'removed_chat_boost', 'managed_bot', 'subscription', 'stopped_message_generation',
})


def _valid_telegram_update_shape(update: Mapping[str, Any]) -> bool:
    if any(key != 'update_id' and key not in _TELEGRAM_UPDATE_FIELDS for key in update):
        return False
    return sum(key in update for key in _TELEGRAM_UPDATE_FIELDS) <= 1


def _secret_bytes(value: object) -> bytes | None:
    if not isinstance(value, str) or not value or not value.strip():
        return None
    try:
        return value.encode('utf-8')
    except UnicodeEncodeError:
        return None


def _single_header(headers: object, name: str) -> str | None:
    """Reject ambiguous differently-cased duplicate keys rather than pick one."""
    if not isinstance(headers, Mapping):
        return None
    matches = [value for key, value in headers.items()
               if isinstance(key, str) and key.lower() == name.lower()]
    if len(matches) != 1 or not isinstance(matches[0], str):
        return None
    return matches[0]


def _compare(actual: object, expected: object) -> bool:
    left, right = _secret_bytes(actual), _secret_bytes(expected)
    return left is not None and right is not None and hmac.compare_digest(left, right)


def verify_telegram_secret(headers: object, expected_secret: object) -> bool:
    """Check the secret configured in Telegram's setWebhook; missing fails closed."""
    return _compare(_single_header(headers, 'X-Telegram-Bot-Api-Secret-Token'), expected_secret)


def verify_max_secret(headers: object, expected_secret: object) -> bool:
    """Check the shared secret supplied on MAX subscription; missing fails closed."""
    return _compare(_single_header(headers, 'X-Max-Bot-Api-Secret'), expected_secret)


def verify_meta_signature(raw_body: object, headers: object, app_secret: object) -> bool:
    """Verify X-Hub-Signature-256 over untouched bytes with the Meta app secret."""
    key = _secret_bytes(app_secret)
    signature = _single_header(headers, 'X-Hub-Signature-256')
    if not isinstance(raw_body, bytes) or key is None or not isinstance(signature, str):
        return False
    if re.fullmatch(r'sha256=[0-9a-f]{64}', signature) is None:
        return False
    expected = 'sha256=' + hmac.new(key, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def verify_meta_challenge(mode: object, received_token: object,
                          expected_token: object, challenge: object) -> str | None:
    """Return challenge verbatim only for an authorized subscribe handshake."""
    if mode != 'subscribe' or not isinstance(challenge, str) or not challenge:
        return None
    return challenge if _compare(received_token, expected_token) else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _identifier(value: object) -> str | None:
    if type(value) is int:
        return str(value)
    if isinstance(value, str) and value and value.strip():
        return value
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _account(value: object) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError('account_id must be a non-empty configured string')
    return value


def parse_telegram_update(payload: object, account_id: object, *,
                          expected_business_connection_id: object = None,
                          own_user_id: object = None) -> list[dict[str, str]]:
    """Normalize private text updates, guarding account binding and outbound echo.

    Business updates are accepted only with an explicit expected connection ID.
    This handles current new text messages only, not edits, deletions or history.
    For private chats, requiring the sender to match the chat peer rejects messages
    sent by the business owner and unidentified senders without a separate API call.
    """
    account = _account(account_id)
    update = _mapping(payload)
    event_id = _identifier(update.get('update_id'))
    if event_id is None or not _valid_telegram_update_shape(update):
        return []
    is_business = 'business_message' in update
    # A Telegram Update contains at most one update type; reject ambiguous inputs.
    if is_business and 'message' in update:
        return []
    message = _mapping(update.get('business_message' if is_business else 'message'))
    chat, sender = _mapping(message.get('chat')), _mapping(message.get('from'))
    chat_id, sender_id = _identifier(chat.get('id')), _identifier(sender.get('id'))
    if (chat.get('type') != 'private' or not chat_id or not sender_id
            or chat_id != sender_id or sender.get('is_bot') is not False
            or _identifier(message.get('message_id')) is None):
        return []
    own = _identifier(own_user_id)
    if own and sender_id == own:
        return []
    text = _text(message.get('text'))
    media_fields = ('photo', 'document', 'audio', 'voice', 'video', 'sticker', 'animation',
                    'contact', 'location', 'video_note', 'poll', 'dice', 'venue',
                    'invoice', 'game', 'story', 'paid_media')
    if text is None or any(message.get(field) is not None for field in media_fields):
        return []
    result = {'platform': 'telegram', 'account_id': account,
              'event_id': event_id, 'provider_message_id': _identifier(message.get('message_id')),
              'chat_id': chat_id, 'text': text}
    if is_business:
        actual = _identifier(message.get('business_connection_id'))
        expected = _identifier(expected_business_connection_id)
        if not actual or not expected or actual != expected:
            return []
        result['business_connection_id'] = actual
    return [result]


def parse_max_update(payload: object, account_id: object, *,
                     own_user_id: object = None) -> list[dict[str, str]]:
    """Normalize identified private text-only message_created updates from MAX.

    MAX documents recipient.chat_id as nullable. A private message without a
    chat_id is deliberately not normalized; a production caller needs a verified
    separate routing policy and must not silently ACK it as a processed message.
    """
    account = _account(account_id)
    update = _mapping(payload)
    if update.get('update_type') != 'message_created':
        return []
    message = _mapping(update.get('message'))
    sender, recipient, body = (_mapping(message.get(k)) for k in ('sender', 'recipient', 'body'))
    sender_id = _identifier(sender.get('user_id'))
    chat_id, event_id, text = (_identifier(recipient.get('chat_id')),
                               _identifier(body.get('mid')), _text(body.get('text')))
    if (recipient.get('chat_type') != 'dialog' or sender.get('is_bot') is not False
            or not sender_id or not chat_id or not event_id or text is None):
        return []
    if body.get('attachments') not in (None, []):
        return []
    own = _identifier(own_user_id)
    if own and sender_id == own:
        return []
    recipient_user = _identifier(recipient.get('user_id'))
    if recipient_user and sender_id == recipient_user:
        return []
    return [{'platform': 'max', 'account_id': account, 'event_id': event_id,
             'chat_id': chat_id, 'text': text}]


def parse_whatsapp_update(payload: object, account_id: object, *,
                          expected_phone_number_id: object = None,
                          own_phone_number: object = None) -> list[dict[str, str]]:
    """Normalize inbound WhatsApp text messages after checking configured number.

    Meta may batch multiple entry/changes/messages. Metadata is an additional
    account binding check, not an authenticated identity by itself; the caller
    must first verify the HMAC over the raw request. Status-only events, media,
    wrong numbers and malformed nested objects are ignored.
    """
    account = _account(account_id)
    expected = _identifier(expected_phone_number_id)
    own = _identifier(own_phone_number)
    root = _mapping(payload)
    if root.get('object') != 'whatsapp_business_account' or not expected:
        return []
    entries = root.get('entry')
    if not isinstance(entries, list):
        return []
    events: list[dict[str, str]] = []
    for entry in entries:
        changes = _mapping(entry).get('changes')
        if not isinstance(changes, list):
            continue
        for change in changes:
            change = _mapping(change)
            if change.get('field') != 'messages':
                continue
            value = _mapping(change.get('value'))
            metadata = _mapping(value.get('metadata'))
            actual = _identifier(metadata.get('phone_number_id'))
            display = metadata.get('display_phone_number')
            display_digits = re.sub(r'[^0-9]', '', display) if isinstance(display, str) else ''
            if actual != expected:
                continue
            messages = value.get('messages')
            if not isinstance(messages, list):
                continue
            for item in messages:
                message = _mapping(item)
                if message.get('type') != 'text':
                    continue
                event_id = _identifier(message.get('id'))
                chat_id = _identifier(message.get('from'))
                text = _text(_mapping(message.get('text')).get('body'))
                if (not event_id or not chat_id or text is None
                        or (own and chat_id == own)
                        or (display_digits and chat_id == display_digits)):
                    continue
                events.append({'platform': 'whatsapp', 'account_id': account,
                               'event_id': event_id, 'chat_id': chat_id, 'text': text})
    return events


def _bindings(configured: object) -> dict[str, str]:
    """Validate explicit provider-ID to local-account bindings from configuration."""
    if not isinstance(configured, Mapping):
        raise ValueError('bindings must be a mapping')
    result: dict[str, str] = {}
    for provider_id, account in configured.items():
        if not isinstance(provider_id, str) or not provider_id or not provider_id.strip():
            raise ValueError('provider binding must be a non-empty string')
        result[provider_id] = _account(account)
    return result


def classify_whatsapp_batch(payload: object, phone_accounts: object, *,
                            own_phone_numbers: object = None) -> dict[str, Any]:
    """Route one Meta batch to all configured phone IDs without hiding unknown IDs.

    Return normalized text events, unique unknown IDs, a count of changes without
    a phone ID, and a count of structurally malformed entries/changes. Only
    ``messages`` changes are relevant; unrelated change types are counted separately.
    This is classification, not authentication, durable receipt or an ACK decision.
    The caller must authenticate raw request bytes before calling this and must not
    confirm delivery until every batch item is handled under a deliberate policy.
    """
    bindings = _bindings(phone_accounts)
    if own_phone_numbers is not None and not isinstance(own_phone_numbers, Mapping):
        raise ValueError('own_phone_numbers must be a mapping when supplied')
    own = {} if own_phone_numbers is None else own_phone_numbers
    result: dict[str, Any] = {'events': [], 'unknown_phone_number_ids': [],
                              'missing_phone_number_ids': 0, 'malformed_items': 0,
                              'ignored_changes': 0, 'unsupported_messages': 0,
                              'ignored_messages': 0, 'status_events': 0}
    root = _mapping(payload)
    entries = root.get('entry')
    if root.get('object') != 'whatsapp_business_account' or not isinstance(entries, list):
        result['malformed_items'] += 1
        return result
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get('changes'), list):
            result['malformed_items'] += 1
            continue
        for change in entry['changes']:
            if not isinstance(change, Mapping):
                result['malformed_items'] += 1
                continue
            if not isinstance(change.get('field'), str):
                result['malformed_items'] += 1
                continue
            if change.get('field') != 'messages':
                result['ignored_changes'] += 1
                continue
            value = change.get('value')
            if not isinstance(value, Mapping):
                result['malformed_items'] += 1
                continue
            provider_id = _identifier(_mapping(value.get('metadata')).get('phone_number_id'))
            if provider_id is None:
                result['missing_phone_number_ids'] += 1
                continue
            if provider_id not in bindings:
                if provider_id not in result['unknown_phone_number_ids']:
                    result['unknown_phone_number_ids'].append(provider_id)
                continue
            messages, statuses = value.get('messages'), value.get('statuses')
            if messages is None and statuses is None:
                result['malformed_items'] += 1
                continue
            if statuses is not None:
                if not isinstance(statuses, list):
                    result['malformed_items'] += 1
                else:
                    for status in statuses:
                        if (not isinstance(status, Mapping)
                                or _identifier(status.get('id')) is None
                                or not isinstance(status.get('status'), str)
                                or not status['status'].strip()):
                            result['malformed_items'] += 1
                        else:
                            result['status_events'] += 1
            if messages is not None and not isinstance(messages, list):
                result['malformed_items'] += 1
                continue
            if (messages is None or messages == []) and (statuses is None or statuses == []):
                result['malformed_items'] += 1
                continue
            for item in messages or []:
                if (not isinstance(item, Mapping) or not isinstance(item.get('type'), str)
                        or _identifier(item.get('id')) is None or _identifier(item.get('from')) is None):
                    result['malformed_items'] += 1
                    continue
                if item['type'] != 'text':
                    result['unsupported_messages'] += 1
                    continue
                if _text(_mapping(item.get('text')).get('body')) is None:
                    result['malformed_items'] += 1
                    continue
                # Reuse narrow filtering for each valid item, without dropping
                # malformed siblings or events from another configured account.
                single_value = dict(value)
                single_value['messages'] = [item]
                single = {'object': 'whatsapp_business_account',
                          'entry': [{'changes': [{'field': 'messages', 'value': single_value}]}]}
                events = parse_whatsapp_update(
                    single, bindings[provider_id], expected_phone_number_id=provider_id,
                    own_phone_number=own.get(provider_id))
                if not events:
                    result['ignored_messages'] += 1
                result['events'].extend(events)
    return result


def classify_telegram_business_update(payload: object, connections: object) -> dict[str, Any]:
    """Classify a Telegram Business update using pre-authorized connection bindings.

    ``kind`` is one of message, connection_change, unknown_connection, unsupported,
    ignored, or malformed. In particular, unknown connection IDs are never reported
    as ordinary ignored updates. Changes of permission and edits/deletions are
    surfaced for the caller to handle; no state is modified here.
    """
    bindings = _bindings(connections)
    if (not isinstance(payload, Mapping) or _identifier(payload.get('update_id')) is None
            or not _valid_telegram_update_shape(payload)):
        return {'kind': 'malformed'}
    event_types = ('business_connection', 'business_message',
                   'edited_business_message', 'deleted_business_messages')
    present = [name for name in event_types if name in payload]
    if not present:
        return {'kind': 'ignored', 'reason': 'not_business_update'}
    if len(present) != 1 or 'message' in payload:
        return {'kind': 'malformed'}
    event_type = present[0]
    data = payload.get(event_type)
    if not isinstance(data, Mapping):
        return {'kind': 'malformed', 'event_type': event_type}
    key = 'id' if event_type == 'business_connection' else 'business_connection_id'
    connection_id = _identifier(data.get(key))
    if connection_id is None:
        return {'kind': 'malformed', 'event_type': event_type}
    if event_type == 'business_connection' and type(data.get('is_enabled')) is not bool:
        return {'kind': 'malformed', 'event_type': event_type, 'connection_id': connection_id}
    if connection_id not in bindings:
        return {'kind': 'unknown_connection', 'event_type': event_type,
                'connection_id': connection_id}
    account = bindings[connection_id]
    context = {'event_type': event_type, 'connection_id': connection_id, 'account_id': account}
    if event_type == 'business_connection':
        rights = data.get('rights')
        if rights is not None and not isinstance(rights, Mapping):
            return {'kind': 'malformed', **context}
        return {'kind': 'connection_change', **context, 'enabled': data['is_enabled'],
                'can_reply': _mapping(rights).get('can_reply') is True}
    if event_type != 'business_message':
        return {'kind': 'unsupported', **context}
    chat = data.get('chat')
    sender = data.get('from')
    if not isinstance(chat, Mapping) or not isinstance(chat.get('type'), str):
        return {'kind': 'malformed', **context}
    # Unsupported non-private chats and non-text messages are intentionally skipped.
    if chat['type'] != 'private' or data.get('text') is None:
        return {'kind': 'ignored', **context, 'reason': 'unsupported_message'}
    if (not isinstance(sender, Mapping) or _identifier(sender.get('id')) is None
            or type(sender.get('is_bot')) is not bool or _identifier(chat.get('id')) is None
            or _identifier(data.get('message_id')) is None or not isinstance(data.get('text'), str)):
        return {'kind': 'malformed', **context}
    events = parse_telegram_update(payload, account,
                                   expected_business_connection_id=connection_id)
    if not events:
        return {'kind': 'ignored', **context, 'reason': 'unsupported_or_outgoing'}
    return {'kind': 'message', **context, 'events': events}
