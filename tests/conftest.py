from pathlib import Path

import pytest

from stockwatcher.ledger import Ledger
from stockwatcher.storage import Store


@pytest.fixture
def store(tmp_path: Path):
    instance = Store(tmp_path / "test.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def ledger(store):
    return Ledger(store)
