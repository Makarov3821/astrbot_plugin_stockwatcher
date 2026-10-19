"""The model belongs to AstrBot. No direct vendor API or separate secret is used."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class ModelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelReply:
    text: str
    metadata: dict


class AstrBotModel:
    def __init__(
        self, context, *, provider_id: str = "", timeout_seconds: int = 90, concurrency: int = 1
    ):
        self.context, self.provider_id, self.timeout_seconds = context, provider_id, timeout_seconds
        self._limit = asyncio.Semaphore(concurrency)

    async def generate(
        self, system: str, prompt: str, origin: str, *, max_output_tokens: int | None = None
    ) -> ModelReply:
        async with self._limit:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    provider_id = (
                        self.provider_id or await self.context.get_current_chat_provider_id(origin)
                    )
                    if not provider_id:
                        raise ModelUnavailable("当前会话未配置 AstrBot 模型。")
                    response = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system,
                        contexts=[],
                        tools=None,
                        **(
                            {"max_tokens": max_output_tokens}
                            if max_output_tokens is not None
                            else {}
                        ),
                    )
                    if getattr(response, "role", "assistant") not in {"assistant", "model"}:
                        raise ModelUnavailable("模型没有返回正常的助手结果。")
                    text = response.completion_text
                    if not isinstance(text, str) or not text.strip() or len(text) > 40000:
                        raise ModelUnavailable("模型返回空结果或超过长度限制。")
                    usage = getattr(response, "usage", None)
                    safe_usage = {}
                    for key in (
                        "input_other",
                        "input_audio",
                        "input_cached",
                        "output",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                    ):
                        value = getattr(usage, key, None)
                        if isinstance(value, int) and value >= 0:
                            safe_usage[key] = value
                    return ModelReply(
                        text,
                        {
                            "provider_id": provider_id,
                            "usage": safe_usage,
                            "adapter": "AstrBot.llm_generate",
                        },
                    )
            except asyncio.CancelledError:
                raise
            except ModelUnavailable:
                raise
            except Exception as exc:
                raise ModelUnavailable(
                    "AstrBot 模型不可用或请求超时；本次保留程序观察，未回显提供商异常。"
                ) from exc
