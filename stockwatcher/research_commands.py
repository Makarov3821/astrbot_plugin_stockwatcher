"""Phase 04-07 commands; settings and evidence remain usable without AI access."""

from __future__ import annotations

import asyncio
import json

from .analysis import AnalysisService
from .arguments import parse_options, required
from .domain import ValidationError, instrument
from .evidence import EvidenceStore
from .profiles import LABELS, Profiles, named_patch
from .sources import SourceService
from .storage import Store


class ResearchCommands:
    def __init__(
        self,
        store: Store,
        profiles: Profiles,
        evidence: EvidenceStore,
        sources: SourceService,
        analysis: AnalysisService,
    ):
        self.store, self.profiles, self.evidence, self.sources, self.analysis = (
            store,
            profiles,
            evidence,
            sources,
            analysis,
        )
        self.monitor = None

    async def nudge(self) -> str:
        def once():
            with self.store.transaction() as db:
                profile = self.profiles._load(db)
                if (
                    profile["onboarding_complete"]
                    or db.execute(
                        "SELECT 1 FROM settings WHERE key='onboarding_hint_shown'"
                    ).fetchone()
                ):
                    return ""
                db.execute("INSERT INTO settings VALUES ('onboarding_hint_shown','true')")
                return "\n首次使用可发送 /盯盘 开始 分步设置；查行情和记账可继续使用。"

        return await asyncio.to_thread(once)

    async def handle(
        self, tokens: list[str], *, origin: str, request_key: str | None
    ) -> str | None:
        command = tokens[0]
        if self.monitor is not None and command in {"监控", "日报", "计划", "通知"}:
            return await self._monitor_command(tokens)
        if command == "开始" and len(tokens) in {1, 2}:
            return await asyncio.to_thread(
                self.profiles.wizard, "开始", symbol=tokens[1] if len(tokens) == 2 else None
            )
        if command == "引导" and len(tokens) == 2:
            return await asyncio.to_thread(self.profiles.wizard, tokens[1])
        if command == "回答" and len(tokens) >= 2:
            rest = tokens[1:]
            if len(rest) == 1 and rest[0] in {"采用默认", "跳过", "暂不判断"}:
                options = {"control": rest[0]}
            elif rest[0] in {"偏多", "中性", "偏空"}:
                options = parse_options(rest[1:], {"期限", "基准", "理由"})
                required(options, "期限")
                options["direction"] = rest[0]
            else:
                options = parse_options(rest, set(LABELS))
            return await asyncio.to_thread(self.profiles.wizard, "回答", options)
        if command == "设置":
            symbol, rest = None, tokens[1:]
            if rest and "=" not in rest[0]:
                symbol, rest = instrument(rest[0]), rest[1:]
            if rest:
                options = parse_options(rest, set(LABELS))
                revision = await asyncio.to_thread(
                    self.profiles.update, named_patch(options), symbol=symbol
                )
                return f"设置已保存，版本 {revision}；旧分析保留，新的研判采用新设置。"
            if symbol:
                result = await asyncio.to_thread(self.profiles.resolved, symbol)
            else:
                result = await asyncio.to_thread(self.profiles.current)
            return (
                json.dumps(result, ensure_ascii=False, indent=2)
                + "\n可编辑："
                + "、".join(LABELS)
                + "。个股字段可用 值=继承；百分数不带%号，未设置不等于0。"
            )
        if command == "大盘预期":
            if len(tokens) == 1:
                profile = await asyncio.to_thread(self.profiles.resolved, "SPY")
                return json.dumps(
                    {"生效预期": profile["outlook"], "已过期": profile["expired_outlook"]},
                    ensure_ascii=False,
                )
            if len(tokens) >= 3:
                options = parse_options(tokens[2:], {"期限", "基准", "理由"})
                revision = await asyncio.to_thread(
                    self.profiles.set_outlook,
                    tokens[1],
                    required(options, "期限"),
                    options.get("基准", "SPY"),
                    options.get("理由", ""),
                )
                return f"市场预期已保存，版本 {revision}；仅作背景，到期停止应用。"
            raise ValidationError(
                "用法：大盘预期 偏多/中性/偏空/暂不判断 期限=YYYY-MM-DD [基准=SPY 理由=...]。"
            )
        if command == "材料" and len(tokens) >= 3:
            action, symbol = tokens[1], instrument(tokens[2])
            if action == "更新" and len(tokens) == 3:
                profile = await asyncio.to_thread(self.profiles.resolved, symbol)
                report = await self.sources.sync(symbol, ir_url=profile["values"]["ir_url"])
                return json.dumps(report, ensure_ascii=False, indent=2)
            if action == "列表" and len(tokens) == 3:
                packet = await asyncio.to_thread(self.evidence.packet, symbol)
                items = [
                    {
                        key: row[key]
                        for key in (
                            "evidence_id",
                            "version",
                            "title",
                            "url",
                            "published_at",
                            "content_level",
                            "verified_source",
                        )
                    }
                    for row in packet["items"]
                ]
                return json.dumps(
                    {"材料": items, "未用于判断": packet["excluded"]}, ensure_ascii=False, indent=2
                )
            if action == "添加":
                options = parse_options(tokens[3:], {"链接", "标题", "发布时间", "内容"})
                evidence_id = await asyncio.to_thread(
                    self.evidence.manual,
                    symbol,
                    url=required(options, "链接"),
                    title=required(options, "标题"),
                    content=required(options, "内容"),
                    published_at=required(options, "发布时间"),
                )
                return f"材料 {evidence_id} 已保存为用户提供、未经抓取核验的备注；不会冒充已核实财务指标。"
            raise ValidationError("材料操作为 更新/列表/添加。")
        if command == "分析" and 2 <= len(tokens) <= 3:
            if len(tokens) == 3 and tokens[2] != "缓存":
                raise ValidationError("第三个参数仅支持 缓存；默认联网查询并刷新证据。")
            report = await self.analysis.analyze(
                tokens[1],
                origin=origin,
                cached=len(tokens) == 3,
                request_key=f"analysis:{request_key}" if request_key else None,
            )
            if (
                self.monitor is not None
                and self.monitor.config()["auto_adopt"]
                and report["result"].get("entry")
                and report["status"] == "validated"
                and report["input"]["market_fresh"]
            ):
                await asyncio.to_thread(self.monitor.adopt, report["id"])
            return await asyncio.to_thread(self.analysis.render, report)
        if command == "分析记录" and len(tokens) == 2:
            rows = await asyncio.to_thread(
                self.store.rows,
                "SELECT id,created_at,status FROM analysis_runs WHERE instrument=? ORDER BY id DESC LIMIT 10",
                (instrument(tokens[1]),),
            )
            return json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2)
        if command == "报告" and len(tokens) == 2:
            try:
                report = await asyncio.to_thread(self.analysis.load, int(tokens[1]))
            except ValueError as exc:
                raise ValidationError("报告 ID 应为整数。") from exc
            return await asyncio.to_thread(self.analysis.render, report)
        if command == "目标":
            if len(tokens) == 1:
                rows = await asyncio.to_thread(
                    self.store.rows, "SELECT * FROM target_proposals ORDER BY id DESC LIMIT 10"
                )
                return json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2)
            if len(tokens) == 3 and tokens[1] in {"采用", "拒绝"}:
                try:
                    proposal_id = int(tokens[2])
                except ValueError as exc:
                    raise ValidationError("目标候选 ID 应为整数。") from exc
                return await asyncio.to_thread(
                    self.analysis.decide_target, proposal_id, tokens[1] == "采用"
                )
            raise ValidationError("用法：目标 [采用|拒绝 候选ID]。")
        return None

    async def _monitor_command(self, tokens):
        command = tokens[0]
        if command == "监控":
            if len(tokens) == 1 or tokens[1:] == ["状态"]:
                return json.dumps(
                    await asyncio.to_thread(self.monitor.status), ensure_ascii=False, indent=2
                )
            if len(tokens) == 2 and tokens[1] in {"开启", "关闭"}:
                result = await asyncio.to_thread(
                    self.monitor.configure, {"enabled": tokens[1] == "开启"}
                )
                return (
                    "监控设置已保存：" + json.dumps(result, ensure_ascii=False) + "；不自动下单。"
                )
            if tokens[1:] == ["巡检"]:
                return json.dumps(await self.monitor.tick(), ensure_ascii=False)
            if len(tokens) >= 3 and tokens[1] == "设置":
                options = parse_options(tokens[2:], {"免扰开始", "免扰结束", "自动采用"})
                patch = {}
                for key, value in options.items():
                    if key == "自动采用":
                        if value not in {"是", "否"}:
                            raise ValidationError(
                                "自动采用使用 是/否，只作用于后续新生成的受支持回调候选。"
                            )
                        patch["auto_adopt"] = value == "是"
                    else:
                        patch[{"免扰开始": "quiet_start", "免扰结束": "quiet_end"}[key]] = value
                return json.dumps(
                    await asyncio.to_thread(self.monitor.configure, patch), ensure_ascii=False
                )
        if command == "日报" and len(tokens) == 2:
            patches = {
                "开启": {"daily": True},
                "关闭": {"daily": False},
                "AI开启": {"daily_ai": True},
                "AI关闭": {"daily_ai": False},
            }
            if tokens[1] in patches:
                return "日报设置已保存（仍需监控开启及接收会话）：" + json.dumps(
                    await asyncio.to_thread(self.monitor.configure, patches[tokens[1]]),
                    ensure_ascii=False,
                )
        if command == "计划":
            if len(tokens) == 1:
                return json.dumps(
                    (await asyncio.to_thread(self.monitor.status))["plans"],
                    ensure_ascii=False,
                    indent=2,
                )
            if len(tokens) == 3 and tokens[1] in {"采用", "取消"}:
                try:
                    number = int(tokens[2])
                except ValueError as exc:
                    raise ValidationError("计划/报告 ID 应为整数。") from exc
                if tokens[1] == "采用":
                    number = await asyncio.to_thread(self.monitor.adopt, number)
                    return (
                        f"已保存监控计划 #{number}，等待候选区与完整日线确认；未开启监控时只保存。"
                    )
                return (
                    "已取消计划。"
                    if await asyncio.to_thread(self.monitor.cancel, number)
                    else "计划不存在或已结束。"
                )
        if command == "通知":
            if len(tokens) == 1:
                return json.dumps(
                    (await asyncio.to_thread(self.monitor.status))["notifications"],
                    ensure_ascii=False,
                    indent=2,
                )
            if len(tokens) == 3 and tokens[1] == "重试":
                try:
                    number = int(tokens[2])
                except ValueError as exc:
                    raise ValidationError("通知 ID 应为整数。") from exc
                from datetime import UTC, datetime

                await asyncio.to_thread(self.monitor.delivery.retry, number, datetime.now(UTC))
                return "失败通知已排队，遵守监控开关、有效期及免打扰设置。"
        raise ValidationError(
            "用法：监控 [开启|关闭|状态|巡检|设置 ...]；日报 开启/关闭/AI开启/AI关闭；计划 [采用 报告ID|取消 计划ID]；通知 [重试 ID]。"
        )
