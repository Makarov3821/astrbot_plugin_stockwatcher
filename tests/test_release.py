import hashlib
import json
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import build
from stockwatcher.ledger import Ledger
from stockwatcher.maintenance import inspect_database, restore_database
from stockwatcher.storage import SCHEMA_VERSION, Store


def test_online_wal_backup_restores_state_with_monitoring_disabled(store, ledger, tmp_path):
    from stockwatcher.domain import ValidationError
    from stockwatcher.profiles import Profiles

    Profiles(store)
    ledger.record("seed", symbol="MU", quantity="2", price="100", day="2026-01-01")
    store.set_setting("monitor_config", {"enabled": True, "daily": True, "auto_adopt": True})
    store.set_setting("notification_origin", "private:session:address")
    backup = store.backup(tmp_path / "backup.sqlite3")
    assert inspect_database(backup)["integrity"] == "ok"
    assert backup.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        store.backup(backup)
    ledger.record("sell", symbol="MU", quantity="1", price="110", day="2026-01-02")
    target = tmp_path / "restored.sqlite3"
    restore_database(backup, target)
    restored = Store(target)
    try:
        assert Ledger(restored).view().positions["US:MU"].quantity == 2
        assert not restored.get_setting("monitor_config")["enabled"]
        assert restored.get_setting("notification_origin") is None
        assert ledger.view().positions["US:MU"].quantity == 1
    finally:
        restored.close()
    with pytest.raises(ValidationError, match="已存在"):
        restore_database(backup, target)


def test_restore_refuses_foreign_or_newer_database_and_retains_original(tmp_path):
    from stockwatcher.domain import ValidationError

    source, target = tmp_path / "other.sqlite3", tmp_path / "target.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA user_version=999")
        db.execute("CREATE TABLE private_data(value TEXT)")
    with pytest.raises(ValidationError):
        restore_database(source, target)
    assert not target.exists()
    assert source.exists()


@pytest.mark.parametrize("version", [2, 3])
def test_intermediate_schema_upgrade_preserves_existing_settings(tmp_path, version):
    from stockwatcher.storage import MONITOR_SCHEMA, RESEARCH_SCHEMA, SCHEMA

    path = tmp_path / f"v{version}.sqlite3"
    with sqlite3.connect(path) as db:
        for statement in (*SCHEMA, *RESEARCH_SCHEMA, *(MONITOR_SCHEMA if version == 3 else ())):
            db.execute(statement)
        db.execute(f"PRAGMA user_version={version}")
        db.execute("INSERT INTO settings VALUES ('keep','123')")
    store = Store(path)
    try:
        assert store.get_setting("keep") == 123
        assert store.rows("PRAGMA user_version")[0][0] == SCHEMA_VERSION
        assert store.rows("SELECT * FROM model_calls") == []
    finally:
        store.close()


def test_release_allowlist_hashes_and_extracted_core_are_self_contained(tmp_path):
    root = Path(__file__).resolve().parents[1]
    archive = build(root, tmp_path / "release")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        assert not any(
            part in name.split("/")
            for name in names
            for part in ("astrbot-temp", ".git", ".venv", "tests", "__pycache__")
        )
        assert not any(name.endswith((".sqlite3", ".env")) for name in names)
        prefix = "astrbot_plugin_stockwatcher/"
        assert package.read(prefix + "LICENSE") == (root / "LICENSE").read_bytes()
        manifest = json.loads(package.read(prefix + "RELEASE_MANIFEST.json"))
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(package.read(prefix + name)).hexdigest() == digest
        package.extractall(tmp_path / "extracted")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from stockwatcher.storage import Store; from stockwatcher.monitor import Monitor; from stockwatcher.deep import DeepResearch; from stockwatcher.config import Settings; import json; from pathlib import Path; Settings.from_mapping(json.loads(Path('configs/minimal.json').read_text())); print('release import ok')",
        ],
        cwd=tmp_path / "extracted" / "astrbot_plugin_stockwatcher",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "release import ok" in result.stdout
    assert archive.with_suffix(".zip.sha256").exists()


def test_maintenance_missing_file_cli_fails_without_creating_it(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    result = subprocess.run(
        [sys.executable, "-m", "stockwatcher.maintenance", "inspect", str(missing)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert not missing.exists()


@pytest.mark.parametrize("grouped", [False, True])
def test_release_refuses_credentials_in_example(tmp_path, grouped):
    from scripts.build_release import ROOT_FILES

    root = Path(__file__).resolve().parents[1]
    for name in ROOT_FILES:
        (tmp_path / name).write_bytes((root / name).read_bytes())
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/minimal.json").write_text(
        json.dumps(
            {"market_data": {"alpaca_secret_key": "fixture-private-secret"}}
            if grouped
            else {"alpaca_secret_key": "fixture-private-secret"}
        )
    )
    with pytest.raises(ValueError, match="credentials"):
        build(tmp_path, tmp_path / "release")
