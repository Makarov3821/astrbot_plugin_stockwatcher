from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from stockwatcher.domain import ValidationError, iso
from stockwatcher.evidence import Evidence, EvidenceStore, canonical_url

NOW = datetime(2026, 1, 6, 22, tzinfo=UTC)


def evidence(**changes):
    fields = dict(
        instrument="MU",
        url="https://example.com/news?id=1&utm_source=test#part",
        publisher="Example",
        provider="first",
        kind="news",
        title="公告",
        content="初稿",
        published_at=iso(NOW - timedelta(hours=1)),
        observed_at=iso(NOW),
        verified_source=True,
    )
    return Evidence(**(fields | changes))


def test_dedup_revision_asof_and_withdrawal(store):
    items = EvidenceStore(store)
    original = evidence()
    key = items.add(original)
    assert key == items.add(
        replace(original, provider="mirror", observed_at=iso(NOW + timedelta(minutes=1)))
    )
    assert len(store.rows("SELECT * FROM evidence_versions")) == 1
    items.add(
        replace(
            original,
            content="更正",
            updated_at=iso(NOW + timedelta(hours=1)),
            observed_at=iso(NOW + timedelta(hours=1)),
        )
    )
    assert items.packet("MU", at=NOW)["items"][0]["content"] == "初稿"
    assert items.packet("MU", at=NOW + timedelta(hours=1))["items"][0]["content"] == "更正"
    items.add(replace(original, status="withdrawn", observed_at=iso(NOW + timedelta(hours=2))))
    withdrawn = items.packet("MU", at=NOW + timedelta(hours=3))
    assert withdrawn["items"] == []
    assert "撤回" in withdrawn["excluded"][0]["reason"]


def test_unknown_publication_and_first_seen_are_not_backdated(store):
    items = EvidenceStore(store)
    items.add(evidence(published_at=None))
    assert items.packet("MU", at=NOW - timedelta(minutes=1))["items"] == []
    assert "未知" in items.packet("MU", at=NOW)["excluded"][0]["reason"]
    with pytest.raises(ValidationError, match="晚于"):
        items.add(evidence(published_at=iso(NOW + timedelta(minutes=1))))


def test_manual_note_cannot_replace_verified_article(store):
    items = EvidenceStore(store)
    trusted = items.add(evidence())
    note = items.manual(
        "MU", url=evidence().url, title="我认为", content="评论", published_at=iso(NOW)
    )
    assert trusted != note
    packet = items.packet("MU")
    assert len(packet["items"]) == 2
    assert {item["verified_source"] for item in packet["items"]} == {False, True}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://a:b@example.com",
        "https://example.com:invalid",
        "https://[bad",
        "https://example.com:80",
    ],
)
def test_invalid_links_are_explainable(url):
    with pytest.raises(ValidationError):
        canonical_url(url)


def test_canonical_url_preserves_identity_query():
    assert (
        canonical_url("https://EXAMPLE.com/a?utm_source=x&id=2#frag")
        == "https://example.com/a?id=2"
    )


def test_exact_syndicated_copy_not_counted_twice(store):
    items = EvidenceStore(store)
    items.add(evidence())
    items.add(evidence(url="https://another.example.com/copy"))
    packet = items.packet("MU", at=NOW)
    assert len(packet["items"]) == 1
    assert len(packet["items"][0]["same_copy_urls"]) == 1
    assert "独立依据" in packet["excluded"][0]["reason"]


def test_subsecond_future_observation_is_excluded(store):
    items = EvidenceStore(store)
    original = evidence()
    items.add(original)
    items.add(
        replace(original, content="稍后更正", observed_at=iso(NOW + timedelta(microseconds=1)))
    )
    assert items.packet("MU", at=NOW)["items"][0]["content"] == "初稿"
    assert (
        items.packet("MU", at=NOW + timedelta(microseconds=1))["items"][0]["content"] == "稍后更正"
    )
