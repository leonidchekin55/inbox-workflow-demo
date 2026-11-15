"""Thread-safe, temporary store for fictional demo data only."""
from copy import deepcopy
from datetime import datetime, timezone
import re
from threading import RLock

STATUSES = frozenset({"new", "in_progress", "ready", "done"})
MAX_DRAFT_LENGTH = 2000

TEMPLATES = (
    {"id": "received", "label": "Обращение получено", "text": "Здравствуйте! Спасибо за обращение. Уточню детали и вернусь с ответом."},
    {"id": "clarify", "label": "Нужно уточнение", "text": "Здравствуйте! Подскажите, пожалуйста, дополнительные детали вашего вопроса."},
    {"id": "waiting", "label": "Ожидание ответа", "text": "Здравствуйте! Напоминаю о нашем вопросе. Когда будет удобно, дайте, пожалуйста, ответ."},
)


class VersionConflictError(Exception):
    """The record was modified since the caller loaded it."""


def valid_text(value):
    if not isinstance(value, str) or len(value) > MAX_DRAFT_LENGTH:
        return False
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class DemoStore:
    """No I/O, credentials, integration, side effects, or production-data support."""

    def __init__(self):
        self._lock = RLock()
        self._messages = []
        self._seen_events = set()
        self._sequence = 0
        self._revision = 0

    def _next_version_locked(self):
        self._revision += 1
        return self._revision

    def _snapshot_locked(self):
        return {"messages": deepcopy(self._messages), "templates": deepcopy(list(TEMPLATES))}

    def state(self):
        with self._lock:
            return self._snapshot_locked()

    def load_demo(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        demo = [
            {"id": "demo-1", "platform": "WhatsApp", "account_id": "1", "chat_id": "demo-chat-1", "text": "Учебный пример: хочу уточнить порядок обращения.", "status": "new", "draft": "", "created_at": now},
            {"id": "demo-2", "platform": "Telegram", "account_id": "2", "chat_id": "demo-chat-2", "text": "Учебный пример: подскажите, какие шаги предусмотрены?", "status": "in_progress", "draft": "", "created_at": now},
            {"id": "demo-3", "platform": "MAX", "account_id": "3", "chat_id": "demo-chat-3", "text": "Учебный пример: ожидаю уточнения по запросу.", "status": "ready", "draft": "", "created_at": now},
        ]
        with self._lock:
            for message in demo:
                message["version"] = self._next_version_locked()
            self._messages = demo
            self._seen_events = {
                ("WhatsApp", "1", "demo-event-1"),
                ("Telegram", "2", "demo-event-2"),
                ("MAX", "3", "demo-event-3"),
            }
            self._sequence = 3
            return self._snapshot_locked()

    def ingest_demo_event(self, platform, account_id, event_id, chat_id, text):
        """Insert a fictional internal event at most once per platform and account.

        This method deliberately has no HTTP route or provider integration.
        """
        if (not isinstance(platform, str) or platform not in {"WhatsApp", "Telegram", "MAX"}
                or not isinstance(account_id, str) or account_id not in {"1", "2", "3"}
                or not isinstance(event_id, str)
                or not re.fullmatch(r"demo-event-[0-9]{1,5}", event_id)
                or not isinstance(chat_id, str)
                or not re.fullmatch(r"demo-chat-[0-9]{1,5}", chat_id)
                or not valid_text(text)
                or not text.startswith("Учебный пример:")):
            raise ValueError("invalid demo event")
        key = (platform, account_id, event_id)
        with self._lock:
            if key in self._seen_events:
                return False, self._snapshot_locked()
            self._sequence += 1
            self._messages.append({
                "id": f"demo-{self._sequence}", "platform": platform,
                "account_id": account_id, "chat_id": chat_id, "text": text,
                "status": "new", "draft": "",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "version": self._next_version_locked(),
            })
            self._seen_events.add(key)
            return True, self._snapshot_locked()

    def update(self, message_id, field, value, expected_version):
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("invalid version")
        if field == "draft":
            if not valid_text(value):
                raise ValueError("invalid draft")
        elif field == "status":
            if not isinstance(value, str) or value not in STATUSES:
                raise ValueError("invalid status")
        else:
            raise ValueError("invalid field")
        with self._lock:
            for message in self._messages:
                if message["id"] == message_id:
                    if message["version"] != expected_version:
                        raise VersionConflictError("stale record")
                    message[field] = value
                    message["version"] = self._next_version_locked()
                    return self._snapshot_locked()
        raise KeyError("unknown message")
