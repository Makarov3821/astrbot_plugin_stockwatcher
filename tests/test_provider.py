import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from stockwatcher.config import Settings
from stockwatcher.domain import MarketBatch, ValidationError, parse_time
from stockwatcher.market import MarketService
from stockwatcher.provider import AlpacaProvider, MarketError

NOW = datetime(2026, 1, 6, 17, tzinfo=UTC)
START = datetime(2026, 1, 5, tzinfo=UTC)
CALENDAR = [
    {"date": "2026-01-05", "open": "09:30", "close": "16:00"},
    {"date": "2026-01-06", "open": "09:30", "close": "16:00"},
]


def raw_bar(stamp, *, close=10):
    return {"t": stamp, "o": 10, "h": 12, "l": 9, "c": close, "v": 100}


def client(handler, settings=None):
    return AlpacaProvider(
        settings or Settings(key_id="test-key", secret_key="test-secret"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_sip_cutoff_pagination_and_completed_daily_sessions():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("calendar"):
            return httpx.Response(200, json=CALENDAR)
        assert request.url.params["feed"] == "sip"
        assert request.url.params["end"] == "2026-01-06T16:44:00Z"
        assert request.headers["APCA-API-SECRET-KEY"] == "test-secret"
        if "page_token" not in request.url.params:
            return httpx.Response(
                200,
                json={"bars": {"MU": [raw_bar("2026-01-05T05:00:00Z")]}, "next_page_token": "next"},
            )
        return httpx.Response(
            200, json={"bars": {"MU": [raw_bar("2026-01-06T05:00:00Z")]}, "next_page_token": None}
        )

    async def scenario():
        provider = client(handler)
        try:
            batch = await provider.bars("MU", START, NOW, now=NOW)
            assert (
                len(batch.bars) == 1
            )  # Today's daily bar is not complete despite its midnight timestamp.
            assert batch.bars[0].close == Decimal("10")
            assert batch.session == "provider_daily"
            assert MarketBatch.from_dict(batch.as_dict()) == batch
        finally:
            await provider.close()

    asyncio.run(scenario())
    assert len(requests) == 3


def test_minute_filter_excludes_premarket_and_partial_bar():
    def handler(request):
        if request.url.path.endswith("calendar"):
            return httpx.Response(200, json=CALENDAR)
        return httpx.Response(
            200,
            json={
                "bars": {
                    "MU": [
                        raw_bar(t)
                        for t in (
                            "2026-01-06T14:29:00Z",
                            "2026-01-06T14:30:00Z",
                            "2026-01-06T16:43:00Z",
                            "2026-01-06T16:44:00Z",
                        )
                    ]
                }
            },
        )

    async def scenario():
        provider = client(handler)
        try:
            batch = await provider.bars("MU", START, NOW, timeframe="1Min", now=NOW)
            assert [b.timestamp.minute for b in batch.bars] == [30, 43]
            assert batch.session == "regular"
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_calendar_dst_and_early_close():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"date": "2026-03-06", "open": "09:30", "close": "16:00"},
                {"date": "2026-03-09", "open": "09:30", "close": "13:00"},
            ],
        )

    async def scenario():
        provider = client(handler)
        try:
            sessions = await provider.calendar(date(2026, 3, 6), date(2026, 3, 9))
            assert sessions[0].opens_at.hour == 14
            assert sessions[1].opens_at.hour == 13
            assert sessions[1].closes_at.hour == 17
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,code", [(401, "permission"), (403, "permission"), (422, "http"), (302, "http")]
)
def test_errors_do_not_expose_server_body_or_credentials(status, code):
    def handler(request):
        return httpx.Response(
            status,
            text="test-secret sensitive response",
            headers={"Location": "https://example.com"},
        )

    async def scenario():
        provider = client(handler)
        try:
            with pytest.raises(MarketError) as captured:
                await provider.calendar(date(2026, 1, 5), date(2026, 1, 5))
            assert captured.value.code == code
            assert "test-secret" not in str(captured.value)
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_rate_limit_retry_is_bounded():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"})

    async def scenario():
        provider = client(handler)
        try:
            with pytest.raises(MarketError, match="重试次数"):
                await provider.calendar(date(2026, 1, 5), date(2026, 1, 5))
        finally:
            await provider.close()

    asyncio.run(scenario())
    assert len(calls) == 3


def test_missing_credentials_makes_no_network_request():
    def handler(request):
        pytest.fail("No network request is expected")

    async def scenario():
        provider = client(handler, Settings())
        try:
            with pytest.raises(MarketError) as captured:
                await provider.calendar(date(2026, 1, 5), date(2026, 1, 5))
            assert captured.value.code == "credentials_missing"
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutation",
    ["nan", "ohlc", "outside", "duplicate", "token", "missing", "bad_time", "missing_bars"],
)
def test_malformed_or_incomplete_data_is_not_cached(store, mutation):
    def handler(request):
        if request.url.path.endswith("calendar"):
            return httpx.Response(200, json=CALENDAR)
        row = raw_bar("2026-01-05T05:00:00Z")
        data = {"bars": {"MU": [row]}}
        if mutation == "nan":
            row["c"] = "NaN"
        elif mutation == "ohlc":
            row["c"] = 20
        elif mutation == "outside":
            row["t"] = "2026-01-07T05:00:00Z"
        elif mutation == "duplicate":
            data["bars"]["MU"].append(raw_bar(row["t"], close=11))
        elif mutation == "token":
            data["next_page_token"] = "repeated"
        elif mutation == "missing":
            row.pop("v")
        elif mutation == "bad_time":
            row["t"] = None
        elif mutation == "missing_bars":
            data = {"unexpected": "not a valid bars response"}
        return httpx.Response(200, json=data)

    async def scenario():
        provider = client(handler)
        service = MarketService(provider, store)
        try:
            with pytest.raises(MarketError):
                await service.query("MU", now=NOW)
            assert await service.cached("MU") is None
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_cache_separates_adjustments_and_does_not_silently_replace_live(store):
    failing = False

    def handler(request):
        if failing:
            return httpx.Response(403)
        if request.url.path.endswith("calendar"):
            return httpx.Response(200, json=CALENDAR)
        return httpx.Response(200, json={"bars": {"MU": [raw_bar("2026-01-05T05:00:00Z")]}})

    async def scenario():
        nonlocal failing
        provider = client(handler)
        service = MarketService(provider, store)
        try:
            batch = await service.query("MU", now=NOW)
            assert await service.cached("MU") == batch
            assert await service.cached("MU", adjustment="all") is None
            failing = True
            with pytest.raises(MarketError):
                await service.query("MU", now=NOW)
            assert await service.cached("MU") == batch
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_reject_naive_time_before_network():
    async def scenario():
        provider = client(lambda request: pytest.fail("No request expected"))
        try:
            with pytest.raises(ValidationError, match="时区"):
                await provider.bars("MU", datetime(2026, 1, 5), NOW, now=NOW)
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_cache_snapshot_revisions_do_not_merge_adjusted_vintages(store):
    def handler(request):
        if request.url.path.endswith("calendar"):
            return httpx.Response(200, json=CALENDAR)
        return httpx.Response(200, json={"bars": {"MU": [raw_bar("2026-01-05T05:00:00Z")]}})

    async def scenario():
        provider = client(handler)
        service = MarketService(provider, store)
        try:
            original = await service.query("MU", now=NOW, adjustment="all")
            from dataclasses import replace

            revised = replace(original, bars=())
            store.save_batch(revised)
            assert (await service.cached("MU", adjustment="all")).bars == ()
            assert len(store.rows("SELECT id FROM market_snapshots")) == 2
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_secret_repr_and_config_validation():
    settings = Settings(key_id="private-id", secret_key="private-secret")
    assert "private" not in repr(settings)
    with pytest.raises(ValidationError):
        Settings(delay_minutes=15)
    assert parse_time("2026-01-05T05:00:00Z").tzinfo == UTC


def test_asset_metadata_and_read_only_endpoints():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.host == "paper-api.alpaca.markets"
        assert request.url.path == "/v2/assets/MU"
        return httpx.Response(
            200,
            json={
                "symbol": "MU",
                "class": "us_equity",
                "exchange": "NASDAQ",
                "status": "active",
                "unexpected_secret": "not part of metadata",
            },
        )

    async def scenario():
        provider = client(handler)
        try:
            asset = await provider.asset("US:MU")
            assert asset["exchange"] == "NASDAQ"
            assert "unexpected_secret" not in asset
        finally:
            await provider.close()

    asyncio.run(scenario())
    assert len(seen) == 1


@pytest.mark.parametrize(
    "payload", [{"symbol": "AAPL", "class": "us_equity"}, {"symbol": "MU", "class": "crypto"}, None]
)
def test_reject_mismatched_asset(payload):
    async def scenario():
        provider = client(lambda request: httpx.Response(200, json=payload))
        try:
            with pytest.raises(MarketError):
                await provider.asset("MU")
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_http_cancellation_does_not_retry():
    started = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        provider = client(handler)
        task = asyncio.create_task(provider.calendar(date(2026, 1, 5), date(2026, 1, 5)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await provider.close()

    asyncio.run(scenario())
    assert len(calls) == 1
