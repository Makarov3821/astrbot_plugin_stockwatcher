import asyncio

import httpx
import pytest

from stockwatcher.commands import Commands
from stockwatcher.config import Settings
from stockwatcher.domain import ValidationError
from stockwatcher.market import MarketService
from stockwatcher.provider import AlpacaProvider


def test_explicit_commands_and_duplicate_delivery(store, ledger):
    async def scenario():
        settings = Settings()
        provider = AlpacaProvider(
            settings,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: pytest.fail("No network expected"))
            ),
        )
        commands = Commands(store, ledger, MarketService(provider, store), settings)
        try:
            first = await commands.handle(
                "/盯盘 持仓 添加 MU 数量=1.5 成本=100 日期=2026-01-01", request_key="one"
            )
            second = await commands.handle(
                "/盯盘 持仓 添加 MU 数量=1.5 成本=100 日期=2026-01-01", request_key="one"
            )
            assert "已登记" in first
            assert "未重复入账" in second
            await commands.handle('/盯盘 关注 添加 MU 理由="等待 回调"')
            assert "等待 回调" in await commands.handle("/盯盘 关注")
            await commands.handle("/盯盘 成交 卖出 MU 数量=0.5 价格=110 日期=2026-01-02")
            assert "US:MU：1 股" in await commands.handle("/盯盘 持仓")
            assert "未知" in await commands.handle("/盯盘 现金")
            assert "AI 尚未实现" in await commands.handle("/盯盘 状态")
            await commands.handle("/盯盘 提醒目标 当前", origin="test:FriendMessage:123")
            assert store.get_setting("notification_origin") == "test:FriendMessage:123"
            assert "暂无" in await commands.handle("/盯盘 缓存 MU")
        finally:
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text",
    [
        "持仓 添加 MU 数量=1 数量=2 成本=100",
        "成交 买入 MU 数量=1 价格=100 错误=foo",
        "现金 设置",
        '关注 添加 MU 理由="未闭合',
        "行情 MU 周线",
        "撤销 abc 原因=错误",
        "拆股 MU 比例=2",
    ],
)
def test_invalid_commands_do_not_change_ledger(store, ledger, text):
    async def scenario():
        provider = AlpacaProvider(Settings())
        commands = Commands(store, ledger, MarketService(provider, store), Settings())
        try:
            with pytest.raises(ValidationError):
                await commands.handle(text)
            assert ledger.events() == []
        finally:
            await provider.close()

    asyncio.run(scenario())
