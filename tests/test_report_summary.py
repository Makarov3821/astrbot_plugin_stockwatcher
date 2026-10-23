"""Reports answer the user's question first; diagnostics and evidence are opt-in."""

import asyncio
import copy
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from research_helpers import NOW, FakeMarket, FakeModel, batch, reply_data
from test_analysis import services
from test_conversation import Context, grouped_config, intent
from test_onboarding_chat import reply
from test_plugin import plugin_module as plugin_module

from stockwatcher.analysis import validate_reply
from stockwatcher.domain import ValidationError, iso
from stockwatcher.evidence import Evidence, EvidenceStore
from stockwatcher.features import metrics
from stockwatcher.model import ModelReply


def test_failed_ai_returns_a_short_honest_answer_not_thirty_news_and_raw_settings(store, ledger):
    service, _, _ = services(store, ledger)
    question = "看一下美光今年的走势，判断一下最近的回调到底了吗？"
    report = asyncio.run(service.analyze("MU", now=NOW, question=question))
    report["input"]["metrics"].update(
        reference_price="856.49",
        ma20="996.8255",
        ma60="965.2233333333333333",
        atr14="49.1664285714285714",
        atr_pct="5.74045564705117",
    )
    report["result"]["warnings"] = ["模型引用了不存在的证据或缺少必要引用。"]
    report["input"]["source_gaps"] = [
        "SEC：未配置请求标识",
        "新闻：新闻达到分页上限，最近事件覆盖不完整。",
    ]
    report["input"]["evidence"]["items"] = [
        {
            "evidence_id": f"ev_fixture_{i}",
            "title": f"测试新闻标题{i}",
            "url": f"https://example.com/news/{i}",
            "published_at": iso(NOW),
            "content_level": "excerpt",
        }
        for i in range(30)
    ]
    summary = service.render(report)
    assert "无法确认这轮回调已经见底" in summary
    assert "未通过核验" in summary
    assert "20日均价 996.83" in summary and "49.17 美元" in summary
    assert "参考价低于这两条均线" in summary
    assert "新闻覆盖不完整" in summary
    assert "max_stock_pct" not in summary and "null" not in summary
    assert "测试新闻标题" not in summary and "holding_loss_usd" not in summary
    assert "49.1664285714285714" not in summary and len(summary) < 1000
    details = service.render(report, detailed=True)
    assert "测试新闻标题29" in details and question in details


@pytest.mark.parametrize("missing", [False, True])
def test_year_return_uses_previous_year_end_and_never_substitutes_a_partial_year(missing):
    count = 2 if missing else 70
    data = metrics(batch(adjustment="all", count=count), batch(adjustment="raw", count=count))
    assert data["year"] == 2026
    if missing:
        assert data["year_start_close"] is None and data["year_return_pct"] is None
        assert not data["year_range_complete"]
    else:
        assert data["year_start_close"] == "165"
        assert abs(Decimal(data["year_return_pct"]) - Decimal(400) / Decimal(165)) < Decimal(
            "1e-25"
        )
        assert data["year_range_complete"]
    assert Decimal(data["year_low"]) < Decimal(data["reference_price"]) < Decimal(data["year_high"])


def test_year_return_is_consistent_across_adjusted_price_scale():
    raw = batch(adjustment="raw")
    first = metrics(batch(adjustment="all"), raw)
    scaled = metrics(batch(adjustment="all", factor=Decimal(2)), raw)
    for key in ("year_return_pct", "year_start_close", "year_high", "year_low"):
        assert Decimal(scaled[key]) == Decimal(first[key])


def test_old_news_fetched_later_cannot_displace_new_news_or_primary_facts(store):
    evidence = EvidenceStore(store)
    keys = []
    for i in range(5):
        keys.append(
            evidence.add(
                Evidence(
                    instrument="MU",
                    url=f"https://example.com/{i}",
                    publisher="fixture",
                    provider="test",
                    kind="news",
                    title=f"测试新闻{i}",
                    content=f"内容{i}",
                    published_at=iso(NOW - timedelta(hours=i + 1)),
                    observed_at=iso(NOW + timedelta(seconds=i)),
                )
            )
        )
    packet = evidence.packet("MU", at=NOW + timedelta(minutes=1), limit=2)
    assert [r["evidence_id"] for r in packet["items"]] == keys[:2]
    fact = evidence.add(
        Evidence(
            instrument="MU",
            url="https://example.com/annual",
            publisher="fixture",
            provider="test",
            kind="financial_fact",
            title="年度事实",
            content="财务资料",
            published_at=iso(NOW - timedelta(days=100)),
            observed_at=iso(NOW),
        )
    )
    packet = evidence.packet("MU", at=NOW + timedelta(minutes=1), limit=3)
    assert [r["evidence_id"] for r in packet["items"]] == [fact, *keys[:2]]


def test_question_reaches_research_and_details_are_read_without_more_model_calls(plugin_module):
    async def scenario():
        context = Context(intent("analysis", "MU"), reply_data())
        plugin = plugin_module.StockWatcherPlugin(context, grouped_config())
        try:
            await plugin.initialize()
            plugin.research.analysis.market = FakeMarket()
            plugin.research.analysis.sources = None
            question = "看一下美光今年的走势，判断一下最近的回调到底了吗？"
            text, message = await reply(plugin, question, "question")
            assert message.stopped and "AI分析已通过校验" in text
            packet = json.loads(context.requests[1]["prompt"])
            assert packet["user_question"] == question
            assert packet["metrics"]["year_return_pct"] is not None
            assert plugin.research.analysis.load(1)["input"]["user_question"] == question
            details, _ = await reply(plugin, "查看详细依据", "details")
            assert "本次问题：" + question in details and "分项风险" in details
            brief, _ = await reply(plugin, "报告1", "brief")
            assert "AI分析已通过校验" in brief and "分项风险" not in brief
            assert len(context.requests) == 2
            assert not plugin.research.profiles.current()["overrides"]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "citations, expected", [([], "缺少必要引用"), (["not_a_supplied_id"], "未提供的证据ID")]
)
def test_citation_diagnostics_locate_failure_without_accepting_bad_evidence(
    store, ledger, citations, expected
):
    service, _, _ = services(store, ledger, FakeModel())
    report = asyncio.run(service.analyze("MU", now=NOW))
    data = copy.deepcopy(reply_data())
    data["reasons"][0]["evidence_ids"] = citations
    with pytest.raises(ValidationError, match=expected) as error:
        validate_reply(ModelReply(json.dumps(data), {}), report["input"], NOW)
    assert "reasons[0].evidence_ids" in str(error.value)


def test_summary_never_hides_expired_or_superseded_state_or_hard_exit(store, ledger):
    service, profiles, _ = services(store, ledger)
    ledger.record("seed", symbol="MU", quantity="1", price="200", day="2026-01-01")
    profiles.update({"hard_stop_price": "175"}, symbol="MU")
    report = asyncio.run(service.analyze("MU", now=NOW))
    text = service.render(report)
    assert "个人退出规则" in text and "退出价" in text
    assert "已过有效期" in text
    report["status"] = "superseded"
    assert "本次结果已失效" in service.render(report)


def test_cached_report_remains_explicit_and_no_prior_report_is_not_invented(
    store, ledger, plugin_module
):
    service, _, _ = services(store, ledger)
    report = asyncio.run(service.analyze("MU", now=NOW, cached=True))
    assert "缓存行情，未验证新近性" in service.render(report)

    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(Context(), grouped_config())
        try:
            text, _ = await reply(plugin, "查看详细依据", "no-report")
            assert "还没有上次分析记录" in text
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
