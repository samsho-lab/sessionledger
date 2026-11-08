from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest

from sessionledger import Conflict, Economy, Rejected


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / "economy.sqlite")
        self.now = 1000
        self.store = Economy(self.path, clock=lambda: self.now)
        for player in ("alice", "bob"):
            self.store.create_player(player)
        self.lease = self.store.claim("alice")

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_duplicate_reward_is_delivered_once_across_restart(self):
        first = self.store.grant("alice", 50, "reward:1")
        self.store.close()
        self.store = Economy(self.path)
        self.assertEqual(first, self.store.grant("alice", 50, "reward:1"))
        self.assertEqual(50, self.store.balance("alice")["balance"])
        self.assertEqual(3, self.store.audit()["events"])

    def test_request_id_payload_collision_is_rejected(self):
        self.store.grant("alice", 50, "reward:1")
        with self.assertRaises(Conflict):
            self.store.grant("bob", 50, "reward:1")
        self.assertEqual(0, self.store.balance("bob")["balance"])

    def test_transfer_conserves_supply_and_retries_return_original_result(self):
        self.store.grant("alice", 50, "reward:1")
        first = self.store.transfer("alice", "bob", 20, "trade:1", self.lease)
        self.store.grant("alice", 7, "reward:2")
        self.assertEqual(first, self.store.transfer("alice", "bob", 20, "trade:1", self.lease))
        self.assertEqual(57, self.store.audit()["circulating_supply"])
        self.assertEqual(20, self.store.balance("bob")["balance"])

    def test_unknown_target_and_overspend_leave_no_partial_changes(self):
        self.store.grant("alice", 50, "reward:1")
        baseline = self.store.audit()
        for target, amount in (("missing", 20), ("bob", 51)):
            with self.assertRaises(Rejected):
                self.store.transfer("alice", target, amount, "trade:bad", self.lease)
        self.assertEqual(baseline, self.store.audit())
        self.assertEqual(50, self.store.balance("alice")["balance"])

    def test_stale_writer_is_fenced_after_reconnect(self):
        with self.assertRaises(Conflict):
            self.store.claim("alice")
        self.now += 60
        fresh = self.store.claim("alice")
        self.assertGreater(fresh["fence"], self.lease["fence"])
        self.store.grant("alice", 20, "reward:1")
        with self.assertRaises(Conflict):
            self.store.spend("alice", 1, "spend:1", self.lease)
        self.store.spend("alice", 1, "spend:1", fresh)

    def test_expired_lease_cannot_renew(self):
        self.now += 61
        with self.assertRaises(Conflict):
            self.store.renew("alice", self.lease)

    def test_renew_extends_valid_session(self):
        self.now += 50
        self.store.renew("alice", self.lease)
        self.now += 20
        with self.assertRaises(Conflict):
            self.store.claim("alice")

    def test_optimistic_version_conflict(self):
        self.store.grant("alice", 20, "reward:1")
        with self.assertRaises(Conflict):
            self.store.spend("alice", 1, "spend:1", self.lease, expected_version=0)
        self.store.spend("alice", 1, "spend:1", self.lease, expected_version=1)

    def test_concurrent_reward_delivery(self):
        def worker(_):
            other = Economy(self.path)
            try:
                return other.grant("alice", 50, "reward:race")
            finally:
                other.close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(30)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(50, self.store.balance("alice")["balance"])

    def test_competing_spends_cannot_overdraw(self):
        self.store.grant("alice", 100, "reward:race")
        def worker(index):
            other = Economy(self.path, clock=lambda: self.now)
            try:
                other.spend("alice", 10, f"spend:{index}", self.lease)
                return True
            except Rejected:
                return False
            finally:
                other.close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            successes = list(pool.map(worker, range(30)))
        self.assertEqual(10, sum(successes))
        self.assertEqual(0, self.store.balance("alice")["balance"])
        self.assertTrue(self.store.audit()["ok"])

    def test_failed_transfer_rolls_back_first_account_update(self):
        self.store.grant("alice", 100, "reward:1")
        self.store.db.execute("""CREATE TRIGGER simulate_failure BEFORE UPDATE ON accounts
            WHEN NEW.player='bob' BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transfer("alice", "bob", 30, "trade:fail", self.lease)
        self.assertEqual(100, self.store.balance("alice")["balance"])
        self.assertEqual(0, self.store.balance("bob")["balance"])
        self.assertEqual(3, self.store.audit()["events"])

    def test_projection_corruption_is_detected_and_repaired(self):
        self.store.grant("alice", 50, "reward:1")
        self.store.db.execute("UPDATE accounts SET balance=999 WHERE player='alice'")
        with self.assertRaises(Rejected):
            self.store.audit()
        self.assertTrue(self.store.rebuild_projection()["ok"])
        self.assertEqual(50, self.store.balance("alice")["balance"])

    def test_journal_is_append_only_and_hash_checked(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM events")
        self.store.db.execute("DROP TRIGGER events_no_update")
        self.store.db.execute("UPDATE events SET event_hash='broken' WHERE seq=1")
        with self.assertRaises(Rejected):
            self.store.audit()

    def test_validation_rejects_boolean_float_negative_and_self_transfer(self):
        for invalid in (True, 1.5, 0, -1, 10**12 + 1):
            with self.assertRaises(Rejected):
                self.store.grant("alice", invalid, "bad")
        with self.assertRaises(Rejected):
            self.store.transfer("alice", "alice", 1, "bad", self.lease)

    def test_seeded_random_transfers_match_independent_model(self):
        self.store.grant("alice", 100, "reward:1")
        self.store.grant("bob", 100, "reward:2")
        leases = {"alice": self.lease, "bob": self.store.claim("bob")}
        model = {"alice": 100, "bob": 100}
        rng = random.Random(771)
        for index in range(200):
            source, target = rng.sample(list(model), 2)
            amount = rng.randint(1, 40)
            if model[source] < amount:
                with self.assertRaises(Rejected):
                    self.store.transfer(source, target, amount, f"random:{index}", leases[source])
            else:
                self.store.transfer(source, target, amount, f"random:{index}", leases[source])
                model[source] -= amount
                model[target] += amount
        self.assertEqual(model, {p: self.store.balance(p)["balance"] for p in model})
        self.assertEqual(200, self.store.audit()["circulating_supply"])


if __name__ == "__main__":
    unittest.main()
