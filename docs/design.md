# Design notes

## Intended user and first useful action

Game/backend engineers diagnosing reward retries and session ownership. The smallest
useful interaction is `python -m sessionledger demo`: a retry storm becomes an inspectable
balance and an audit result with no account, network service, or dependency install.

Observed evidence: the local tests reproduce the described failures and pass on the
development machine. Audience demand and usefulness to other developers remain
hypotheses. The next product test is to give the demo to a game engineer and see whether
they can explain the duplicate-write protection and reproduce their own failure case.
Repeat value would come from saving incident scenarios and comparing fixes. Shareable,
sanitized scenarios are more useful here than social features or additional UI.

## Write boundary

```text
validate input -> acquire SQLite write transaction -> validate live session
 -> look up request fingerprint -> validate expected version and funds
 -> update all touched balances -> append journal event -> save response -> commit
```

Successful state changes, journal entries, and retry responses share one transaction.
An exception rolls them all back. An identical request returns the original response,
even if the account has changed since then. Callers needing the latest state must read
`balance()` separately. A retry still requires a currently valid session for player
spend/transfer operations; a reconnect can retry using its new lease.

Request identifiers are global to this database. Use stable, namespaced server-generated
keys such as `match:<match-id>:<player-id>`. Generating a new ID for every retry defeats
deduplication. Lease credentials are deliberately excluded from the command fingerprint
so a new valid session can retry a prior command.

## Sessions

Claims are serialized with writes. One unexpired lease per player is allowed. After expiry,
the next claim increments a persistent generation and rotates the token. Both token and
generation must match on every player-authorized write. Renewal is allowed only while
the current lease is alive. Time is injected for reproducible boundary tests.

The default wall clock assumes a trustworthy local host. Multi-host clock skew is not
solved. There is no automatic lease release on disconnect, so a crashed server can cause
a reconnect wait until expiry. That trade-off favors clear safety over availability.

## Replay and recovery

Player creation and successful economy operations are journaled. Replay reconstructs
balance/version state, validates command deltas and recorded results, and checks the
hash chain. The audit uses a consistent read transaction so concurrent writes cannot
make its event view disagree with its balance view merely due to timing.

`rebuild_projection()` locks writes, verifies replay first, then repairs existing account
balances. It does not rebuild sessions or retry records. Missing/extra account identities
require manual recovery. Backups and trusted event storage remain necessary.

## Explicit limits

- SQLite serializes local writers. This is not a sharded or geographically distributed service.
- One currency with integer units, bounded to signed 64-bit account balances.
- No inventory fulfillment: spending coins is not an item purchase until an inventory
  write can join the same transaction or an explicitly designed delivery protocol.
- No client authentication, network transport, rate limiting, migrations, or retention policy.
- Trusted `grant()` callers can mint arbitrary rewards; integrate policy/authorization
  before exposing this through a server API.
- Hash chaining has no external trust anchor. Database administrators can forge history.
- Tests exercise concurrent connections and injected SQL failure, not actual OS termination
  or hardware power failure. Those are distinct upcoming tests.

## Why Python and SQLite

The goal is a transparent reference model for failure reasoning. Python's standard library
provides portable concurrency tests and SQLite offers actual transaction rollback without
service setup. A simple in-memory dictionary would hide persistence and locking issues;
a full distributed stack would obscure the first experiment.

A future Roblox/Luau integration must revisit atomicity rather than copying the SQL
implementation mechanically. The next engineering milestone should test that boundary,
not claim the reference model is already safe for a game platform.
