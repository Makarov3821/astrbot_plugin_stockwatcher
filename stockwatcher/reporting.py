"""Readable report summaries. Stored evidence and diagnostics remain available on demand."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from .domain import parse_time


def number(value, *, signed=False):
    try:
        numeric = Decimal(str(value))
        if not numeric.is_finite():
            return "未知"
        return format(numeric, "+.2f" if signed else ".2f")
    except (InvalidOperation, TypeError, ValueError):
        return "未知"


def short(text, limit=180):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def report_summary(report):
    packet, result = report["input"], report["result"]
    features, context = packet["metrics"], packet["portfolio"]
    status, symbol = report["status"], report["instrument"].removeprefix("US:")
    state = {
        "validated": "AI分析已通过校验",
        "degraded": "AI未完成，仅有程序观察",
        "observed": "程序观察，未调用AI",
        "superseded": "设置或账本已变化，需重新分析",
    }.get(status, "观察记录")
    lines = [f"{symbol} 分析 #{report['id']}｜{state}"]
    expired = parse_time(result["valid_until"]) < datetime.now(UTC)
    if expired:
        lines.append("这份报告已过有效期，仅供复盘，请重新分析。")
    question = packet.get("user_question") or ""
    bottom_question = bool(
        re.search(r"见底|底部|跌完|止跌|(?:回调|下跌|跌势).{0,12}到底", question)
    )
    if status == "validated":
        lines.append("结论：" + short(result["summary"], 240))
    elif status == "superseded":
        lines.append("结论：本次结果已失效，不据此生成新的操作计划。")
    elif bottom_question:
        lines.append("结论：目前无法确认这轮回调已经见底；本次没有形成通过校验的AI判断。")
    else:
        lines.append(
            "结论：本次没有形成有效的AI研判，以下只描述已取得的行情。"
            if status == "degraded"
            else "以下是程序根据日线生成的走势观察。"
        )
    mode = (
        "缓存行情，未验证新近性" if packet["market_mode"] == "explicit_cache" else "Alpaca延迟日线"
    )
    lines.append(
        f"依据截至 {features['trade_date']} 的{mode}：参考收盘价 {number(features['reference_price'])} 美元。"
    )
    if not packet["market_fresh"] and packet["market_mode"] != "explicit_cache":
        lines.append("尚未确认行情为最近完整交易日，不能据此判断当前入场时机。")
    if features.get("year_return_pct") is not None:
        lines.append(
            f"年内走势：{features['year']}年以来 {number(features['year_return_pct'], signed=True)}%（相对上一年末收盘，复权口径）。"
        )
    elif "year" in features and ("今年" in question or "年内" in question):
        lines.append("年内走势：缺少上一年末基准，暂不能给出完整年内涨跌幅。")
    ma20, ma60 = features.get("ma20"), features.get("ma60")
    if ma20 is not None and ma60 is not None:
        price = Decimal(features["reference_price"])
        relation = (
            "低于"
            if price < min(Decimal(ma20), Decimal(ma60))
            else "高于"
            if price > max(Decimal(ma20), Decimal(ma60))
            else "位于两者之间"
        )
        lines.append(
            f"趋势参考：20日均价 {number(ma20)}、60日均价 {number(ma60)} 美元；参考价{relation}这两条均线。"
            if relation != "位于两者之间"
            else f"趋势参考：20日均价 {number(ma20)}、60日均价 {number(ma60)} 美元；参考价位于两者之间。"
        )
    if features.get("drawdown20_high_pct") is not None:
        lines.append(
            f"近期回撤：相对近20个交易日高点回落 {number(features['drawdown20_high_pct'])}%。"
        )
    if features.get("atr14") is not None:
        lines.append(
            f"波动参考：近14日平均真实波幅约 {number(features['atr14'])} 美元（约为参考价的 {number(features.get('atr_pct'))}%）。"
        )
    if status == "validated":
        for title, key in (("主要依据", "reasons"), ("需要警惕", "counterarguments")):
            if result.get(key):
                lines.append(title + "：" + short(result[key][0]["text"], 120))
    if context["hard_exit_reasons"] and status != "superseded":
        lines.append("个人退出规则：" + short("；".join(context["hard_exit_reasons"]), 160))
    elif status == "validated":
        labels = {
            "hold": "继续观察持仓",
            "watch": "观察等待",
            "consider_buy": "等待买入候选条件确认",
            "consider_add": "等待加仓候选条件确认",
            "reduce": "考虑减仓",
            "exit": "考虑退出",
            "insufficient": "信息不足，暂不形成操作判断",
        }
        lines.append(
            ("当时的行动倾向：" if expired else "行动倾向：")
            + labels.get(result["final_action"], "观察等待")
            + "。"
        )
    if status == "validated" and result.get("entry"):
        entry = result["entry"]
        lines.append(
            f"回调候选：{number(entry['lower'])}—{number(entry['upper'])} 美元，低于 {number(entry['invalid_below'])} 美元失效；仍需日线确认。"
        )
        lines.append(
            "候选已过期，不能作为新的入场计划采用。"
            if expired
            else f"可用 /盯盘 计划 采用 {report['id']} 保存提醒计划；不会自动买入。"
        )
        if context.get("missing"):
            lines.append("账户风险或估值信息不全，暂不计算加仓数量。")
    if status == "validated" and result.get("target_proposal"):
        lines.append(
            f"另有AI目标情景候选 {number(result['target_proposal']['price'])} 美元；生成候选不会自动改写个人目标，采用状态见详情。"
        )
    if status == "validated" and result.get("invalidations"):
        lines.append("后续复核：" + short(result["invalidations"][0], 120))
    elif bottom_question:
        lines.append(
            "后续观察：是否停止创新低、能否持续收复近期均线；这些确认信号也不能保证最终底部。"
        )
    warnings = result.get("warnings", [])
    if status == "degraded":
        if any("引用" in item or "证据" in item for item in warnings):
            lines.append("分析未完成原因：AI引用的依据未通过核验，本次未采纳其结论。")
        else:
            lines.append("分析未完成原因：模型请求或结果校验失败，具体原因保留在详情中。")
    gaps = packet.get("source_gaps", [])
    missing_sources = []
    if any("SEC" in item for item in gaps):
        missing_sources.append("公司披露未完整核实")
    if any("新闻" in item for item in gaps):
        missing_sources.append("新闻覆盖不完整")
    if any("公开时间" in item for item in gaps):
        missing_sources.append("部分材料缺少公开时间")
    if missing_sources:
        lines.append("信息缺口：" + "；".join(missing_sources) + "。")
    research = report.get("model", {}).get("research")
    if research:
        lines.append(
            f"多角色研究：完成 {len(research.get('roles', {}))}/5 个角色；独立观点和失败信息在详情中。"
        )
    lines.append(f"详细依据、新闻链接和诊断：/盯盘 报告 {report['id']} 详情")
    return "\n".join(lines)
