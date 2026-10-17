"""Read-only SEC, allowlisted company IR and Alpaca/Benzinga evidence adapters."""

from __future__ import annotations

import asyncio
import json
import re
import time as clock
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from .domain import NEW_YORK, ValidationError, instrument, iso, parse_time
from .evidence import Evidence, EvidenceStore, canonical_url
from .provider import AlpacaProvider, MarketError
from .storage import Store


class SourceError(RuntimeError):
    pass


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.dates = [], [], []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "meta" and (attrs.get("property") or attrs.get("name")) in {
            "article:published_time",
            "date",
            "datePublished",
            "pubdate",
        }:
            self.dates.append(attrs.get("content", ""))
        if tag == "time" and attrs.get("datetime"):
            self.dates.append(attrs["datetime"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self.parts)[:40000]


def page_text(value: str) -> str:
    parser = PageText()
    parser.feed(value)
    return parser.text()


class PublicSources:
    def __init__(
        self,
        store: Store,
        evidence: EvidenceStore,
        *,
        user_agent: str = "",
        ir_hosts: tuple[str, ...] = ("investors.micron.com",),
        client: httpx.AsyncClient | None = None,
        pace_seconds: float = 0.3,
    ):
        self.store, self.evidence = store, evidence
        self.user_agent = user_agent
        self.ir_hosts = {host.lower() for host in ir_hosts}
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._pace_seconds, self._last_request = pace_seconds, 0.0
        self._lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def _read(self, url: str, *, sec: bool = False, maximum: int = 8_000_000) -> bytes:
        host = urlsplit(canonical_url(url)).hostname
        if sec:
            if host not in {"www.sec.gov", "data.sec.gov"}:
                raise SourceError("SEC 请求地址不受支持。")
            if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.user_agent):
                raise SourceError("SEC 需配置含真实联系邮箱的 sec_user_agent；未发送未标识请求。")
        elif host not in self.ir_hosts:
            raise SourceError("公司页面主机不在配置的 IR 允许列表，未抓取任意外部地址。")
        async with self._lock:
            await asyncio.sleep(
                max(0, self._pace_seconds - (clock.monotonic() - self._last_request))
            )
            self._last_request = clock.monotonic()
            try:
                async with self.client.stream(
                    "GET",
                    url,
                    headers={"User-Agent": self.user_agent if sec else "StockWatcher/0.2"},
                ) as response:
                    if response.status_code != 200:
                        raise SourceError(
                            f"公开材料接口 HTTP {response.status_code}，未将其视为无新闻或无公告。"
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > maximum:
                            raise SourceError("公开材料超过体积上限，请使用原文链接或手工摘要。")
                    return bytes(content)
            except httpx.HTTPError as exc:
                raise SourceError("公开材料网络请求失败，未回显原始异常。") from exc

    async def _json(self, url: str):
        raw = await self._read(url, sec=True)
        try:
            return json.loads(raw, parse_float=Decimal)
        except (ValueError, UnicodeError) as exc:
            raise SourceError("SEC 返回格式无效。") from exc

    async def cik(self, symbol: str) -> str:
        ticker = instrument(symbol)[3:].replace(".", "-")
        cached = await asyncio.to_thread(self.store.get_setting, "sec_ticker_map")
        now = datetime.now(UTC)
        if not cached or now - parse_time(cached["fetched_at"]) > timedelta(days=1):
            raw = await self._json("https://www.sec.gov/files/company_tickers.json")
            mapping = {}
            if not isinstance(raw, dict):
                raise SourceError("SEC 标的映射格式错误。")
            for row in raw.values():
                if (
                    isinstance(row, dict)
                    and isinstance(row.get("ticker"), str)
                    and str(row.get("cik_str", "")).isdigit()
                ):
                    mapping[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
            cached = {"fetched_at": iso(now), "mapping": mapping}
            await asyncio.to_thread(self.store.set_setting, "sec_ticker_map", cached)
        result = cached["mapping"].get(ticker)
        if not result or not re.fullmatch(r"\d{10}", result):
            raise SourceError("SEC 标的映射中未找到该公司。")
        return result

    async def sec(self, symbol: str, *, now: datetime | None = None) -> dict:
        symbol = instrument(symbol)
        now = now or datetime.now(UTC)
        cik = await self.cik(symbol)
        payload = await self._json(f"https://data.sec.gov/submissions/CIK{cik}.json")
        try:
            recent = payload["filings"]["recent"]
            accessions = recent["accessionNumber"]
            fields = [recent[name] for name in ("form", "primaryDocument", "acceptanceDateTime")]
            if not isinstance(accessions, list) or any(
                not isinstance(values, list) or len(values) != len(accessions) for values in fields
            ):
                raise ValueError("Mismatched filing columns")
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError("SEC 披露索引缺少完整日期或文件信息。") from exc
        count, gaps, accepted = 0, [], {}
        for accession, form, filename, stamp in zip(accessions, *fields, strict=True):
            if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                continue
            try:
                published = parse_time(stamp)
            except ValueError:
                continue
            accepted[accession] = published
            if (
                not isinstance(form, str)
                or form not in {"10-K", "10-Q", "8-K", "20-F", "6-K"}
                or published > now
                or count >= 3
            ):
                continue
            if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{filename}"
            body, level = "", "metadata"
            try:
                raw = await self._read(url, sec=True, maximum=3_000_000)
                body, level = page_text(raw.decode("utf-8", errors="replace")), "excerpt"
            except SourceError as exc:
                gaps.append(str(exc))
            await asyncio.to_thread(
                self.evidence.add,
                Evidence(
                    instrument=symbol,
                    url=url,
                    publisher="SEC EDGAR",
                    provider="sec",
                    kind="filing",
                    title=f"{symbol} {form} {accession}",
                    content=body,
                    published_at=iso(published),
                    observed_at=iso(datetime.now(UTC)),
                    content_level=level,
                    verified_source=True,
                    data={"form": form, "accession": accession, "cik": cik},
                ),
            )
            count += 1
        try:
            count += await self._financial_facts(symbol, cik, accepted, now)
        except (SourceError, ValueError, TypeError, KeyError) as exc:
            gaps.append(
                str(exc) if isinstance(exc, SourceError) else "结构化财务数据缺失或格式异常。"
            )
        return {"source": "SEC", "count": count, "gaps": gaps}

    async def _financial_facts(self, symbol: str, cik: str, accepted: dict, now: datetime) -> int:
        payload = await self._json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("facts"), dict)
            or not isinstance(payload["facts"].get("us-gaap"), dict)
        ):
            raise SourceError("SEC 结构化财务数据格式异常。")
        taxonomy = payload["facts"]["us-gaap"]
        count = 0
        metrics = {
            "eps_diluted": (["EarningsPerShareDiluted"], "USD/shares"),
            "revenue": (["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"], "USD"),
            "net_income": (["NetIncomeLoss"], "USD"),
        }
        for metric, (tags, unit) in metrics.items():
            facts = next(
                (
                    taxonomy[tag]["units"].get(unit, [])
                    for tag in tags
                    if isinstance(taxonomy.get(tag), dict)
                    and isinstance(taxonomy[tag].get("units"), dict)
                ),
                [],
            )
            if not isinstance(facts, list) or any(not isinstance(row, dict) for row in facts):
                raise SourceError("SEC 财务项目列表格式异常。")
            selected = {}
            for fact in sorted(
                facts,
                key=lambda row: (str(row.get("end", "")), str(row.get("filed", ""))),
                reverse=True,
            ):
                try:
                    accession = fact["accn"]
                    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                        continue
                    value = Decimal(str(fact["val"]))
                    start, end = date.fromisoformat(fact["start"]), date.fromisoformat(fact["end"])
                    days = (end - start).days + 1
                    annual = 300 <= days <= 400
                    category = "annual" if annual else "quarterly" if 60 <= days <= 110 else "other"
                    published = accepted.get(accession)
                    if published is None:
                        published = datetime.combine(
                            date.fromisoformat(fact["filed"]), time(23, 59, 59), NEW_YORK
                        ).astimezone(UTC)
                    if (
                        category == "other"
                        or category in selected
                        or published > now
                        or not value.is_finite()
                    ):
                        continue
                    selected[category] = (fact, value, start, end, annual, published)
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    continue
            for fact, value, start, end, annual, published in selected.values():
                accession = fact["accn"]
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
                data = {
                    "identity": f"{metric}:{start}:{end}",
                    "metric": metric,
                    "value": format(value, "f"),
                    "unit": unit,
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "annual": annual,
                    "accession": accession,
                }
                await asyncio.to_thread(
                    self.evidence.add,
                    Evidence(
                        instrument=symbol,
                        url=url,
                        publisher="SEC XBRL",
                        provider="sec",
                        kind="financial_fact",
                        title=f"{metric} {start} 至 {end}",
                        content=json.dumps(data, ensure_ascii=False),
                        published_at=iso(published),
                        observed_at=iso(datetime.now(UTC)),
                        content_level="facts",
                        verified_source=True,
                        data=data,
                    ),
                )
                count += 1
        return count

    async def ir(self, symbol: str, url: str, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        url = canonical_url(url)
        raw = await self._read(url, maximum=2_000_000)
        parser = PageText()
        parser.feed(raw.decode("utf-8", errors="replace"))
        published = None
        for stamp in parser.dates:
            try:
                candidate = parse_time(stamp)
                if candidate <= now:
                    published = iso(candidate)
                    break
            except ValueError:
                continue
        # An IR landing page is not assigned a made-up publication date.
        evidence_id = await asyncio.to_thread(
            self.evidence.add,
            Evidence(
                instrument=instrument(symbol),
                url=url,
                publisher=urlsplit(url).hostname,
                provider="ir",
                kind="company_material",
                title=f"{instrument(symbol)} 公司材料",
                content=parser.text(),
                published_at=published,
                observed_at=iso(datetime.now(UTC)),
                content_level="excerpt",
                verified_source=True,
            ),
        )
        return {
            "source": "IR",
            "evidence_id": evidence_id,
            "gaps": []
            if published
            else ["公司页面未提供可确认的公开时间，已存档但不用于时点判断。"],
        }


class SourceService:
    def __init__(
        self, store: Store, evidence: EvidenceStore, public: PublicSources, alpaca: AlpacaProvider
    ):
        self.store, self.evidence, self.public, self.alpaca = store, evidence, public, alpaca

    async def _news(self, symbol: str, now: datetime) -> int:
        symbol = instrument(symbol)
        params = {
            "symbols": symbol[3:],
            "start": iso(now - timedelta(days=14)),
            "end": iso(now),
            "include_content": "true",
            "sort": "desc",
            "limit": 20,
        }
        count, seen, invalid = 0, set(), 0
        for _ in range(3):
            data = await self.alpaca._get(self.alpaca.DATA_URL + "/v1beta1/news", params)
            if not isinstance(data, dict) or not isinstance(data.get("news"), list):
                raise SourceError("新闻响应格式异常。")
            for article in data["news"]:
                try:
                    published, updated = (
                        parse_time(article["created_at"]),
                        parse_time(article["updated_at"]),
                    )
                    if published > now or updated > now:
                        continue
                    symbols = article.get("symbols", [])
                    if symbol[3:] not in symbols:
                        continue
                    content = article.get("content") or article.get("summary") or ""
                    if not isinstance(content, str):
                        raise ValueError("Invalid content")
                    await asyncio.to_thread(
                        self.evidence.add,
                        Evidence(
                            instrument=symbol,
                            url=article["url"],
                            publisher="Benzinga",
                            provider="alpaca",
                            kind="news",
                            title=article["headline"][:500],
                            content=page_text(content),
                            published_at=iso(published),
                            updated_at=iso(updated),
                            observed_at=iso(datetime.now(UTC)),
                            verified_source=True,
                            content_level="excerpt" if content else "metadata",
                            data={"article_id": str(article["id"])},
                        ),
                    )
                    count += 1
                except (KeyError, TypeError, ValueError):
                    invalid += 1
            token = data.get("next_page_token")
            if not token:
                if invalid:
                    raise SourceError(
                        f"新闻存在 {invalid} 条无效记录，已存有效材料；覆盖可能不完整。"
                    )
                return count
            if not isinstance(token, str) or token in seen:
                raise SourceError("新闻分页异常，覆盖可能不完整。")
            seen.add(token)
            params["page_token"] = token
        raise SourceError("新闻达到分页上限，最近事件覆盖不完整。")

    async def sync(self, symbol: str, *, ir_url: str = "", now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        symbol = instrument(symbol)
        results, gaps = [], []
        for name, operation in (
            ("SEC", lambda: self.public.sec(symbol, now=now)),
            ("新闻", lambda: self._news(symbol, now)),
        ):
            try:
                result = await operation()
                results.append(
                    result if isinstance(result, dict) else {"source": name, "count": result}
                )
                if isinstance(result, dict):
                    gaps.extend(result.get("gaps", []))
            except (SourceError, MarketError, ValidationError) as exc:
                gaps.append(f"{name}：{exc}")
        selected_ir = ir_url or (
            "https://investors.micron.com/overview/default.aspx" if symbol == "US:MU" else ""
        )
        if selected_ir:
            try:
                result = await self.public.ir(symbol, selected_ir, now=now)
                results.append(result)
                gaps.extend(result.get("gaps", []))
            except (SourceError, ValidationError) as exc:
                gaps.append(f"IR：{exc}")
        else:
            gaps.append("未配置该公司的 IR 页面。")
        report = {
            "instrument": symbol,
            "query_asof": iso(now),
            "synced_at": iso(datetime.now(UTC)),
            "sources": results,
            "gaps": gaps,
        }
        await asyncio.to_thread(self.store.set_setting, f"source_sync:{symbol}", report)
        return report
