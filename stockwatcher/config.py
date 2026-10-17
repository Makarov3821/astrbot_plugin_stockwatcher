"""Runtime configuration; credentials are never included in repr or diagnostics."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .domain import ValidationError


@dataclass(frozen=True)
class Settings:
    key_id: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    account_mode: str = "paper"
    timeout_seconds: int = 15
    delay_minutes: int = 16
    display_timezone: str = "Asia/Shanghai"

    def __post_init__(self):
        if self.account_mode not in {"paper", "live"}:
            raise ValidationError("Alpaca 账户模式应为 paper 或 live。")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValidationError("请求超时应为 1～60 秒。")
        if not 16 <= self.delay_minutes <= 1440:
            raise ValidationError("免费 SIP 查询至少预留 16 分钟延迟缓冲。")
        try:
            ZoneInfo(self.display_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError("展示时区必须是有效的 IANA 时区，例如 Asia/Shanghai。") from exc

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.secret_key)

    @classmethod
    def from_mapping(cls, config: dict | None = None) -> Settings:
        config = config or {}
        try:
            return cls(
                key_id=str(
                    config.get("alpaca_key_id") or os.environ.get("APCA_API_KEY_ID", "")
                ).strip(),
                secret_key=str(
                    config.get("alpaca_secret_key") or os.environ.get("APCA_API_SECRET_KEY", "")
                ).strip(),
                account_mode=str(
                    config.get("alpaca_account_mode")
                    or os.environ.get("STOCKWATCHER_ACCOUNT_MODE", "paper")
                ),
                timeout_seconds=int(config.get("request_timeout_seconds", 15)),
                delay_minutes=int(config.get("delay_minutes", 16)),
                display_timezone=str(config.get("display_timezone", "Asia/Shanghai")),
            )
        except (ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("插件数值配置格式错误，请检查超时和延迟设置。") from exc
