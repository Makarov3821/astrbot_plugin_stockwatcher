from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from stockwatcher.domain import ValidationError, amount, instrument
from stockwatcher.ledger import Ledger
from stockwatcher.storage import Store


def test_weighted_cost_cash_and_partial_exit(ledger):
    ledger.record("cash_set", cash="1000", day="2026-01-01")
    ledger.record("buy", symbol="MU", quantity="2", price="100", fee="2", day="2026-01-02")
    ledger.record("buy", symbol="MU", quantity="2", price="150", fee="2", day="2026-01-03")
    ledger.record("sell", symbol="MU", quantity="1", price="160", fee="1", day="2026-01-04")
    view = ledger.view()
    position = view.positions["US:MU"]
    assert (position.quantity, position.cost_basis, position.average_cost) == (
        Decimal("3"),
        Decimal("378"),
        Decimal("126"),
    )
    assert position.realized_pnl == Decimal("33")
    assert view.cash == Decimal("655")


def test_seed_does_not_imply_cash_or_prior_history(ledger):
    ledger.record("seed", symbol="MU", quantity="10", price="100", day="2026-01-02")
    ledger.record("sell", symbol="MU", quantity="1", price="110", day="2026-01-03")
    view = ledger.view()
    assert view.cash is None
    assert view.positions["US:MU"].has_opening_seed
    assert view.positions["US:MU"].realized_pnl == Decimal("10")


def test_unknown_cash_is_not_zero_and_zero_is_known(ledger):
    ledger.record("buy", symbol="MU", quantity="1", price="100", day="2026-01-01")
    assert ledger.view().cash is None
    ledger.record("cash_set", cash="0", day="2026-01-02")
    with pytest.raises(ValidationError, match="余额为负"):
        ledger.record("buy", symbol="MU", quantity="1", price="1", day="2026-01-03")
    assert len(ledger.events()) == 2


def test_failed_oversell_rolls_back(ledger):
    ledger.record("seed", symbol="MU", quantity="0.5", price="100", day="2026-01-01")
    with pytest.raises(ValidationError, match="超过"):
        ledger.record("sell", symbol="MU", quantity="0.6", price="100", day="2026-01-02")
    assert len(ledger.events()) == 1
    assert ledger.view().positions["US:MU"].quantity == Decimal("0.5")


def test_backdated_entry_is_replayed_by_economic_date(ledger):
    ledger.record("seed", symbol="MU", quantity="1", price="100", day="2026-01-01")
    ledger.record("sell", symbol="MU", quantity="1", price="120", day="2026-01-03")
    ledger.record("buy", symbol="MU", quantity="1", price="200", day="2026-01-02")
    view = ledger.view()
    assert view.positions["US:MU"].cost_basis == Decimal("150")
    assert view.positions["US:MU"].realized_pnl == Decimal("-30")
    with pytest.raises(ValidationError, match="之前登记"):
        ledger.record("buy", symbol="MU", quantity="1", price="50", day="2025-12-31")


def test_void_rejected_if_it_invalidates_later_sale(ledger):
    buy, _ = ledger.record("buy", symbol="MU", quantity="1", price="100", day="2026-01-01")
    sell, _ = ledger.record("sell", symbol="MU", quantity="1", price="110", day="2026-01-02")
    with pytest.raises(ValidationError, match="超过"):
        ledger.void(buy, "买入输错")
    assert all(row["voided_at"] is None for row in ledger.events())
    assert ledger.void(sell, "先撤销后续卖出")
    assert ledger.void(buy, "再修正买入")
    assert not ledger.void(buy, "重复操作")
    assert ledger.view().positions == {}
    assert len(ledger.events()) == 2


def test_request_idempotency_including_parallel_delivery(ledger):
    def insert(_):
        return ledger.record(
            "buy", symbol="MU", quantity="1", price="100", day="2026-01-01", request_key="message-1"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(insert, range(8)))
    assert sum(created for _, created in receipts) == 1
    assert len({event_id for event_id, _ in receipts}) == 1
    assert ledger.view().positions["US:MU"].quantity == 1
    with pytest.raises(ValidationError, match="不同内容"):
        ledger.record(
            "buy", symbol="MU", quantity="2", price="100", day="2026-01-01", request_key="message-1"
        )


def test_fractional_sales_leave_no_rounding_residue(ledger):
    ledger.record("buy", symbol="MU", quantity="3", price="100", fee="0.01", day="2026-01-01")
    for _ in range(3):
        ledger.record("sell", symbol="MU", quantity="1", price="100", day="2026-01-02")
    position = ledger.view().positions["US:MU"]
    assert position.quantity == 0
    assert position.cost_basis == 0
    assert abs(position.realized_pnl - Decimal("-0.01")) < Decimal("1e-30")


def test_split_preserves_basis_and_reversal_checks(ledger):
    ledger.record("seed", symbol="MU", quantity="1.5", price="100", day="2026-01-01")
    event_id, _ = ledger.record(
        "split", symbol="MU", numerator="2", denominator="1", day="2026-01-02"
    )
    position = ledger.view().positions["US:MU"]
    assert (position.quantity, position.cost_basis, position.average_cost) == (3, 150, 50)
    ledger.record("sell", symbol="MU", quantity="2", price="60", day="2026-01-03")
    with pytest.raises(ValidationError):
        ledger.void(event_id, "撤销后会超卖")


def test_cash_income_and_withdrawals_are_not_stock_pnl(ledger):
    with pytest.raises(ValidationError, match="先设置"):
        ledger.record("deposit", cash="10", day="2026-01-01")
    ledger.record("cash_set", cash="100", day="2026-01-01")
    ledger.record("deposit", cash="10", day="2026-01-02")
    ledger.record("income", cash="2.5", day="2026-01-03", note="分红")
    ledger.record("withdraw", cash="12.5", day="2026-01-04")
    assert ledger.view().cash == 100
    assert ledger.view().positions == {}


def test_seed_cannot_overwrite_existing_records(ledger):
    ledger.record("seed", symbol="MU", quantity="1", price="100", day="2026-01-01")
    with pytest.raises(ValidationError, match="已有记录"):
        ledger.record("seed", symbol="MU", quantity="2", price="200", day="2026-01-02")


def test_reopen_reconstructs_identical_state(tmp_path):
    path = tmp_path / "book.sqlite3"
    store = Store(path)
    ledger = Ledger(store)
    ledger.record("buy", symbol="MU", quantity="0.125", price="100", day="2026-01-01")
    ledger.watch("MSFT", "等待回调")
    store.set_setting("notification_origin", "test:FriendMessage:123")
    original = ledger.view()
    store.close()
    store = Store(path)
    assert Ledger(store).view() == original
    assert Ledger(store).watchlist()[0]["reason"] == "等待回调"
    assert store.get_setting("notification_origin") == "test:FriendMessage:123"
    store.close()


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-1", "1e99", "1.123456789", "", "1;DROP TABLE"]
)
def test_reject_invalid_amounts(value):
    with pytest.raises(ValidationError):
        amount(value)


@pytest.mark.parametrize("value", ["CN:600000", "MU/../x", "美光", "", "US:MU:extra"])
def test_reject_unsupported_or_unsafe_instruments(value):
    with pytest.raises(ValidationError):
        instrument(value)
