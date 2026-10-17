"""Market query orchestration; caches are explicit, never silently treated as live."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .domain import MarketBatch, instrument
from .provider import AlpacaProvider
from .storage import Store


class MarketService:
    def __init__(self, provider: AlpacaProvider, store: Store):
        self.provider = provider
        self.store = store

    async def query(
        self,
        symbol: str,
        *,
        timeframe: str = "1Day",
        adjustment: str = "raw",
        now: datetime | None = None,
    ) -> MarketBatch:
        now = now or datetime.now(UTC)
        start = now - timedelta(days=7 if timeframe == "1Min" else 550)
        batch = await self.provider.bars(
            symbol, start, now, timeframe=timeframe, adjustment=adjustment, now=now
        )
        await asyncio.to_thread(self.store.save_batch, batch)
        return batch

    async def cached(
        self, symbol: str, timeframe: str = "1Day", adjustment: str = "raw"
    ) -> MarketBatch | None:
        return await asyncio.to_thread(
            self.store.latest_batch, instrument(symbol), timeframe, adjustment
        )

    async def diagnose(self, symbol: str) -> dict:
        asset = await self.provider.asset(symbol)
        daily = await self.query(symbol)
        adjusted = await self.query(symbol, adjustment="all")
        minute = await self.query(symbol, timeframe="1Min")
        return {
            "source": "alpaca",
            "asset": asset,
            "instrument": daily.instrument,
            "feed": "sip",
            "daily_count": len(daily.bars),
            "adjusted_daily_count": len(adjusted.bars),
            "regular_minute_count": len(minute.bars),
            "query_cutoff": minute.as_dict()["requested_end"],
            "fetched_at": minute.as_dict()["fetched_at"],
            "latest_minute": minute.bars[-1].as_dict() if minute.bars else None,
            "daily_session": "provider_daily",
            "daily_adjustment": "raw",
            "limitations": [
                "供应商日线时段覆盖尚需实测，不标称纯常规时段日线。",
                "可访问和字段校验通过不代表免费服务长期可用性已经验证。",
                "返回空集可能与标的无数据、历史覆盖或停牌有关，需要进一步核实。",
            ],
        }
