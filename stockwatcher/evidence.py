"""Versioned source records: publications, observations, corrections and citations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .domain import ValidationError, instrument, iso, parse_time
from .storage import Store


def observation_stamp(value: datetime) -> str:
    # Fixed precision is necessary for SQLite's lexicographic timestamp comparisons.
    return parse_time(iso(value)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValidationError("材料链接无效。")
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "https"
            and parts.hostname
            and not parts.username
            and not parts.password
            and parts.port in {None, 443}
        )
    except ValueError as exc:
        raise ValidationError("材料链接无效。") from exc
    if not valid:
        raise ValidationError("材料链接应为无账号密码的 HTTPS 地址。")
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}
    ]
    return urlunsplit(
        ("https", parts.hostname.lower(), parts.path or "/", urlencode(sorted(query)), "")
    )


@dataclass(frozen=True)
class Evidence:
    instrument: str
    url: str
    publisher: str
    provider: str
    kind: str
    title: str
    content: str
    published_at: str | None
    observed_at: str
    updated_at: str | None = None
    content_level: str = "excerpt"
    verified_source: bool = False
    status: str = "active"
    data: dict = field(default_factory=dict)


class EvidenceStore:
    def __init__(self, store: Store):
        self.store = store

    def add(self, item: Evidence) -> str:
        symbol = instrument(item.instrument)
        url = canonical_url(item.url)
        if item.kind not in {"filing", "financial_fact", "company_material", "news", "user_note"}:
            raise ValidationError("不支持的材料类型。")
        if (
            not isinstance(item.title, str)
            or not item.title
            or len(item.title) > 500
            or not isinstance(item.content, str)
            or len(item.content) > 40000
        ):
            raise ValidationError("材料标题需为 1～500 字，正文不超过 40000 字。")
        if not isinstance(item.data, dict) or type(item.verified_source) is not bool:
            raise ValidationError("材料结构化数据或核验状态无效。")
        observed = parse_time(item.observed_at)
        published = parse_time(item.published_at) if item.published_at else None
        updated = parse_time(item.updated_at) if item.updated_at else None
        if (published and published > observed) or (updated and updated > observed):
            raise ValidationError("材料公开或更新时间晚于抓取时间，拒绝保存。")
        if item.status not in {"active", "withdrawn"} or item.content_level not in {
            "metadata",
            "excerpt",
            "full",
            "user_note",
            "facts",
        }:
            raise ValidationError("材料状态或正文覆盖类型无效。")
        identity = (
            item.data.get("identity", "")
            if item.kind == "financial_fact"
            else "user_note"
            if item.kind == "user_note"
            else ""
        )
        if not isinstance(identity, str) or len(identity) > 300:
            raise ValidationError("材料身份字段无效。")
        evidence_id = "ev_" + hashlib.sha256(f"{symbol}|{url}|{identity}".encode()).hexdigest()[:20]
        payload = {
            **asdict(item),
            "instrument": symbol,
            "url": url,
            "observed_at": observation_stamp(observed),
            "published_at": iso(published) if published else None,
            "updated_at": iso(updated) if updated else None,
        }
        # Supplier differences and repeated fetch times do not create independent evidence.
        hashed = {
            key: value for key, value in payload.items() if key not in {"observed_at", "provider"}
        }
        digest = hashlib.sha256(
            json.dumps(hashed, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT version,content_hash FROM evidence_versions WHERE evidence_id=? ORDER BY version DESC LIMIT 1",
                (evidence_id,),
            ).fetchone()
            if previous and previous["content_hash"] == digest:
                return evidence_id
            version = previous["version"] + 1 if previous else 1
            db.execute(
                "INSERT INTO evidence_versions VALUES (?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    version,
                    symbol,
                    url,
                    payload["published_at"],
                    payload["observed_at"],
                    digest,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
        return evidence_id

    def packet(self, symbol: str, *, at: datetime | None = None, limit: int = 30) -> dict:
        at = at or datetime.now(UTC)
        rows = self.store.rows(
            """SELECT * FROM evidence_versions e WHERE instrument=? AND observed_at<=?
            AND version=(SELECT MAX(version) FROM evidence_versions other
                WHERE other.evidence_id=e.evidence_id AND other.observed_at<=?)
            ORDER BY CASE WHEN json_extract(payload,'$.kind') IN ('financial_fact','filing') THEN 0 ELSE 1 END,
                julianday(published_at) DESC,observed_at DESC,evidence_id LIMIT ?""",
            (instrument(symbol), observation_stamp(at), observation_stamp(at), limit),
        )
        usable, excluded = [], []
        news_copies = {}
        for row in rows:
            item = json.loads(row["payload"])
            item.update(evidence_id=row["evidence_id"], version=row["version"])
            if item["status"] != "active":
                excluded.append({"evidence_id": row["evidence_id"], "reason": "材料已撤回"})
            elif not item["published_at"]:
                excluded.append(
                    {"evidence_id": row["evidence_id"], "reason": "公开时间未知，未用于时点研判"}
                )
            elif parse_time(item["published_at"]) > at or (
                item["updated_at"] and parse_time(item["updated_at"]) > at
            ):
                excluded.append({"evidence_id": row["evidence_id"], "reason": "晚于分析时点"})
            else:
                if (
                    item["kind"] == "news"
                    and item["content_level"] != "metadata"
                    and item["content"].strip()
                ):
                    key = (
                        re.sub(r"\s+", " ", item["title"] + " " + item["content"])
                        .strip()
                        .casefold()
                    )
                    if key in news_copies:
                        news_copies[key].setdefault("same_copy_urls", []).append(item["url"])
                        excluded.append(
                            {
                                "evidence_id": row["evidence_id"],
                                "reason": "与已纳入新闻正文及标题完全相同，未作为独立依据重复计数",
                            }
                        )
                        continue
                    news_copies[key] = item
                usable.append(item)
        return {"items": usable, "excluded": excluded, "as_of": iso(at)}

    def manual(self, symbol: str, *, url: str, title: str, content: str, published_at: str) -> str:
        return self.add(
            Evidence(
                instrument=instrument(symbol),
                url=url,
                publisher="用户提供，未经抓取核验",
                provider="manual",
                kind="user_note",
                title=title,
                content=content,
                published_at=published_at,
                observed_at=iso(datetime.now(UTC)),
                content_level="user_note",
                verified_source=False,
            )
        )
