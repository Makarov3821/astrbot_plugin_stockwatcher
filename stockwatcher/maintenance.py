"""Offline inspect/restore and online SQLite backup. No HTTP or model calls."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .domain import ValidationError
from .storage import SCHEMA_VERSION, Store, atomic_database


def readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def inspect_database(path: Path) -> dict:
    with closing(readonly(path)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValidationError("数据库完整性检查失败。")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if (
            not 1 <= version <= SCHEMA_VERSION
            or not {"settings", "ledger_events", "market_snapshots", "watchlist"} <= tables
        ):
            raise ValidationError("不是受支持的 StockWatcher 数据库版本或表结构。")
        return {
            "schema_version": version,
            "integrity": "ok",
            "counts": {
                name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                for name in (
                    "ledger_events",
                    "watchlist",
                    "analysis_runs",
                    "monitor_plans",
                    "model_calls",
                )
                if name in tables
            },
        }


def restore_database(source: Path, destination: Path) -> dict:
    inspect_database(source)
    if (
        destination.exists()
        or Path(str(destination) + "-wal").exists()
        or Path(str(destination) + "-shm").exists()
    ):
        raise ValidationError(
            "恢复目标已存在或仍有 WAL/SHM 文件；请先停用插件并保留原数据，恢复到全新路径。"
        )

    def write(path):
        source_db, target_db = readonly(source), sqlite3.connect(path)
        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
            source_db.close()
        store = Store(path)
        try:
            with store.transaction() as db:
                row = db.execute("SELECT value FROM settings WHERE key='monitor_config'").fetchone()
                config = json.loads(row[0]) if row else {}
                config.update(enabled=False, daily=False, auto_adopt=False)
                db.execute(
                    "INSERT INTO settings VALUES ('monitor_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(config),),
                )
                db.execute("DELETE FROM settings WHERE key='notification_origin'")
                db.execute(
                    "UPDATE notification_outbox SET status='cancelled' WHERE status IN ('pending','sending','failed')"
                )
                db.execute(
                    "UPDATE monitor_plans SET state='superseded',reason='从备份恢复，需重新分析采用' WHERE state IN ('waiting','armed')"
                )
                db.execute(
                    "UPDATE target_proposals SET status='restored_inactive' WHERE status='pending'"
                )
                db.execute(
                    "UPDATE monitor_jobs SET status='failed',error='从备份恢复，不自动重放历史作业' WHERE status IN ('running','retry')"
                )
        finally:
            store.close()
        inspect_database(path)

    atomic_database(destination, write)
    return {
        "status": "restored",
        **inspect_database(destination),
        "monitoring": "disabled; reselect destination and review before enabling",
    }


def main():
    parser = argparse.ArgumentParser(
        description="StockWatcher 本地数据检查/恢复；恢复不会覆盖现有目标。"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect").add_argument("source", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        result = (
            inspect_database(args.source)
            if args.command == "inspect"
            else restore_database(args.source, args.destination)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, sqlite3.Error, ValidationError):
        print(
            json.dumps(
                {
                    "status": "failed",
                    "message": "数据库不存在、不完整、版本不支持或目标已存在；原始数据未覆盖。",
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
