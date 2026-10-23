"""AstrBot personal stock assistant; order execution is never performed."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .stockwatcher.access import allowed_origins, event_private_origin, is_identity_command
from .stockwatcher.analysis import AnalysisService
from .stockwatcher.budget import BudgetedModel
from .stockwatcher.commands import Commands
from .stockwatcher.config import Settings
from .stockwatcher.deep import DeepResearch
from .stockwatcher.delivery import Delivery, message_parts
from .stockwatcher.domain import ValidationError
from .stockwatcher.evidence import EvidenceStore
from .stockwatcher.ledger import Ledger
from .stockwatcher.market import MarketService
from .stockwatcher.model import AstrBotModel
from .stockwatcher.monitor import Monitor
from .stockwatcher.profiles import Profiles
from .stockwatcher.provider import AlpacaProvider, MarketError
from .stockwatcher.research_commands import ResearchCommands, looks_like_wizard_reply
from .stockwatcher.sources import PublicSources, SourceService
from .stockwatcher.storage import Store


class StockWatcherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._raw_config = config
        self.settings = Settings.from_mapping(config)
        self._access_notice_logged = False
        self._init_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self.store: Store | None = None
        self.provider: AlpacaProvider | None = None
        self.commands: Commands | None = None
        self.public_sources: PublicSources | None = None
        self.research: ResearchCommands | None = None
        self.monitor: Monitor | None = None
        self._worker: asyncio.Task | None = None

    def _allowed_origins(self) -> tuple[str, ...]:
        try:
            return allowed_origins(self._raw_config.get("allowed_origins", []))
        except ValidationError:
            return ()

    def _origin_allowed(self, origin: str) -> bool:
        return bool(origin) and origin in self._allowed_origins()

    async def initialize(self) -> None:
        """打开宿主数据目录并启动可取消的巡检任务；未订阅时不请求行情。"""
        async with self._init_lock:
            if self.commands is not None or self._closing:
                return
            if not self._allowed_origins():
                if not self._access_notice_logged:
                    logger.warning(
                        "StockWatcher 消息入口白名单至少需要一项；业务和自动任务未启动，仅 /盯盘 入口 可用。"
                    )
                    self._access_notice_logged = True
                return
            path = (
                Path(StarTools.get_data_dir("astrbot_plugin_stockwatcher")) / "stockwatcher.sqlite3"
            )
            opening = asyncio.create_task(asyncio.to_thread(Store, path))
            try:
                self.store = await asyncio.shield(opening)
            except asyncio.CancelledError:
                # A cancelled to_thread call would otherwise abandon a live SQLite handle.
                opened = await opening
                await asyncio.to_thread(opened.close)
                raise
            try:
                self.provider = AlpacaProvider(self.settings)
                ledger = Ledger(self.store)
                market = MarketService(self.provider, self.store)
                profiles = Profiles(self.store, self.settings.investment_defaults)
                evidence = EvidenceStore(self.store)
                self.public_sources = PublicSources(
                    self.store,
                    evidence,
                    user_agent=self.settings.sec_user_agent,
                    ir_hosts=self.settings.ir_allowed_hosts,
                )
                sources = SourceService(self.store, evidence, self.public_sources, self.provider)
                adapter = AstrBotModel(
                    self.context,
                    provider_id=self.settings.model_provider_id,
                    timeout_seconds=self.settings.model_timeout_seconds,
                    concurrency=self.settings.deep_concurrency,
                )
                model = BudgetedModel(
                    adapter, self.store, self.settings, origin_allowed=self._origin_allowed
                )
                analysis = AnalysisService(
                    self.store, profiles, ledger, market, evidence, sources, model
                )
                analysis.deep_engine = DeepResearch(model, self.settings)
                analysis.max_model_input_chars = self.settings.model_max_input_chars
                self.research = ResearchCommands(self.store, profiles, evidence, sources, analysis)
                self.monitor = Monitor(
                    analysis,
                    Delivery(
                        self.store,
                        self._send,
                        timezone=self.settings.display_timezone,
                        origin_allowed=self._origin_allowed,
                    ),
                    self.settings,
                )
                self.research.monitor = self.monitor
                self.commands = Commands(self.store, ledger, market, self.settings, self.research)
                self._worker = asyncio.create_task(self.monitor.run(), name="stockwatcher-monitor")
            except BaseException:
                if self.public_sources is not None:
                    await self.public_sources.close()
                if self.provider is not None:
                    await self.provider.close()
                await asyncio.to_thread(self.store.close)
                self.store = None
                raise

    async def _dispatch(self, text: str, origin: str, request_key: str | None) -> str:
        assert self.commands is not None and self.research is not None
        result = await self.commands.handle(text, origin=origin, request_key=request_key)
        if self.monitor is not None:
            await asyncio.to_thread(self.monitor.reconcile)
        if not any(word in text.split() for word in ("开始", "回答", "引导")):
            result += await self.research.nudge()
        return result

    async def _send(self, origin: str, text: str):
        if not self._origin_allowed(origin):
            return False
        return await self.context.send_message(origin, MessageChain().message(text))

    @staticmethod
    def _request_key(event, origin):
        message_id = getattr(getattr(event, "message_obj", None), "message_id", "")
        return hashlib.sha256(f"{origin}:{message_id}".encode()).hexdigest() if message_id else None

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def wizard_reply(self, event: AstrMessageEvent):
        """接收当前授权入口的引导短回答；普通聊天和显式指令不接管。"""
        origin = event_private_origin(event)
        if self._closing or not self._origin_allowed(origin) or self.research is None:
            return
        text = event.get_message_str().strip()
        if not looks_like_wizard_reply(text):
            return
        # WakingStage may strip a wake prefix. Registered commands still take precedence.
        handlers = event.get_extra("activated_handlers", []) or []
        if any(
            type(item).__name__ in {"CommandFilter", "CommandGroupFilter"}
            for handler in handlers
            for item in getattr(handler, "event_filters", [])
        ):
            return
        task = asyncio.create_task(
            self.research.chat_reply(
                text, origin=origin, request_key=self._request_key(event, origin)
            )
        )
        self._tasks.add(task)
        try:
            response = await task
        except Exception as exc:
            logger.error("StockWatcher wizard reply failed (%s)", type(exc).__name__)
            response = (
                "本次引导回答未完成，请用 /盯盘 引导 状态 检查草稿；未由普通聊天模型代为确认。"
            )
        finally:
            self._tasks.discard(task)
        if response is None:
            return
        for part in message_parts(response):
            if not self._origin_allowed(origin):
                event.stop_event()
                return
            yield event.plain_result(part)
        # Stop only AFTER yielding the response, so AstrBot's response stage can send it.
        event.stop_event()

    async def _push_test(self, current_origin: str) -> str:
        assert self.store is not None
        target = await asyncio.to_thread(
            self.store.get_setting, "notification_origin", current_origin
        )
        if not target:
            target = current_origin
        if not self._origin_allowed(target):
            return "提醒目标不在消息入口白名单中；请从已授权私聊使用 /盯盘 提醒目标 当前 重新选择。"
        try:
            found = await self._send(
                target, "StockWatcher 主动消息测试：此消息不代表已启动自动盯盘。"
            )
        except Exception:
            # Platform exceptions may contain tokens or session addresses. Do not echo them.
            return "推送测试失败，请检查目标会话及平台主动消息能力。"
        if found is False:
            return "推送测试失败：未找到对应平台，请重新设置提醒目标。"
        return "已调用平台主动发送接口，请确认是否收到测试消息；最终送达取决于平台。"

    @filter.command("盯盘", alias={"stockwatch"})
    async def stockwatch(self, event: AstrMessageEvent):
        """个人盯盘助理：持仓与关注、偏好引导、行情研判、监控和日报。"""
        origin = event_private_origin(event)
        if origin is None:
            yield event.plain_result("StockWatcher 仅接受已授权的私聊入口；请在私聊中配置和使用。")
            return
        text = event.get_message_str().strip()
        if is_identity_command(text):
            yield event.plain_result(
                "当前私聊入口 UMO：\n"
                + origin
                + "\n请在插件设置的「消息入口白名单」逐项添加完整标识，至少一项，可添加多个；保存后重载插件。此指令仅显示本会话标识，不自动授权。"
            )
            return
        if not self._origin_allowed(origin):
            yield event.plain_result(
                "当前入口未授权。请在插件设置的「消息入口白名单」添加至少一个私聊入口；可用 /盯盘 入口 查看本会话标识。"
            )
            return
        if self._closing:
            yield event.plain_result("StockWatcher 正在关闭，请稍后重试。")
            return
        try:
            await self.initialize()
            if not self._origin_allowed(origin):
                return
            if self._closing:
                yield event.plain_result("StockWatcher 正在关闭，请稍后重试。")
                return
            assert self.commands is not None
            request_key = self._request_key(event, origin)
            words = text.split()
            if len(words) == 2 and words[-1] == "推送测试":
                coroutine = self._push_test(origin)
            else:
                coroutine = self._dispatch(text, origin, request_key)
            task = asyncio.create_task(coroutine)
            self._tasks.add(task)
            try:
                response = await task
            finally:
                self._tasks.discard(task)
            for part in message_parts(response):
                if not self._origin_allowed(origin):
                    return
                yield event.plain_result(part)
        except (ValidationError, MarketError) as exc:
            if self._origin_allowed(origin):
                yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("StockWatcher command failed (%s)", type(exc).__name__)
            if self._origin_allowed(origin):
                yield event.plain_result(
                    "操作未完成。请查看插件运行状态；原始异常未回显，以避免泄露配置。"
                )

    async def terminate(self) -> None:
        """卸载或重载时等待任务取消，再关闭网络客户端与数据库。"""
        self._closing = True
        async with self._init_lock:
            tasks = list(self._tasks)
            if self._worker is not None:
                tasks.append(self._worker)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
            if self.provider is not None:
                await self.provider.close()
            if self.public_sources is not None:
                await self.public_sources.close()
            if self.store is not None:
                await asyncio.to_thread(self.store.close)
            self.commands = None
