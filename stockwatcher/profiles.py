"""Versioned personal preferences, field inheritance and resumable onboarding."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from .domain import NEW_YORK, ValidationError, amount, instrument, iso, parse_time
from .storage import Store

TEMPLATE_VERSION = "trend-v1.0"
DEFAULTS = {
    "account_type": "unknown",
    "strategy": "trend_v1",
    "horizon_days": 60,
    "allow_add": None,
    "max_stock_pct": None,
    "max_total_pct": None,
    "risk_budget_pct": None,
    "drawdown_pct": None,
    "drawdown_basis": None,
    "drawdown_effect": "review",
    "target_price": None,
    "target_deadline": None,
    "target_action": "review",
    "thesis": "",
    "review_target": True,
    "hard_stop_price": None,
    "notification_time": "07:30",
    "ir_url": "",
}
LABELS = {
    "账户": "account_type",
    "策略": "strategy",
    "周期": "horizon_days",
    "允许加仓": "allow_add",
    "单股上限": "max_stock_pct",
    "总仓位上限": "max_total_pct",
    "单次风险": "risk_budget_pct",
    "回撤": "drawdown_pct",
    "基准": "drawdown_basis",
    "用途": "drawdown_effect",
    "目标": "target_price",
    "目标期限": "target_deadline",
    "到价": "target_action",
    "理由": "thesis",
    "复核目标": "review_target",
    "退出价": "hard_stop_price",
    "日报": "notification_time",
    "公司页面": "ir_url",
}
CHOICES = {
    "account_type": {
        "现金": "cash",
        "保证金": "margin",
        "未知": "unknown",
        "cash": "cash",
        "margin": "margin",
        "unknown": "unknown",
    },
    "strategy": {"趋势": "trend_v1", "trend_v1": "trend_v1"},
    "drawdown_basis": {
        "成本": "cost",
        "持仓高点": "holding_high",
        "近20日高点": "rolling20_high",
        "cost": "cost",
        "holding_high": "holding_high",
        "rolling20_high": "rolling20_high",
    },
    "drawdown_effect": {"复核": "review", "退出": "exit", "review": "review", "exit": "exit"},
    "target_action": {
        "提醒": "alert",
        "复核": "review",
        "减仓": "reduce",
        "退出": "exit",
        "alert": "alert",
        "review": "review",
        "reduce": "reduce",
        "exit": "exit",
    },
}
CAPS = {"max_stock_pct", "max_total_pct", "risk_budget_pct"}
INDIVIDUAL_FIELDS = set(DEFAULTS) - {"account_type", "max_total_pct", "notification_time"}
QUESTIONS = {
    "stock_target": "个股目标（可选）：目标=... 目标期限=YYYY-MM-DD 到价=提醒/复核/减仓/退出；不确定可回复 跳过。个人周期和风控继续继承。",
    "stock_thesis": "个股期待（可选）：理由=你的持有逻辑或等待入场条件；也可回复 跳过。",
    "stock_add": "个股加仓偏好尚未知：允许加仓=是/否；也可回复 跳过。允许加仓仍需满足组合风险上限。",
    "account": "账户类型：你实际使用现金账户还是保证金账户？可直接回复 现金、保证金 或 未知。这不是 Alpaca 数据凭证类型，也不接入券商交易。",
    "strategy": "默认使用日线趋势观察，投资时间尺度为60天；不是必须持有60天。回复 采用默认，或按格式填写 策略=趋势 周期=天数。也可在插件设置的长期投资偏好中修改。",
    "risk": "设置加仓习惯和风险限制，也可在插件设置的长期投资偏好中填写。\n允许加仓：是否愿意增加已有仓位。\n单股上限、总仓位上限：股票市值最多占账户总资产的百分比。\n单次风险：单次新增仓位从入场价跌到失效价时，计划损失占总资产的百分比，不是投入资金比例。\n按格式填写 允许加仓=是/否 单股上限=数字 总仓位上限=数字 单次风险=数字；百分数不带%号，未确定可回复 跳过，保留未知。",
    "drawdown": "可选：回撤=... 基准=成本/持仓高点/近20日高点 用途=复核/退出。也可回复 跳过，不设自动退出阈值。",
    "outlook": "大盘预期：偏多/中性/偏空 期限=YYYY-MM-DD 基准=SPY 理由=...；也可回复 暂不判断。首版仅作为背景，不直接提高仓位。",
    "notification": "日报时间偏好：日报=07:30，或 采用默认。此处只保存偏好；另行开启监控和日报后才自动发送。",
}


def _validate_patch(patch: dict, *, individual: bool = False) -> dict:
    result = {}
    for key, value in patch.items():
        if key not in DEFAULTS or (individual and key not in INDIVIDUAL_FIELDS):
            raise ValidationError(f"不支持的设置字段：{key}。")
        if value == "继承":
            if not individual:
                raise ValidationError("个人默认设置不能继承个股设置。")
            result[key] = "__inherit__"
            continue
        if value in (None, "未设置", "清除", "unset"):
            if key in {
                "allow_add",
                "max_stock_pct",
                "max_total_pct",
                "risk_budget_pct",
                "drawdown_pct",
                "drawdown_basis",
                "target_price",
                "target_deadline",
                "hard_stop_price",
            }:
                result[key] = None
                continue
            raise ValidationError(f"{key} 不能留空。")
        if key in {"allow_add", "review_target"}:
            mapping = {
                "是": True,
                "否": False,
                "允许": True,
                "禁止": False,
                "true": True,
                "false": False,
            }
            if isinstance(value, bool):
                result[key] = value
            elif str(value).lower() in mapping:
                result[key] = mapping[str(value).lower()]
            else:
                raise ValidationError("布尔设置使用 是/否。")
        elif key in CAPS | {"drawdown_pct"}:
            numeric = amount(str(value))
            if numeric > 100:
                raise ValidationError("百分比应为 0～100；数值 0 是实际限制，不等于未设置。")
            result[key] = str(numeric)
        elif key in {"target_price", "hard_stop_price"}:
            result[key] = str(amount(str(value), positive=True))
        elif key == "horizon_days":
            if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 3650:
                raise ValidationError("周期应为 1～3650 天。")
            result[key] = int(value)
        elif key in CHOICES:
            if str(value) not in CHOICES[key]:
                raise ValidationError(f"{key} 的选项不受支持；暂不执行自定义策略代码。")
            result[key] = CHOICES[key][str(value)]
        elif key == "target_deadline":
            try:
                parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValidationError("目标期限格式为 YYYY-MM-DD。") from exc
            result[key] = parsed.isoformat()
        elif key == "notification_time":
            try:
                parsed = time.fromisoformat(str(value))
            except ValueError as exc:
                raise ValidationError("日报时间格式为 HH:MM。") from exc
            if parsed.tzinfo is not None or len(str(value)) != 5:
                raise ValidationError("日报时间格式为 HH:MM，按展示时区解释。")
            result[key] = parsed.strftime("%H:%M")
        else:
            if not isinstance(value, str) or len(value) > 2000:
                raise ValidationError("理由或页面地址最多 2000 字。")
            result[key] = value
    return result


def named_patch(options: dict) -> dict:
    unknown = set(options) - set(LABELS)
    if unknown:
        raise ValidationError(f"不支持的设置：{', '.join(sorted(unknown))}。")
    return {LABELS[key]: value for key, value in options.items()}


class Profiles:
    def __init__(
        self, store: Store, configured_defaults: dict | None = None, *, timezone="Asia/Shanghai"
    ):
        self.store = store
        self.timezone = timezone
        configured = _validate_patch(configured_defaults or {})
        with store.transaction() as db:
            current = self._load(db)
            previous = current.get("config_seen", {})
            changes = {
                key: value
                for key, value in configured.items()
                if previous.get(key, object()) != value
            }
            removed = set(previous) - set(configured)
            if current["revision"] == 0 or changes or removed:
                current["defaults"].update(changes)
                for key in removed:
                    current["defaults"].pop(key, None)
                current["config_seen"] = configured
                self._save(db, current, "同步插件配置默认值")

    @staticmethod
    def _load(db) -> dict:
        row = db.execute("SELECT value FROM settings WHERE key='personal_profile'").fetchone()
        return (
            json.loads(row[0])
            if row
            else {
                "revision": 0,
                "defaults": {},
                "overrides": {},
                "outlooks": {},
                "config_seen": {},
                "onboarding_complete": False,
            }
        )

    def current(self) -> dict:
        return self.store.get_setting("personal_profile")

    @staticmethod
    def _check(profile: dict) -> None:
        defaults = {**DEFAULTS, **profile["defaults"]}
        candidates = [defaults]
        for overrides in profile["overrides"].values():
            for key in CAPS:
                if (
                    overrides.get(key) is not None
                    and defaults.get(key) is not None
                    and Decimal(overrides[key]) > Decimal(defaults[key])
                ):
                    raise ValidationError("个股上限不能突破个人总约束，请先明确修改个人上限。")
            candidates.append({**defaults, **overrides})
        for values in candidates:
            if (
                values["target_price"]
                and values["hard_stop_price"]
                and Decimal(values["hard_stop_price"]) >= Decimal(values["target_price"])
            ):
                raise ValidationError("退出价必须低于目标价，请核对设置含义。")
            if values["drawdown_pct"] is not None and values["drawdown_basis"] is None:
                raise ValidationError("回撤阈值需要明确基准：成本、持仓高点或近20日高点。")

    def _save(self, db, profile: dict, reason: str) -> int:
        self._check(profile)
        profile["revision"] += 1
        payload = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        db.execute(
            "INSERT INTO profile_versions VALUES (?,?,?,?)",
            (profile["revision"], iso(datetime.now(UTC)), reason, payload),
        )
        db.execute(
            "INSERT INTO settings VALUES ('personal_profile',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (payload,),
        )
        return profile["revision"]

    def update(
        self, patch: dict, *, symbol: str | None = None, expected_revision: int | None = None
    ) -> int:
        patch = _validate_patch(patch, individual=bool(symbol))
        symbol = instrument(symbol) if symbol else None
        with self.store.transaction() as db:
            profile = self._load(db)
            if expected_revision is not None and profile["revision"] != expected_revision:
                raise ValidationError("设置已变化，请查看新设置后重新提交，旧草稿未覆盖。")
            destination = (
                profile["overrides"].setdefault(symbol, {}) if symbol else profile["defaults"]
            )
            for key, value in patch.items():
                if value == "__inherit__":
                    destination.pop(key, None)
                else:
                    destination[key] = value
            return self._save(db, profile, "修改个股设置" if symbol else "修改个人设置")

    def resolved(self, symbol: str, *, now: datetime | None = None) -> dict:
        profile = self.current()
        symbol = instrument(symbol)
        overrides = profile["overrides"].get(symbol, {})
        values = {**DEFAULTS, **profile["defaults"], **overrides}
        sources = {
            key: "个股" if key in overrides else "个人" if key in profile["defaults"] else "模板"
            for key in values
        }
        for key in CAPS:
            parent = profile["defaults"].get(key)
            if parent is not None and (
                values[key] is None or Decimal(values[key]) > Decimal(parent)
            ):
                values[key], sources[key] = parent, "个人约束"
        now = now or datetime.now(UTC)
        outlook = profile["outlooks"].get("US")
        expired = bool(outlook and parse_time(outlook["expires_at"]) < now)
        return {
            "revision": profile["revision"],
            "template_version": TEMPLATE_VERSION,
            "instrument": symbol,
            "values": values,
            "sources": sources,
            "outlook": None if expired else outlook,
            "expired_outlook": outlook if expired else None,
            "onboarding_complete": profile["onboarding_complete"],
        }

    @staticmethod
    def outlook(direction: str, deadline: str, benchmark: str, reason: str, now: datetime) -> dict:
        if direction not in {"偏多", "中性", "偏空", "暂不判断"}:
            raise ValidationError("大盘预期选择 偏多/中性/偏空/暂不判断。")
        try:
            day = datetime.strptime(deadline, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValidationError("大盘预期期限格式为 YYYY-MM-DD。") from exc
        expires = datetime.combine(day, time(23, 59, 59), NEW_YORK)
        if expires < now or len(reason) > 2000:
            raise ValidationError("预期期限不能已过期，理由最多 2000 字。")
        return {
            "direction": direction,
            "expires_at": iso(expires),
            "benchmark": instrument(benchmark),
            "reason": reason,
            "mode": "background",
            "created_at": iso(now),
        }

    def set_outlook(
        self,
        direction: str,
        deadline: str,
        benchmark: str = "SPY",
        reason: str = "",
        *,
        now: datetime | None = None,
    ) -> int:
        value = self.outlook(direction, deadline, benchmark, reason, now or datetime.now(UTC))
        with self.store.transaction() as db:
            profile = self._load(db)
            profile["outlooks"]["US"] = value
            return self._save(db, profile, "修改市场预期")

    def wizard(
        self,
        action: str,
        options: dict | None = None,
        *,
        now: datetime | None = None,
        symbol: str | None = None,
        origin: str | None = None,
        request_key: str | None = None,
    ) -> str:
        now = now or datetime.now(UTC)
        options = options or {}
        symbol = instrument(symbol) if symbol else None
        with self.store.transaction() as db:
            profile = self._load(db)
            row = db.execute("SELECT value FROM settings WHERE key='onboarding_state'").fetchone()
            state = json.loads(row[0]) if row else None
            receipt_key = self.wizard_receipt_key(request_key, origin, action, options, symbol)
            if state and receipt_key in state.get("receipts", {}):
                return state["receipts"][receipt_key]
            if state and state["state"] not in {"cancelled", "completed"} and origin:
                if action == "状态" and state.get("origin") not in {None, origin}:
                    return "引导草稿位于另一入口；可用 /盯盘 引导 继续 明确转到当前入口。"
                if state.get("origin") not in {None, origin} and action not in {
                    "开始",
                    "继续",
                    "状态",
                }:
                    raise ValidationError(
                        "引导正在另一入口进行；请用 /盯盘 引导 继续 明确转到当前入口。"
                    )
                if action != "状态":
                    state["origin"] = origin
            if (
                action == "开始"
                and symbol
                and state
                and state["state"] not in {"cancelled", "completed"}
                and state.get("scope") != symbol
            ):
                raise ValidationError("已有未完成的引导，请继续或取消当前草稿，再开始另一只股票。")
            if action == "开始" and (not state or state["state"] in {"cancelled", "completed"}):
                supplied = profile["defaults"]
                steps = []
                for step, fields in (
                    ("account", {"account_type"}),
                    ("strategy", {"strategy", "horizon_days"}),
                    ("risk", {"allow_add", "max_stock_pct", "max_total_pct", "risk_budget_pct"}),
                    ("drawdown", {"drawdown_pct", "drawdown_basis"}),
                    ("notification", {"notification_time"}),
                ):
                    if not fields <= supplied.keys():
                        steps.append(step)
                if "US" not in profile["outlooks"]:
                    steps.insert(max(0, len(steps) - 1), "outlook")
                if symbol:
                    effective = {**DEFAULTS, **supplied, **profile["overrides"].get(symbol, {})}
                    steps = []
                    if effective["target_price"] is None or effective["target_deadline"] is None:
                        steps.append("stock_target")
                    if not effective["thesis"]:
                        steps.append("stock_thesis")
                    if effective["allow_add"] is None:
                        steps.append("stock_add")
                state = {
                    "state": "collecting",
                    "base_revision": profile["revision"],
                    "draft": copy.deepcopy(profile),
                    "steps": steps,
                    "index": 0,
                    "touched_at": iso(now),
                    "scope": symbol,
                    "origin": origin,
                    "receipts": {},
                }
            elif state is None:
                raise ValidationError("尚未开始引导，请用 /盯盘 开始。")
            elif action == "取消":
                state["state"] = "cancelled"
            elif action == "暂停":
                state["state"] = "paused"
            elif action in {"开始", "继续", "状态"}:
                if state["state"] in {"cancelled", "completed"}:
                    return "引导已结束；修改偏好可用 /盯盘 设置。"
                if action != "状态":
                    state["state"] = "collecting"
            elif action == "返回":
                state["index"] = max(0, state["index"] - 1)
                state["state"] = "collecting"
            elif action == "确认":
                if state["state"] != "review" or profile["revision"] != state["base_revision"]:
                    raise ValidationError("尚未到确认步骤或设置已变化；请取消旧草稿后重新开始。")
                if not state.get("scope"):
                    state["draft"]["onboarding_complete"] = True
                self._save(db, state["draft"], "完成首次引导")
                state["state"] = "completed"
            elif action == "回答":
                if state["state"] != "collecting" or state["index"] >= len(state["steps"]):
                    raise ValidationError("引导未在收集状态，请查看 /盯盘 引导 状态。")
                if now - parse_time(state["touched_at"]) > timedelta(hours=24):
                    state["state"] = "paused"
                    return self._save_wizard_reply(
                        db, state, "引导已暂停，草稿仍保留。使用 /盯盘 引导 继续。", receipt_key
                    )
                step = state["steps"][state["index"]]
                control = options.get("control")
                if step == "outlook":
                    if control in {"跳过", "暂不判断", "采用默认"}:
                        state["draft"]["outlooks"].pop("US", None)
                    else:
                        if not {"direction", "期限"} <= options.keys() or not set(options) <= {
                            "direction",
                            "期限",
                            "基准",
                            "理由",
                        }:
                            raise ValidationError(
                                "大盘预期需填写方向和期限，请按当前步骤提示回答。"
                            )
                        state["draft"]["outlooks"]["US"] = self.outlook(
                            options["direction"],
                            options["期限"],
                            options.get("基准", "SPY"),
                            options.get("理由", ""),
                            now,
                        )
                elif control == "跳过":
                    pass
                elif control == "采用默认" and step in {"strategy", "notification"}:
                    keys = (
                        {"strategy", "horizon_days"}
                        if step == "strategy"
                        else {"notification_time"}
                    )
                    state["draft"]["defaults"].update({key: DEFAULTS[key] for key in keys})
                else:
                    allowed = {
                        "stock_target": {"目标", "目标期限", "到价"},
                        "stock_thesis": {"理由"},
                        "stock_add": {"允许加仓"},
                        "account": {"账户"},
                        "strategy": {"策略", "周期"},
                        "risk": {"允许加仓", "单股上限", "总仓位上限", "单次风险"},
                        "drawdown": {"回撤", "基准", "用途"},
                        "notification": {"日报"},
                    }[step]
                    if not options or not set(options) <= allowed:
                        raise ValidationError(
                            "回答与当前步骤不匹配，请按提示填写；可跳过未确定的内容。"
                        )
                    scope = state.get("scope")
                    patch = _validate_patch(named_patch(options), individual=bool(scope))
                    destination = (
                        state["draft"]["overrides"].setdefault(scope, {})
                        if scope
                        else state["draft"]["defaults"]
                    )
                    for key, value in patch.items():
                        if value == "__inherit__":
                            destination.pop(key, None)
                        else:
                            destination[key] = value
                    self._check(state["draft"])
                state["index"] += 1
            else:
                raise ValidationError("引导操作为 开始/继续/暂停/返回/取消/状态/确认。")
            state["touched_at"] = iso(now)
            if state["state"] == "collecting" and state["index"] == len(state["steps"]):
                state["state"] = "review"
            return self._save_wizard_reply(db, state, self._wizard_prompt(state), receipt_key)

    @staticmethod
    def wizard_receipt_key(request_key, origin, action, options, symbol=None):
        if not request_key:
            return None
        return hashlib.sha256(
            json.dumps(
                [request_key, origin, action, options, symbol], ensure_ascii=False, sort_keys=True
            ).encode()
        ).hexdigest()

    def _save_wizard_reply(self, db, state, reply, receipt_key):
        if receipt_key:
            receipts = state.setdefault("receipts", {})
            receipts[receipt_key] = reply
            while len(receipts) > 32:
                receipts.pop(next(iter(receipts)))
        self._write_wizard(db, state)
        return reply

    def _wizard_prompt(self, state):
        if state["state"] == "review":
            from .presentation import outlook_text, preferences_text, profile_text

            draft = state["draft"]
            summary = (
                f"{state['scope']} 待保存的个股期待：\n"
                + preferences_text(draft["overrides"].get(state["scope"], {}), stock=True)
                if state.get("scope")
                else profile_text(draft)
            )
            return (
                "待确认，以下内容尚未生效：\n"
                + summary
                + "\n\n"
                + outlook_text(draft, self.timezone)
                + "\n未填写的风险限制仍为未知，不据此计算加仓数量。"
                + "\n直接回复 确认 保存，返回 修改上一步，或 取消 放弃草稿；不会自动开启监控或日报。"
            )
        if state["state"] == "collecting":
            return (
                f"步骤 {state['index'] + 1}/{len(state['steps'])}："
                + QUESTIONS[state["steps"][state["index"]]]
                + "\n当前入口可直接回复 跳过 或按本步格式填写；暂停可回复 暂停，之后用 /盯盘 引导 继续 恢复。"
            )
        return {
            "paused": "已暂停，草稿已保存；普通聊天不再接管。用 /盯盘 引导 继续 恢复。",
            "cancelled": "已取消，生效设置未改变。",
            "completed": "设置已生效。添加股票后可用 /盯盘 设置 MU 单独定制。",
        }[state["state"]]

    @staticmethod
    def _write_wizard(db, state: dict) -> None:
        db.execute(
            "INSERT INTO settings VALUES ('onboarding_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(state, ensure_ascii=False),),
        )
