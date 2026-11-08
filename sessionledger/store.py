"""SQLite reference model for server-authoritative multiplayer economy writes.

This is a local engineering lab, not a Roblox DataStore adapter or a money service.
The caller is a trusted game server; never expose grant() to untrusted clients.
"""
from contextlib import contextmanager
import hashlib
import json
import secrets
import sqlite3
import time


class Rejected(ValueError):
    """A command violated a domain invariant."""


class Conflict(Rejected):
    """A stale session, version, or reused request identifier was rejected."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Economy:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                player TEXT PRIMARY KEY, balance INTEGER NOT NULL CHECK(balance >= 0),
                version INTEGER NOT NULL CHECK(version >= 0));
            CREATE TABLE IF NOT EXISTS sessions (
                player TEXT PRIMARY KEY REFERENCES accounts(player),
                token TEXT NOT NULL, fence INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL,
                previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append only'); END;
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def _write(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise Rejected("identifiers must contain 1-128 nonblank characters")

    @staticmethod
    def _amount(amount):
        if type(amount) is not int or not 0 < amount <= 10**12:
            raise Rejected("amount must be a positive integer no greater than 10^12")

    def _append(self, body):
        previous = self.db.execute(
            "SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        previous = previous[0] if previous else "0" * 64
        encoded = canonical(body)
        self.db.execute(
            "INSERT INTO events(body, previous_hash, event_hash) VALUES(?,?,?)",
            (encoded, previous, digest(previous + encoded)),
        )

    def create_player(self, player):
        self._name(player)
        with self._write():
            exists = self.db.execute("SELECT 1 FROM accounts WHERE player=?", (player,)).fetchone()
            if not exists:
                self.db.execute("INSERT INTO accounts VALUES(?,0,0)", (player,))
                self._append({"kind": "create", "player": player})

    def balance(self, player):
        row = self.db.execute("SELECT balance,version FROM accounts WHERE player=?", (player,)).fetchone()
        if row is None:
            raise Rejected("unknown player")
        return dict(row)

    def claim(self, player, ttl=60):
        """Claim an expired lease; a new fencing generation invalidates old writers."""
        if type(ttl) not in (int, float) or not 0 < ttl <= 3600:
            raise Rejected("lease TTL must be in (0, 3600]")
        with self._write():
            self.balance(player)
            prior = self.db.execute("SELECT * FROM sessions WHERE player=?", (player,)).fetchone()
            now = self.clock()
            if prior and prior["expires"] > now:
                raise Conflict("player already has an active session")
            lease = {"token": secrets.token_hex(16), "fence": prior["fence"] + 1 if prior else 1}
            self.db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(player) DO UPDATE SET "
                "token=excluded.token,fence=excluded.fence,expires=excluded.expires",
                (player, lease["token"], lease["fence"], now + ttl),
            )
            return lease

    def _session(self, player, lease):
        row = self.db.execute("SELECT * FROM sessions WHERE player=?", (player,)).fetchone()
        if (not row or row["expires"] <= self.clock()
                or row["token"] != lease.get("token") or row["fence"] != lease.get("fence")):
            raise Conflict("expired or superseded session")

    def renew(self, player, lease, ttl=60):
        if type(ttl) not in (int, float) or not 0 < ttl <= 3600:
            raise Rejected("lease TTL must be in (0, 3600]")
        with self._write():
            self._session(player, lease)
            self.db.execute("UPDATE sessions SET expires=? WHERE player=?", (self.clock() + ttl, player))

    def grant(self, player, amount, request_id):
        """Trusted-server reward: reuse match:<match_id>:<player> on delivery retries."""
        return self._command("grant", player, amount, request_id)

    def spend(self, player, amount, request_id, lease, expected_version=None):
        return self._command("spend", player, amount, request_id, lease, expected_version)

    def transfer(self, player, target, amount, request_id, lease, expected_version=None):
        self._name(target)
        if player == target:
            raise Rejected("self transfers are not supported")
        return self._command("transfer", player, amount, request_id, lease, expected_version, target)

    def _command(self, kind, player, amount, request_id, lease=None, expected_version=None, target=None):
        self._name(player)
        self._name(request_id)
        self._amount(amount)
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise Rejected("expected_version must be a nonnegative integer")
        command = dict(kind=kind, player=player, amount=amount, target=target,
                       expected_version=expected_version, request_id=request_id)
        fingerprint = digest(canonical(command))
        with self._write():
            if kind != "grant":
                self._session(player, lease or {})
            prior = self.db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise Conflict("request ID was already used for a different command")
                return json.loads(prior["result"])
            current = self.balance(player)
            if expected_version is not None and current["version"] != expected_version:
                raise Conflict("stale account version")
            deltas = {player: amount if kind == "grant" else -amount}
            if target:
                self.balance(target)
                deltas[target] = amount
            for account, delta in deltas.items():
                state = self.balance(account)
                new_balance = state["balance"] + delta
                if not 0 <= new_balance <= 2**63 - 1:
                    raise Rejected("insufficient funds or balance overflow")
                self.db.execute("UPDATE accounts SET balance=?,version=version+1 WHERE player=?",
                                (new_balance, account))
            result = {account: self.balance(account) for account in sorted(deltas)}
            self._append({"kind": "transaction", "command": command, "deltas": deltas, "result": result})
            self.db.execute("INSERT INTO requests VALUES(?,?,?)", (request_id, fingerprint, canonical(result)))
            return result

    def _replay(self):
        states, previous, seen = {}, "0" * 64, set()
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            if row["previous_hash"] != previous or digest(previous + row["body"]) != row["event_hash"]:
                raise Rejected(f"event chain failed at sequence {row['seq']}")
            previous = row["event_hash"]
            event = json.loads(row["body"])
            if event["kind"] == "create":
                if event["player"] in states:
                    raise Rejected("duplicate player creation")
                states[event["player"]] = {"balance": 0, "version": 0}
                continue
            command = event["command"]
            request = command["request_id"]
            if request in seen:
                raise Rejected("duplicate request in journal")
            seen.add(request)
            amount = command["amount"]
            self._amount(amount)
            expected = {command["player"]: amount if command["kind"] == "grant" else -amount}
            if command["kind"] == "transfer":
                expected[command["target"]] = amount
            if command["kind"] not in ("grant", "spend", "transfer") or expected != event["deltas"]:
                raise Rejected("invalid transaction semantics")
            for player, delta in event["deltas"].items():
                state = states[player]
                state["balance"] += delta
                state["version"] += 1
                if not 0 <= state["balance"] <= 2**63 - 1:
                    raise Rejected("invalid replayed balance")
            if event["result"] != {p: states[p] for p in event["deltas"]}:
                raise Rejected("stored result differs from replay")
        return states

    def audit(self):
        """Consistent read snapshot: compare journal replay with the current projection."""
        self.db.execute("BEGIN")
        try:
            expected = self._replay()
            actual = {r["player"]: {"balance": r["balance"], "version": r["version"]}
                      for r in self.db.execute("SELECT * FROM accounts")}
            if expected != actual:
                raise Rejected("account projection differs from journal")
            count = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            return {"ok": True, "players": len(expected), "events": count,
                    "circulating_supply": sum(s["balance"] for s in expected.values())}
        finally:
            self.db.execute("ROLLBACK")

    def rebuild_projection(self):
        """Repair existing account balances from verified events, under a write lock."""
        with self._write():
            states = self._replay()
            actual = {r[0] for r in self.db.execute("SELECT player FROM accounts")}
            if actual != set(states):
                raise Rejected("account identities differ; manual recovery required")
            for player, state in states.items():
                self.db.execute("UPDATE accounts SET balance=?,version=? WHERE player=?",
                                (state["balance"], state["version"], player))
        return self.audit()
