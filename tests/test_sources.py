import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from stockwatcher.config import Settings
from stockwatcher.evidence import EvidenceStore
from stockwatcher.provider import AlpacaProvider
from stockwatcher.sources import PublicSources, SourceError, SourceService

NOW = datetime(2026, 1, 6, 22, tzinfo=UTC)
ACCESSION = "0000723125-25-000001"


def public(store, handler, **options):
    return PublicSources(
        store,
        EvidenceStore(store),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        pace_seconds=0,
        **options,
    )


def test_sec_requires_contact_and_ir_does_not_fetch_unknown_hosts(store):
    source = public(store, lambda request: pytest.fail("Request should not happen"))

    async def scenario():
        try:
            with pytest.raises(SourceError, match="联系邮箱"):
                await source.sec("MU")
            with pytest.raises(SourceError, match="允许列表"):
                await source.ir("MU", "https://unknown.example.com/a")
        finally:
            await source.close()

    asyncio.run(scenario())


def test_sec_filings_and_latest_period_facts_have_real_publication_times(store):
    seen = []

    def handler(request):
        seen.append(request)
        assert "contact@example.com" in request.headers["User-Agent"]
        assert "APCA-API-KEY-ID" not in request.headers
        if request.url.path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"ticker": "MU", "cik_str": 723125}})
        if "/submissions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "filings": {
                        "recent": {
                            "accessionNumber": [ACCESSION],
                            "form": ["10-K"],
                            "primaryDocument": ["annual.htm"],
                            "acceptanceDateTime": ["2025-10-01T20:15:00Z"],
                        }
                    }
                },
            )
        if "/companyfacts/" in request.url.path:
            facts = [
                {
                    "accn": ACCESSION,
                    "start": "2024-09-01",
                    "end": "2025-08-31",
                    "filed": "2025-10-01",
                    "val": 5.25,
                },
                {
                    "accn": ACCESSION,
                    "start": "2023-09-01",
                    "end": "2024-08-31",
                    "filed": "2026-01-05",
                    "val": 1.0,
                },
            ]
            return httpx.Response(
                200,
                json={
                    "facts": {
                        "us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": facts}}}
                    }
                },
            )
        return httpx.Response(
            200, text="<h1>Annual results</h1><script>not evidence</script><p>Filing text</p>"
        )

    source = public(store, handler, user_agent="StockWatcher contact@example.com")

    async def scenario():
        try:
            report = await source.sec("MU", now=NOW)
            assert report["count"] == 2
            packet = source.evidence.packet("MU")
            fact = next(row for row in packet["items"] if row["kind"] == "financial_fact")
            assert fact["data"]["value"] == "5.25"
            assert fact["data"]["period_end"] == "2025-08-31"
            assert fact["published_at"] == "2025-10-01T20:15:00Z"
            assert (
                source.evidence.packet("MU", at=NOW)["items"] == []
            )  # first observed now, not at historical query cutoff
            filing = next(row for row in packet["items"] if row["kind"] == "filing")
            assert "not evidence" not in filing["content"]
            assert len(seen) == 4
        finally:
            await source.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"facts": None},
        {"facts": {"us-gaap": []}},
        {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [None]}}}}},
    ],
)
def test_malformed_financial_data_is_a_source_gap(store, payload):
    source = public(
        store,
        lambda request: httpx.Response(200, json=payload),
        user_agent="test contact@example.com",
    )

    async def scenario():
        try:
            with pytest.raises(SourceError):
                await source._financial_facts("US:MU", "0000723125", {}, NOW)
        finally:
            await source.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("dated", [True, False])
def test_ir_extracts_only_explicit_aware_publication_date(store, dated):
    meta = (
        '<meta property="article:published_time" content="2026-01-05T12:00:00Z">' if dated else ""
    )
    source = public(
        store, lambda request: httpx.Response(200, text=meta + "<p>Company results</p>")
    )

    async def scenario():
        try:
            report = await source.ir("MU", "https://investors.micron.com/example", now=NOW)
            packet = source.evidence.packet("MU")
            assert bool(packet["items"]) == dated
            assert bool(report["gaps"]) != dated
            if not dated:
                assert packet["excluded"]
        finally:
            await source.close()

    asyncio.run(scenario())


def test_public_redirect_and_response_size_are_bounded(store):
    async def scenario():
        for response, pattern in [
            (httpx.Response(302, headers={"Location": "https://example.com/private"}), "HTTP 302"),
            (httpx.Response(200, content=b"123456"), "体积上限"),
        ]:
            source = public(store, lambda request, response=response: response)
            try:
                with pytest.raises(SourceError, match=pattern):
                    await source._read("https://investors.micron.com/a", maximum=5)
            finally:
                await source.close()

    asyncio.run(scenario())


def test_news_pagination_filtering_and_source_failures_are_explicit(store):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1beta1/news"
        assert request.url.params["symbols"] == "MU"
        if "page_token" in request.url.params:
            return httpx.Response(200, json={"news": [], "next_page_token": None})
        article = {
            "id": 1,
            "created_at": "2026-01-05T15:00:00Z",
            "updated_at": "2026-01-05T15:00:00Z",
            "symbols": ["MU"],
            "url": "https://www.benzinga.com/item/1",
            "headline": "Company news",
            "summary": "Summary",
        }
        return httpx.Response(
            200,
            json={
                "news": [article, article | {"id": 2, "symbols": ["AAPL"]}],
                "next_page_token": "next",
            },
        )

    async def direct():
        public_source = public(store, lambda request: pytest.fail("No public requests"))
        alpaca = AlpacaProvider(
            Settings(key_id="test", secret_key="hidden"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            assert (
                await SourceService(store, public_source.evidence, public_source, alpaca)._news(
                    "MU", NOW
                )
                == 1
            )
            assert len(public_source.evidence.packet("MU")["items"]) == 1
        finally:
            await public_source.close()
            await alpaca.close()

    asyncio.run(direct())
    assert len(seen) == 2


def test_sync_unavailable_sources_report_gaps_not_no_news(store):
    source = public(store, lambda request: pytest.fail("No public request"))
    alpaca = AlpacaProvider(
        Settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: pytest.fail("No credentials"))
        ),
    )

    async def scenario():
        try:
            result = await SourceService(store, source.evidence, source, alpaca).sync(
                "MSFT", now=NOW
            )
            assert len(result["gaps"]) == 3
            assert result["sources"] == []
        finally:
            await source.close()
            await alpaca.close()

    asyncio.run(scenario())
