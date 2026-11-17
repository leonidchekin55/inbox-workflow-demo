"""SMTP delivery of alert metadata with conservative handling of unknown outcomes.

No messages or credentials are stored. SMTP acceptance confirms server acceptance,
not delivery to the mailbox. A previously interrupted attempt requires review.
"""

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.headerregistry import Address
from email.message import EmailMessage
import fcntl
import os
import smtplib
import sqlite3
import ssl


@dataclass(frozen=True)
class EmailSettings:
    host: str
    port: int
    sender: str
    recipient: str
    username: str = ''
    password: str = field(default='', repr=False)
    security: str = 'starttls'

    def __post_init__(self):
        for name in ('host', 'sender', 'recipient'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n'):
                raise ValueError(f'{name} must be a non-empty single-line string')
        for name in ('sender', 'recipient'):
            try:
                parsed = Address(addr_spec=getattr(self, name))
            except (ValueError, IndexError) as exc:
                raise ValueError(f'{name} must be a single email address') from exc
            if not parsed.username or not parsed.domain or parsed.addr_spec != getattr(self, name):
                raise ValueError(f'{name} must be a single email address')
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise ValueError('port must be between 1 and 65535')
        if self.security not in ('starttls', 'ssl'):
            raise ValueError('SMTP requires starttls or ssl')
        if not isinstance(self.username, str) or not isinstance(self.password, str):
            raise ValueError('credentials must be strings')
        if bool(self.username) != bool(self.password):
            raise ValueError('both username and password are required')


def make_message(settings, alert):
    message = EmailMessage()
    message['From'] = settings.sender
    message['To'] = settings.recipient
    message['Subject'] = 'Новое обращение клиента'
    platform = str(alert['platform']).casefold()
    name = {'telegram': 'Telegram', 'org.telegram.messenger': 'Telegram',
            'whatsapp': 'WhatsApp', 'com.whatsapp': 'WhatsApp', 'com.whatsapp.w4b': 'WhatsApp',
            'max': 'MAX', 'ru.oneme.app': 'MAX'}.get(platform, 'мессенджере')
    kind = 'звонок' if alert['kind'] == 'call' else 'сообщение'
    message.set_content(f'Новое обращение в {name}: {kind}. Откройте соответствующее приложение для просмотра.\n')
    return message


class EmailDelivery:
    """A single-recipient queue consumer; one instance may run per database.

    Network failures before send are retryable. Once the SMTP send has begun,
    interruptions are marked uncertain and held until a person resolves them.
    All connections validate the server certificate.
    """

    def __init__(self, queue, settings, *, smtp_class=smtplib.SMTP, smtp_ssl_class=smtplib.SMTP_SSL):
        self.queue = queue
        self.settings = settings
        self.smtp_class = smtp_class
        self.smtp_ssl_class = smtp_ssl_class
        with sqlite3.connect(self.queue.database) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS email_attempts (
                alert_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN ('started', 'retryable', 'uncertain', 'accepted')),
                updated_at TEXT NOT NULL,
                FOREIGN KEY(alert_id) REFERENCES daily_alerts(id)
            )''')

    @contextmanager
    def _exclusive(self):
        descriptor = os.open(self.queue.database + '.mail.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _begin(self, alert, now):
        timestamp = now.astimezone(timezone.utc).isoformat(timespec='microseconds')
        with sqlite3.connect(self.queue.database, isolation_level=None) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute('''SELECT e.state, a.status, a.lease_token FROM daily_alerts AS a
                    LEFT JOIN email_attempts AS e ON a.id = e.alert_id WHERE a.id = ?''', (alert['id'],)).fetchone()
                if row is None or row[1] != 'inflight' or row[2] != alert['lease_token']:
                    conn.rollback()
                    return 'stale'
                if row[0] in ('accepted', 'started', 'uncertain'):
                    conn.commit()
                    return row[0]
                conn.execute('''INSERT INTO email_attempts (alert_id, state, updated_at) VALUES (?, 'started', ?)
                    ON CONFLICT(alert_id) DO UPDATE SET state = 'started', updated_at = excluded.updated_at''',
                    (alert['id'], timestamp))
                conn.commit()
                return 'ready'
            except Exception:
                conn.rollback()
                raise

    def _finish(self, alert, now, outcome):
        """Atomically fence retries and never overwrite a server acceptance."""
        timestamp = now.astimezone(timezone.utc).isoformat(timespec='microseconds')
        retry_at = (datetime.max.replace(tzinfo=timezone.utc) if outcome == 'uncertain'
                    else (now + timedelta(minutes=5)).astimezone(timezone.utc)).isoformat(timespec='microseconds')
        with sqlite3.connect(self.queue.database, isolation_level=None) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute('''SELECT e.state, a.status, a.lease_token FROM daily_alerts AS a
                    LEFT JOIN email_attempts AS e ON a.id = e.alert_id WHERE a.id = ?''', (alert['id'],)).fetchone()
                if row is None:
                    conn.rollback()
                    return 'stale'
                if outcome == 'accepted' or row[0] == 'accepted':
                    conn.execute('''INSERT INTO email_attempts (alert_id, state, updated_at) VALUES (?, 'accepted', ?)
                        ON CONFLICT(alert_id) DO UPDATE SET state = 'accepted', updated_at = excluded.updated_at''',
                        (alert['id'], timestamp))
                    conn.execute("UPDATE daily_alerts SET status = 'delivered', delivered_at = ?, lease_token = NULL, lease_until = NULL, next_attempt_at = NULL WHERE id = ?", (timestamp, alert['id']))
                    conn.commit()
                    return 'accepted'
                if row[1] != 'inflight' or row[2] != alert['lease_token']:
                    conn.rollback()
                    return 'stale'
                conn.execute('''INSERT INTO email_attempts (alert_id, state, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(alert_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at''',
                    (alert['id'], outcome, timestamp))
                conn.execute("UPDATE daily_alerts SET status = 'pending', next_attempt_at = ?, lease_token = NULL, lease_until = NULL WHERE id = ?", (retry_at, alert['id']))
                conn.commit()
                return outcome
            except Exception:
                conn.rollback()
                raise

    def run_one(self, *, now):
        """Process one queued alert and return its state; never called automatically."""
        with self._exclusive() as acquired:
            if not acquired:
                return 'busy'
            return self._run_one(now=now)

    def _run_one(self, *, now):
        alert = self.queue.claim_next(now=now, lease_seconds=120)
        if alert is None:
            return 'empty'
        state = self._begin(alert, now)
        if state == 'stale':
            return 'stale'
        if state == 'accepted':
            return self._finish(alert, now, 'accepted')
        if state in ('started', 'uncertain'):
            return self._finish(alert, now, 'uncertain')
        message = make_message(self.settings, alert)
        send_started = False
        connection = None
        try:
            context = ssl.create_default_context()
            if self.settings.security == 'ssl':
                connection = self.smtp_ssl_class(self.settings.host, self.settings.port, timeout=30, context=context)
            else:
                connection = self.smtp_class(self.settings.host, self.settings.port, timeout=30)
                connection.ehlo_or_helo_if_needed()
                connection.starttls(context=context)
                connection.ehlo()
            if self.settings.username:
                connection.login(self.settings.username, self.settings.password)
            send_started = True
            refused = connection.send_message(message, from_addr=self.settings.sender, to_addrs=[self.settings.recipient])
            if refused:
                # The configured single recipient was explicitly refused.
                send_started = False
                return self._finish(alert, now, 'retryable')
            return self._finish(alert, now, 'accepted')
        except smtplib.SMTPRecipientsRefused:
            return self._finish(alert, now, 'retryable')
        except (OSError, smtplib.SMTPException):
            return self._finish(alert, now, 'uncertain' if send_started else 'retryable')
        finally:
            if connection is not None:
                try:
                    connection.quit()
                except (OSError, smtplib.SMTPException):
                    connection.close()

    def review(self):
        """List attempts whose outcome requires human review, without message contents."""
        with sqlite3.connect(self.queue.database) as conn:
            rows = conn.execute("SELECT alert_id, state, updated_at FROM email_attempts WHERE state IN ('started', 'uncertain') ORDER BY alert_id").fetchall()
        return [{'alert_id': row[0], 'state': row[1], 'updated_at': row[2]} for row in rows]

    def resolve(self, alert_id, *, retry, now):
        """After review, explicitly allow a retry or record verified acceptance."""
        if type(retry) is not bool:
            raise ValueError('retry must be a bool')
        with self._exclusive() as acquired:
            if not acquired:
                return False
            return self._resolve(alert_id, retry=retry, now=now)

    def _resolve(self, alert_id, *, retry, now):
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('now must include a timezone')
        timestamp = now.astimezone(timezone.utc).isoformat(timespec='microseconds')
        with sqlite3.connect(self.queue.database, isolation_level=None) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute('''SELECT e.state, a.status, a.lease_until FROM email_attempts AS e
                    JOIN daily_alerts AS a ON a.id = e.alert_id WHERE e.alert_id = ?''', (alert_id,)).fetchone()
                if row is None or row[0] not in ('started', 'uncertain'):
                    conn.rollback()
                    return False
                expired = row[1] == 'inflight' and row[2] is not None and row[2] <= timestamp
                if row[1] != 'pending' and not expired:
                    conn.rollback()
                    return False
                conn.execute('UPDATE email_attempts SET state = ?, updated_at = ? WHERE alert_id = ?',
                             ('retryable' if retry else 'accepted', timestamp, alert_id))
                conn.execute("UPDATE daily_alerts SET status = 'pending', next_attempt_at = ?, lease_token = NULL, lease_until = NULL WHERE id = ?", (timestamp, alert_id))
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
