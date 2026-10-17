"""SQLite persistence. Ledger writes and replay validation share one transaction."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .domain import MarketBatch, ValidationError

SCHEMA_VERSION = 2
SCHEMA = (
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE ledger_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, instrument TEXT, event_date TEXT NOT NULL,
        payload TEXT NOT NULL, request_key TEXT UNIQUE,
        created_at TEXT NOT NULL, voided_at TEXT, void_note TEXT
    )""",
    "CREATE INDEX ledger_order ON ledger_events(event_date, id)",
    """CREATE TABLE watchlist (
        instrument TEXT PRIMARY KEY, reason TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, instrument TEXT NOT NULL,
        timeframe TEXT NOT NULL, adjustment TEXT NOT NULL,
        fetched_at TEXT NOT NULL, payload TEXT NOT NULL
    )""",
    "CREATE INDEX market_lookup ON market_snapshots(instrument, timeframe, adjustment, id)",
)

RESEARCH_SCHEMA = (
    "CREATE TABLE profile_versions (revision INTEGER PRIMARY KEY, created_at TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL)",
    """CREATE TABLE evidence_versions (
        evidence_id TEXT NOT NULL, version INTEGER NOT NULL, instrument TEXT NOT NULL,
        canonical_url TEXT NOT NULL, published_at TEXT, observed_at TEXT NOT NULL,
        content_hash TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(evidence_id,version))""",
    "CREATE INDEX evidence_lookup ON evidence_versions(instrument,observed_at)",
    """CREATE TABLE analysis_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, instrument TEXT NOT NULL,
        created_at TEXT NOT NULL, profile_revision INTEGER NOT NULL,
        status TEXT NOT NULL, input_json TEXT NOT NULL, result_json TEXT NOT NULL,
        model_json TEXT NOT NULL, raw_response TEXT NOT NULL, request_key TEXT UNIQUE)""",
    """CREATE TABLE target_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id INTEGER NOT NULL UNIQUE,
        instrument TEXT NOT NULL, profile_revision INTEGER NOT NULL,
        price TEXT NOT NULL, target_deadline TEXT NOT NULL, adopt_before TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', decided_at TEXT)""",
)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_mode=WAL")
            with self.transaction() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise ValidationError("数据库版本高于当前插件，拒绝打开以避免覆盖。")
                if version == 0:
                    for statement in SCHEMA:
                        db.execute(statement)
                    version = 1
                if version == 1:
                    for statement in RESEARCH_SCHEMA:
                        db.execute(statement)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Store is closed")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def rows(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, parameters).fetchall()

    def get_setting(self, key: str, default=None):
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def save_batch(self, batch: MarketBatch, *, retain: int = 5) -> None:
        # Whole query snapshots: adjusted histories are never appended to older adjustment vintages.
        data = batch.as_dict()
        key = (batch.instrument, batch.timeframe, batch.adjustment)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO market_snapshots(instrument,timeframe,adjustment,fetched_at,payload) VALUES (?,?,?,?,?)",
                (*key, data["fetched_at"], json.dumps(data, ensure_ascii=False)),
            )
            db.execute(
                """DELETE FROM market_snapshots WHERE instrument=? AND timeframe=? AND adjustment=?
                AND id NOT IN (SELECT id FROM market_snapshots WHERE instrument=? AND timeframe=? AND adjustment=? ORDER BY id DESC LIMIT ?)""",
                (*key, *key, retain),
            )

    def latest_batch(
        self, symbol: str, timeframe: str = "1Day", adjustment: str = "raw"
    ) -> MarketBatch | None:
        rows = self.rows(
            "SELECT payload FROM market_snapshots WHERE instrument=? AND timeframe=? AND adjustment=? ORDER BY id DESC LIMIT 1",
            (symbol, timeframe, adjustment),
        )
        return MarketBatch.from_dict(json.loads(rows[0]["payload"])) if rows else None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
