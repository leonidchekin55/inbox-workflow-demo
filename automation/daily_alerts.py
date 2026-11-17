"""Persistent daily alert queue, independent of messaging services and delivery.

Callers supply verified events and canonical client IDs, validate event timestamps
including delayed and future events, and implement delivery and access control.
No message contents or destination details are stored here. An alert remains
pending until a worker claims it and explicitly acknowledges successful delivery.
If a delivery result is unknown, a retry can produce a duplicate.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3
from zoneinfo import ZoneInfo


class EventConflictError(ValueError):
    """An event ID was reused with different metadata."""


def _required_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _utc_time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    return _utc_time(value).isoformat(timespec="microseconds")


class DailyAlertQueue:
    """One queued alert per local day and group, determined by first arrival.

    Arrival order is used even if a later-arriving event has an earlier timestamp.

    Grouping is fixed when a database is created. Channel groups include the
    platform and account. Client IDs must already identify the same client across
    accounts. Unidentified events are grouped individually, so the daily limit
    cannot be applied to an unknown client.
    Lease expiry makes work retryable, but cannot guarantee exactly-once delivery.
    Older all/channel records did not retain client identities. Replays of those
    records raise EventConflictError because their identities cannot be verified.
    """

    def __init__(self, database, *, grouping, timezone_name):
        if grouping not in {"all", "channel", "client"}:
            raise ValueError("grouping must be all, channel, or client")
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("a persistent SQLite database file is required")
        self.grouping = grouping
        self.timezone_name = _required_string(timezone_name, "timezone_name")
        self.timezone = ZoneInfo(timezone_name)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self):
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("CREATE TABLE IF NOT EXISTS alert_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("""CREATE TABLE IF NOT EXISTS daily_alerts (
                    id INTEGER PRIMARY KEY,
                    local_day TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('message', 'call')),
                    occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'inflight', 'delivered')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_until TEXT,
                    next_attempt_at TEXT,
                    delivered_at TEXT,
                    UNIQUE(local_day, group_key)
                )""")
                connection.execute("""CREATE TABLE IF NOT EXISTS alert_events (
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    client_identity TEXT,
                    alert_id INTEGER NOT NULL REFERENCES daily_alerts(id),
                    PRIMARY KEY(platform, account_id, event_id)
                )""")
                expected = {"grouping": self.grouping, "timezone": self.timezone_name}
                stored = dict(connection.execute("SELECT key, value FROM alert_settings"))
                if stored and stored != expected:
                    raise ValueError("database grouping or timezone differs from configured settings")
                for key, value in expected.items():
                    connection.execute("INSERT OR IGNORE INTO alert_settings (key, value) VALUES (?, ?)", (key, value))
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(alert_events)")}
                if "client_identity" not in columns:
                    connection.execute("ALTER TABLE alert_events ADD COLUMN client_identity TEXT")
                    if self.grouping == "client":
                        rows = connection.execute("SELECT rowid, group_key FROM alert_events").fetchall()
                        for row in rows:
                            try:
                                parts = json.loads(row["group_key"])
                            except (TypeError, ValueError):
                                continue
                            if isinstance(parts, list) and len(parts) == 2 and parts[0] == "client" and isinstance(parts[1], str):
                                identity = json.dumps([parts[1]])
                            elif isinstance(parts, list) and len(parts) in (3, 4) and parts[0] == "unknown":
                                identity = json.dumps([None])
                            else:
                                continue
                            connection.execute("UPDATE alert_events SET client_identity = ? WHERE rowid = ?", (identity, row["rowid"]))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _group_key(self, platform, account_id, event_id, client_id):
        if self.grouping == "all":
            parts = ["all"]
        elif self.grouping == "channel":
            parts = ["channel", platform, account_id]
        elif client_id is None:
            parts = ["unknown", platform, account_id, event_id]
        else:
            parts = ["client", _required_string(client_id, "client_id")]
        return json.dumps(parts, ensure_ascii=True, separators=(",", ":"))

    def enqueue(self, *, platform, account_id, event_id, kind, occurred_at, client_id=None):
        """Record an event. Return (alert_id, created), including on redelivery.

        Replays of legacy events with unverifiable identity raise EventConflictError.
        """
        platform = _required_string(platform, "platform")
        account_id = _required_string(account_id, "account_id")
        event_id = _required_string(event_id, "event_id")
        if kind not in {"message", "call"}:
            raise ValueError("kind must be message or call")
        if client_id is not None:
            _required_string(client_id, "client_id")
        occurred_at = _utc_time(occurred_at)
        instant = _timestamp(occurred_at)
        local_day = occurred_at.astimezone(self.timezone).date().isoformat()
        group_key = self._group_key(platform, account_id, event_id, client_id)
        client_identity = json.dumps([client_id])
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT kind, occurred_at, group_key, client_identity, alert_id FROM alert_events "
                    "WHERE platform = ? AND account_id = ? AND event_id = ?",
                    (platform, account_id, event_id),
                ).fetchone()
                if prior:
                    if prior["client_identity"] is None:
                        raise EventConflictError("client identity for this legacy event cannot be verified")
                    if (prior["kind"], prior["occurred_at"], prior["client_identity"]) != (kind, instant, client_identity):
                        raise EventConflictError("event ID was reused with different metadata")
                    connection.commit()
                    return prior["alert_id"], False
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO daily_alerts "
                    "(local_day, group_key, platform, account_id, kind, occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (local_day, group_key, platform, account_id, kind, instant),
                )
                created = cursor.rowcount == 1
                alert_id = connection.execute(
                    "SELECT id FROM daily_alerts WHERE local_day = ? AND group_key = ?",
                    (local_day, group_key),
                ).fetchone()["id"]
                connection.execute(
                    "INSERT INTO alert_events (platform, account_id, event_id, kind, occurred_at, group_key, client_identity, alert_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (platform, account_id, event_id, kind, instant, group_key, client_identity, alert_id),
                )
                connection.commit()
                return alert_id, created
            except Exception:
                connection.rollback()
                raise

    def claim_next(self, *, now, lease_seconds=60):
        """Lease the oldest pending or expired alert, returning a dict or None."""
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        now = _utc_time(now)
        now_text = _timestamp(now)
        until = _timestamp(now + timedelta(seconds=lease_seconds))
        token = secrets.token_urlsafe(24)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT id FROM daily_alerts WHERE "
                    "(status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
                    "OR (status = 'inflight' AND lease_until <= ?) ORDER BY id LIMIT 1",
                    (now_text, now_text),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                connection.execute(
                    "UPDATE daily_alerts SET status = 'inflight', lease_token = ?, lease_until = ?, "
                    "next_attempt_at = NULL, attempts = attempts + 1 WHERE id = ?", (token, until, row["id"]),
                )
                alert = dict(connection.execute("SELECT * FROM daily_alerts WHERE id = ?", (row["id"],)).fetchone())
                connection.commit()
                return alert
            except Exception:
                connection.rollback()
                raise

    def acknowledge(self, *, alert_id, lease_token, delivered_at):
        """Mark delivered only when the caller confirms delivery for its lease."""
        return self._finish(alert_id, lease_token, delivered_at=_timestamp(delivered_at), next_attempt_at=None)

    def fail(self, *, alert_id, lease_token, retry_at):
        """Schedule a failed lease for retry at the caller-selected time."""
        return self._finish(alert_id, lease_token, delivered_at=None, next_attempt_at=_timestamp(retry_at))

    def _finish(self, alert_id, lease_token, *, delivered_at, next_attempt_at):
        _required_string(lease_token, "lease_token")
        status = "delivered" if delivered_at is not None else "pending"
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE daily_alerts SET status = ?, delivered_at = ?, next_attempt_at = ?, "
                "lease_token = NULL, lease_until = NULL "
                "WHERE id = ? AND status = 'inflight' AND lease_token = ?",
                (status, delivered_at, next_attempt_at, alert_id, lease_token),
            )
            return cursor.rowcount == 1

    def get(self, alert_id):
        """Read alert state without modifying it."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM daily_alerts WHERE id = ?", (alert_id,)).fetchone()
            return dict(row) if row else None
