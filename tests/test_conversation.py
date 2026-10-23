"""Exercise natural language through the real plugin entry with a fake AstrBot provider."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboarding_chat import command, event, reply
from test_plugin import Event
from test_plugin import plugin_module as plugin_module

from stockwatcher.config import Settings, migrate_config
from stockwatcher.conversation import parse_intent
from stockwatcher.domain import ValidationError


def defaults(nodes):
    return {
        k: defaults(n["items"]) if n["type"] == "object" else copy.deepcopy(n.get("default"))
        for k, n in nodes.items()
    }


class Config(dict):
    saves = 0

    def save_config(self):
        self.saves += 1


def grouped_config(*origins):
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
    config = Config(defaults(schema))
    config["access"]["allowed_origins"] = list(origins or (Event.unified_msg_origin,))
    return config


class Context:
    def __init__(self, *intents):
        self.intents = list(intents)
        self.requests = []

    async def get_current_chat_provider_id(self, origin):
        return "host-model"

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        value = self.intents.pop(0)
        return SimpleNamespace(
            role="assistant",
            completion_text=value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False),
        )


def intent(action, symbol=None, **params):
    return {"action": action, "symbol": symbol, "parameters": params}


def test_legacy_settings_migrate_once_without_losing_credentials_or_whitelist():
    config = grouped_config()
    config["access"]["allowed_origins"] = []
    config.update(
        allowed_origins=[Event.unified_msg_origin],
        alpaca_key_id="fixture-key",
        alpaca_secret_key="fixture-secret",
        delay_minutes=20,
    )
    assert migrate_config(config)
    result = Settings.from_mapping(config)
    assert result.allowed_origins == (Event.unified_msg_origin,)
    assert result.key_id == "fixture-key" and result.secret_key == "fixture-secret"
    assert result.delay_minutes == 20
    assert config["alpaca_secret_key"] == "" and not config["allowed_origins"]
    assert not migrate_config(config)
    config["access"]["allowed_origins"] = []
    assert not Settings.from_mapping(config).allowed_origins


def test_new_ui_choices_are_not_overwritten_by_legacy_values():
    config = grouped_config("new:FriendMessage:owner")
    config["allowed_origins"] = [Event.unified_msg_origin]
    migrate_config(config)
    assert Settings.from_mapping(config).allowed_origins == ("new:FriendMessage:owner",)


def test_requested_queries_read_real_records_without_model_or_generic_chat(plugin_module):
    async def scenario():
        context, config = Context(), grouped_config()
        plugin = plugin_module.StockWatcherPlugin(context, config)
        try:
            await command(plugin, "盯盘 大盘预期 偏多 期限=2099-12-31 理由=关注盈利改善")
            await command(plugin, "盯盘 关注 添加 MU 理由=等待回调", "watch")
            await command(plugin, "盯盘 设置 MU 目标=1000 理由=长期持有", "stock")
            result, message = await reply(plugin, "隐藏设置纵览", "overview")
            assert message.stopped and all(
                s in result
                for s in (
                    "偏多",
                    "关注盈利改善",
                    "1000 美元",
                    "等待回调",
                    "回调提醒计划",
                    "AI目标调整候选",
                )
            )
            result, message = await reply(plugin, "我现在的大盘走向判断是什么", "outlook")
            assert message.stopped and "偏多" in result
            assert "个股期待" not in result and "1000" not in result
            assert "created_at" not in result and "background" not in result
            assert not context.requests
            assert not plugin.store.rows("SELECT * FROM model_calls")
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_model_identifies_target_edit_but_only_confirmation_saves_it(plugin_module):
    async def scenario():
        context = Context(intent("set_stock", "美光", 目标="1000", 理由="等待业绩改善"))
        config = grouped_config()
        plugin = plugin_module.StockWatcherPlugin(context, config)
        try:
            result, message = await reply(
                plugin, "我希望美光到1000美元，理由是等待业绩改善", "edit"
            )
            assert message.stopped and "尚未生效" in result and "1000 美元" in result
            assert not plugin.research.profiles.current()["overrides"]
            result, _ = await reply(plugin, "确认", "confirm")
            assert "已保存" in result
            assert plugin.research.profiles.resolved("MU")["values"]["target_price"] == "1000"
            assert not context.requests[0]["tools"]
            assert not context.requests[0]["contexts"]
            assert len(plugin.store.rows("SELECT * FROM model_calls WHERE purpose='intent'")) == 1
            saved = await reply(plugin, "确认", "confirm")
            assert saved[0] == result
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_followup_uses_dialog_to_supply_missing_deadline(plugin_module):
    async def scenario():
        context = Context(
            intent("clarify", question="看多预期有效到哪一天？"),
            intent("set_outlook", direction="偏多", 期限="2099-12-31"),
        )
        plugin = plugin_module.StockWatcherPlugin(context, grouped_config())
        try:
            assert "哪一天" in (await reply(plugin, "我觉得大盘偏多", "first"))[0]
            assert "尚未生效" in (await reply(plugin, "到2099年年底", "followup"))[0]
            prompt = json.loads(context.requests[1]["prompt"])
            assert prompt["recent_dialog"][0]["user"] == "我觉得大盘偏多"
            assert not plugin.research.profiles.current()["outlooks"]
            await reply(plugin, "确认", "save")
            assert "偏多" in (await reply(plugin, "我现在的大盘走向判断是什么", "read"))[0]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_notifications_can_be_enabled_from_ui_and_chat_changes_mirror_back(plugin_module):
    async def scenario():
        config = grouped_config()
        config["notifications"].update(
            notification_origin=Event.unified_msg_origin, enabled=True, daily=True
        )
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            await plugin.initialize()
            assert plugin.monitor.config()["enabled"] and plugin.monitor.config()["daily"]
            assert plugin.store.get_setting("notification_origin") == Event.unified_msg_origin
            assert "后台监控：开启" in (await reply(plugin, "监控状态", "read"))[0]
            assert "尚未生效" in (await reply(plugin, "关闭监控", "stop"))[0]
            assert plugin.monitor.config()["enabled"]
            await reply(plugin, "确认", "save")
            assert not plugin.monitor.config()["enabled"]
            assert not config["notifications"]["enabled"]
            await command(plugin, "盯盘 设置 周期=180 允许加仓=否", "prefs")
            assert config["investment_defaults"]["horizon_days"] == 180
            assert config["investment_defaults"]["allow_add"] == "false"
            assert config.saves > 1
        finally:
            await plugin.terminate()
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            await plugin.initialize()
            assert not plugin.monitor.config()["enabled"]
            assert plugin.research.profiles.resolved("MU")["values"]["horizon_days"] == 180
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_monitor_request_without_target_is_not_started_and_is_explained(plugin_module):
    async def scenario():
        config = grouped_config()
        config["notifications"]["enabled"] = True
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            response, _ = await reply(plugin, "监控状态", "state")
            assert "接收入口" in response and "监控未开启" in response
            assert not plugin.monitor.config()["enabled"]
            await reply(plugin, "把提醒发到这里", "target")
            await reply(plugin, "确认", "target-confirm")
            assert config["notifications"]["notification_origin"] == Event.unified_msg_origin
            await reply(plugin, "开启监控", "enable")
            await reply(plugin, "确认", "enable-confirm")
            assert plugin.monitor.config()["enabled"]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        intent("shell", command="rm -rf /"),
        intent("outlook", 目标="1000"),
        intent("set_stock", "MU", 总仓位上限="100"),
        intent("set_target_origin", target="other:FriendMessage:stranger"),
        intent("set_monitor", enabled="true"),
        intent("set_stock", "A股:贵州茅台", 目标="1000"),
        intent("record_position", "MU", 数量=True, 成本="100"),
        intent("record_trade", "MU", side="下单", 数量="1", 价格="100"),
        '{"action":"overview","action":"watch","symbol":"MU","parameters":{}}',
        '{"action":"overview","symbol":null,"parameters":{},"reply":"已经替你买入"}',
    ],
)
def test_invalid_or_unbounded_model_operations_are_rejected(payload):
    with pytest.raises(ValidationError):
        parse_intent(payload if isinstance(payload, str) else json.dumps(payload))


def test_dedicated_channel_handles_model_failure_without_claiming_success(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), grouped_config())
        try:
            result, message = await reply(plugin, "把一个不明确的东西改一下", "unclear")
            assert message.stopped and "没有修改记录" in result
            assert not plugin.research.profiles.current()["overrides"]
            assert "聊天设置总览" in (await reply(plugin, "隐藏设置总览", "overview"))[0]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_pending_edit_is_entry_bound_and_invalidated_by_intervening_change(plugin_module):
    async def scenario():
        first, second = Event.unified_msg_origin, "other:FriendMessage:owner"
        plugin = plugin_module.StockWatcherPlugin(
            Context(intent("set_stock", "MU", 目标="1000")), grouped_config(first, second)
        )
        try:
            await reply(plugin, "美光目标改为1000", "edit")
            assert "没有等待确认" in (await reply(plugin, "确认", "wrong-entry", second))[0]
            await command(plugin, "盯盘 设置 MU 理由=新的看法", "intervening")
            assert "已发生变化" in (await reply(plugin, "确认", "old-confirm"))[0]
            assert plugin.research.profiles.resolved("MU")["values"]["target_price"] is None
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_pending_can_be_cancelled_or_expire_without_mutation(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(
            Context(intent("watch", "MU"), intent("watch", "AAPL")), grouped_config()
        )
        try:
            await reply(plugin, "关注美光", "edit")
            assert "已取消" in (await reply(plugin, "取消", "cancel"))[0]
            assert not plugin.commands.ledger.watchlist()
            await reply(plugin, "关注苹果", "new-edit")
            key = plugin.conversation._pending_key(Event.unified_msg_origin)
            pending = plugin.store.get_setting(key)
            pending["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
            plugin.store.set_setting(key, pending)
            assert "超过15分钟" in (await reply(plugin, "确认", "expired"))[0]
            assert not plugin.commands.ledger.watchlist()
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_unlisted_or_group_message_cannot_initialize_chat_or_call_ai(plugin_module):
    async def scenario():
        context = Context()
        plugin = plugin_module.StockWatcherPlugin(context, grouped_config())
        try:
            result, message = await reply(
                plugin, "隐藏设置总览", "outsider", "other:FriendMessage:unknown"
            )
            assert not result and not message.stopped and plugin.store is None
            grouped = event("分析美光", "group")
            grouped.get_group_id = lambda: "group"
            assert [part async for part in plugin.wizard_reply(grouped)] == []
            assert not context.requests
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_registration_of_other_command_is_respected_in_exclusive_mode(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(Context(), grouped_config())
        try:
            message = event("重置", "other")
            message.extra["activated_handlers"] = [
                SimpleNamespace(event_filters=[type("CommandFilter", (), {})()])
            ]
            assert [part async for part in plugin.wizard_reply(message)] == []
            assert not message.stopped and plugin.store is None
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_pending_confirmation_survives_reload_and_does_not_duplicate_ledger(plugin_module):
    async def scenario():
        config = grouped_config()
        plugin = plugin_module.StockWatcherPlugin(
            Context(intent("record_position", "MU", 数量="2", 成本="100")), config
        )
        await reply(plugin, "登记已有美光2股，成本100美元", "position")
        assert not plugin.commands.ledger.view().positions
        await plugin.terminate()
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            results = await asyncio.gather(
                reply(plugin, "确认", "same"), reply(plugin, "确认", "same")
            )
            assert results[0][0] == results[1][0]
            assert "已登记" in results[0][0]
            assert len(plugin.store.rows("SELECT * FROM ledger_events")) == 1
            assert plugin.commands.ledger.view().positions["US:MU"].quantity == 2
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_upgrade_retains_saved_destination_and_subscriptions_then_mirrors_ui(
    plugin_module, tmp_path
):
    from stockwatcher.storage import Store

    old = Store(tmp_path / "stockwatcher.sqlite3")
    old.set_setting("notification_origin", Event.unified_msg_origin)
    old.set_setting("monitor_config", {"enabled": True, "daily": True})
    old.close()

    async def scenario():
        config = grouped_config()
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            await plugin.initialize()
            assert plugin.monitor.config()["enabled"] and plugin.monitor.config()["daily"]
            assert config["notifications"]["notification_origin"] == Event.unified_msg_origin
            assert config["notifications"]["enabled"] and config["notifications"]["daily"]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_ui_edits_after_reload_apply_and_invalidate_an_older_chat_draft(plugin_module):
    async def scenario():
        config = grouped_config()
        plugin = plugin_module.StockWatcherPlugin(
            Context(intent("set_stock", "MU", 目标="1000")), config
        )
        await reply(plugin, "把美光目标改成1000", "edit")
        await plugin.terminate()
        config["investment_defaults"].update(horizon_days=180, allow_add="false")
        config["notifications"].update(notification_origin=Event.unified_msg_origin, enabled=True)
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            await plugin.initialize()
            assert plugin.monitor.config()["enabled"]
            assert plugin.research.profiles.resolved("MU")["values"]["horizon_days"] == 180
            assert "已发生变化" in (await reply(plugin, "确认", "old"))[0]
            assert plugin.research.profiles.resolved("MU")["values"]["target_price"] is None
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_config_write_failure_is_reported_and_does_not_lose_saved_preferences_on_reload(
    plugin_module,
):
    class FailingConfig(Config):
        fail = False
        snapshot = None

        def save_config(self):
            if self.fail:
                raise OSError("fixture write failure")
            self.snapshot = copy.deepcopy(dict(self))

    async def scenario():
        config = FailingConfig(grouped_config())
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        await plugin.initialize()
        config.fail = True
        result = await command(plugin, "盯盘 设置 周期=180", "pref")
        assert "已保存" in result and "配置页同步失败" in result
        await plugin.terminate()
        # Recreate the host object from the last successfully written file.
        config = FailingConfig(config.snapshot)
        plugin = plugin_module.StockWatcherPlugin(Context(), config)
        try:
            await plugin.initialize()
            assert plugin.research.profiles.resolved("MU")["values"]["horizon_days"] == 180
            assert config["investment_defaults"]["horizon_days"] == 180
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_starting_wizard_cancels_previous_chat_edit_and_review_uses_chinese(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(
            Context(intent("set_stock", "MU", 目标="1000")), grouped_config()
        )
        try:
            await reply(plugin, "把美光目标改成1000", "edit")
            assert "之前的聊天修改草稿已取消" in await command(plugin, "盯盘 开始", "wizard")
            for i in range(10):
                state = plugin.store.get_setting("onboarding_state")
                if state["state"] == "review":
                    break
                result, _ = await reply(plugin, "跳过", f"skip-{i}")
            assert "个人长期偏好" in result and "单股仓位上限：未设置" in result
            assert "account_type" not in result and "null" not in result
            assert (
                plugin.store.get_setting(plugin.conversation._pending_key(Event.unified_msg_origin))
                is None
            )
            assert plugin.research.profiles.resolved("MU")["values"]["target_price"] is None
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
