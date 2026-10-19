import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from stockwatcher.domain import ValidationError
from stockwatcher.ledger import Ledger
from stockwatcher.storage import Store


def test_newer_database_is_rejected_without_reset(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
        db.execute("CREATE TABLE existing (value TEXT)")
        db.execute("INSERT INTO existing VALUES ('keep')")
    with pytest.raises(ValidationError, match="高于"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999
        assert db.execute("SELECT value FROM existing").fetchone()[0] == "keep"


def test_two_connections_cannot_both_spend_same_cash(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first, second = Store(path), Store(path)
    Ledger(first).record("cash_set", cash="100", day="2026-01-01")

    def buy(store):
        try:
            Ledger(store).record("buy", symbol="MU", quantity="1", price="100", day="2026-01-02")
            return True
        except ValidationError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(buy, (first, second)))
        assert sum(results) == 1
        assert Ledger(first).view().cash == 0
    finally:
        first.close()
        second.close()


def test_v1_migration_preserves_ledger_and_settings(tmp_path):
    import json
    import sqlite3

    from stockwatcher.ledger import Ledger
    from stockwatcher.storage import SCHEMA, Store

    path = tmp_path / "v1.sqlite3"
    db = sqlite3.connect(path)
    for statement in SCHEMA:
        db.execute(statement)
    db.execute("PRAGMA user_version=1")
    db.execute("INSERT INTO settings VALUES ('notification_origin',?)", (json.dumps("original"),))
    payload = json.dumps({"quantity": "2", "price": "100", "fee": "0", "note": "original"})
    db.execute(
        "INSERT INTO ledger_events(kind,instrument,event_date,payload,created_at) VALUES ('seed','US:MU','2026-01-01',?,'2026-01-01T00:00:00Z')",
        (payload,),
    )
    db.commit()
    db.close()
    store = Store(path)
    try:
        from stockwatcher.storage import SCHEMA_VERSION

        assert store.rows("PRAGMA user_version")[0][0] == SCHEMA_VERSION
        assert store.get_setting("notification_origin") == "original"
        assert Ledger(store).view().positions["US:MU"].quantity == 2
        assert store.rows("SELECT * FROM analysis_runs") == []
    finally:
        store.close()
