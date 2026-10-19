"""Opt-in daily research, zero-model patrol and explicitly adopted entry plans."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .analysis import AnalysisService
from .delivery import Delivery
from .domain import NEW_YORK, MarketBatch, ValidationError, iso, parse_time
from .provider import MarketError

DEFAULT_MONITOR = {
    "enabled": False,
    "daily": False,
    "daily_ai": False,
    "quiet_start": "22:00",
    "quiet_end": "07:00",
    "auto_adopt": False,
}
ACTIVE = {"waiting", "armed"}


class Monitor:
    def __init__(self, analysis: AnalysisService, delivery: Delivery, settings):
        self.analysis, self.delivery, self.settings = analysis, delivery, settings
        self.store, self.profiles, self.ledger, self.market = (
            analysis.store,
            analysis.profiles,
            analysis.ledger,
            analysis.market,
        )
        self._lock = asyncio.Lock()

    def config(self):
        return {**DEFAULT_MONITOR, **self.store.get_setting("monitor_config", {})}

    def configure(self, patch: dict):
        current = self.config()
        if set(patch) - set(DEFAULT_MONITOR):
            raise ValidationError("不支持的监控设置。")
        for key, value in patch.items():
            if key.startswith("quiet_"):
                try:
                    parsed = time.fromisoformat(value)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("免打扰时间格式为 HH:MM。") from exc
                if parsed.tzinfo or len(value) != 5:
                    raise ValidationError("免打扰时间格式为 HH:MM。")
            elif type(value) is not bool:
                raise ValidationError("监控开关使用 是/否。")
        current.update(patch)
        if current["enabled"] and not self.store.get_setting("notification_origin"):
            raise ValidationError("先用 /盯盘 提醒目标 当前 选择接收会话，再开启。")
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO settings VALUES ('monitor_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(current),),
            )
            if not current["enabled"]:
                db.execute(
                    "UPDATE notification_outbox SET status='cancelled' WHERE status IN ('pending','sending')"
                )
            elif not current["daily"]:
                db.execute(
                    "UPDATE notification_outbox SET status='cancelled' WHERE status IN ('pending','sending') AND event_key LIKE 'daily:%'"
                )
        return current

    def _signature(self, db):
        events = db.execute("SELECT * FROM ledger_events ORDER BY event_date,id").fetchall()
        return hashlib.sha256(
            json.dumps([dict(row) for row in events], sort_keys=True).encode()
        ).hexdigest(), events

    def adopt(self, run_id: int, *, now: datetime | None = None):
        now = now or datetime.now(UTC)
        report = self.analysis.load(run_id)
        packet, result = report["input"], report["result"]
        if (
            report["status"] != "validated"
            or not packet["market_fresh"]
            or not result.get("entry")
            or parse_time(result["valid_until"]) <= now
        ):
            raise ValidationError("仅可采用未过期、行情有效且校验通过的回调候选。")
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT id FROM monitor_plans WHERE analysis_id=?", (run_id,)
            ).fetchone()
            if old:
                return old["id"]
            signature, _ = self._signature(db)
            if (
                packet["preference"]["revision"] != self.profiles._load(db)["revision"]
                or signature != packet["ledger_signature"]
            ):
                raise ValidationError("设置或账本已变化，请重新分析后采用。")
            db.execute(
                "UPDATE monitor_plans SET state='superseded',reason='已采用更新计划' WHERE instrument=? AND state IN ('waiting','armed')",
                (report["instrument"],),
            )
            cursor = db.execute(
                "INSERT INTO monitor_plans(analysis_id,instrument,profile_revision,ledger_signature,state,created_at,expires_at,reason,payload) VALUES (?,?,?,?,'waiting',?,?,?,?)",
                (
                    run_id,
                    report["instrument"],
                    report["profile_revision"],
                    signature,
                    iso(now),
                    result["valid_until"],
                    "等待进入候选区",
                    json.dumps(
                        {
                            "entry": result["entry"],
                            "held": packet["portfolio"]["held"],
                            "quantity": packet["portfolio"]["quantity"],
                        }
                    ),
                ),
            )
            return cursor.lastrowid

    def reconcile(self, *, now: datetime | None = None):
        now = now or datetime.now(UTC)
        with self.store.transaction() as db:
            revision = self.profiles._load(db)["revision"]
            signature, events = self._signature(db)
            book = self.ledger._replay([row for row in events if not row["voided_at"]])
            for row in db.execute(
                "SELECT * FROM monitor_plans WHERE state IN ('waiting','armed')"
            ).fetchall():
                state, reason = None, ""
                if parse_time(row["expires_at"]) <= now:
                    state, reason = "expired", "有效期已过，等待重新分析"
                elif row["profile_revision"] != revision:
                    state, reason = "superseded", "个人设置已变化，旧计划停用"
                elif row["ledger_signature"] != signature:
                    data = json.loads(row["payload"])
                    position = book.positions.get(row["instrument"])
                    quantity = position.quantity if position else Decimal(0)
                    state = "completed" if quantity > Decimal(data["quantity"]) else "superseded"
                    reason = (
                        "检测到账本新增持仓，结束候选，后续按持仓监控"
                        if state == "completed"
                        else "账本已变化，旧计划停用"
                    )
                if state:
                    self._transition(db, dict(row), state, reason, now)
            # Pending trigger messages must not survive changed preferences or ledger.
            for row in db.execute(
                "SELECT id,event_key FROM notification_outbox WHERE status IN ('pending','sending') AND event_key LIKE 'plan:%'"
            ).fetchall():
                parts = row["event_key"].split(":")
                if parts[-1] not in {"armed", "triggered"}:
                    continue
                plan = db.execute(
                    "SELECT * FROM monitor_plans WHERE id=?", (int(parts[1]),)
                ).fetchone()
                if plan and (
                    plan["profile_revision"] != revision
                    or plan["ledger_signature"] != signature
                    or parse_time(plan["expires_at"]) <= now
                ):
                    db.execute(
                        "UPDATE notification_outbox SET status='cancelled' WHERE id=?", (row["id"],)
                    )

    def _transition(self, db, plan: dict, state: str, reason: str, now: datetime, *, asof=None):
        db.execute(
            "UPDATE monitor_plans SET state=?,reason=?,armed_at=CASE WHEN ?='armed' THEN ? ELSE armed_at END,last_asof=COALESCE(?,last_asof) WHERE id=?",
            (state, reason, state, asof or iso(now), asof, plan["id"]),
        )
        config, origin = self.config(), self.store.get_setting("notification_origin")
        if config["enabled"] and origin:
            self.delivery.enqueue(
                db,
                f"plan:{plan['id']}:{state}",
                origin,
                f"计划 #{plan['id']} {plan['instrument']}：{state}\n{reason}\n行情时点 {asof or '无新增行情'}；不自动下单或改持仓。",
                now,
                ttl_hours=max(
                    0, min(24, (parse_time(plan["expires_at"]) - now).total_seconds() / 3600)
                )
                if state in {"armed", "triggered"}
                else 24,
                guard={
                    "profile_revision": plan["profile_revision"],
                    "ledger_signature": plan["ledger_signature"],
                }
                if state in {"armed", "triggered"}
                else None,
            )

    def cancel(self, plan_id: int):
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE monitor_plans SET state='cancelled',reason='用户取消' WHERE id=? AND state IN ('waiting','armed')",
                (plan_id,),
            ).rowcount
            db.execute(
                "UPDATE notification_outbox SET status='cancelled' WHERE event_key LIKE ? AND status IN ('pending','sending')",
                (f"plan:{plan_id}:%",),
            )
        return bool(changed)

    def retry_jobs(self):
        with self.store.transaction() as db:
            return db.execute(
                "UPDATE monitor_jobs SET status='retry',attempts=0,next_at=? WHERE status='failed'",
                (iso(datetime.now(UTC)),),
            ).rowcount

    def symbols(self):
        held = sorted(key for key, pos in self.ledger.view().positions.items() if pos.quantity > 0)
        watched = [row["instrument"] for row in self.ledger.watchlist()]
        plans = [
            row["instrument"]
            for row in self.store.rows(
                "SELECT instrument FROM monitor_plans WHERE state IN ('waiting','armed') ORDER BY id"
            )
        ]
        return list(dict.fromkeys(held + plans + sorted(watched)))

    def _evaluate(self, symbol: str, batch: MarketBatch, now: datetime, packet: dict | None = None):
        bar = batch.bars[-1]
        asof = packet["market_asof"] if packet else iso(bar.timestamp + timedelta(minutes=1))
        with self.store.transaction() as db:
            revision = self.profiles._load(db)["revision"]
            signature, events = self._signature(db)
            book = self.ledger._replay([row for row in events if not row["voided_at"]])
            profile = self.profiles.resolved(symbol, now=now)
            position = book.positions.get(symbol)
            values, labels = profile["values"], []
            if position and position.quantity > 0:
                if values["hard_stop_price"] is not None and bar.close <= Decimal(
                    values["hard_stop_price"]
                ):
                    labels.append("触及个人明确退出价")
                if values["target_price"] is not None and bar.close >= Decimal(
                    values["target_price"]
                ):
                    labels.append("达到个人目标，处理偏好：" + values["target_action"])
                if (
                    values["drawdown_pct"] is not None
                    and values["drawdown_basis"] == "cost"
                    and position.average_cost > 0
                ):
                    if (1 - bar.close / position.average_cost) * 100 >= Decimal(
                        values["drawdown_pct"]
                    ):
                        labels.append("达到成本回撤阈值，处理偏好：" + values["drawdown_effect"])
                if (
                    packet
                    and packet["ledger_signature"] == signature
                    and packet["preference"]["revision"] == revision
                ):
                    labels += (
                        packet["portfolio"]["hard_exit_reasons"] + packet["portfolio"]["warnings"]
                    )
            origin = self.store.get_setting("notification_origin")
            if labels and origin:
                day = bar.timestamp.astimezone(NEW_YORK).date().isoformat()
                digest = hashlib.sha256(
                    json.dumps(sorted(set(labels)), ensure_ascii=False).encode()
                ).hexdigest()[:12]
                self.delivery.enqueue(
                    db,
                    f"risk:{symbol}:{day}:{revision}:{signature[:12]}:{digest}",
                    origin,
                    f"{symbol} 延迟规则提醒\n"
                    + "；".join(sorted(set(labels)))
                    + f"\n截至 {asof}，参考收盘 {bar.close} USD；不保证当前可成交。",
                    now,
                    guard={"profile_revision": revision, "ledger_signature": signature},
                )
            for row in db.execute(
                "SELECT * FROM monitor_plans WHERE instrument=? AND state IN ('waiting','armed')",
                (symbol,),
            ).fetchall():
                plan = dict(row)
                if (
                    plan["profile_revision"] != revision
                    or plan["ledger_signature"] != signature
                    or parse_time(plan["expires_at"]) <= now
                    or parse_time(asof) <= parse_time(plan["created_at"])
                ):
                    continue
                if plan["last_asof"] and parse_time(asof) <= parse_time(plan["last_asof"]):
                    continue
                entry = json.loads(plan["payload"])["entry"]
                eligible = (
                    [
                        item
                        for item in batch.bars
                        if item.timestamp >= parse_time(plan["created_at"])
                        and (
                            not plan["last_asof"]
                            or item.timestamp + timedelta(minutes=1) > parse_time(plan["last_asof"])
                        )
                    ]
                    if not packet
                    else [bar]
                )
                if not eligible:
                    continue
                # A daily high/low may precede adoption on that same day. Use its close only then.
                whole_day = (
                    not packet
                    or bar.timestamp.astimezone(NEW_YORK).date()
                    > parse_time(plan["created_at"]).astimezone(NEW_YORK).date()
                )
                low = min(item.low for item in eligible) if whole_day else bar.close
                if low <= Decimal(entry["invalid_below"]):
                    self._transition(
                        db,
                        plan,
                        "invalidated",
                        "已观测区间触及候选失效价，停止原入场计划",
                        now,
                        asof=asof,
                    )
                    continue
                touched = (
                    any(
                        item.low <= Decimal(entry["upper"]) and item.high >= Decimal(entry["lower"])
                        for item in eligible
                    )
                    if whole_day
                    else Decimal(entry["lower"]) <= bar.close <= Decimal(entry["upper"])
                )
                if plan["state"] == "waiting" and touched:
                    self._transition(
                        db,
                        plan,
                        "armed",
                        "进入候选区，仍等待完整日线确认与风险复核",
                        now,
                        asof=asof,
                    )
                    plan["state"], plan["armed_at"] = "armed", asof
                if (
                    packet
                    and plan["state"] == "armed"
                    and parse_time(plan["armed_at"]) <= parse_time(asof)
                ):
                    features = packet["metrics"]
                    confirmed = (
                        features["ma20"] is not None and bar.close > Decimal(features["ma20"])
                        if entry["confirmation"] == "daily_close_above_ma20"
                        else features["two_positive_closes"]
                    )
                    in_zone = Decimal(entry["lower"]) <= bar.close <= Decimal(entry["upper"])
                    if (
                        confirmed
                        and in_zone
                        and packet["portfolio"]["increase_allowed"]
                        and signature == packet["ledger_signature"]
                        and revision == packet["preference"]["revision"]
                    ):
                        self._transition(
                            db,
                            plan,
                            "triggered",
                            "完整日线满足候选区与确认条件，当前记账风险检查通过；股数须按本次行情重新评估",
                            now,
                            asof=asof,
                        )
                db.execute("UPDATE monitor_plans SET last_asof=? WHERE id=?", (asof, plan["id"]))

    async def tick(self, *, now: datetime | None = None):
        frozen = now is not None
        now = now or datetime.now(UTC)
        async with self._lock:
            await asyncio.to_thread(self.reconcile, now=now)
            config = await asyncio.to_thread(self.config)
            if not config["enabled"]:
                return {"enabled": False}
            symbols = await asyncio.to_thread(self.symbols)
            origin = await asyncio.to_thread(self.store.get_setting, "notification_origin")
            result = {
                "checked": [],
                "gaps": [],
                "omitted": symbols[self.settings.monitor_max_symbols :],
            }
            try:
                sessions = await self.market.provider.calendar(
                    (now - timedelta(days=16)).astimezone(NEW_YORK).date(),
                    now.astimezone(NEW_YORK).date(),
                )
                cutoff = now - timedelta(minutes=self.settings.delay_minutes)
                closed = [session for session in sessions if session.closes_at <= cutoff]
                today = next(
                    (
                        session
                        for session in sessions
                        if session.day == now.astimezone(NEW_YORK).date()
                    ),
                    None,
                )
                for symbol in symbols[: self.settings.monitor_max_symbols]:
                    try:
                        # Re-read switches between symbols; disable stops subsequent work.
                        config = await asyncio.to_thread(self.config)
                        if not config["enabled"]:
                            break
                        if today and today.opens_at + timedelta(
                            minutes=1
                        ) <= cutoff < today.closes_at + timedelta(minutes=5):
                            batch = await self.market.query(symbol, timeframe="1Min", now=now)
                            if (
                                not batch.bars
                                or (frozen and batch.fetched_at > now)
                                or batch.session != "regular"
                                or batch.adjustment != "raw"
                                or batch.bars[-1].timestamp + timedelta(minutes=1) > cutoff
                                or batch.bars[-1].timestamp
                                < min(cutoff, today.closes_at) - timedelta(minutes=3)
                            ):
                                result["gaps"].append(f"{symbol} 分钟行情不够新近，未触发")
                            else:
                                await asyncio.to_thread(self._evaluate, symbol, batch, now)
                        if closed:
                            await self._daily(symbol, closed[-1], origin, config, now, frozen)
                        result["checked"].append(symbol)
                    except (MarketError, ValidationError) as exc:
                        result["gaps"].append(f"{symbol}：{exc}")
            except MarketError as exc:
                result["gaps"].append(str(exc))
            if result["gaps"] or result["omitted"]:
                with self.store.transaction() as db:
                    self.delivery.enqueue(
                        db,
                        f"coverage:{now.date()}:{now.hour}",
                        origin or "",
                        "巡检覆盖缺口：" + json.dumps(result, ensure_ascii=False),
                        now,
                    )
            await asyncio.to_thread(
                self.store.set_setting, "monitor_last_run", {"at": iso(now), **result}
            )
            result["sent"] = await self.delivery.flush(
                await asyncio.to_thread(self.config), now=now if frozen else None
            )
            return result

    async def _daily(self, symbol, session, origin, config, now, frozen):
        local = now.astimezone(ZoneInfo(self.settings.display_timezone))
        profile = await asyncio.to_thread(self.profiles.resolved, symbol, now=now)
        daily_due = (
            config["daily"] and local.strftime("%H:%M") >= profile["values"]["notification_time"]
        )
        key = (
            f"daily:{session.day}:{symbol}:{profile['revision']}"
            if daily_due
            else f"close:{session.day}:{symbol}:{profile['revision']}"
        )
        owner = await asyncio.to_thread(self.delivery.claim_job, key, now)
        if not owner:
            return
        try:
            attempts = self.store.rows("SELECT attempts FROM monitor_jobs WHERE job_key=?", (key,))[
                0
            ]["attempts"]
            report = await self.analysis.analyze(
                symbol,
                origin=origin or "",
                request_key=f"{key}:attempt:{attempts}",
                now=now if frozen else None,
                program_only=not (daily_due and config["daily_ai"]),
            )
            packet = report["input"]
            if (
                not packet["market_fresh"]
                or packet["metrics"]["trade_date"] != session.day.isoformat()
                or report["status"] == "superseded"
            ):
                raise ValidationError("收盘数据未就绪或报告已失效，稍后重试")
            await asyncio.to_thread(
                self._evaluate,
                symbol,
                MarketBatch.from_dict(packet["market_snapshots"]["raw"]),
                now,
                packet,
            )
            if daily_due and origin:
                with self.store.transaction() as db:
                    current = self.config()
                    if current["enabled"] and current["daily"]:
                        self.delivery.enqueue(
                            db,
                            key,
                            origin,
                            f"{session.day} 收盘日报 / 分析 #{report['id']}\n"
                            + self.analysis.render(report),
                            now,
                            guard={
                                "profile_revision": report["profile_revision"],
                                "ledger_signature": packet["ledger_signature"],
                            },
                        )
            await asyncio.to_thread(
                self.delivery.finish_job, key, owner, now, {"analysis_id": report["id"]}
            )
        except (MarketError, ValidationError) as exc:
            await asyncio.to_thread(self.delivery.finish_job, key, owner, now, error=str(exc))
            raise

    async def run(self):
        await asyncio.sleep(1)
        while True:
            try:
                async with asyncio.timeout(900):
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.to_thread(
                    self.store.set_setting,
                    "monitor_last_error",
                    {
                        "at": iso(datetime.now(UTC)),
                        "error": "巡检超时或异常，等待下轮恢复；原始异常已隐藏",
                    },
                )
            await asyncio.sleep(self.settings.monitor_poll_seconds)

    def status(self):
        return {
            "config": self.config(),
            "last_run": self.store.get_setting("monitor_last_run"),
            "last_error": self.store.get_setting("monitor_last_error"),
            "plans": [
                dict(row)
                for row in self.store.rows(
                    "SELECT id,instrument,state,reason,expires_at FROM monitor_plans ORDER BY id DESC LIMIT 20"
                )
            ],
            "jobs": [
                dict(row)
                for row in self.store.rows(
                    "SELECT job_key,status,attempts,error FROM monitor_jobs ORDER BY rowid DESC LIMIT 10"
                )
            ],
            "notifications": [
                dict(row)
                for row in self.store.rows(
                    "SELECT id,status,next_part,attempts,last_error FROM notification_outbox ORDER BY id DESC LIMIT 10"
                )
            ],
        }
