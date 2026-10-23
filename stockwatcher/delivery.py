"""Durable leases and notification outbox; delivery is at least once after a crash."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .domain import ValidationError, iso, parse_time
from .storage import Store


def message_parts(text: str, limit: int = 1500) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while line:
            size = min(len(line), limit - len(current))
            current += line[:size]
            line = line[size:]
            if len(current) == limit:
                chunks.append(current)
                current = ""
    if current:
        chunks.append(current)
    return chunks or [""]


def in_quiet_hours(now: datetime, timezone: str, start: str, end: str) -> bool:
    local = now.astimezone(ZoneInfo(timezone)).strftime("%H:%M")
    if start == end:
        return False
    return start <= local < end if start < end else local >= start or local < end


class Delivery:
    def __init__(self, store: Store, sender, *, timezone="Asia/Shanghai", origin_allowed=None):
        self.store, self.sender, self.timezone = store, sender, timezone
        self.origin_allowed = origin_allowed or (lambda origin: False)
        self._lock = asyncio.Lock()

    def enqueue(
        self, db, key: str, origin: str, text: str, now: datetime, *, ttl_hours=24, guard=None
    ):
        db.execute(
            "INSERT OR IGNORE INTO notification_outbox(event_key,origin,payload,next_at,created_at,expires_at,guard_json) VALUES (?,?,?,?,?,?,?)",
            (
                key,
                origin,
                json.dumps(message_parts(text), ensure_ascii=False),
                iso(now),
                iso(now),
                iso(now + timedelta(hours=ttl_hours)),
                json.dumps(guard) if guard else None,
            ),
        )

    def _context_ok(self, row):
        if not row["guard_json"]:
            return True
        guard = json.loads(row["guard_json"])
        profile = self.store.get_setting("personal_profile")
        events = self.store.rows("SELECT * FROM ledger_events ORDER BY event_date,id")
        signature = hashlib.sha256(
            json.dumps([dict(item) for item in events], sort_keys=True).encode()
        ).hexdigest()
        return (
            profile["revision"] == guard["profile_revision"]
            and signature == guard["ledger_signature"]
        )

    def claim_job(self, key: str, now: datetime) -> str | None:
        owner = uuid.uuid4().hex
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM monitor_jobs WHERE job_key=?", (key,)).fetchone()
            if row:
                if row["status"] in {"done", "failed"} or parse_time(row["next_at"]) > now:
                    return None
                if row["status"] == "running" and parse_time(row["lease_until"]) > now:
                    return None
                attempts = row["attempts"] + 1
            else:
                attempts = 1
            db.execute(
                "INSERT INTO monitor_jobs(job_key,status,lease_until,attempts,next_at,owner) VALUES (?,'running',?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET status='running',lease_until=excluded.lease_until,attempts=excluded.attempts,owner=excluded.owner",
                (key, iso(now + timedelta(minutes=30)), attempts, iso(now), owner),
            )
        return owner

    def finish_job(
        self,
        key: str,
        owner: str,
        now: datetime,
        result: dict | None = None,
        error: str | None = None,
    ):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM monitor_jobs WHERE job_key=? AND owner=?", (key, owner)
            ).fetchone()
            if not row:
                return
            db.execute(
                "UPDATE monitor_jobs SET status=?,result=?,error=?,next_at=? WHERE job_key=? AND owner=?",
                (
                    "failed" if error and row["attempts"] >= 3 else "retry" if error else "done",
                    json.dumps(result),
                    error,
                    iso(now + timedelta(minutes=min(30, 2 ** row["attempts"]))),
                    key,
                    owner,
                ),
            )

    def retry(self, notification_id: int, now: datetime):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM notification_outbox WHERE id=?", (notification_id,)
            ).fetchone()
            if not row or row["status"] != "failed" or parse_time(row["expires_at"]) <= now:
                raise ValidationError("仅可重试未过期且发送失败的通知。")
            db.execute(
                "UPDATE notification_outbox SET status='pending',attempts=0,next_at=? WHERE id=?",
                (iso(now), notification_id),
            )

    def _claim(self, now: datetime, config: dict) -> dict | None:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM notification_outbox WHERE status IN ('pending','sending') ORDER BY id LIMIT 100"
            ).fetchall()
            for row in rows:
                if (
                    not self.origin_allowed(row["origin"])
                    or not self._context_ok(row)
                    or parse_time(row["expires_at"]) <= now
                    or row["origin"] != self.store.get_setting("notification_origin")
                    or (row["event_key"].startswith("daily:") and not config["daily"])
                ):
                    db.execute(
                        "UPDATE notification_outbox SET status='cancelled' WHERE id=?", (row["id"],)
                    )
                    continue
                if parse_time(row["next_at"]) > now or (
                    row["status"] == "sending"
                    and row["lease_until"]
                    and parse_time(row["lease_until"]) > now
                ):
                    continue
                db.execute(
                    "UPDATE notification_outbox SET status='sending',lease_until=? WHERE id=?",
                    (iso(now + timedelta(minutes=10)), row["id"]),
                )
                return dict(row)
        return None

    async def flush(self, config: dict, *, now: datetime | None = None) -> int:
        frozen = now is not None
        now = now or datetime.now(UTC)
        if not config["enabled"] or in_quiet_hours(
            now, self.timezone, config["quiet_start"], config["quiet_end"]
        ):
            return 0
        sent = 0
        async with self._lock:
            for _ in range(3):
                row = await asyncio.to_thread(self._claim, now, config)
                if row is None:
                    break
                try:
                    parts = json.loads(row["payload"])
                    for index in range(row["next_part"], len(parts)):
                        # Recheck subscription and target between chunks, including concurrent stop.
                        latest = await asyncio.to_thread(
                            self.store.get_setting, "monitor_config", config
                        )
                        target = await asyncio.to_thread(
                            self.store.get_setting, "notification_origin"
                        )
                        state = await asyncio.to_thread(
                            self.store.rows,
                            "SELECT status FROM notification_outbox WHERE id=?",
                            (row["id"],),
                        )
                        if (
                            not self.origin_allowed(row["origin"])
                            or parse_time(row["expires_at"])
                            <= (now if frozen else datetime.now(UTC))
                            or in_quiet_hours(
                                now if frozen else datetime.now(UTC),
                                self.timezone,
                                latest["quiet_start"],
                                latest["quiet_end"],
                            )
                            or not await asyncio.to_thread(self._context_ok, row)
                            or not latest["enabled"]
                            or target != row["origin"]
                            or state[0]["status"] != "sending"
                            or (row["event_key"].startswith("daily:") and not latest["daily"])
                        ):
                            break
                        async with asyncio.timeout(30):
                            found = await self.sender(
                                row["origin"],
                                f"[通知 #{row['id']} {index + 1}/{len(parts)}]\n" + parts[index],
                            )
                        if found is False:
                            raise RuntimeError("Platform unavailable")
                        await asyncio.to_thread(self._part_sent, row["id"], index + 1, len(parts))
                    else:
                        sent += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.to_thread(self._failed, row, now if frozen else datetime.now(UTC))
        return sent

    def _part_sent(self, row_id, next_part, count):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE notification_outbox SET next_part=?,status=? WHERE id=? AND status='sending'",
                (next_part, "sent" if next_part == count else "sending", row_id),
            )

    def _failed(self, row: dict, now: datetime):
        attempts = row["attempts"] + 1
        with self.store.transaction() as db:
            db.execute(
                "UPDATE notification_outbox SET attempts=?,status=?,next_at=?,lease_until=NULL,last_error=? WHERE id=? AND status='sending'",
                (
                    attempts,
                    "failed" if attempts >= 5 else "pending",
                    iso(now + timedelta(minutes=min(60, 2**attempts))),
                    "平台发送失败或超时（原始异常已隐藏）",
                    row["id"],
                ),
            )
