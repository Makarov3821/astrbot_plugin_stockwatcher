"""Import the actual entry point against a small, explicit AstrBot API harness.

This checks our lifecycle and command routing, not actual platform delivery or
compatibility with a running AstrBot installation.
"""

import asyncio
import importlib
import logging
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def plugin_module(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("stockwatcher_host_test")
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, "stockwatcher_host_test", package)

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            assert name == "astrbot_plugin_stockwatcher"
            return tmp_path

    class MessageChain:
        def message(self, text):
            self.text = text
            return self

    def command(name, **kwargs):
        assert name == "盯盘"

        def decorate(func):
            return func

        return decorate

    for name, attributes in {
        "astrbot": {},
        "astrbot.api": {"AstrBotConfig": dict, "logger": logging.getLogger("host-test")},
        "astrbot.api.event": {
            "AstrMessageEvent": object,
            "MessageChain": MessageChain,
            "filter": types.SimpleNamespace(
                command=command,
                EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE="private"),
                event_message_type=lambda *args, **kwargs: lambda handler: handler,
            ),
        },
        "astrbot.api.star": {"Star": Star, "StarTools": StarTools, "Context": object},
    }.items():
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    imported = importlib.import_module("stockwatcher_host_test.main")
    yield imported
    for name in list(sys.modules):
        if name.startswith("stockwatcher_host_test."):
            del sys.modules[name]


class Event:
    unified_msg_origin = "test:FriendMessage:123"
    message_obj = types.SimpleNamespace(message_id="message-id")

    def __init__(self, text):
        self.text = text
        self.extra = {}
        self.stopped = False

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def stop_event(self):
        self.stopped = True

    def get_message_type(self):
        return types.SimpleNamespace(value="FriendMessage")

    def get_group_id(self):
        return ""

    def get_message_str(self):
        return self.text

    def plain_result(self, text):
        return text


def allowed_config(*origins):
    return {"allowed_origins": list(origins or (Event.unified_msg_origin,))}


async def responses(plugin, text):
    return [item async for item in plugin.stockwatch(Event(text))]


def test_initialize_route_idempotency_and_restart(plugin_module):
    class Context:
        async def send_message(self, origin, chain):
            return True

    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(Context(), allowed_config())
        await asyncio.gather(plugin.initialize(), plugin.initialize())
        original_store = plugin.store
        assert "StockWatcher" in (await responses(plugin, "盯盘 状态"))[0]
        command = "盯盘 持仓 添加 MU 数量=1 成本=100 日期=2026-01-01"
        assert "已登记" in (await responses(plugin, command))[0]
        assert "未重复入账" in (await responses(plugin, command))[0]
        await plugin.initialize()
        assert plugin.store is original_store
        await plugin.terminate()
        assert plugin.provider._closed
        assert plugin.store._closed
        await plugin.terminate()
        replacement = plugin_module.StockWatcherPlugin(Context(), allowed_config())
        assert "US:MU：1 股" in (await responses(replacement, "盯盘 持仓"))[0]
        await replacement.terminate()

    asyncio.run(scenario())


def test_push_platform_not_found_is_not_reported_success(plugin_module):
    class Context:
        async def send_message(self, origin, chain):
            return False

    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(Context(), allowed_config())
        try:
            assert "未找到对应平台" in (await responses(plugin, "盯盘 推送测试"))[0]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_push_uses_saved_target_and_redacts_errors(plugin_module):
    calls = []

    class Context:
        async def send_message(self, origin, chain):
            calls.append((origin, chain.text))
            raise RuntimeError("secret-token-in-platform-exception")

    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(
            Context(), allowed_config(Event.unified_msg_origin, "test:FriendMessage:other")
        )
        await plugin.initialize()
        plugin.store.set_setting("notification_origin", "test:FriendMessage:other")
        try:
            result = (await responses(plugin, "盯盘 推送测试"))[0]
            assert "失败" in result
            assert "secret-token" not in result
            assert calls[0][0] == "test:FriendMessage:other"
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_terminate_cancels_inflight_work(plugin_module):
    started = asyncio.Event()

    class Context:
        async def send_message(self, origin, chain):
            started.set()
            await asyncio.Event().wait()

    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(Context(), allowed_config())
        task = asyncio.create_task(responses(plugin, "盯盘 推送测试"))
        await started.wait()
        await plugin.terminate()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert plugin._tasks == set()
        assert plugin.store._closed

    asyncio.run(scenario())


def test_after_termination_no_lazy_reinitialization(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), allowed_config())
        await plugin.terminate()
        assert "正在关闭" in (await responses(plugin, "盯盘 状态"))[0]
        assert plugin.store is None

    asyncio.run(scenario())


def test_preferences_stock_wizard_and_hint_flow_through_actual_entry(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), allowed_config())
        try:
            first = (await responses(plugin, "盯盘 设置"))[0]
            assert "首次使用" in first
            assert "首次使用" not in (await responses(plugin, "盯盘 设置"))[0]
            assert (
                "版本" in (await responses(plugin, "盯盘 设置 账户=现金 周期=180 允许加仓=否"))[0]
            )
            assert "个股目标" in (await responses(plugin, "盯盘 开始 MU"))[0]
            assert (
                "个股期待" in (await responses(plugin, "盯盘 回答 目标=200 目标期限=2026-12-31"))[0]
            )
            assert "待确认" in (await responses(plugin, '盯盘 回答 理由="长期 期待"'))[0]
            assert "已生效" in (await responses(plugin, "盯盘 引导 确认"))[0]
            assert '"target_price": "200"' in (await responses(plugin, "盯盘 设置 MU"))[0]
            assert "候选" in (await responses(plugin, "盯盘 目标"))[0] or not plugin.store.rows(
                "SELECT * FROM target_proposals"
            )
            assert "缺少" in (await responses(plugin, "盯盘 分析 MU 缓存"))[0]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
