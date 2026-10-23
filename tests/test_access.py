import asyncio
import types

import pytest
from test_plugin import Event, allowed_config
from test_plugin import plugin_module as plugin_module

from stockwatcher.access import allowed_origins, event_private_origin
from stockwatcher.config import Settings
from stockwatcher.domain import ValidationError


async def invoke(
    plugin, text, *, origin=Event.unified_msg_origin, group="", message_type="FriendMessage"
):
    event = Event(text)
    event.unified_msg_origin = origin
    event.get_group_id = lambda: group
    event.get_message_type = lambda: types.SimpleNamespace(value=message_type)
    return "".join([part async for part in plugin.stockwatch(event)])


@pytest.mark.parametrize(
    "values",
    [
        None,
        "bot:FriendMessage:1",
        [""],
        [" "],
        ["*"],
        ["bot:GroupMessage:1"],
        ["bot:FriendMessage:*"],
        ["*:FriendMessage:1"],
        ["bot:FriendMessage:1\nother"],
        [1],
        [None],
        ["bot:FriendMessage:1", "invalid"],
    ],
)
def test_invalid_whitelist_never_becomes_allow_all(values):
    with pytest.raises(ValidationError):
        Settings.from_mapping({"allowed_origins": values})


def test_multiple_origins_are_exact_private_sessions_with_platform_instance_scope():
    config = Settings.from_mapping(
        {
            "allowed_origins": [
                "bot-a:FriendMessage:123",
                "bot-b:FriendMessage:123",
                " bot-a:FriendMessage:123 ",
                "bot-c:FriendMessage:user:subsession",
            ]
        }
    )
    assert config.allowed_origins == (
        "bot-a:FriendMessage:123",
        "bot-b:FriendMessage:123",
        "bot-c:FriendMessage:user:subsession",
    )
    assert allowed_origins([]) == ()
    assert "private-id" not in repr(Settings(allowed_origins=("bot:FriendMessage:private-id",)))


def test_empty_whitelist_stays_in_setup_mode_without_resources(plugin_module):
    async def scenario():
        config = {"allowed_origins": []}
        plugin = plugin_module.StockWatcherPlugin(object(), config)
        try:
            await plugin.initialize()
            assert plugin.store is None and plugin._worker is None and plugin.provider is None
            response = await invoke(plugin, "/盯盘 入口")
            assert Event.unified_msg_origin in response and "不自动授权" in response
            assert config["allowed_origins"] == []
            assert "未授权" in await invoke(plugin, "盯盘 持仓")
            assert plugin.store is None
            config["allowed_origins"].append(Event.unified_msg_origin)
            assert "StockWatcher" in await invoke(plugin, "盯盘 状态")
            assert plugin.store is not None
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text",
    [
        "盯盘 帮助",
        "盯盘 状态",
        "盯盘 持仓",
        "盯盘 现金 设置 1000",
        "盯盘 设置 目标=100",
        "盯盘 提醒目标 当前",
        "盯盘 推送测试",
        "盯盘 分析 MU",
        "盯盘 深度 MU",
        "盯盘 材料 更新 MU",
        "盯盘 备份",
        "stockwatch 成交 买入 MU 数量=1 价格=100",
    ],
)
def test_unlisted_source_cannot_initialize_or_execute_any_business_command(plugin_module, text):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), allowed_config())
        try:
            assert "未授权" in await invoke(plugin, text, origin="other:FriendMessage:attacker")
            assert plugin.store is None and plugin.provider is None and plugin.commands is None
            assert not plugin._tasks
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "group,message_type",
    [
        ("group-id", "GroupMessage"),
        ("group-id", "FriendMessage"),
        ("", "GroupMessage"),
        ("", "OtherMessage"),
    ],
)
def test_group_or_contradictory_metadata_cannot_use_an_authorized_private_origin(
    plugin_module, group, message_type
):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), allowed_config())
        try:
            assert "仅接受" in await invoke(
                plugin, "盯盘 入口", group=group, message_type=message_type
            )
            assert plugin.store is None
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_missing_event_identity_is_denied():
    assert event_private_origin(object()) is None
    event = Event("盯盘 状态")
    event.unified_msg_origin = "bot:GroupMessage:g"
    assert event_private_origin(event) is None


def test_two_authorized_entries_can_use_the_same_personal_ledger(plugin_module):
    async def scenario():
        first, second = "first:FriendMessage:alice", "second:FriendMessage:bob"
        plugin = plugin_module.StockWatcherPlugin(object(), allowed_config(first, second))
        try:
            assert "已登记" in await invoke(plugin, "盯盘 现金 设置 1234", origin=first)
            assert "1234" in await invoke(plugin, "stockwatch 现金", origin=second)
            assert "未授权" in await invoke(
                plugin, "盯盘 现金", origin="first:FriendMessage:alice2"
            )
            assert "未授权" in await invoke(
                plugin, "盯盘 现金", origin="unknown:FriendMessage:alice"
            )
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_revocation_blocks_existing_ledger_access_worker_and_queued_delivery(plugin_module):
    sent = []

    class Context:
        async def send_message(self, *args):
            sent.append(args)
            return True

    async def scenario():
        config = allowed_config()
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            await invoke(plugin, "盯盘 现金 设置 1234")
            await invoke(plugin, "盯盘 提醒目标 当前")
            plugin.monitor.configure(
                {"enabled": True, "quiet_start": "00:00", "quiet_end": "00:00"}
            )
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            with plugin.store.transaction() as db:
                plugin.monitor.delivery.enqueue(
                    db, "private-event", Event.unified_msg_origin, "private-holding-detail", now
                )
            config["allowed_origins"].clear()
            result = await invoke(plugin, "盯盘 现金")
            assert "未授权" in result and "1234" not in result
            assert (await plugin.monitor.tick())["enabled"] is False
            assert await plugin.monitor.delivery.flush(plugin.monitor.config()) == 0
            assert await plugin._send(Event.unified_msg_origin, "private") is False
            assert not sent
            assert plugin.store.rows("SELECT status FROM notification_outbox")[0][0] == "cancelled"
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_invalid_live_config_does_not_keep_old_authorization(plugin_module):
    async def scenario():
        config = allowed_config()
        plugin = plugin_module.StockWatcherPlugin(object(), config)
        try:
            await invoke(plugin, "盯盘 状态")
            config["allowed_origins"] = [Event.unified_msg_origin, "*"]
            assert "未授权" in await invoke(plugin, "盯盘 持仓")
            assert not plugin._origin_allowed(Event.unified_msg_origin)
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_old_saved_target_outside_whitelist_cannot_receive_push_test(plugin_module):
    class Context:
        async def send_message(self, *args):
            pytest.fail("Unlisted destination must not receive any message")

    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(Context(), allowed_config())
        try:
            await plugin.initialize()
            plugin.store.set_setting("notification_origin", "other:FriendMessage:someone")
            assert "白名单" in await invoke(plugin, "盯盘 推送测试")
            assert (await plugin.monitor.tick())["enabled"] is False
            with pytest.raises(ValueError, match="白名单"):
                plugin.monitor.configure({"enabled": True})
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_upgrade_without_whitelist_keeps_old_data_but_does_not_start_worker(
    plugin_module, tmp_path
):
    from stockwatcher.ledger import Ledger
    from stockwatcher.storage import Store

    path = tmp_path / "stockwatcher.sqlite3"
    old = Store(path)
    Ledger(old).record("seed", symbol="MU", quantity="3", price="100", day="2026-01-01")
    old.set_setting("monitor_config", {"enabled": True, "daily": True})
    old.set_setting("notification_origin", Event.unified_msg_origin)
    old.close()

    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), {})
        try:
            await plugin.initialize()
            assert plugin.store is None and plugin._worker is None
            assert "未授权" in await invoke(plugin, "盯盘 持仓")
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
    reopened = Store(path)
    try:
        assert Ledger(reopened).view().positions["US:MU"].quantity == 3
        assert reopened.get_setting("monitor_config")["enabled"]
    finally:
        reopened.close()


def test_unlisted_identity_query_returns_only_its_own_origin(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), allowed_config())
        try:
            other = "another-bot:FriendMessage:own-id"
            response = await invoke(plugin, "stockwatch 入口", origin=other)
            assert other in response
            assert Event.unified_msg_origin not in response
            assert plugin.store is None
            response = await invoke(plugin, "盯盘 入口 " + Event.unified_msg_origin, origin=other)
            assert "未授权" in response
            assert plugin.store is None
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_revocation_between_notification_parts_stops_remaining_parts(plugin_module):
    config = allowed_config()
    sent = []

    class Context:
        async def send_message(self, origin, chain):
            sent.append(chain.text)
            config["allowed_origins"].clear()
            return True

    async def scenario():
        from datetime import UTC, datetime

        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            await invoke(plugin, "盯盘 提醒目标 当前")
            cfg = plugin.monitor.configure(
                {"enabled": True, "quiet_start": "00:00", "quiet_end": "00:00"}
            )
            with plugin.store.transaction() as db:
                plugin.monitor.delivery.enqueue(
                    db, "long", Event.unified_msg_origin, "private" * 500, datetime.now(UTC)
                )
            assert await plugin.monitor.delivery.flush(cfg) == 0
            assert len(sent) == 1
            assert "1/3" in sent[0]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_revocation_during_data_collection_blocks_model_and_budget(plugin_module):
    from research_helpers import NOW, FakeMarket

    class Context:
        async def get_current_chat_provider_id(self, *args):
            pytest.fail("Revoked origin must not resolve or call a provider")

    async def scenario():
        config = allowed_config()
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            await plugin.initialize()

            class RevokingMarket(FakeMarket):
                async def query(self, *args, **kwargs):
                    config["allowed_origins"].clear()
                    return await super().query(*args, **kwargs)

            plugin.research.analysis.market = RevokingMarket()
            plugin.research.analysis.sources = None
            report = await plugin.research.analysis.analyze(
                "MU", origin=Event.unified_msg_origin, now=NOW
            )
            assert report["status"] == "degraded"
            assert any("授权已撤销" in value for value in report["result"]["warnings"])
            assert not plugin.store.rows("SELECT * FROM model_calls")
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_revoked_entry_does_not_receive_late_command_errors(plugin_module):
    async def scenario():
        config = allowed_config()
        plugin = plugin_module.StockWatcherPlugin(object(), config)

        async def revoked_error(*args):
            config["allowed_origins"].clear()
            raise plugin_module.ValidationError("private-ledger-detail")

        try:
            await plugin.initialize()
            plugin._dispatch = revoked_error
            assert await invoke(plugin, "盯盘 持仓") == ""
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
