"""Replay actual entry handlers for bare onboarding replies and pipeline ownership."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_plugin import Event, allowed_config
from test_plugin import plugin_module as plugin_module


def event(text, identity, origin=Event.unified_msg_origin):
    result = Event(text)
    result.message_obj = SimpleNamespace(message_id=identity)
    result.unified_msg_origin = origin
    return result


async def command(plugin, text, identity="start", origin=Event.unified_msg_origin):
    return "".join([part async for part in plugin.stockwatch(event(text, identity, origin))])


async def reply(plugin, text, identity, origin=Event.unified_msg_origin):
    message = event(text, identity, origin)
    result = "".join([part async for part in plugin.wizard_reply(message)])
    return result, message


def wizard_config(*origins):
    return {
        **allowed_config(*origins),
        "investment_defaults": {
            "account_type": "unknown",
            "strategy": "trend_v1",
            "horizon_days": 60,
            "notification_time": "07:30",
        },
    }


def test_reported_three_step_risk_wizard_accepts_bare_skip_before_stopping_pipeline(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), wizard_config())
        try:
            first = await command(plugin, "盯盘 开始")
            assert "步骤 1/3" in first and "允许加仓" in first
            message = event("跳过", "bare-skip")
            handler = plugin.wizard_reply(message)
            result = await anext(handler)
            assert "步骤 2/3" in result
            assert not message.stopped  # Stop before yield would prevent AstrBot from sending it.
            assert [part async for part in handler] == []
            assert message.stopped
            assert plugin.store.get_setting("onboarding_state")["index"] == 1
            assert plugin.research.profiles.current()["defaults"].get("max_stock_pct") is None
            assert not plugin.store.rows("SELECT * FROM model_calls")
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_whole_wizard_uses_bare_options_and_commits_only_after_confirmation(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), allowed_config())
        try:
            assert "账户类型" in await command(plugin, "盯盘 开始")
            answers = [
                "现金",
                "采用默认",
                "允许加仓=否 单股上限=20 总仓位上限=60 单次风险=1",
                "跳过",
                "暂不判断",
                "采用默认",
            ]
            for index, text in enumerate(answers):
                result, message = await reply(plugin, text, f"answer-{index}")
                assert result and message.stopped
                assert not plugin.research.profiles.current()["onboarding_complete"]
            assert "待确认" in result
            before = plugin.research.profiles.current()["revision"]
            final, message = await reply(plugin, "确认", "confirm")
            assert "已生效" in final and message.stopped
            profile = plugin.research.profiles.resolved("MU")
            assert profile["onboarding_complete"]
            assert profile["values"]["account_type"] == "cash"
            assert profile["values"]["allow_add"] is False
            assert profile["values"]["max_stock_pct"] == "20"
            assert profile["revision"] == before + 1
            duplicate, duplicate_event = await reply(plugin, "确认", "confirm")
            assert duplicate == final and duplicate_event.stopped
            assert plugin.research.profiles.current()["revision"] == before + 1
            normal, message = await reply(plugin, "跳过", "after-finish")
            assert not normal and not message.stopped
            assert not plugin.monitor.config()["enabled"]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text", ["今天有什么新闻？", "/todo list", "todo list", "我想跳过这个话题", "foo=bar"]
)
def test_unrelated_chat_and_commands_are_not_claimed(plugin_module, text):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), wizard_config())
        try:
            await command(plugin, "盯盘 开始")
            result, message = await reply(plugin, text, "unrelated")
            assert not result and not message.stopped
            assert plugin.store.get_setting("onboarding_state")["index"] == 0
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["跳过", "确认", "采用默认", "现金", "允许加仓=否"])
def test_short_messages_without_a_wizard_are_not_claimed(plugin_module, text):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), allowed_config())
        try:
            await plugin.initialize()
            result, message = await reply(plugin, text, "no-wizard")
            assert not result and not message.stopped
            assert plugin.store.get_setting("onboarding_state") is None
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_other_registered_command_has_priority_over_bare_wizard_word(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), wizard_config())
        try:
            await command(plugin, "盯盘 开始")
            message = event("跳过", "other-plugin-command")
            message.extra["activated_handlers"] = [
                SimpleNamespace(event_filters=[type("CommandFilter", (), {})()])
            ]
            assert [part async for part in plugin.wizard_reply(message)] == []
            assert not message.stopped
            assert plugin.store.get_setting("onboarding_state")["index"] == 0
            own = event("盯盘 回答 跳过", "explicit-answer")
            assert [part async for part in plugin.wizard_reply(own)] == []
            result = "".join([part async for part in plugin.stockwatch(own)])
            assert "步骤 2/3" in result
            assert plugin.store.get_setting("onboarding_state")["index"] == 1
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text", ["单股上限=101", "单股上限=20 单股上限=30", "采用默认", '理由="未闭合']
)
def test_invalid_recognized_answer_does_not_fall_through_or_advance(plugin_module, text):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), wizard_config())
        try:
            await command(plugin, "盯盘 开始")
            result, message = await reply(plugin, text, "bad-answer")
            assert "未保存" in result and message.stopped
            assert plugin.store.get_setting("onboarding_state")["index"] == 0
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_entry_binding_requires_explicit_transfer_between_whitelisted_entries(plugin_module):
    async def scenario():
        first, second = Event.unified_msg_origin, "other:FriendMessage:owner"
        plugin = plugin_module.StockWatcherPlugin(object(), wizard_config(first, second))
        try:
            await command(plugin, "盯盘 开始")
            result, message = await reply(plugin, "跳过", "second-entry", second)
            assert not result and not message.stopped
            assert "另一入口" in await command(plugin, "盯盘 引导 状态", "second-state", second)
            assert plugin.store.get_setting("onboarding_state")["origin"] == first
            assert "步骤 1/3" in await command(plugin, "盯盘 引导 继续", "transfer", second)
            result, message = await reply(plugin, "跳过", "old-entry")
            assert not result and not message.stopped
            assert "另一入口" in await command(plugin, "盯盘 回答 跳过", "old-explicit")
            result, message = await reply(plugin, "跳过", "new-entry", second)
            assert "步骤 2/3" in result and message.stopped
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_pause_timeout_and_resume_preserve_draft(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), wizard_config())
        try:
            await command(plugin, "盯盘 开始")
            state = plugin.store.get_setting("onboarding_state")
            state["touched_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
            plugin.store.set_setting("onboarding_state", state)
            result, message = await reply(plugin, "跳过", "timeout")
            assert "暂停" in result and message.stopped
            assert plugin.store.get_setting("onboarding_state")["index"] == 0
            result, message = await reply(plugin, "跳过", "paused-chat")
            assert not result and not message.stopped
            await command(plugin, "盯盘 引导 继续", "resume")
            result, message = await reply(plugin, "跳过", "resumed-answer")
            assert "步骤 2/3" in result and message.stopped
            await reply(plugin, "取消", "cancel")
            result, message = await reply(plugin, "确认", "after-cancel")
            assert not result and not message.stopped
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_duplicate_skip_is_idempotent_in_parallel_and_after_restart(plugin_module):
    async def scenario():
        config = wizard_config()
        plugin = plugin_module.StockWatcherPlugin(object(), config)
        await command(plugin, "盯盘 开始")
        answers = await asyncio.gather(
            reply(plugin, "跳过", "same-message"), reply(plugin, "跳过", "same-message")
        )
        assert answers[0][0] == answers[1][0]
        assert plugin.store.get_setting("onboarding_state")["index"] == 1
        await plugin.terminate()
        plugin = plugin_module.StockWatcherPlugin(object(), config)
        try:
            await plugin.initialize()
            response, message = await reply(plugin, "跳过", "same-message")
            assert response == answers[0][0] and message.stopped
            assert plugin.store.get_setting("onboarding_state")["index"] == 1
            await reply(plugin, "跳过", "new-message")
            assert plugin.store.get_setting("onboarding_state")["index"] == 2
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_legacy_draft_requires_resume_and_rejects_unlisted_or_group_replies(plugin_module):
    async def scenario():
        plugin = plugin_module.StockWatcherPlugin(object(), wizard_config())
        try:
            await plugin.initialize()
            plugin.research.profiles.wizard("开始")  # persisted v0.6.0 draft had no origin
            response, message = await reply(plugin, "跳过", "legacy-unbound")
            assert "引导 继续" in response and "未保存" in response and message.stopped
            assert plugin.store.get_setting("onboarding_state")["index"] == 0
            await command(plugin, "盯盘 引导 继续", "bind-legacy")
            outsider, message = await reply(
                plugin, "跳过", "outsider", "other:FriendMessage:stranger"
            )
            assert not outsider and not message.stopped
            grouped = event("跳过", "group")
            grouped.get_group_id = lambda: "group-id"
            assert [part async for part in plugin.wizard_reply(grouped)] == []
            assert not grouped.stopped
            response, message = await reply(plugin, "跳过", "legacy-answer")
            assert "步骤 2/3" in response and message.stopped
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
