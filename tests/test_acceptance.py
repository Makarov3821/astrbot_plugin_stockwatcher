"""Offline installation story against the actual plugin entry, HTTP and host API fixtures."""

import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import httpx
from test_plugin import Event
from test_plugin import plugin_module as plugin_module

from stockwatcher.domain import NEW_YORK, iso
from stockwatcher.evidence import Evidence


def test_private_assistant_installation_story(plugin_module):
    messages = []

    class Context:
        async def get_current_chat_provider_id(self, origin):
            return "test-host-provider"

        async def llm_generate(self, **kwargs):
            from research_helpers import reply_data

            prompt = json.loads(kwargs["prompt"])
            if "role" in prompt:
                data = {
                    "role": prompt["role"],
                    "horizon_days": 60,
                    "stance": "mixed",
                    "arguments": [{"text": "测试证据支持观察", "evidence_ids": ["metrics"]}],
                    "uncertainties": [{"text": "财报未来变化仍未知", "evidence_ids": ["metrics"]}],
                }
            else:
                packet = prompt.get("packet", prompt)
                fact = next(
                    item for item in packet["evidence"]["items"] if item["kind"] == "financial_fact"
                )
                data = reply_data(
                    action="consider_add",
                    valid_for_days=7,
                    target_proposal={
                        "kind": "valuation",
                        "eps_evidence_id": fact["evidence_id"],
                        "multiple": "50",
                        "horizon_days": 180,
                        "reason": "测试估值假设，存在倍数收缩风险",
                    },
                    entry={
                        "lower": "160",
                        "upper": "165",
                        "invalid_below": "130",
                        "confirmation": "daily_close_above_ma20",
                        "reason": "测试回调候选",
                    },
                )
            assert kwargs["tools"] is None
            return SimpleNamespace(role="assistant", completion_text=json.dumps(data), usage=None)

        async def send_message(self, origin, chain):
            messages.append((origin, chain.text))
            return True

    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(
            Context(),
            {
                "alpaca_key_id": "fixture",
                "alpaca_secret_key": "fixture-secret",
                "allowed_origins": [Event.unified_msg_origin],
            },
        )
        await plugin.initialize()
        plugin._worker.cancel()  # This story advances the worker explicitly; no wall-clock races.
        await asyncio.gather(plugin._worker, return_exceptions=True)
        today = datetime.now(NEW_YORK).date()
        day = today - timedelta(days=120)
        sessions, bars = [], []
        index = 0
        while day <= today:
            if day.weekday() < 5:
                sessions.append({"date": day.isoformat(), "open": "09:30", "close": "16:00"})
            day += timedelta(days=1)
        # Keep fixture prices independent of the number of calendar days.
        for session in sessions[-70:]:
            price = 100 + index
            bars.append(
                {
                    "t": iso(
                        datetime.combine(
                            datetime.fromisoformat(session["date"]).date(), time(), NEW_YORK
                        )
                    ),
                    "o": price,
                    "h": price + 1,
                    "l": price - 1,
                    "c": price,
                    "v": 100,
                }
            )
            index += 1

        # The current incomplete daily row is deliberately returned; the provider must filter it.
        def market_http(request):
            assert request.method == "GET"
            if request.url.path.endswith("calendar"):
                start, end = request.url.params["start"], request.url.params["end"]
                return httpx.Response(
                    200, json=[row for row in sessions if start <= row["date"] <= end]
                )
            if request.url.path.endswith("/bars"):
                symbols = request.url.params["symbols"]
                return httpx.Response(200, json={"bars": {symbols: bars}})
            if request.url.path.endswith("/news"):
                return httpx.Response(200, json={"news": [], "next_page_token": None})
            raise AssertionError(f"Unexpected fixture endpoint {request.url.path}")

        await plugin.provider._client.aclose()
        plugin.provider._client = httpx.AsyncClient(transport=httpx.MockTransport(market_http))
        await plugin.public_sources.client.aclose()
        plugin.public_sources.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text="<p>IR overview without publication date</p>"
                )
            )
        )
        sequence = 0

        async def command(text):
            nonlocal sequence
            sequence += 1
            event = Event(text)
            event.message_obj = SimpleNamespace(message_id=str(sequence))
            return "".join([part async for part in plugin.stockwatch(event)])

        try:
            assert "StockWatcher" in await command("盯盘 状态")
            await command("盯盘 持仓 添加 MU 数量=10 成本=100 日期=2026-01-01")
            await command("盯盘 现金 设置 5000 日期=2026-01-01")
            await command("盯盘 关注 添加 MSFT 理由=等待回调")
            await command("盯盘 设置 允许加仓=是 单股上限=50 总仓位上限=80 单次风险=1 日报=00:00")
            await command("盯盘 设置 MU 目标=220 目标期限=2027-04-01")
            now = datetime.now(UTC)
            plugin.research.evidence.add(
                Evidence(
                    instrument="MU",
                    url="https://www.sec.gov/Archives/fixture/",
                    publisher="offline fixture",
                    provider="test",
                    kind="financial_fact",
                    title="离线年度 EPS 样例",
                    content="这是测试数据",
                    published_at=iso(now - timedelta(days=30)),
                    observed_at=iso(now - timedelta(days=1)),
                    verified_source=True,
                    content_level="facts",
                    data={
                        "metric": "eps_diluted",
                        "annual": True,
                        "unit": "USD/shares",
                        "period_end": (now.date() - timedelta(days=60)).isoformat(),
                        "value": "5",
                        "identity": "fixture-annual",
                    },
                )
            )
            report_text = await command("盯盘 分析 MU")
            assert "AI分析已通过校验" in report_text
            assert (
                plugin.research.analysis.latest("MU")["result"]["target_proposal"]["price"]
                == "250.00"
            )
            assert plugin.research.profiles.resolved("MU")["values"]["target_price"] == "220"
            assert "已保存监控计划" in await command("盯盘 计划 采用 1")
            assert "已更新" in await command("盯盘 目标 采用 1")
            assert plugin.store.rows("SELECT state FROM monitor_plans")[0][0] == "superseded"
            assert (
                plugin.research.analysis.load(1)["input"]["preference"]["values"]["target_price"]
                == "220"
            )
            deep = await command("盯盘 深度 MU")
            assert "多角色研究" in deep and "完成 5/5 个角色" in deep
            detail = await command("盯盘 报告 2 详情")
            assert "bull" in detail and "bear" in detail
            calls = plugin.research.analysis.model.stats()["calls"]
            assert calls == 7
            await command("盯盘 提醒目标 当前")
            await command("盯盘 监控 开启")
            await command("盯盘 监控 设置 免扰开始=00:00 免扰结束=00:00")
            await command("盯盘 日报 开启")
            assert "checked" in await command("盯盘 监控 巡检")
            assert messages
            await command("盯盘 监控 巡检")
            assert (
                plugin.research.analysis.model.stats()["calls"] == calls
            )  # rule/daily path has no LLM
            assert "本地一致性备份" in await command("盯盘 备份")
            assert list((plugin.store.path.parent / "backups").glob("*.sqlite3"))
            await command("盯盘 监控 关闭")
        finally:
            await plugin.terminate()
        assert plugin._worker.done()
        assert plugin.provider._closed and plugin.store._closed
        replacement = plugin_module.StockWatcherPlugin(
            Context(), {"allowed_origins": [Event.unified_msg_origin]}
        )
        try:
            assert "10 股" in "".join(
                [part async for part in replacement.stockwatch(Event("盯盘 持仓"))]
            )
            assert not replacement.monitor.config()["enabled"]
        finally:
            await replacement.terminate()

    asyncio.run(scenario())
