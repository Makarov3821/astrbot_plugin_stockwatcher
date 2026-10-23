"""Explicit commands, independent of the messaging platform."""

from __future__ import annotations

import asyncio
import json
import shlex
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import __version__
from .arguments import parse_options, required
from .config import Settings
from .domain import NEW_YORK, MarketBatch, ValidationError, instrument, text_number
from .ledger import Ledger
from .market import MarketService
from .storage import SCHEMA_VERSION, Store

HELP = """StockWatcher v0.6：个人设置、证据与 AI 研判
/盯盘 入口（只读查看当前私聊 UMO；业务需在插件设置添加白名单）
/盯盘 监控 [开启|关闭|状态|巡检|重试]
/盯盘 监控 设置 免扰开始=22:00 免扰结束=07:00 自动采用=否
/盯盘 日报 开启/关闭/AI开启/AI关闭
/盯盘 计划 [采用 报告ID|取消 计划ID]
/盯盘 通知 [重试 通知ID]
/盯盘 开始 [MU] | 回答 ... | 引导 继续/暂停/返回/取消/确认
/盯盘 设置 [MU] [周期=180 目标=... 目标期限=YYYY-MM-DD ...]
/盯盘 大盘预期 偏多 期限=YYYY-MM-DD [基准=SPY 理由=...]
/盯盘 材料 更新 MU | 列表 MU
/盯盘 材料 添加 MU 链接=https://... 标题=... 发布时间=ISO时间 内容="原文摘要"
/盯盘 分析 MU [缓存]
/盯盘 深度 MU [缓存]
/盯盘 用量
/盯盘 备份 [列表]
/盯盘 分析记录 MU | 报告 分析ID [详情|引用诊断]
/盯盘 目标 [采用|拒绝 候选ID]
/盯盘 状态
/盯盘 行情 MU [日线|分钟] [raw|split|dividend|all]
/盯盘 缓存 MU [日线|分钟] [raw|split|dividend|all]
/盯盘 日历 [YYYY-MM-DD]
/盯盘 诊断 [MU]
/盯盘 持仓
/盯盘 持仓 添加 MU 数量=10 成本=100 [日期=YYYY-MM-DD]
/盯盘 关注 [添加 MU 理由=观察回调 | 移除 MU]
/盯盘 成交 买入 MU 数量=1 价格=100 [费用=0] [日期=YYYY-MM-DD]
/盯盘 成交 卖出 MU 数量=1 价格=110 [费用=0] [日期=YYYY-MM-DD]
/盯盘 现金 [设置|入金|出金|收入 金额 [日期=YYYY-MM-DD]]
/盯盘 拆股 MU 比例=2:1 [日期=YYYY-MM-DD]
/盯盘 记录
/盯盘 撤销 记录ID 原因=录入有误
/盯盘 提醒目标 [当前|清除]
/盯盘 推送测试

金额为 USD；碎股最多 8 位小数。日期按纽约交易日，同日按登记顺序。
已有仓位用期初持仓登记，不扣现金；现金未知时不校验购买力。
AI 复用 AstrBot 模型，目标候选不会自动覆盖个人目标。监控和日报默认关闭，需明确开启。"""


class Commands:
    def __init__(
        self, store: Store, ledger: Ledger, market: MarketService, settings: Settings, research=None
    ):
        self.store, self.ledger, self.market, self.settings = store, ledger, market, settings
        self.research = research

    def status(self) -> str:
        view = self.ledger.view()
        configured = "已配置，尚需运行诊断验证实际权限" if self.settings.configured else "未配置"
        return (
            f"StockWatcher {__version__} / 数据库版本 {SCHEMA_VERSION}\n"
            f"个人组合：{sum(p.quantity > 0 for p in view.positions.values())} 只持仓，{len(self.ledger.watchlist())} 只关注\n"
            f"现金 USD：{text_number(view.cash) if view.cash is not None else '未知（未校验购买力）'}\n"
            f"Alpaca：{configured}；账户模式 {self.settings.account_mode}\n"
            f"行情：SIP 历史接口，查询至少延迟 {self.settings.delay_minutes} 分钟\n"
            f"提醒目标：{'已设置' if self.store.get_setting('notification_origin') else '未设置'}\n"
            + (
                "AI：复用当前会话或指定的 AstrBot 模型；实际可用性需运行分析验证。"
                if self.research
                else "AI：当前实例未配置研判服务。"
            )
            + (
                "\n后台监控："
                + ("开启" if self.research.monitor.config()["enabled"] else "关闭")
                + "；收盘日报："
                + ("开启" if self.research.monitor.config()["daily"] else "关闭")
                + "；AI日报："
                + ("开启" if self.research.monitor.config()["daily_ai"] else "关闭")
                if self.research and self.research.monitor
                else "\n当前入口未配置监控服务。"
            )
        )

    def holdings(self) -> str:
        view = self.ledger.view()
        lines = ["持仓（USD，加权平均账面成本，非税务成本）"]
        for position in view.positions.values():
            if position.quantity:
                lines.append(
                    f"{position.instrument}：{text_number(position.quantity)} 股，均价 {position.average_cost:.6f}，成本总额 {position.cost_basis:.6f}"
                )
        if len(lines) == 1:
            lines.append("暂无持仓。")
        realized = sum((p.realized_pnl for p in view.positions.values()), Decimal("0"))
        lines.append(f"已登记卖出的已实现损益：{realized:.6f}；不含未登记历史、税项及现金分红。")
        lines.append(
            "现金：" + (text_number(view.cash) if view.cash is not None else "未知，未校验购买力")
        )
        return "\n".join(lines)

    def _write(self, kind: str, options: dict, symbol: str | None, request_key: str | None) -> str:
        kwargs = {
            "symbol": symbol,
            "day": options.get("日期"),
            "note": options.get("备注", ""),
            "request_key": request_key,
        }
        if kind in {"seed", "buy", "sell"}:
            kwargs.update(
                quantity=required(options, "数量"),
                price=required(options, "成本" if kind == "seed" else "价格"),
                fee=options.get("费用", "0"),
            )
        elif kind == "split":
            ratio = required(options, "比例").split(":")
            if len(ratio) != 2:
                raise ValidationError("拆股比例格式为 新股数:旧股数，例如 2:1。")
            kwargs.update(numerator=ratio[0], denominator=ratio[1])
        else:
            kwargs["cash"] = required(options, "金额")
        event_id, created = self.ledger.record(kind, **kwargs)
        result = f"{'已登记' if created else '重复请求未重复入账'}，记录 #{event_id}。"
        if kind == "seed":
            result += "期初持仓不扣现金，更早的成交与收益未知。"
        if self.ledger.view().cash is None and kind in {"buy", "sell"}:
            result += "现金余额未知，本次未校验购买力。"
        return result

    def _local(self, tokens: list[str], origin: str, request_key: str | None) -> str:
        command = tokens[0]
        if command == "状态" and len(tokens) == 1:
            return self.status()
        if command == "持仓":
            if len(tokens) == 1:
                return self.holdings()
            if len(tokens) >= 4 and tokens[1] == "添加":
                options = parse_options(tokens[3:], {"数量", "成本", "费用", "日期", "备注"})
                return self._write("seed", options, tokens[2], request_key)
        elif command == "关注":
            if len(tokens) == 1:
                rows = self.ledger.watchlist()
                return "关注池\n" + (
                    "\n".join(
                        f"{row['instrument']}：{row['reason'] or '未填写理由'}" for row in rows
                    )
                    if rows
                    else "暂无关注标的。"
                )
            if len(tokens) >= 3 and tokens[1] == "添加":
                options = parse_options(tokens[3:], {"理由"})
                self.ledger.watch(tokens[2], options.get("理由", ""))
                return f"已关注 {instrument(tokens[2])}。已有持仓与关注状态分别保留。"
            if len(tokens) == 3 and tokens[1] == "移除":
                return (
                    "已移除关注，成交记录未变。"
                    if self.ledger.unwatch(tokens[2])
                    else "该股票不在关注池。"
                )
        elif command == "成交" and len(tokens) >= 4 and tokens[1] in {"买入", "卖出"}:
            options = parse_options(tokens[3:], {"数量", "价格", "费用", "日期", "备注"})
            return self._write(
                "buy" if tokens[1] == "买入" else "sell", options, tokens[2], request_key
            )
        elif command == "现金":
            if len(tokens) == 1:
                view = self.ledger.view()
                return (
                    f"现金 USD：{text_number(view.cash)}"
                    if view.cash is not None
                    else "现金未知。可先用 /盯盘 现金 设置 金额 登记余额快照。"
                )
            kinds = {"设置": "cash_set", "入金": "deposit", "出金": "withdraw", "收入": "income"}
            if len(tokens) >= 3 and tokens[1] in kinds:
                options = parse_options(tokens[3:], {"日期", "备注"})
                options["金额"] = tokens[2]
                return self._write(kinds[tokens[1]], options, None, request_key)
        elif command == "拆股" and len(tokens) >= 3:
            options = parse_options(tokens[2:], {"比例", "日期", "备注"})
            return self._write("split", options, tokens[1], request_key)
        elif command == "记录" and len(tokens) == 1:
            rows = self.ledger.events()
            lines = ["最近 20 条账本记录（撤销不删除历史）"]
            for row in rows:
                data = json.loads(row["payload"])
                lines.append(
                    f"#{row['id']} {row['event_date']} {row['kind']} {row['instrument'] or 'USD'} "
                    f"{json.dumps(data, ensure_ascii=False)}"
                    + (f" [已撤销：{row['void_note']}]" if row["voided_at"] else "")
                )
            return "\n".join(lines) if rows else "暂无账本记录。"
        elif command == "撤销" and len(tokens) >= 3:
            try:
                event_id = int(tokens[1])
            except ValueError as exc:
                raise ValidationError("记录 ID 应为整数。") from exc
            options = parse_options(tokens[2:], {"原因"})
            changed = self.ledger.void(event_id, required(options, "原因"))
            return "已撤销并重新核算。" if changed else "该记录已撤销，无需重复操作。"
        elif command == "提醒目标" and len(tokens) <= 2:
            if len(tokens) == 1:
                return (
                    "提醒目标已设置，可用“当前”替换或“清除”移除。"
                    if self.store.get_setting("notification_origin")
                    else "尚未设置；使用 /盯盘 提醒目标 当前。"
                )
            if tokens[1] == "当前":
                if not origin:
                    raise ValidationError("当前入口没有可保存的会话地址。")
                self.store.set_setting("notification_origin", origin)
                return "已保存当前会话为提醒目标；监控开关可用 /盯盘 监控 查看。"
            if tokens[1] == "清除":
                self.store.set_setting("notification_origin", None)
                return "已清除提醒目标。"
        raise ValidationError("指令或参数不完整，请使用 /盯盘 帮助 查看示例。")

    def format_batch(self, batch: MarketBatch, *, cached: bool = False) -> str:
        tz = ZoneInfo(self.settings.display_timezone)
        lines = [
            f"{'缓存记录（未联网刷新）' if cached else '延迟行情'}：{batch.instrument}",
            f"来源 {batch.source}/{batch.feed}，周期 {batch.timeframe}，复权 {batch.adjustment}，数量 {len(batch.bars)}",
            f"查询截止：{batch.requested_end.astimezone(tz).isoformat()}",
            f"抓取时间：{batch.fetched_at.astimezone(tz).isoformat()}",
        ]
        if batch.bars:
            bar = batch.bars[-1]
            if batch.timeframe == "1Day":
                lines.append(
                    f"最近已结束交易日：{bar.timestamp.astimezone(NEW_YORK).date()}（供应商日线，时段覆盖待实测）"
                )
            else:
                lines.append(
                    f"最后完整分钟起点：{bar.timestamp.astimezone(tz).isoformat()}（常规时段）"
                )
            lines.append(
                f"OHLC：{bar.open} / {bar.high} / {bar.low} / {bar.close}；成交量 {bar.volume} 股"
            )
        else:
            lines.append("没有可用数据，不能据此推断最新价格或停牌原因。")
        lines.append("上述为历史时点数据，不表示当前仍能按该价格成交。")
        return "\n".join(lines)

    async def handle(self, text: str, *, origin: str = "", request_key: str | None = None) -> str:
        try:
            tokens = shlex.split(text)
        except ValueError as exc:
            raise ValidationError('引号未闭合；包含空格的理由可写成 理由="等待 回调"。') from exc
        if tokens and tokens[0].lstrip("/!#") in {"盯盘", "stockwatch"}:
            tokens.pop(0)
        if not tokens or tokens == ["帮助"] or tokens == ["help"]:
            return HELP
        if self.research is not None:
            response = await self.research.handle(tokens, origin=origin, request_key=request_key)
            if response is not None:
                return response
        if tokens[0] in {"行情", "缓存"}:
            if not 2 <= len(tokens) <= 4:
                raise ValidationError("用法：/盯盘 行情 MU [日线|分钟] [raw|split|dividend|all]")
            timeframe = {"日线": "1Day", "分钟": "1Min"}.get(
                tokens[2] if len(tokens) >= 3 else "日线"
            )
            adjustment = tokens[3] if len(tokens) == 4 else "raw"
            if timeframe is None or adjustment not in {"raw", "split", "dividend", "all"}:
                raise ValidationError("周期应为日线或分钟，复权应为 raw/split/dividend/all。")
            batch = await (
                self.market.cached(tokens[1], timeframe, adjustment)
                if tokens[0] == "缓存"
                else self.market.query(tokens[1], timeframe=timeframe, adjustment=adjustment)
            )
            return (
                self.format_batch(batch, cached=tokens[0] == "缓存")
                if batch
                else "暂无该口径的缓存；配置行情后执行查询。"
            )
        if tokens[0] == "日历" and len(tokens) <= 2:
            try:
                day = (
                    date.fromisoformat(tokens[1])
                    if len(tokens) == 2
                    else datetime.now(NEW_YORK).date()
                )
            except ValueError as exc:
                raise ValidationError("日期格式应为 YYYY-MM-DD。") from exc
            sessions = await self.market.provider.calendar(day, day)
            if not sessions:
                return f"{day} 不是美股交易日（来源 Alpaca 日历）。"
            session = sessions[0]
            return f"{day} 常规时段（纽约）：{session.opens_at.astimezone(NEW_YORK).isoformat()} 至 {session.closes_at.astimezone(NEW_YORK).isoformat()}"
        if tokens[0] == "诊断" and len(tokens) <= 2:
            result = await self.market.diagnose(tokens[1] if len(tokens) == 2 else "MU")
            return json.dumps(result, ensure_ascii=False, indent=2)
        return await asyncio.to_thread(self._local, tokens, origin, request_key)
