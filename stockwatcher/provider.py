"""Read-only Alpaca data adapter with bounded retries and explicit SIP cutoff."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from .config import Settings
from .domain import (
    NEW_YORK,
    Bar,
    MarketBatch,
    MarketSession,
    ValidationError,
    instrument,
    iso,
    parse_time,
    utc_time,
)


class MarketError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AlpacaProvider:
    DATA_URL = "https://data.alpaca.markets"
    ACCOUNT_URLS = {
        "paper": "https://paper-api.alpaca.markets",
        "live": "https://api.alpaca.markets",
    }

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        self._limit = asyncio.Semaphore(2)
        self._closed = False

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    async def _get(self, url: str, params: dict):
        if not self.settings.configured:
            raise MarketError(
                "credentials_missing",
                "尚未配置 Alpaca 凭证；请在插件配置页或本地环境变量中设置，不要发到聊天里。",
            )
        if self._closed:
            raise MarketError("closed", "行情客户端已关闭。")
        headers = {
            "APCA-API-KEY-ID": self.settings.key_id,
            "APCA-API-SECRET-KEY": self.settings.secret_key,
        }
        async with self._limit:
            for attempt in range(3):
                try:
                    response = await self._client.get(url, params=params, headers=headers)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt == 2:
                        raise MarketError(
                            "network", "行情请求超时或网络不可达，请稍后重试。"
                        ) from exc
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                except httpx.HTTPError as exc:
                    raise MarketError("transport", "行情传输失败，请检查网络配置。") from exc
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    if attempt < 2:
                        try:
                            delay = max(
                                0, min(3, float(response.headers.get("Retry-After", "0.5")))
                            )
                        except ValueError:
                            delay = 0.5
                        await asyncio.sleep(delay)
                        continue
                    raise MarketError(
                        "rate_limit" if response.status_code == 429 else "upstream",
                        "行情服务限流或暂时不可用，重试次数已用尽。",
                    )
                if response.status_code in {401, 403}:
                    raise MarketError(
                        "permission", "Alpaca 凭证无效或无该接口权限；请检查账户模式、凭证和订阅。"
                    )
                if response.status_code >= 300:
                    raise MarketError(
                        "http",
                        f"行情请求被拒绝（HTTP {response.status_code}）；请检查代码、参数和账户权限。",
                    )
                try:
                    return response.json(parse_float=Decimal)
                except (ValueError, UnicodeError) as exc:
                    raise MarketError("malformed", "行情服务返回了无效 JSON。") from exc
        raise AssertionError("unreachable")

    async def calendar(self, start: date, end: date) -> tuple[MarketSession, ...]:
        if end < start or (end - start).days > 800:
            raise ValidationError("交易日历范围需为 0～800 天。")
        data = await self._get(
            self.ACCOUNT_URLS[self.settings.account_mode] + "/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        if not isinstance(data, list):
            raise MarketError("malformed", "交易日历格式异常。")
        sessions = []
        seen = set()
        try:
            for item in data:
                day = date.fromisoformat(item["date"])
                opening_time = time.fromisoformat(item["open"])
                closing_time = time.fromisoformat(item["close"])
                if opening_time.tzinfo is not None or closing_time.tzinfo is not None:
                    raise ValueError("Expected market-local time")
                opening = datetime.combine(day, opening_time, NEW_YORK)
                closing = datetime.combine(day, closing_time, NEW_YORK)
                if day in seen or not start <= day <= end or opening >= closing:
                    raise ValueError("Invalid session")
                seen.add(day)
                sessions.append(
                    MarketSession(day, opening.astimezone(UTC), closing.astimezone(UTC))
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketError("malformed", "交易日历包含缺失、重复或异常的交易时段。") from exc
        return tuple(sorted(sessions, key=lambda session: session.day))

    async def asset(self, symbol: str) -> dict:
        symbol = instrument(symbol)
        data = await self._get(
            self.ACCOUNT_URLS[self.settings.account_mode] + f"/v2/assets/{symbol[3:]}", {}
        )
        if (
            not isinstance(data, dict)
            or data.get("symbol") != symbol[3:]
            or data.get("class") != "us_equity"
        ):
            raise MarketError("asset", "标的资料缺失、代码不匹配或不是美股股票/ETF。")
        # Persist/display only explicitly selected fields, not arbitrary server content.
        return {
            key: data.get(key)
            for key in ("symbol", "name", "exchange", "status", "class", "tradable", "fractionable")
        }

    @staticmethod
    def _bar(row: dict) -> Bar:
        try:
            numbers = {
                target: Decimal(str(row[source]))
                for target, source in (
                    ("open", "o"),
                    ("high", "h"),
                    ("low", "l"),
                    ("close", "c"),
                    ("volume", "v"),
                )
            }
            if any(not value.is_finite() for value in numbers.values()):
                raise ValueError("Non-finite price")
            if (
                any(numbers[key] <= 0 for key in ("open", "high", "low", "close"))
                or numbers["volume"] < 0
            ):
                raise ValueError("Negative or zero price")
            if numbers["low"] > min(numbers["open"], numbers["close"]) or numbers["high"] < max(
                numbers["open"], numbers["close"]
            ):
                raise ValueError("Inconsistent OHLC")
            return Bar(timestamp=parse_time(row["t"]), **numbers)
        except (TypeError, KeyError, ValueError, InvalidOperation) as exc:
            raise MarketError(
                "malformed", "行情包含缺失、非数值或异常 OHLCV 数据；本次结果未写入缓存。"
            ) from exc

    async def bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        timeframe: str = "1Day",
        adjustment: str = "raw",
        now: datetime | None = None,
    ) -> MarketBatch:
        symbol = instrument(symbol)
        now = utc_time(now or datetime.now(UTC))
        start = utc_time(start)
        cutoff = min(utc_time(end), now - timedelta(minutes=self.settings.delay_minutes))
        if not start < cutoff or (cutoff - start).days > 730:
            raise ValidationError("行情范围应早于免费访问截止时间，且不超过 730 天。")
        if timeframe not in {"1Day", "1Min"} or adjustment not in {
            "raw",
            "split",
            "dividend",
            "all",
        }:
            raise ValidationError("仅支持 1Day/1Min 和 raw/split/dividend/all 复权口径。")
        if timeframe == "1Min" and (cutoff - start).days > 10:
            raise ValidationError("单次分钟查询不超过 10 天，请缩小范围。")
        sessions = await self.calendar(
            start.astimezone(NEW_YORK).date(), cutoff.astimezone(NEW_YORK).date()
        )
        by_day = {session.day: session for session in sessions}
        params = {
            "symbols": symbol[3:],
            "timeframe": timeframe,
            "start": iso(start),
            "end": iso(cutoff),
            "feed": "sip",
            "adjustment": adjustment,
            "limit": 10000,
            "sort": "asc",
        }
        bars: dict[datetime, Bar] = {}
        seen_tokens = set()
        for _ in range(50):
            data = await self._get(self.DATA_URL + "/v2/stocks/bars", params)
            if (
                not isinstance(data, dict)
                or "bars" not in data
                or not isinstance(data.get("bars"), (dict, type(None)))
            ):
                raise MarketError("malformed", "行情响应缺少 bars 数据。")
            rows = (data.get("bars") or {}).get(symbol[3:], [])
            if not isinstance(rows, list):
                raise MarketError("malformed", "行情 bars 格式异常。")
            if len(rows) > 10000:
                raise MarketError("malformed", "单页行情超过请求上限，拒绝保存。")
            for row in rows:
                bar = self._bar(row)
                if not start <= bar.timestamp <= cutoff:
                    raise MarketError("malformed", "行情包含请求范围外的时间。")
                session = by_day.get(bar.timestamp.astimezone(NEW_YORK).date())
                if session is None:
                    raise MarketError("calendar_mismatch", "行情与交易日历不一致，请检查数据来源。")
                if timeframe == "1Min":
                    # End timestamps describe starts of bars. Partial bars are not accepted.
                    if not session.opens_at <= bar.timestamp or bar.timestamp + timedelta(
                        minutes=1
                    ) > min(session.closes_at, cutoff):
                        continue
                elif session.closes_at > cutoff:
                    continue  # Today's midnight daily bar does not make today's session complete.
                if bar.timestamp in bars and bars[bar.timestamp] != bar:
                    raise MarketError("malformed", "同一时间存在冲突行情，拒绝合并。")
                bars[bar.timestamp] = bar
            token = data.get("next_page_token")
            if token is None:
                break
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise MarketError("pagination", "行情分页令牌异常，未保存不完整结果。")
            seen_tokens.add(token)
            params["page_token"] = token
        else:
            raise MarketError("pagination", "行情超过分页上限，请缩小查询范围。")
        return MarketBatch(
            instrument=symbol,
            timeframe=timeframe,
            adjustment=adjustment,
            session="regular" if timeframe == "1Min" else "provider_daily",
            requested_start=start,
            requested_end=cutoff,
            fetched_at=datetime.now(UTC),
            bars=tuple(bars[key] for key in sorted(bars)),
        )
