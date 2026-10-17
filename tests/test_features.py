from dataclasses import replace
from decimal import Decimal as D

import pytest
from research_helpers import batch

from stockwatcher.domain import ValidationError
from stockwatcher.features import metrics, quantity_cap, risk_context
from stockwatcher.profiles import Profiles


def test_indicators_are_reproducible_and_levels_convert_to_raw():
    result = metrics(batch(), batch(adjustment="raw", factor=D(2)), batch("SPY"))
    assert result["ma20"] == "319.0"
    assert result["ma60"] == "279.0"
    assert result["atr14"] == "4"
    assert result["volume_ratio20"] == "1"
    assert result["trend"] == "up"
    assert result["two_positive_closes"] is True
    assert D(result["return5_pct"]) == pytest.approx(D(5) / 164 * 100)
    assert D(result["relative_return20_pct"]) == 0
    assert D(result["volatility20_annual_pct"]) > 0


def test_short_history_does_not_become_medium_risk(store, ledger):
    adjusted, raw = batch(count=10), batch(adjustment="raw", count=10)
    features = metrics(adjusted, raw)
    profile = Profiles(store).resolved("MU")
    risk = risk_context(
        features, profile, ledger.view(), {}, adjusted=adjusted, events=[], market_fresh=True
    )
    assert features["trend"] == "unknown"
    assert risk["risk"]["short_term"]["level"] == "unknown"
    assert risk["equity"] is None
    assert risk["increase_allowed"] is False


def test_mismatched_price_basis_is_rejected():
    with pytest.raises(ValidationError):
        metrics(batch(adjustment="raw"), batch(adjustment="raw"))
    with pytest.raises(ValidationError):
        metrics(batch(), replace(batch(adjustment="raw"), bars=batch(adjustment="raw").bars[:-1]))


def test_risk_budget_and_missing_other_holding_quote(store, ledger):
    profiles = Profiles(store)
    profiles.update(
        {"allow_add": True, "max_stock_pct": "50", "max_total_pct": "80", "risk_budget_pct": "1"}
    )
    ledger.record("cash_set", cash="10000", day="2026-01-01")
    adjusted, raw = batch(), batch(adjustment="raw")
    features = metrics(adjusted, raw)
    context = risk_context(
        features,
        profiles.resolved("MU"),
        ledger.view(),
        {"US:MU": "169"},
        adjusted=adjusted,
        events=[],
        market_fresh=True,
    )
    assert context["gross_allocation_cap"] == "5000"
    assert quantity_cap(context, D(100), D(90)) == "10"
    ledger.record("seed", symbol="AAPL", quantity="1", price="100", day="2026-01-01")
    context = risk_context(
        features,
        profiles.resolved("MU"),
        ledger.view(),
        {"US:MU": "169"},
        adjusted=adjusted,
        events=[],
        market_fresh=True,
    )
    assert context["missing_quotes"] == ["US:AAPL"]
    assert context["equity"] is None
    assert not context["increase_allowed"]


def test_cost_drawdown_soft_vs_hard_and_unknown_holding_peak(store, ledger):
    profiles = Profiles(store)
    ledger.record("seed", symbol="MU", quantity="10", price="220", day="2026-01-01")
    adjusted, raw = batch(), batch(adjustment="raw")
    features = metrics(adjusted, raw)
    events = store.rows("SELECT * FROM ledger_events ORDER BY event_date,id")

    def risk():
        return risk_context(
            features,
            profiles.resolved("MU"),
            ledger.view(),
            {"US:MU": "169"},
            adjusted=adjusted,
            events=events,
            market_fresh=True,
        )

    profiles.update({"drawdown_pct": "20", "drawdown_basis": "cost"})
    assert risk()["warnings"] and not risk()["hard_exit_reasons"]
    profiles.update({"drawdown_effect": "exit"})
    assert risk()["hard_exit_reasons"]
    profiles.update({"drawdown_basis": "holding_high"})
    assert risk()["drawdown_pct"] is None
    assert not risk()["hard_exit_reasons"]


def test_user_goal_action_is_explicit(store, ledger):
    profiles = Profiles(store)
    ledger.record("seed", symbol="MU", quantity="1", price="100", day="2026-01-01")
    profiles.update({"target_price": "150", "target_action": "exit"}, symbol="MU")
    context = risk_context(
        metrics(batch(), batch(adjustment="raw")),
        profiles.resolved("MU"),
        ledger.view(),
        {"US:MU": "169"},
        adjusted=batch(),
        events=[],
        market_fresh=True,
    )
    assert context["target_reached"]
    assert context["hard_exit_reasons"]


def test_zero_risk_budget_is_a_real_prohibition(store, ledger):
    profiles = Profiles(store)
    profiles.update({"max_stock_pct": "50", "max_total_pct": "80", "risk_budget_pct": "0"})
    ledger.record("cash_set", cash="10000", day="2026-01-01")
    context = risk_context(
        metrics(batch(), batch(adjustment="raw")),
        profiles.resolved("MU"),
        ledger.view(),
        {"US:MU": "169"},
        adjusted=batch(),
        events=[],
        market_fresh=True,
    )
    assert context["risk_budget_amount"] == "0"
    assert context["gross_allocation_cap"] == "5000"
    assert not context["increase_allowed"]
    assert quantity_cap(context, D(100), D(90)) is None
