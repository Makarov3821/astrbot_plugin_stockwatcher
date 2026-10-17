"""Validated identifiers, exact ledger amounts, and timezone-aware market records."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
ZERO = Decimal("0")


class ValidationError(ValueError):
    """An input or state that can be explained to the user."""


def instrument(value: str) -> str:
    value = value.strip().upper()
    if ":" in value:
        market, value = value.split(":", 1)
        if market != "US":
            raise ValidationError("当前版本仅支持美股，示例：US:MU 或 MU。")
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", value):
        raise ValidationError("请填写美股代码，例如 MU、AAPL、BRK.B，不使用公司名称。")
    return f"US:{value}"


def amount(value: str | Decimal, *, positive: bool = False) -> Decimal:
    text = str(value)
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,8})?", text):
        raise ValidationError("金额或数量必须是非负十进制数，最多 12 位整数、8 位小数。")
    result = Decimal(text)
    if positive and result <= ZERO:
        raise ValidationError("数量和价格必须大于零。")
    return result


def trade_day(value: str | None, *, today: date | None = None) -> str:
    today = today or datetime.now(NEW_YORK).date()
    try:
        result = date.fromisoformat(value) if value else today
    except (TypeError, ValueError) as exc:
        raise ValidationError("日期格式应为 YYYY-MM-DD。") from exc
    if result > today:
        raise ValidationError("不能登记未来日期的实际成交或余额。")
    return result.isoformat()


def utc_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("行情时间必须包含时区。")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    return utc_time(value).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("行情时间字段不是有效的 ISO 时间字符串。")
    return utc_time(datetime.fromisoformat(value.replace("Z", "+00:00")))


def text_number(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)


@dataclass
class Position:
    instrument: str
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    has_opening_seed: bool = False

    @property
    def average_cost(self) -> Decimal:
        return self.cost_basis / self.quantity if self.quantity else ZERO


@dataclass
class LedgerView:
    positions: dict[str, Position] = field(default_factory=dict)
    cash: Decimal | None = None


@dataclass(frozen=True)
class MarketSession:
    day: date
    opens_at: datetime
    closes_at: datetime


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def as_dict(self) -> dict:
        return {
            "timestamp": iso(self.timestamp),
            **{
                name: str(getattr(self, name))
                for name in ("open", "high", "low", "close", "volume")
            },
        }


@dataclass(frozen=True)
class MarketBatch:
    instrument: str
    timeframe: str
    adjustment: str
    session: str
    requested_start: datetime
    requested_end: datetime
    fetched_at: datetime
    bars: tuple[Bar, ...]
    feed: str = "sip"
    source: str = "alpaca"

    def as_dict(self) -> dict:
        return {
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "adjustment": self.adjustment,
            "session": self.session,
            "requested_start": iso(self.requested_start),
            "requested_end": iso(self.requested_end),
            "fetched_at": iso(self.fetched_at),
            "feed": self.feed,
            "source": self.source,
            "bars": [bar.as_dict() for bar in self.bars],
        }

    @classmethod
    def from_dict(cls, data: dict) -> MarketBatch:
        return cls(
            instrument=data["instrument"],
            timeframe=data["timeframe"],
            adjustment=data["adjustment"],
            session=data["session"],
            requested_start=parse_time(data["requested_start"]),
            requested_end=parse_time(data["requested_end"]),
            fetched_at=parse_time(data["fetched_at"]),
            feed=data["feed"],
            source=data["source"],
            bars=tuple(
                Bar(
                    timestamp=parse_time(row["timestamp"]),
                    **{
                        name: Decimal(row[name])
                        for name in ("open", "high", "low", "close", "volume")
                    },
                )
                for row in data["bars"]
            ),
        )
