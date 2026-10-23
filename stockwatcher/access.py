"""Exact private-session allowlist; an empty list never authorizes business access."""

from __future__ import annotations

from .domain import ValidationError


def private_origin(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("消息入口白名单必须填写完整私聊 UMO 字符串。")
    value = value.strip()
    parts = value.split(":", 2)
    if (
        len(value) > 1024
        or len(parts) != 3
        or not parts[0]
        or not parts[2]
        or parts[1] != "FriendMessage"
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or "*" in value
        or "?" in value
    ):
        raise ValidationError(
            "消息入口白名单只接受完整私聊 UMO：平台实例ID:FriendMessage:会话ID；不支持群聊、空项或通配符。"
        )
    return value


def allowed_origins(values) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValidationError("消息入口白名单应为列表，可添加多个私聊入口。")
    return tuple(dict.fromkeys(private_origin(value) for value in values))


def event_private_origin(event) -> str | None:
    """Use trusted event metadata, never sender labels or message-body identifiers."""
    try:
        message_type = event.get_message_type()
        if getattr(message_type, "value", None) != "FriendMessage" or event.get_group_id():
            return None
        return private_origin(event.unified_msg_origin)
    except Exception:
        return None


def is_identity_command(text: str) -> bool:
    words = text.split()
    return (
        len(words) == 2 and words[0].lstrip("/!#") in {"盯盘", "stockwatch"} and words[1] == "入口"
    )
