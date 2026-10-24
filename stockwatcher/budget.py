"""Shared persisted call budget. Token reservation is an estimate, not a bill cap."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .domain import iso
from .model import ModelReply, ModelUnavailable


def compact_packet(packet: dict, max_chars: int) -> dict:
    packet = copy.deepcopy(packet)
    # Old evidence IDs are audit history, not evidence supplied for this research call.
    if isinstance(packet.get("previous"), dict):
        packet["previous"].pop("evidence_versions", None)
    items = packet["evidence"]["items"]
    for item in items:
        if len(item["content"]) > 1600:
            item["content"] = item["content"][:1600]
            item["prompt_truncated"] = True
    omitted = packet["evidence"].get("prompt_omitted_count", 0)
    while len(json.dumps(packet, ensure_ascii=False)) > max_chars and items:
        items.pop()
        omitted += 1
    packet["evidence"]["prompt_omitted_count"] = omitted
    if len(json.dumps(packet, ensure_ascii=False)) > max_chars:
        raise ModelUnavailable("账户或偏好上下文超过单次模型输入预算，请减少冗长备注。")
    return packet


class BudgetedModel:
    def __init__(self, model, store, settings, *, clock=None, origin_allowed=None):
        self.model, self.store, self.settings = model, store, settings
        self.origin_allowed = origin_allowed
        self.clock = clock or (lambda: datetime.now(UTC))

    def _reserve(self, system, prompt, purpose, limit):
        now = self.clock()
        day = now.astimezone(ZoneInfo(self.settings.display_timezone)).date().isoformat()
        if len(prompt) > self.settings.model_max_input_chars:
            raise ModelUnavailable("模型输入超过单次字符预算。")
        # UTF-8 bytes are a conservative planning estimate; provider tokenizers may differ.
        reservation = len((system + prompt).encode("utf-8")) + limit
        with self.store.transaction() as db:
            used = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(reserved_tokens),0) FROM model_calls WHERE budget_day=?",
                (day,),
            ).fetchone()
            if (
                used[0] >= self.settings.model_daily_calls
                or used[1] + reservation > self.settings.model_daily_token_budget
            ):
                raise ModelUnavailable(
                    "已达每日模型调用/估算 token 预算，保留程序观察；可查看 /盯盘 用量。"
                )
            cursor = db.execute(
                "INSERT INTO model_calls(created_at,budget_day,purpose,status,reserved_tokens,input_chars,output_limit) VALUES (?,?,?,'reserved',?,?,?)",
                (iso(now), day, purpose, reservation, len(prompt), limit),
            )
            return cursor.lastrowid

    def ensure_calls(self, count):
        stats = self.stats()
        if stats["calls"] + count > stats["call_limit"]:
            raise ModelUnavailable(
                "深度研究需要预留 6 次调用额度；当前剩余额度不足，未启动角色调用。"
            )

    def ensure_plan(self, requests):
        """Check a whole bounded plan without charging it; each call still reserves atomically."""
        self.ensure_calls(len(requests))
        stats = self.stats()
        required = sum(
            len((system + prompt).encode("utf-8"))
            + min(
                output_limit or self.settings.model_max_output_tokens,
                self.settings.model_max_output_tokens,
            )
            for system, prompt, output_limit in requests
        )
        remaining = max(0, stats["token_planning_limit"] - stats["reserved_token_estimate"])
        plan = {
            "planned_calls": len(requests),
            "estimated_tokens": required,
            "remaining_tokens": remaining,
            "status": "ready" if required <= remaining else "insufficient",
        }
        if required > remaining:
            failure = ModelUnavailable(
                f"深度研究整轮预计需预留约 {required:,} 个预算单位，今日剩余 {remaining:,}，"
                "预算不足，本次未启动角色调用。可查看 /盯盘 用量；"
                "在插件设置「05 · AI 理解与研判」调整每日估算 token 预留预算并重载，或等待次日。"
            )
            failure.budget_plan = plan
            raise failure
        return plan

    async def generate(self, system, prompt, origin, *, purpose="single", max_output_tokens=None):
        if self.origin_allowed is not None and not self.origin_allowed(origin):
            raise ModelUnavailable("消息入口授权已撤销，未启动模型调用。")
        limit = min(
            max_output_tokens or self.settings.model_max_output_tokens,
            self.settings.model_max_output_tokens,
        )
        reservation = asyncio.create_task(
            asyncio.to_thread(self._reserve, system, prompt, purpose, limit)
        )
        try:
            call_id = await asyncio.shield(reservation)
        except asyncio.CancelledError:
            completed = (await asyncio.gather(reservation, return_exceptions=True))[0]
            if isinstance(completed, int):
                await asyncio.to_thread(self._finish, completed, "cancelled", None)
            raise
        try:
            if self.origin_allowed is not None and not self.origin_allowed(origin):
                raise ModelUnavailable("消息入口授权已撤销，未启动模型调用。")
            reply = await self.model.generate(system, prompt, origin, max_output_tokens=limit)
            await asyncio.to_thread(self._finish, call_id, "returned", reply)
            return ModelReply(
                reply.text, {**reply.metadata, "budget_call_id": call_id, "output_limit": limit}
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(self._finish, call_id, "cancelled", None)
            raise
        except Exception as exc:
            await asyncio.to_thread(self._finish, call_id, "failed", None)
            if isinstance(exc, ModelUnavailable):
                raise
            raise ModelUnavailable("模型请求失败，预算保留且原始异常已隐藏。") from exc

    def _finish(self, call_id, status, reply):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE model_calls SET status=?,usage=?,metadata=? WHERE id=?",
                (
                    status,
                    json.dumps(reply.metadata.get("usage", {}) if reply else {}),
                    json.dumps({"provider_id": reply.metadata.get("provider_id")} if reply else {}),
                    call_id,
                ),
            )

    def stats(self):
        day = self.clock().astimezone(ZoneInfo(self.settings.display_timezone)).date().isoformat()
        rows = self.store.rows(
            "SELECT purpose,status,reserved_tokens,usage FROM model_calls WHERE budget_day=? ORDER BY id",
            (day,),
        )
        return {
            "day": day,
            "timezone": self.settings.display_timezone,
            "calls": len(rows),
            "call_limit": self.settings.model_daily_calls,
            "reserved_token_estimate": sum(row["reserved_tokens"] for row in rows),
            "token_planning_limit": self.settings.model_daily_token_budget,
            "detail": [dict(row) for row in rows],
            "meaning": "输入按 UTF-8 字节数保守估算，加输出额度预留；失败/取消也占用。不是提供商账单或严格费用保证。",
        }
