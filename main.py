"""AstrBot entry point through step 07; no autonomous polling or order execution."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .stockwatcher.analysis import AnalysisService
from .stockwatcher.commands import Commands
from .stockwatcher.config import Settings
from .stockwatcher.domain import ValidationError
from .stockwatcher.evidence import EvidenceStore
from .stockwatcher.ledger import Ledger
from .stockwatcher.market import MarketService
from .stockwatcher.model import AstrBotModel
from .stockwatcher.profiles import Profiles
from .stockwatcher.provider import AlpacaProvider, MarketError
from .stockwatcher.research_commands import ResearchCommands
from .stockwatcher.sources import PublicSources, SourceService
from .stockwatcher.storage import Store


class StockWatcherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = Settings.from_mapping(config)
        self._init_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self.store: Store | None = None
        self.provider: AlpacaProvider | None = None
        self.commands: Commands | None = None
        self.public_sources: PublicSources | None = None
        self.research: ResearchCommands | None = None

    async def initialize(self) -> None:
        async with self._init_lock:
            if self.commands is not None or self._closing:
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
                model = AstrBotModel(
                    self.context,
                    provider_id=self.settings.model_provider_id,
                    timeout_seconds=self.settings.model_timeout_seconds,
                )
                analysis = AnalysisService(
                    self.store, profiles, ledger, market, evidence, sources, model
                )
                self.research = ResearchCommands(self.store, profiles, evidence, sources, analysis)
                self.commands = Commands(self.store, ledger, market, self.settings, self.research)
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
        if not any(word in text.split() for word in ("开始", "回答", "引导")):
            result += await self.research.nudge()
        return result

    async def _push_test(self, current_origin: str) -> str:
        assert self.store is not None
        target = await asyncio.to_thread(
            self.store.get_setting, "notification_origin", current_origin
        )
        if not target:
            target = current_origin
        try:
            found = await self.context.send_message(
                target,
                MessageChain().message("StockWatcher 主动消息测试：此消息不代表已启动自动盯盘。"),
            )
        except Exception:
            # Platform exceptions may contain tokens or session addresses. Do not echo them.
            return "推送测试失败，请检查目标会话及平台主动消息能力。"
        if found is False:
            return "推送测试失败：未找到对应平台，请重新设置提醒目标。"
        return "已调用平台主动发送接口，请确认是否收到测试消息；最终送达取决于平台。"

    @filter.command("盯盘", alias={"stockwatch"})
    async def stockwatch(self, event: AstrMessageEvent):
        if self._closing:
            yield event.plain_result("StockWatcher 正在关闭，请稍后重试。")
            return
        try:
            await self.initialize()
            if self._closing:
                yield event.plain_result("StockWatcher 正在关闭，请稍后重试。")
                return
            assert self.commands is not None
            text = event.get_message_str().strip()
            origin = event.unified_msg_origin
            message_id = getattr(getattr(event, "message_obj", None), "message_id", "")
            request_key = (
                hashlib.sha256(f"{origin}:{message_id}".encode()).hexdigest()
                if message_id
                else None
            )
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
            yield event.plain_result(response)
        except (ValidationError, MarketError) as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("StockWatcher command failed (%s)", type(exc).__name__)
            yield event.plain_result(
                "操作未完成。请查看插件运行状态；原始异常未回显，以避免泄露配置。"
            )

    async def terminate(self) -> None:
        self._closing = True
        async with self._init_lock:
            tasks = list(self._tasks)
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
