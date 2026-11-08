import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile

from .store import Economy, Rejected


def demo(path):
    store = Economy(path)
    for player in ("alice", "bob"):
        store.create_player(player)
    print("1. Deliver the same match reward 20 times from independent connections")
    def deliver(_):
        worker = Economy(path)
        try:
            return worker.grant("alice", 100, "match:demo-1:alice")
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(deliver, range(20)))
    print("   Alice:", store.balance("alice"))
    lease = store.claim("alice")
    print("2. Transfer 30 coins, then reject an overspend")
    store.transfer("alice", "bob", 30, "trade:demo-1", lease)
    try:
        store.spend("alice", 100, "shop:demo-1", lease)
    except Rejected as error:
        print("   Rejected:", error)
    print("3. Replay the journal and verify balances")
    print(json.dumps(store.audit(), indent=2))
    print("   Alice:", store.balance("alice"), "Bob:", store.balance("bob"))
    store.close()


def main():
    parser = argparse.ArgumentParser(description="Reproduce multiplayer economy failure modes")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("demo", help="Run an isolated, disposable retry/concurrency scenario")
    for name in ("audit", "rebuild"):
        command = sub.add_parser(name)
        command.add_argument("database", type=Path)
    args = parser.parse_args()
    if args.action == "demo":
        with tempfile.TemporaryDirectory() as folder:
            demo(str(Path(folder) / "economy.sqlite"))
        return
    if not args.database.is_file():
        parser.error("database does not exist")
    store = Economy(args.database)
    try:
        result = store.audit() if args.action == "audit" else store.rebuild_projection()
        print(json.dumps(result, indent=2))
    except Rejected as error:
        parser.exit(1, f"Audit failed: {error}\n")
    finally:
        store.close()


if __name__ == "__main__":
    main()
