"""Deterministic descriptive metrics and personal risk constraints, no probabilities."""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_DOWN, Decimal, localcontext

from .domain import NEW_YORK, LedgerView, MarketBatch, ValidationError

RULE_VERSION = "daily-trend-0.2.0"
D = Decimal


def _pct(end: Decimal, start: Decimal) -> str | None:
    return str((end / start - 1) * 100) if start > 0 else None


def metrics(adjusted: MarketBatch, raw: MarketBatch, benchmark: MarketBatch | None = None) -> dict:
    if (
        adjusted.instrument != raw.instrument
        or adjusted.timeframe != "1Day"
        or raw.timeframe != "1Day"
        or raw.adjustment != "raw"
        or adjusted.adjustment != "all"
        or adjusted.feed != raw.feed
        or adjusted.session != raw.session
    ):
        raise ValidationError("技术与原始行情口径不一致。")
    if not adjusted.bars or not raw.bars:
        raise ValidationError("日线不足，无法形成分析。")
    bars = adjusted.bars
    if bars[-1].timestamp != raw.bars[-1].timestamp:
        raise ValidationError("原始和调整日线不是同一交易日，拒绝混算。")
    if any(a.timestamp >= b.timestamp for a, b in zip(bars, bars[1:], strict=False)):
        raise ValidationError("日线必须按时间递增，且不能重复。")
    with localcontext() as context:
        context.prec = 34
        closes = [bar.close for bar in bars]
        factor = raw.bars[-1].close / closes[-1]
        result = {
            "rule_version": RULE_VERSION,
            "instrument": raw.instrument,
            "trade_date": bars[-1].timestamp.astimezone(NEW_YORK).date().isoformat(),
            "bar_count": len(bars),
            "reference_price": str(raw.bars[-1].close),
            "price_basis": "raw",
            "technical_adjustment": adjusted.adjustment,
            "level_conversion_factor": str(factor),
            "daily_session": adjusted.session,
            "ma20": None,
            "ma60": None,
            "atr14": None,
            "atr_pct": None,
            "volatility20_annual_pct": None,
            "volume_ratio20": None,
            "return5_pct": None,
            "return10_pct": None,
            "return20_pct": None,
            "deviation20_pct": None,
            "drawdown20_high_pct": None,
            "relative_return20_pct": None,
            "low20": None,
            "high20": None,
            "two_positive_closes": len(closes) >= 3 and closes[-1] > closes[-2] > closes[-3],
        }
        year = bars[-1].timestamp.astimezone(NEW_YORK).year
        this_year = [bar for bar in bars if bar.timestamp.astimezone(NEW_YORK).year == year]
        prior = [bar for bar in bars if bar.timestamp.astimezone(NEW_YORK).year < year]
        result.update(
            year=year,
            year_start_close=str(prior[-1].close * factor) if prior else None,
            year_return_pct=_pct(closes[-1], prior[-1].close) if prior else None,
            year_high=str(max(bar.high for bar in this_year) * factor),
            year_low=str(min(bar.low for bar in this_year) * factor),
            year_range_complete=bool(prior),
        )
        for window in (20, 60):
            if len(closes) >= window:
                result[f"ma{window}"] = str(sum(closes[-window:]) / window * factor)
        for window in (5, 10, 20):
            if len(closes) > window:
                result[f"return{window}_pct"] = _pct(closes[-1], closes[-window - 1])
        if len(bars) >= 20:
            high, low = max(bar.high for bar in bars[-20:]), min(bar.low for bar in bars[-20:])
            result.update(
                high20=str(high * factor),
                low20=str(low * factor),
                drawdown20_high_pct=str(max(D(0), (1 - closes[-1] / high) * 100)),
            )
            result["deviation20_pct"] = _pct(raw.bars[-1].close, D(result["ma20"]))
        if len(bars) >= 15:
            ranges = [
                max(
                    bar.high - bar.low,
                    abs(bar.high - previous.close),
                    abs(bar.low - previous.close),
                )
                for previous, bar in zip(bars[-15:-1], bars[-14:], strict=True)
            ]
            atr = sum(ranges) / 14
            result.update(atr14=str(atr * factor), atr_pct=str(atr / closes[-1] * 100))
        if len(closes) >= 21:
            returns = [
                closes[index] / closes[index - 1] - 1
                for index in range(len(closes) - 20, len(closes))
            ]
            mean = sum(returns) / 20
            variance = sum((value - mean) ** 2 for value in returns) / 19
            result["volatility20_annual_pct"] = str(variance.sqrt() * D(252).sqrt() * 100)
            average_volume = sum(bar.volume for bar in bars[-21:-1]) / 20
            result["volume_ratio20"] = (
                str(bars[-1].volume / average_volume) if average_volume > 0 else None
            )
        if benchmark and benchmark.timeframe == "1Day" and benchmark.bars:
            own, other = (
                {bar.timestamp: bar.close for bar in bars},
                {bar.timestamp: bar.close for bar in benchmark.bars},
            )
            common = sorted(own.keys() & other.keys())
            if len(common) >= 21 and common[-1] == bars[-1].timestamp:
                start, end = common[-21], common[-1]
                result["relative_return20_pct"] = str(
                    (own[end] / own[start] - other[end] / other[start]) * 100
                )
        result["trend"] = "unknown"
        if result["ma60"] is not None:
            result["trend"] = (
                "up" if raw.bars[-1].close > D(result["ma20"]) > D(result["ma60"]) else "weak"
            )
        return result


def holding_cycle(events: list[dict], symbol: str) -> tuple[date | None, bool]:
    quantity, started, complete = D(0), None, False
    for row in events:
        if row["instrument"] != symbol or row["voided_at"]:
            continue
        payload = json.loads(row["payload"])
        if row["kind"] in {"seed", "buy"}:
            if quantity == 0:
                started = date.fromisoformat(row["event_date"])
                complete = row["kind"] == "buy"
            quantity += D(payload["quantity"])
        elif row["kind"] == "sell":
            quantity -= D(payload["quantity"])
            if quantity == 0:
                started, complete = None, False
        elif row["kind"] == "split":
            quantity *= D(payload["numerator"]) / D(payload["denominator"])
    return started, complete


def risk_context(
    features: dict,
    profile: dict,
    ledger: LedgerView,
    prices: dict[str, str],
    *,
    adjusted: MarketBatch,
    events: list[dict],
    market_fresh: bool,
) -> dict:
    values, symbol = profile["values"], profile["instrument"]
    price = D(features["reference_price"])
    position = ledger.positions.get(symbol)
    held = bool(position and position.quantity > 0)
    missing_quotes = [
        key for key, pos in ledger.positions.items() if pos.quantity > 0 and key not in prices
    ]
    gross = sum(
        (
            pos.quantity * D(prices[key])
            for key, pos in ledger.positions.items()
            if pos.quantity > 0 and key in prices
        ),
        D(0),
    )
    equity = ledger.cash + gross if ledger.cash is not None and not missing_quotes else None
    exposure = position.quantity * price if held else D(0)
    weight = exposure / equity * 100 if equity is not None and equity > 0 else None
    dd = None
    basis = values["drawdown_basis"]
    if held and basis == "cost" and position.average_cost > 0:
        dd = max(D(0), (1 - price / position.average_cost) * 100)
    elif held and basis == "rolling20_high" and features["drawdown20_high_pct"] is not None:
        dd = D(features["drawdown20_high_pct"])
    elif held and basis == "holding_high":
        started, complete = holding_cycle(events, symbol)
        first = adjusted.bars[0].timestamp.astimezone(NEW_YORK).date()
        if complete and started and first <= started:
            eligible = [
                bar.high
                for bar in adjusted.bars
                if bar.timestamp.astimezone(NEW_YORK).date() >= started
            ]
            if eligible:
                high = max(eligible) * D(features["level_conversion_factor"])
                dd = max(D(0), (1 - price / high) * 100)
    hard, warnings, missing = [], [], []
    target_reached = values["target_price"] is not None and price >= D(values["target_price"])
    if held and target_reached:
        if values["target_action"] == "exit":
            hard.append("达到个人目标且用户明确选择到价退出")
        else:
            warnings.append("达到个人目标，按用户偏好" + values["target_action"])
    if held and values["hard_stop_price"] is not None and price <= D(values["hard_stop_price"]):
        hard.append("触及用户明确的退出价")
    if values["drawdown_pct"] is not None and held:
        if dd is None:
            warnings.append("所选回撤基准缺少完整历史，未据此判断退出")
        elif dd >= D(values["drawdown_pct"]):
            (hard if values["drawdown_effect"] == "exit" else warnings).append(
                "触及用户回撤阈值：" + values["drawdown_effect"]
            )
    for field in ("max_stock_pct", "max_total_pct", "risk_budget_pct"):
        if values[field] is None:
            missing.append(field)
    if equity is None or equity <= 0:
        missing.append("完整现金与组合估值")
    if not market_fresh:
        missing.append("可核实的新近完整行情")
    if features["trend"] == "unknown":
        missing.append("至少60个交易日的趋势数据")
    if (
        weight is not None
        and values["max_stock_pct"] is not None
        and weight > D(values["max_stock_pct"])
    ):
        warnings.append("单股权重超过个人上限")
    total_pct = gross / equity * 100 if equity is not None and equity > 0 else None
    if (
        total_pct is not None
        and values["max_total_pct"] is not None
        and total_pct > D(values["max_total_pct"])
    ):
        warnings.append("总仓位超过个人上限")
    budget = None
    if not missing:
        stock_room = equity * D(values["max_stock_pct"]) / 100 - exposure
        total_room = equity * D(values["max_total_pct"]) / 100 - gross
        budget = max(D(0), min(stock_room, total_room, ledger.cash))
    risk = {
        "short_term": {
            "level": "unknown",
            "method": "descriptive-v1",
            "window": "过去20日指标；观察未来5交易日，不是概率",
        },
        "position": {"level": "unknown", "weight_pct": str(weight) if weight is not None else None},
        "event": {"level": "unknown", "reason": "尚未完整核实事件日历"},
        "thesis": {"level": "unknown", "reason": "交由基于证据的 AI 研判"},
    }
    volatility, atr = features["volatility20_annual_pct"], features["atr_pct"]
    if volatility is not None and atr is not None:
        high = D(volatility) >= 50 or D(atr) >= 4
        medium = D(volatility) >= 25 or D(atr) >= 2
        risk["short_term"].update(
            level="high" if high else "medium" if medium else "low",
            volatility20_annual_pct=volatility,
            atr_pct=atr,
        )
    if weight is not None and values["max_stock_pct"] is not None:
        cap = D(values["max_stock_pct"])
        risk["position"]["level"] = (
            "high" if weight > cap else "medium" if weight > cap * D("0.8") else "low"
        )
    return {
        "held": held,
        "quantity": str(position.quantity) if held else "0",
        "average_cost": str(position.average_cost) if held else None,
        "cash": str(ledger.cash) if ledger.cash is not None else None,
        "equity": str(equity) if equity is not None else None,
        "weight_pct": str(weight) if weight is not None else None,
        "total_weight_pct": str(total_pct) if total_pct is not None else None,
        "missing_quotes": missing_quotes,
        "drawdown_pct": str(dd) if dd is not None else None,
        "drawdown_basis": basis,
        "hard_exit_reasons": hard,
        "warnings": warnings,
        "missing": missing,
        "target_reached": target_reached,
        "target_action": values["target_action"],
        "increase_allowed": not missing
        and not hard
        and not warnings
        and budget is not None
        and budget > 0
        and D(values["risk_budget_pct"]) > 0
        and (not held or values["allow_add"] is True),
        "gross_allocation_cap": str(budget) if budget is not None else None,
        "risk_budget_amount": str(equity * D(values["risk_budget_pct"]) / 100)
        if equity is not None and values["risk_budget_pct"] is not None
        else None,
        "stress_scenario": {
            "assumed_decline_pct": "20",
            "holding_loss_usd": str(exposure * D("0.2")),
            "account_loss_pct": str(exposure * D("0.2") / equity * 100)
            if equity and equity > 0
            else None,
            "meaning": "人为压力情景，不是预测或最大损失保证",
        },
        "risk": risk,
    }


def quantity_cap(context: dict, entry: Decimal, stop: Decimal) -> str | None:
    if not context["increase_allowed"] or context["risk_budget_amount"] is None or stop >= entry:
        return None
    # Conservative whole-share gross upper bound; not settled funds or an executable order.
    quantity = min(
        D(context["gross_allocation_cap"]) / entry,
        D(context["risk_budget_amount"]) / (entry - stop),
    )
    return str(quantity.to_integral_value(rounding=ROUND_DOWN))
