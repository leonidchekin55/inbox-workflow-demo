from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from automation.daily_alerts import DailyAlertQueue, EventConflictError


UTC = timezone.utc


class DailyAlertQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "alerts.sqlite"
        self.now = datetime(2026, 11, 17, 12, tzinfo=UTC)

    def queue(self, grouping="client", timezone_name="Europe/Moscow"):
        return DailyAlertQueue(self.database, grouping=grouping, timezone_name=timezone_name)

    def event(self, queue, event_id, *, platform="telegram", account_id="1", kind="message", at=None, client_id="client-1"):
        return queue.enqueue(platform=platform, account_id=account_id, event_id=event_id,
                             kind=kind, occurred_at=at or self.now, client_id=client_id)

    def test_client_grouping_day_boundary_and_cross_channel(self):
        queue = self.queue()
        before_midnight = datetime(2026, 11, 17, 20, 59, tzinfo=UTC)
        first, created = self.event(queue, "1", at=before_midnight)
        self.assertTrue(created)
        same, created = self.event(queue, "2", platform="whatsapp", kind="call", at=before_midnight)
        self.assertFalse(created)
        self.assertEqual(first, same)
        next_day, created = self.event(queue, "3", at=before_midnight + timedelta(minutes=1))
        self.assertTrue(created)
        self.assertNotEqual(first, next_day)
        self.assertEqual(queue.get(first)["local_day"], "2026-11-17")
        self.assertEqual(queue.get(next_day)["local_day"], "2026-11-18")
        other, created = self.event(queue, "4", client_id="client-2", at=before_midnight)
        self.assertTrue(created)
        self.assertNotEqual(first, other)

    def test_unknown_clients_create_per_event_and_deduplicate_replays(self):
        queue = self.queue()
        first, _ = self.event(queue, "1", client_id=None)
        same, created = self.event(queue, "1", client_id=None)
        self.assertFalse(created)
        self.assertEqual(first, same)
        another, created = self.event(queue, "2", client_id=None)
        self.assertTrue(created)
        self.assertNotEqual(first, another)
        for platform, account in [("telegram", "2"), ("whatsapp", "1")]:
            other, created = self.event(queue, "1", platform=platform, account_id=account, client_id=None)
            self.assertTrue(created)
            self.assertNotEqual(first, other)
        known, created = self.event(queue, "3", client_id="client-1")
        self.assertTrue(created)
        self.assertNotEqual(first, known)

    def test_all_and_channel_modes(self):
        for mode in ("all", "channel"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                queue = DailyAlertQueue(Path(directory) / "db.sqlite", grouping=mode, timezone_name="UTC")
                first, _ = self.event(queue, "1")
                same, created = self.event(queue, "2", client_id="different")
                self.assertEqual(first, same)
                self.assertFalse(created)
                second, created = self.event(queue, "1", platform="whatsapp")
                self.assertEqual(created, mode == "channel")
                self.assertEqual(first == second, mode == "all")
                account, created = self.event(queue, "1", account_id="2")
                self.assertEqual(created, mode == "channel")
                self.assertEqual(first == account, mode == "all")

    def test_redelivery_conflict_and_configuration_persistence(self):
        queue = self.queue()
        first, created = self.event(queue, "1")
        self.assertTrue(created)
        reopened = self.queue()
        duplicate, created = self.event(reopened, "1")
        self.assertEqual(first, duplicate)
        self.assertFalse(created)
        with self.assertRaises(EventConflictError):
            self.event(reopened, "1", kind="call")
        with self.assertRaises(EventConflictError):
            self.event(reopened, "1", client_id="client-2")
        with self.assertRaises(ValueError):
            self.queue(grouping="all")
        with self.assertRaises(ValueError):
            self.queue(timezone_name="UTC")
        self.assertEqual(reopened.get(first)["status"], "pending")

    def test_replay_client_identity_conflicts_in_every_grouping(self):
        for mode in ("all", "channel", "client"):
            for original in (None, "client-1"):
                with self.subTest(mode=mode, original=original), TemporaryDirectory() as directory:
                    queue = DailyAlertQueue(Path(directory) / "db.sqlite", grouping=mode, timezone_name="UTC")
                    alert_id, _ = self.event(queue, "1", client_id=original)
                    self.assertEqual(self.event(queue, "1", client_id=original), (alert_id, False))
                    replacement = "client-2" if original is not None else "client-1"
                    with self.assertRaises(EventConflictError):
                        self.event(queue, "1", client_id=replacement)
                    if original is not None:
                        with self.assertRaises(EventConflictError):
                            self.event(queue, "1", client_id=None)
                    self.assertEqual(queue.get(alert_id)["status"], "pending")

    def test_legacy_events_are_preserved_during_identity_migration(self):
        for mode in ("all", "channel", "client"):
            for original in (None, "client-1"):
                with self.subTest(mode=mode, original=original), TemporaryDirectory() as directory:
                    path = Path(directory) / "db.sqlite"
                    queue = DailyAlertQueue(path, grouping=mode, timezone_name="UTC")
                    alert_id, _ = self.event(queue, "1", client_id=original)
                    with sqlite3.connect(path) as connection:
                        if mode == "client" and original is None:
                            connection.execute("UPDATE alert_events SET group_key = ?", ('["unknown","telegram","1"]',))
                        connection.execute("ALTER TABLE alert_events DROP COLUMN client_identity")
                    reopened = DailyAlertQueue(path, grouping=mode, timezone_name="UTC")
                    self.assertEqual(reopened.get(alert_id)["status"], "pending")
                    with sqlite3.connect(path) as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0], 1)
                    if mode == "client":
                        self.assertEqual(self.event(reopened, "1", client_id=original), (alert_id, False))
                        with self.assertRaises(EventConflictError):
                            self.event(reopened, "1", client_id="other")
                    else:
                        with self.assertRaisesRegex(EventConflictError, "cannot be verified"):
                            self.event(reopened, "1", client_id=original)
                    fresh_id, created = self.event(reopened, "2", client_id="client-2")
                    self.assertEqual(created, mode == "client")
                    self.assertEqual(fresh_id == alert_id, mode != "client")
                    reopened_again = DailyAlertQueue(path, grouping=mode, timezone_name="UTC")
                    self.assertEqual(reopened_again.get(alert_id)["status"], "pending")

    def test_queue_failure_retry_and_confirmed_delivery(self):
        queue = self.queue()
        alert_id, _ = self.event(queue, "1", kind="call")
        self.assertEqual(queue.get(alert_id)["status"], "pending")
        claim = queue.claim_next(now=self.now)
        self.assertEqual(claim["status"], "inflight")
        self.assertEqual(claim["kind"], "call")
        self.assertIsNone(claim["delivered_at"])
        self.assertIsNone(queue.claim_next(now=self.now))
        self.assertFalse(queue.acknowledge(alert_id=alert_id, lease_token="wrong", delivered_at=self.now))
        self.assertTrue(queue.fail(alert_id=alert_id, lease_token=claim["lease_token"], retry_at=self.now))
        self.assertEqual(queue.get(alert_id)["status"], "pending")
        retry = self.queue().claim_next(now=self.now)
        self.assertEqual(retry["attempts"], 2)
        self.assertNotEqual(retry["lease_token"], claim["lease_token"])
        self.assertFalse(queue.acknowledge(alert_id=alert_id, lease_token=claim["lease_token"], delivered_at=self.now))
        self.assertTrue(queue.acknowledge(alert_id=alert_id, lease_token=retry["lease_token"], delivered_at=self.now))
        self.assertEqual(queue.get(alert_id)["status"], "delivered")
        self.assertIsNotNone(queue.get(alert_id)["delivered_at"])
        self.assertIsNone(queue.claim_next(now=self.now + timedelta(days=1)))
        same, created = self.event(queue, "2")
        self.assertEqual(same, alert_id)
        self.assertFalse(created)
        self.assertIsNone(queue.claim_next(now=self.now + timedelta(days=1)))

    def test_expired_lease_recovery_and_stale_worker(self):
        queue = self.queue()
        alert_id, _ = self.event(queue, "1")
        original = queue.claim_next(now=self.now, lease_seconds=10)
        self.assertIsNone(queue.claim_next(now=self.now + timedelta(seconds=9)))
        recovered = queue.claim_next(now=self.now + timedelta(seconds=10))
        self.assertEqual(recovered["id"], alert_id)
        self.assertFalse(queue.fail(alert_id=alert_id, lease_token=original["lease_token"], retry_at=self.now))
        self.assertFalse(queue.acknowledge(alert_id=alert_id, lease_token=original["lease_token"], delivered_at=self.now))
        self.assertTrue(queue.acknowledge(alert_id=alert_id, lease_token=recovered["lease_token"], delivered_at=self.now))

    def test_parallel_events_and_workers_create_one_alert(self):
        queue = self.queue()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda number: self.event(queue, str(number)), range(48)))
        self.assertEqual(len({alert_id for alert_id, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)
        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(lambda _: queue.claim_next(now=self.now), range(20)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_failed_alert_waits_for_retry_without_blocking_queue(self):
        queue = self.queue()
        first, _ = self.event(queue, "1", client_id="client-1")
        second, _ = self.event(queue, "2", client_id="client-2")
        initial = queue.claim_next(now=self.now)
        self.assertEqual(initial["id"], first)
        retry_at = self.now + timedelta(minutes=5)
        self.assertTrue(queue.fail(alert_id=first, lease_token=initial["lease_token"], retry_at=retry_at))
        self.assertEqual(queue.get(first)["status"], "pending")
        self.assertEqual(queue.get(first)["next_attempt_at"], retry_at.isoformat(timespec="microseconds"))
        queue = self.queue()
        next_alert = queue.claim_next(now=self.now)
        self.assertEqual(next_alert["id"], second)
        self.assertTrue(queue.acknowledge(alert_id=second, lease_token=next_alert["lease_token"], delivered_at=self.now))
        self.assertIsNone(queue.claim_next(now=retry_at - timedelta(microseconds=1)))
        self.assertEqual(queue.claim_next(now=retry_at)["id"], first)

    def test_parallel_replay_of_same_event(self):
        queue = self.queue()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.event(queue, "same-event"), range(32)))
        self.assertEqual(len({alert_id for alert_id, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)

    def test_daylight_saving_repeat_stays_in_same_local_day(self):
        queue = self.queue(timezone_name="America/New_York")
        before_fallback = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        first, created = self.event(queue, "1", at=before_fallback)
        self.assertTrue(created)
        repeated_hour, created = self.event(queue, "2", at=before_fallback + timedelta(hours=1))
        self.assertFalse(created)
        self.assertEqual(first, repeated_hour)
        self.assertEqual(queue.get(first)["local_day"], "2026-11-01")

    def test_validates_input_without_writing(self):
        queue = self.queue()
        with self.assertRaises(ValueError):
            self.event(queue, "1", at=datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            self.event(queue, "1", kind="unknown")
        with self.assertRaises(ValueError):
            self.event(queue, "1", client_id="")
        with self.assertRaises(ValueError):
            queue.claim_next(now=self.now, lease_seconds=0)
        self.assertIsNone(queue.claim_next(now=self.now))
        with self.assertRaises(ValueError):
            DailyAlertQueue(":memory:", grouping="all", timezone_name="UTC")


if __name__ == "__main__":
    unittest.main()
