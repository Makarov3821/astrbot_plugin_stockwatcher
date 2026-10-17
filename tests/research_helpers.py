import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from stockwatcher.domain import Bar, MarketBatch, MarketSession, instrument
from stockwatcher.model import ModelReply

NOW = datetime(2026, 1, 6, 22, tzinfo=UTC)


def batch(symbol="MU", adjustment="all", count=70, factor=D(1)):
    end = datetime(2026, 1, 6, 5, tzinfo=UTC)
    dates = []
    day = end
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day)
        day -= timedelta(days=1)
    dates.reverse()
    bars = tuple(
        Bar(
            stamp,
            (D(100) + i) * factor,
            (D(101) + i) * factor,
            (D(99) + i) * factor,
            (D(100) + i) * factor,
            D(100),
        )
        for i, stamp in enumerate(dates)
    )
    return MarketBatch(
        instrument(symbol),
        "1Day",
        adjustment,
        "provider_daily",
        dates[0],
        NOW - timedelta(minutes=16),
        NOW,
        bars,
    )


class FakeMarket:
    provider = None

    def __init__(self):
        self.provider = self
        self.calls = []

    async def query(self, symbol, *, adjustment="raw", now=None):
        self.calls.append((symbol, adjustment))
        return batch(symbol, adjustment)

    async def cached(self, symbol, *, adjustment="raw"):
        return batch(symbol, adjustment)

    async def calendar(self, start, end):
        return [MarketSession(NOW.date(), NOW.replace(hour=14, minute=30), NOW.replace(hour=21))]


def reply_data(**changes):
    data = {
        "action": "watch",
        "summary": "等待回调并复核信息",
        "horizon_days": 60,
        "valid_for_days": 1,
        "reasons": [{"text": "价格在均线上方", "evidence_ids": ["metrics"]}],
        "counterarguments": [{"text": "缺少未来事件日历", "evidence_ids": ["metrics"]}],
        "thesis": {"status": "unknown", "text": "材料不足", "evidence_ids": []},
        "event_risk": {"level": "unknown", "reason": "日历未核实", "evidence_ids": []},
        "target_proposal": None,
        "entry": None,
        "invalidations": ["新公告改变原判断"],
    }
    return data | changes


class FakeModel:
    def __init__(self, data=None, hook=None):
        self.data = data or reply_data()
        self.hook = hook
        self.calls = []

    async def generate(self, system, prompt, origin):
        self.calls.append(json.loads(prompt))
        if self.hook:
            self.hook()
        return ModelReply(json.dumps(self.data), {"provider_id": "test-model"})
