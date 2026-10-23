"""Runtime configuration; credentials are never included in repr or diagnostics."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .access import allowed_origins
from .domain import ValidationError

CONFIG_GROUPS = ("access", "market_data", "notifications", "ai", "advanced")


def configuration_values(config):
    """Read grouped settings while retaining the pre-0.7 flat format for callers."""
    result = dict(config)
    for group in CONFIG_GROUPS:
        values = config.get(group, {})
        if not isinstance(values, dict):
            raise ValidationError(f"配置分组 {group} 应为对象。")
        result.update(values)
    return result


def migrate_config(config):
    """Host keeps hidden legacy fields until this one-time, lossless migration runs."""
    if config.get("config_layout_version", 0) >= 1:
        return False
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
    for group in CONFIG_GROUPS:
        destination = config.setdefault(group, {})
        if not isinstance(destination, dict):
            raise ValidationError(f"配置分组 {group} 应为对象。")
        for key, node in schema[group]["items"].items():
            if key in config:
                if destination.get(key, node.get("default")) == node.get("default"):
                    destination[key] = copy.deepcopy(config[key])
                if key in schema:
                    config[key] = copy.deepcopy(schema[key]["default"])
                else:
                    config.pop(key)
    config["config_layout_version"] = 1
    return True


@dataclass(frozen=True)
class Settings:
    key_id: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    allowed_origins: tuple[str, ...] = field(default=(), repr=False)
    account_mode: str = "paper"
    timeout_seconds: int = 15
    delay_minutes: int = 16
    display_timezone: str = "Asia/Shanghai"
    sec_user_agent: str = field(default="", repr=False)
    ir_allowed_hosts: tuple[str, ...] = ("investors.micron.com",)
    model_provider_id: str = ""
    model_timeout_seconds: int = 90
    investment_defaults: dict = field(default_factory=dict)
    exclusive_chat: bool = True
    notification_preferences: dict = field(default_factory=dict)
    monitor_poll_seconds: int = 300
    monitor_max_symbols: int = 5
    model_daily_calls: int = 24
    model_daily_token_budget: int = 200000
    model_max_input_chars: int = 24000
    model_max_output_tokens: int = 2048
    deep_concurrency: int = 2
    deep_timeout_seconds: int = 300

    def __post_init__(self):
        object.__setattr__(self, "allowed_origins", allowed_origins(self.allowed_origins))
        if self.account_mode not in {"paper", "live"}:
            raise ValidationError("Alpaca 账户模式应为 paper 或 live。")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValidationError("请求超时应为 1～60 秒。")
        if not 16 <= self.delay_minutes <= 1440:
            raise ValidationError("免费 SIP 查询至少预留 16 分钟延迟缓冲。")
        if not 5 <= self.model_timeout_seconds <= 300:
            raise ValidationError("模型超时应为 5～300 秒。")
        if not 60 <= self.monitor_poll_seconds <= 3600 or not 1 <= self.monitor_max_symbols <= 20:
            raise ValidationError("巡检间隔应为 60～3600 秒；每轮标的上限为 1～20。")
        if (
            not 0 <= self.model_daily_calls <= 1000
            or not 0 <= self.model_daily_token_budget <= 10000000
        ):
            raise ValidationError(
                "模型每日调用上限为 0～1000，预算为 0～10000000；0 表示禁用模型调用。"
            )
        if (
            not 8000 <= self.model_max_input_chars <= 100000
            or not 512 <= self.model_max_output_tokens <= 8192
        ):
            raise ValidationError("模型单次输入字符上限 8000～100000，输出 token 上限 512～8192。")
        if not 1 <= self.deep_concurrency <= 3 or not 30 <= self.deep_timeout_seconds <= 900:
            raise ValidationError("深度并发应为 1～3，总超时为 30～900 秒。")
        if not isinstance(self.investment_defaults, dict):
            raise ValidationError("个人默认配置应为一个对象。")
        if type(self.exclusive_chat) is not bool:
            raise ValidationError("独占聊天使用开关选项。")
        notification = self.notification_preferences
        if notification.get("notification_origin"):
            allowed_origins([notification["notification_origin"]])
        for key in ("enabled", "daily", "daily_ai", "auto_adopt"):
            if key in notification and type(notification[key]) is not bool:
                raise ValidationError("监控和日报开关必须为布尔值。")
        from datetime import time

        for key in ("quiet_start", "quiet_end"):
            if key in notification:
                value = notification[key]
                try:
                    parsed = time.fromisoformat(value)
                    if len(value) != 5 or parsed.tzinfo:
                        raise ValueError
                except (ValueError, TypeError):
                    raise ValidationError("免打扰时间格式为 HH:MM。") from None
        if not isinstance(self.ir_allowed_hosts, (tuple, list)) or any(
            not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", host)
            for host in self.ir_allowed_hosts
        ):
            raise ValidationError("IR 允许列表只填写域名，不填写 URL、IP 或本地地址。")
        try:
            ZoneInfo(self.display_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError("展示时区必须是有效的 IANA 时区，例如 Asia/Shanghai。") from exc

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.secret_key)

    @classmethod
    def from_mapping(cls, config: dict | None = None) -> Settings:
        config = configuration_values(config or {})
        if not isinstance(config.get("investment_defaults", {}), dict):
            raise ValidationError("个人默认配置应为一个对象。")
        try:
            return cls(
                key_id=str(
                    config.get("alpaca_key_id") or os.environ.get("APCA_API_KEY_ID", "")
                ).strip(),
                secret_key=str(
                    config.get("alpaca_secret_key") or os.environ.get("APCA_API_SECRET_KEY", "")
                ).strip(),
                allowed_origins=allowed_origins(config.get("allowed_origins", [])),
                account_mode=str(
                    config.get("alpaca_account_mode")
                    or os.environ.get("STOCKWATCHER_ACCOUNT_MODE", "paper")
                ),
                timeout_seconds=int(config.get("request_timeout_seconds", 15)),
                delay_minutes=int(config.get("delay_minutes", 16)),
                display_timezone=str(config.get("display_timezone", "Asia/Shanghai")),
                sec_user_agent=str(
                    config.get("sec_user_agent")
                    or os.environ.get("STOCKWATCHER_SEC_USER_AGENT", "")
                ).strip(),
                ir_allowed_hosts=tuple(config.get("ir_allowed_hosts", ["investors.micron.com"])),
                model_provider_id=str(config.get("model_provider_id") or "").strip(),
                model_timeout_seconds=int(config.get("model_timeout_seconds", 90)),
                monitor_poll_seconds=int(config.get("monitor_poll_seconds", 300)),
                monitor_max_symbols=int(config.get("monitor_max_symbols", 5)),
                model_daily_calls=int(config.get("model_daily_calls", 24)),
                model_daily_token_budget=int(config.get("model_daily_token_budget", 200000)),
                model_max_input_chars=int(config.get("model_max_input_chars", 24000)),
                model_max_output_tokens=int(config.get("model_max_output_tokens", 2048)),
                deep_concurrency=int(config.get("deep_concurrency", 2)),
                deep_timeout_seconds=int(config.get("deep_timeout_seconds", 300)),
                investment_defaults={
                    key: value
                    for key, value in config.get("investment_defaults", {}).items()
                    if value != ""
                },
                exclusive_chat=config.get("exclusive_chat", True),
                notification_preferences={
                    key: config[key]
                    for key in (
                        "notification_origin",
                        "enabled",
                        "daily",
                        "daily_ai",
                        "quiet_start",
                        "quiet_end",
                        "auto_adopt",
                    )
                    if key in config
                },
            )
        except (ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("插件数值配置格式错误，请检查超时和延迟设置。") from exc
