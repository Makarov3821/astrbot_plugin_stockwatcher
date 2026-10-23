"""Chinese user-facing summaries, shared by commands, onboarding and conversation."""

from datetime import datetime
from zoneinfo import ZoneInfo

from .profiles import DEFAULTS

NAMES = {
    "account_type": "账户类型",
    "strategy": "观察方式",
    "horizon_days": "观察周期",
    "allow_add": "允许给已有持仓加仓",
    "max_stock_pct": "单股仓位上限",
    "max_total_pct": "总仓位上限",
    "risk_budget_pct": "单次风险预算",
    "drawdown_pct": "回撤阈值",
    "drawdown_basis": "回撤参考基准",
    "drawdown_effect": "触及回撤后的处理",
    "notification_time": "日报时间",
    "target_price": "个人目标价",
    "target_deadline": "目标期限",
    "target_action": "到达目标后的处理",
    "thesis": "持有或关注理由",
    "review_target": "允许 AI 提出目标调整候选",
    "hard_stop_price": "明确退出价",
    "ir_url": "公司公告页面",
}
VALUES = {
    "unknown": "尚未指定",
    "cash": "现金账户",
    "margin": "保证金账户",
    "trend_v1": "日线趋势观察",
    "cost": "买入成本",
    "holding_high": "本轮持仓期间高点",
    "rolling20_high": "近20个交易日高点",
    "review": "提醒重新评估",
    "exit": "提示退出",
    "reduce": "提示减仓",
    "alert": "提醒",
    "__inherit__": "沿用个人设置",
}
PLAN_STATES = {
    "waiting": "等待回调",
    "armed": "已进入候选区，等待日线确认",
    "triggered": "已满足提醒条件",
    "expired": "已过期",
    "invalidated": "已失效",
    "superseded": "已被新设置或计划替代",
    "completed": "已结束",
    "cancelled": "已取消",
    "pending": "等待采用或拒绝",
    "accepted": "已采用",
    "rejected": "已拒绝",
    "restored_inactive": "恢复备份后停用",
}
PERSONAL_FIELDS = tuple(list(DEFAULTS)[:10]) + ("notification_time",)


def value_text(key, value):
    if value is None or value == "":
        return "未设置"
    if type(value) is bool:
        return "是" if value else "否"
    if key.endswith("_pct"):
        return f"{value}%"
    if key == "horizon_days":
        return f"{value} 天"
    if key in {"target_price", "hard_stop_price"}:
        return f"{value} 美元"
    return VALUES.get(str(value), str(value))


def preferences_text(values, *, stock=False):
    fields = values.keys() if stock else PERSONAL_FIELDS
    return "\n".join(f"{NAMES.get(k, k)}：{value_text(k, values.get(k))}" for k in fields)


def outlook_text(profile, timezone="Asia/Shanghai"):
    outlook = profile.get("outlook") or profile.get("expired_outlook")
    if outlook is None:
        outlook = profile.get("outlooks", {}).get("US")
    if not outlook or outlook["direction"] == "暂不判断":
        return "美股大盘预期：暂不判断（未设置有效的方向判断）。"
    expires = datetime.fromisoformat(outlook["expires_at"].replace("Z", "+00:00"))
    from datetime import UTC

    expired = expires < datetime.now(UTC)
    local = expires.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"美股大盘预期：{outlook['direction']}"
        + ("（已过期，不再用于新分析）" if expired else "")
        + f"\n有效至：{local}（{timezone}）"
        + f"\n参照标的：{outlook['benchmark'].removeprefix('US:')}"
        + f"\n判断理由：{outlook.get('reason') or '未填写'}"
        + "\n用途：作为分析背景，不自动放宽仓位或风险限制。"
    )


def profile_text(profile):
    if "values" in profile:
        return f"{profile['instrument']} 当前生效偏好：\n" + preferences_text(
            profile["values"], stock=True
        )
    return "个人长期偏好：\n" + preferences_text({**DEFAULTS, **profile["defaults"]})
