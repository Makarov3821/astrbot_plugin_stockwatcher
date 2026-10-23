from datetime import UTC, datetime, timedelta

import pytest

from stockwatcher.domain import ValidationError
from stockwatcher.profiles import Profiles

NOW = datetime(2026, 1, 6, 22, tzinfo=UTC)


def test_inheritance_false_zero_unknown_and_personal_ceiling(store):
    profiles = Profiles(store)
    profiles.update({"allow_add": True, "max_stock_pct": "20"})
    profiles.update({"allow_add": False, "max_stock_pct": "0"}, symbol="MU")
    values = profiles.resolved("MU")["values"]
    assert values["allow_add"] is False
    assert values["max_stock_pct"] == "0"
    profiles.update({"allow_add": "继承", "max_stock_pct": None}, symbol="MU")
    resolved = profiles.resolved("MU")
    assert resolved["values"]["allow_add"] is True
    assert resolved["values"]["max_stock_pct"] == "20"
    assert resolved["sources"]["max_stock_pct"] == "个人约束"
    revision = profiles.current()["revision"]
    with pytest.raises(ValidationError, match="总约束"):
        profiles.update({"max_stock_pct": "30"}, symbol="MU")
    assert profiles.current()["revision"] == revision


def test_restart_config_does_not_overwrite_chat_and_keeps_audit(store):
    profiles = Profiles(store, {"horizon_days": 60})
    profiles.update({"horizon_days": 180})
    restarted = Profiles(store, {"horizon_days": 60})
    assert restarted.resolved("MU")["values"]["horizon_days"] == 180
    assert restarted.current()["revision"] == 2
    changed = Profiles(store, {"horizon_days": 90})
    assert changed.resolved("MU")["values"]["horizon_days"] == 90
    assert len(store.rows("SELECT * FROM profile_versions")) == 3
    with pytest.raises(ValidationError, match="已变化"):
        changed.update({"horizon_days": 100}, expected_revision=1)


def test_outlook_expires_with_new_york_deadline(store):
    profiles = Profiles(store)
    profiles.set_outlook("偏多", "2026-01-06", now=NOW)
    assert profiles.resolved("MU", now=NOW)["outlook"]["mode"] == "background"
    expired = profiles.resolved("MU", now=NOW + timedelta(days=1))
    assert expired["outlook"] is None
    assert expired["expired_outlook"]["direction"] == "偏多"


def test_wizard_draft_pause_resume_confirm_and_skip(store):
    profiles = Profiles(store)
    assert "账户类型" in profiles.wizard("开始", now=NOW)
    profiles.wizard("回答", {"账户": "现金"}, now=NOW)
    assert profiles.resolved("MU")["values"]["account_type"] == "unknown"
    profiles.wizard("暂停", now=NOW)
    assert "日线趋势" in Profiles(store).wizard("继续", now=NOW)
    profiles.wizard("回答", {"control": "采用默认"}, now=NOW)
    for _ in range(3):  # risk, drawdown, outlook
        profiles.wizard("回答", {"control": "跳过"}, now=NOW)
    assert "待确认" in profiles.wizard("回答", {"control": "采用默认"}, now=NOW)
    assert profiles.current()["revision"] == 1
    assert "已生效" in profiles.wizard("确认", now=NOW)
    assert profiles.current()["onboarding_complete"]
    assert profiles.resolved("MU")["values"]["account_type"] == "cash"
    assert profiles.resolved("MU")["values"]["max_stock_pct"] is None


def test_wizard_does_not_repeat_configured_fields_and_rejects_conflict(store):
    profiles = Profiles(store, {"account_type": "cash", "strategy": "trend_v1", "horizon_days": 60})
    assert "允许加仓" in profiles.wizard("开始", now=NOW)
    for _ in range(4):
        profiles.wizard("回答", {"control": "跳过"}, now=NOW)
    profiles.update({"horizon_days": 180})
    with pytest.raises(ValidationError, match="已变化"):
        profiles.wizard("确认", now=NOW)
    assert profiles.resolved("MU")["values"]["horizon_days"] == 180


def test_wizard_timeout_preserves_draft(store):
    profiles = Profiles(store)
    profiles.wizard("开始", now=NOW)
    assert "暂停" in profiles.wizard("回答", {"账户": "现金"}, now=NOW + timedelta(days=2))
    assert store.get_setting("onboarding_state")["index"] == 0


def test_stock_wizard_only_requests_specific_missing_preferences(store):
    profiles = Profiles(store, {"account_type": "cash", "horizon_days": 180, "allow_add": False})
    assert "个股目标" in profiles.wizard("开始", symbol="MU", now=NOW)
    with pytest.raises(ValidationError, match="未完成"):
        profiles.wizard("开始", symbol="AAPL", now=NOW)
    profiles.wizard("回答", {"目标": "200", "目标期限": "2026-12-31"}, now=NOW)
    assert "待确认" in profiles.wizard("回答", {"理由": "等待景气改善"}, now=NOW)
    profiles.wizard("确认", now=NOW)
    resolved = profiles.resolved("MU")
    assert resolved["values"]["target_price"] == "200"
    assert resolved["values"]["horizon_days"] == 180
    assert resolved["sources"]["horizon_days"] == "个人"
    assert resolved["values"]["allow_add"] is False
    assert not resolved["onboarding_complete"]  # stock setup must not finish account onboarding
    assert "待确认" in profiles.wizard("开始", symbol="MU", now=NOW)


@pytest.mark.parametrize(
    "patch",
    [
        {"drawdown_pct": "20"},
        {"max_stock_pct": "101"},
        {"horizon_days": True},
        {"strategy": "任意代码"},
        {"target_price": "90", "hard_stop_price": "100"},
    ],
)
def test_invalid_preferences_leave_revision_unchanged(store, patch):
    profiles = Profiles(store)
    with pytest.raises(ValidationError):
        profiles.update(patch)
    assert profiles.current()["revision"] == 1
