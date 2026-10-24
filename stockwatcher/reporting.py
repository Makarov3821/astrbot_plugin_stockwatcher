"""Readable report summaries. Stored evidence and diagnostics remain available on demand."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from .citations import preference_path_hints
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


ROLE_NAMES = {
    "fundamental": "基本面",
    "technical": "技术面",
    "event": "事件",
    "bull": "多方",
    "bear": "空方",
}
STANCE_NAMES = {"support": "偏支持", "oppose": "偏反对", "mixed": "利弊并存", "unknown": "信息不足"}


def failure_label(reason):
    if "超时" in reason:
        return "请求超时"
    if "预算" in reason or "额度" in reason:
        return "调用预算不足"
    if "引用" in reason or "证据ID" in reason:
        return "引用未通过校验"
    if any(word in reason for word in ("JSON", "结构", "格式", "文本", "观察期限", "角色需")):
        return "返回格式未通过校验"
    if "不可用" in reason or "请求失败" in reason:
        return "模型请求未完成"
    return "未完成，原因见详情" if reason else "未记录具体原因"


def research_lines(research, *, detailed=False):
    roles, errors = research.get("roles", {}), research.get("errors", {})
    count = sum(role in roles for role in ROLE_NAMES)
    lines = [
        f"多角色研究：完成 {count}/5 个角色；"
        + (
            "五个角色已完成，汇总未完成。"
            if count == 5 and "synthesis" in errors
            else "并非完整五角色结论。"
            if count < 5
            else "五个角色与汇总均已完成。"
        )
    ]
    if research.get("quality") == "single_fallback":
        lines.append("本次已退回单模型备选，不能视为多角色研究完成。")
    elif research.get("quality") == "failed" or "synthesis" in errors:
        lines.append("汇总未完成，以下仅保留已通过校验的角色观点。")
    if count:
        lines.append("角色观点（观察期限分别列出，不按人数推算胜率）：")
    else:
        lines.append("没有已完成的研究角色，无法比较角色观点。")
    for role, name in ROLE_NAMES.items():
        if role not in roles:
            continue
        data = roles[role]
        title = f"{name}{'（' + role + '）' if detailed else ''}｜{data['horizon_days']}天｜{STANCE_NAMES.get(data['stance'], '信息不足')}"
        if detailed:
            lines.append(title)
            for label, key in (("论点", "arguments"), ("反证/缺口", "uncertainties")):
                for item in data[key]:
                    lines.append(f"{label}：{item['text']} [{', '.join(item['evidence_ids'])}]")
        else:
            argument = short(data["arguments"][0]["text"], 80)
            uncertainty = short(data["uncertainties"][0]["text"], 60)
            lines.append(f"{title}：{argument}；保留疑点：{uncertainty}")
    missing = [
        name + "（" + failure_label(errors.get(role, "")) + "）"
        for role, name in ROLE_NAMES.items()
        if role not in roles
    ]
    if missing:
        lines.append("缺失角色：" + "、".join(missing))
    if detailed:
        for role, reason in errors.items():
            lines.append(
                f"失败诊断 {ROLE_NAMES.get(role, '汇总' if role == 'synthesis' else role)}（{role}）：{reason}"
            )
    elif "synthesis" in errors:
        lines.append("汇总失败：" + failure_label(errors["synthesis"]))
    lines.append("所有角色共用本次材料，不会因角色增加而自动补齐财报或实时行情。")
    return lines


def citation_diagnostics(report):
    """Small diagnostic view; never echo complete model output or all news by default."""
    research = report.get("model", {}).get("research") or {}
    diagnostics = research.get("diagnostics", {})
    lines = [f"报告 #{report['id']} 引用诊断（不重新调用模型）"]

    def append_error(error):
        lines.append("出错位置：`" + error["field"] + "`")
        codes = {
            "unknown_reference": "引用不在本次输入中",
            "missing": "缺少必要引用",
            "invalid_type": "需要引用数组",
            "invalid_item_type": "数组含有非字符串",
            "too_many": "引用超过数量上限",
        }
        lines.append("原因：" + codes.get(error["code"], error["code"]))
        if error.get("rejected_values"):
            lines.append(
                "被拒绝引用：" + "、".join(f"`{value}`" for value in error["rejected_values"][:3])
            )
        hints = error.get("available_path_hints") or preference_path_hints(
            error.get("rejected_values", []), research.get("shared_evidence_ids", [])
        )
        if hints:
            lines.append(
                "可用路径提示（需核对论据，不自动替换）："
                + "、".join(f"`{path}`" for path in hints.values())
            )

    if diagnostics:
        names = {**ROLE_NAMES, "synthesis": "汇总"}
        labels = {
            "validated": "通过校验",
            "rejected": "校验拒绝",
            "unavailable": "请求未完成",
            "cancelled": "已取消",
            "skipped": "未调用",
            "not_started": "未启动",
            "requesting": "请求中",
            "returned": "已返回",
        }
        for key, name in names.items():
            item = diagnostics.get(key)
            if not item:
                continue
            lines.append(f"{name}：{labels.get(item['status'], item['status'])}")
            if item.get("citation_error"):
                append_error(item["citation_error"])
            elif item.get("error"):
                lines.append("诊断：" + short(item["error"], 160))
        lines.append("已返回的角色原始输出保存在本地报告记录中，此处不整段发送。")
    elif research.get("errors"):
        lines.append(
            "这是旧报告，仅保存失败摘要，未保存失败角色原文或具体被拒绝的引用，无法追溯精确ID。"
        )
        for role, error in research["errors"].items():
            lines.append(
                f"{ROLE_NAMES.get(role, '汇总' if role == 'synthesis' else role)}：{short(error, 140)}"
            )
    elif report.get("model", {}).get("citation_error"):
        append_error(report["model"]["citation_error"])
    elif report.get("status") == "validated":
        lines.append("这份记录没有引用校验失败。")
    else:
        lines.append("这份记录没有保留精确的引用失败诊断；未据此推测错误ID。")
    references = research.get("shared_evidence_ids", [])
    plan = research.get("budget_preflight", {})
    if plan.get("status") == "insufficient":
        lines.append(
            f"整轮预算预检：预计需 {plan['estimated_tokens']:,}，今日剩余 {plan['remaining_tokens']:,}；预算不足，未启动角色调用。"
        )
    if references:
        lines.append(
            f"本次角色实际可用材料编号：{sum(ref.startswith('ev_') for ref in references)}个；裁掉或仅出现在历史记录中的材料不可作为本次引用。"
        )
    lines.append(
        "允许精确引用实际提供的指标/账户/偏好字段与材料缺口；未知新闻编号和不存在的字段仍会被拒绝。"
    )
    return "\n".join(lines)


def budget_summary(stats):
    remaining_calls = max(0, stats["call_limit"] - stats["calls"])
    remaining_tokens = max(0, stats["token_planning_limit"] - stats["reserved_token_estimate"])
    return "\n".join(
        [
            f"模型用量（{stats['day']}，{stats['timezone']}）",
            f"调用次数：已用 {stats['calls']} / 上限 {stats['call_limit']}；剩余 {remaining_calls} 次。",
            f"估算预算：已预留 {stats['reserved_token_estimate']:,} / 上限 {stats['token_planning_limit']:,}；剩余 {remaining_tokens:,}。",
            "普通分析、深度研究、复杂自然语言理解及AI日报共享预算；一轮深度最多6次调用。",
            "输入按UTF-8字节数加输出额度保守预留，失败和取消也占用；不是厂商实际token用量或账单。",
            f"按 {stats['timezone']} 每日00:00重置。可在插件设置「05 · AI 理解与研判」调整每日调用次数及估算预算，保存并重载。",
        ]
    )


def report_summary(report):
    packet, result = report["input"], report["result"]
    features, context = packet["metrics"], packet["portfolio"]
    status, symbol = report["status"], report["instrument"].removeprefix("US:")
    research = report.get("model", {}).get("research")
    if not research and packet.get("research_mode") == "deep":
        research = {
            "quality": "not_started",
            "roles": {},
            "errors": {"synthesis": "；".join(result.get("warnings", []))},
        }
    role_count = sum(role in research.get("roles", {}) for role in ROLE_NAMES) if research else 0
    state = {
        "validated": "AI分析已通过校验",
        "degraded": "AI未完成，仅有程序观察",
        "observed": "程序观察，未调用AI",
        "superseded": "设置或账本已变化，需重新分析",
    }.get(status, "观察记录")
    if research and status == "validated":
        state = (
            "五角色研究与汇总已通过校验"
            if role_count == 5 and not research.get("errors")
            else f"深度研究未完整完成（{role_count}/5），仅有部分角色汇总"
            if role_count
            else "深度未完成，已退回单模型备选"
        )
    elif research and status == "degraded":
        state = f"深度汇总未完成（角色完成{role_count}/5），仅有程序观察"
    lines = [f"{symbol} {'深度研究' if research else '分析'} #{report['id']}｜{state}"]
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
    plan = (research or {}).get("budget_preflight", {})
    if plan.get("status") == "insufficient":
        lines.append(
            f"整轮预算不足：预计需预留约 {plan['estimated_tokens']:,}，今日剩余 {plan['remaining_tokens']:,}；本次未启动模型调用。"
        )
        lines.append(
            "可查看 /盯盘 用量，在插件设置「05 · AI 理解与研判」调整每日预算并重载，或等待次日。"
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
        if not research or status != "validated":
            lines.append(
                f"趋势参考：20日均价 {number(ma20)}、60日均价 {number(ma60)} 美元；参考价{relation}这两条均线。"
                if relation != "位于两者之间"
                else f"趋势参考：20日均价 {number(ma20)}、60日均价 {number(ma60)} 美元；参考价位于两者之间。"
            )
    if features.get("drawdown20_high_pct") is not None and (not research or status != "validated"):
        lines.append(
            f"近期回撤：相对近20个交易日高点回落 {number(features['drawdown20_high_pct'])}%。"
        )
    if features.get("atr14") is not None and (not research or status != "validated"):
        lines.append(
            f"波动参考：近14日平均真实波幅约 {number(features['atr14'])} 美元（约为参考价的 {number(features.get('atr_pct'))}%）。"
        )
    if research:
        lines += research_lines(research)
    if status == "validated" and not research:
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
    lines.append(f"详细依据、新闻链接和诊断：/盯盘 报告 {report['id']} 详情")
    return "\n".join(lines)
