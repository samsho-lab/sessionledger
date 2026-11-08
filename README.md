# SessionLedger

A local multiplayer economy reliability lab: reproduce duplicate reward delivery,
stale player sessions, concurrent spending, and projection corruption in seconds.

Built around a common game-server problem: a match completes, a reward write succeeds,
the response is lost, and another server retries. A retry should not mint extra coins.
An old server should also lose write authority when a player's session moves elsewhere.

## Run it

Python 3.11 or newer. Runtime and tests use only the standard library.
From the repository root:

```sh
python -m sessionledger demo
python -m unittest discover -s tests -v
```

The demo creates a disposable SQLite database, sends the same reward 20 times from
independent connections, transfers coins, rejects an overspend, and audits the result.
It does not touch your game or connect to any external service.

Expected final state:

```text
Alice: {'balance': 70, 'version': 2}
Bob: {'balance': 30, 'version': 1}
circulating_supply: 100
```

## Use the engine

```python
from sessionledger import Economy

economy = Economy("example.sqlite")
try:
    economy.create_player("alice")
    economy.create_player("bob")
    session = economy.claim("alice")
    economy.grant("alice", 100, "match:round-001:alice")
    economy.transfer("alice", "bob", 30, "trade:001", session)
    print(economy.audit())
finally:
    economy.close()
```

Each writer/thread opens its own `Economy` connection. Keep the session token on the
trusted server. `grant()` is a trusted-server administrative entry point, not a client
API. This package deliberately provides no unauthenticated HTTP endpoint.

For an existing database:

```sh
python -m sessionledger audit example.sqlite
python -m sessionledger rebuild example.sqlite
```

`audit` is read-only apart from SQLite connection setup; `rebuild` explicitly repairs
the balance/version projection from the verified event journal. Back up the database
before repair. It refuses to repair changed account identities or a broken hash chain.

## What is implemented

| Failure mode | Mechanism | Observable behavior |
|---|---|---|
| Repeated match reward | Persistent request ID + canonical command fingerprint | Exact retry returns the original response |
| Reusing an ID for another command | Payload conflict detection | Rejected without a write |
| Two servers own one player | Expiring session lease and fencing generation | Expired/superseded writer rejected |
| Two purchases spend the same coins | SQLite `BEGIN IMMEDIATE` transaction | No negative player balance |
| UI sends an outdated state version | Optional optimistic version check | Stale command rejected |
| Transfer fails after sender update | Single atomic transaction | Both sides, request record, and event roll back |
| Balance projection is corrupted | Event replay + projection comparison | Audit fails; explicit rebuild restores values |
| Journal bytes are altered | SHA-256 hash chain + append-only SQL triggers | Inconsistent chain detected |

The event chain detects accidental edits under the stated trust model. It is **not**
tamper-proof against an administrator who can rewrite the database and hashes.

## Verification

The 15-test suite covers independent-connection concurrency, database reopen persistence,
idempotency conflicts, session expiry and renewal, fencing, optimistic versions,
transaction rollback via an injected database failure, replay repair, journal corruption,
input validation, and 200 seeded transfers checked against an independent balance model.

The included CI configuration runs tests and the demo on Windows and Linux across
Python 3.11, 3.12, and 3.13. A checked-in workflow is not a claim that every CI run passed;
see the repository's actual Actions results.

## Structure

```text
sessionledger/store.py       transaction boundary, leases, journal, audit/rebuild
sessionledger/__main__.py    isolated demo and recovery commands
tests/test_store.py          failure and invariant tests
docs/design.md              assumptions, trade-offs, next experiments
```

## Current scope and next work

This is a new, AI-assisted engineering project, created as an original development
baseline. It is not an extracted employer codebase, a record of past production work,
or an existing Roblox integration. No customer data, employer assets, or credentials
are included. The problems are inspired by multiplayer systems engineering.

Next planned milestones:

1. Subprocess termination tests at commit boundaries, including power-loss-style recovery.
2. Multi-currency accounts and atomic item/coin trades, with conservation tests.
3. Deterministic scenario files and a report comparing failure policies.
4. A Luau adapter specification that explicitly handles the lack of cross-key
   transactions in a remote game persistence layer.

The repository is intentionally a foundation for further hands-on development. It does
not claim production deployment, external users, performance benchmarks, or distributed
database guarantees.
