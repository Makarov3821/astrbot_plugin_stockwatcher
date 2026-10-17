import asyncio
import copy
import json
from datetime import timedelta

import pytest
from research_helpers import NOW, FakeMarket, FakeModel, reply_data

from stockwatcher.analysis import AnalysisService, validate_reply
from stockwatcher.domain import ValidationError, iso
from stockwatcher.evidence import Evidence, EvidenceStore
from stockwatcher.model import ModelReply
from stockwatcher.profiles import Profiles


def services(store, ledger, model=None):
    profiles, evidence = Profiles(store), EvidenceStore(store)
    service = AnalysisService(store, profiles, ledger, FakeMarket(), evidence, None, model)
    return service, profiles, evidence


def eps(evidence, *, verified=True, annual=True, value="5", period="2025-08-31"):
    return evidence.add(
        Evidence(
            instrument="MU",
            url="https://www.sec.gov/Archives/example/",
            publisher="SEC XBRL",
            provider="sec",
            kind="financial_fact",
            title="年度 EPS",
            content="历史数据",
            published_at=iso(NOW - timedelta(days=60)),
            observed_at=iso(NOW - timedelta(days=59)),
            verified_source=verified,
            content_level="facts",
            data={
                "identity": "eps",
                "metric": "eps_diluted",
                "annual": annual,
                "unit": "USD/shares",
                "value": value,
                "period_end": period,
            },
        )
    )


def target(key):
    return {
        "kind": "valuation",
        "eps_evidence_id": key,
        "multiple": "30",
        "horizon_days": 180,
        "reason": "假设倍数仍有压缩风险",
    }


def test_analysis_is_auditable_idempotent_and_caches_are_observations(store, ledger):
    model = FakeModel()
    service, _, _ = services(store, ledger, model)

    async def scenario():
        report = await service.analyze("MU", now=NOW, request_key="msg1")
        assert report["status"] == "validated"
        assert report["input"]["market_fresh"]
        assert report["input"]["metrics"]["reference_price"] == "169"
        assert "market_snapshots" not in model.calls[0]
        assert report["input"]["market_snapshots"]["raw"]["adjustment"] == "raw"
        assert (await service.analyze("MU", now=NOW, request_key="msg1"))["id"] == report["id"]
        assert len(model.calls) == 1
        cached = await service.analyze("MU", now=NOW, cached=True)
        assert not cached["input"]["market_fresh"]
        assert cached["input"]["previous"]["id"] == report["id"]
        assert cached["result"]["final_action"] == "watch"
        assert "缓存" in service.render(cached)

    asyncio.run(scenario())


@pytest.mark.parametrize("response", ["garbage", "{}", '{"action":NaN}'])
def test_invalid_model_falls_back_without_fabricated_research(store, ledger, response):
    class BrokenModel:
        async def generate(self, *args):
            return ModelReply(response, {})

    service, _, _ = services(store, ledger, BrokenModel())
    report = asyncio.run(service.analyze("MU", now=NOW))
    assert report["status"] == "degraded"
    assert report["result"]["final_action"] == "watch"
    assert report["result"]["target_proposal"] is None


def test_no_model_still_applies_hard_exit_and_stale_data_blocks_action(store, ledger):
    service, profiles, _ = services(store, ledger)
    ledger.record("seed", symbol="MU", quantity="1", price="200", day="2026-01-01")
    profiles.update({"hard_stop_price": "175"}, symbol="MU")
    assert asyncio.run(service.analyze("MU", now=NOW))["result"]["final_action"] == "exit"
    assert (
        asyncio.run(service.analyze("MU", cached=True, now=NOW))["result"]["final_action"]
        == "watch"
    )


def test_buy_is_blocked_without_budget_and_has_share_cap_when_ready(store, ledger):
    entry = {
        "lower": "140",
        "upper": "150",
        "invalid_below": "130",
        "confirmation": "daily_close_above_ma20",
        "reason": "候选区，仍需等待确认",
    }
    model = FakeModel(reply_data(action="consider_buy", entry=entry))
    service, profiles, _ = services(store, ledger, model)
    first = asyncio.run(service.analyze("MU", now=NOW))
    assert first["result"]["final_action"] == "watch"
    profiles.update({"max_stock_pct": "50", "max_total_pct": "80", "risk_budget_pct": "1"})
    ledger.record("cash_set", cash="10000", day="2026-01-01")
    second = asyncio.run(service.analyze("MU", now=NOW))
    assert second["result"]["final_action"] == "consider_buy"
    assert second["result"]["entry"]["whole_share_gross_cap"] == "5"
    assert "未启用" in second["result"]["entry"]["monitoring"]


def test_valuation_is_computed_and_only_adopted_explicitly(store, ledger):
    service, profiles, evidence = services(store, ledger)
    profiles.update({"target_price": "120"}, symbol="MU")
    key = eps(evidence)
    service.model = FakeModel(reply_data(target_proposal=target(key)))
    report = asyncio.run(service.analyze("MU", now=NOW))
    assert report["result"]["target_proposal"]["price"] == "150.00"
    assert profiles.resolved("MU")["values"]["target_price"] == "120"
    proposal = store.rows("SELECT * FROM target_proposals")[0]
    assert "已更新" in service.decide_target(proposal["id"], True, now=NOW)
    assert profiles.resolved("MU")["values"]["target_price"] == "150.00"
    assert "已处理" in service.decide_target(proposal["id"], True, now=NOW)
    assert report["input"]["preference"]["values"]["target_price"] == "120"


@pytest.mark.parametrize(
    "changes", [{"verified": False}, {"annual": False}, {"value": "-5"}, {"period": "2020-01-01"}]
)
def test_untrusted_or_inapplicable_eps_does_not_support_target(store, ledger, changes):
    service, _, evidence = services(store, ledger)
    key = eps(evidence, **changes)
    service.model = FakeModel(reply_data(target_proposal=target(key)))
    report = asyncio.run(service.analyze("MU", now=NOW))
    assert report["status"] == "degraded"
    assert not store.rows("SELECT * FROM target_proposals")


@pytest.mark.parametrize("change", ["profile", "ledger"])
def test_inflight_profile_or_ledger_change_invalidates_output(store, ledger, change):
    service, profiles, evidence = services(store, ledger)
    key = eps(evidence)
    hook = (
        (lambda: profiles.update({"horizon_days": 90}))
        if change == "profile"
        else (lambda: ledger.record("cash_set", cash="10000", day="2026-01-01"))
    )
    service.model = FakeModel(reply_data(target_proposal=target(key)), hook=hook)
    report = asyncio.run(service.analyze("MU", now=NOW))
    assert report["status"] == "superseded"
    assert report["result"]["target_proposal"] is None
    assert not store.rows("SELECT * FROM target_proposals")


@pytest.mark.parametrize("mode", ["expired", "changed", "rejected"])
def test_target_adoption_rechecks_lifetime_and_preferences(store, ledger, mode):
    service, profiles, evidence = services(store, ledger)
    service.model = FakeModel(reply_data(target_proposal=target(eps(evidence))))
    asyncio.run(service.analyze("MU", now=NOW))
    key = store.rows("SELECT id FROM target_proposals")[0]["id"]
    if mode == "changed":
        profiles.update({"horizon_days": 90})
    if mode == "rejected":
        assert "原目标" in service.decide_target(key, False, now=NOW)
    else:
        with pytest.raises(ValidationError):
            service.decide_target(
                key, True, now=NOW + timedelta(days=8) if mode == "expired" else NOW
            )
    assert profiles.resolved("MU")["values"]["target_price"] is None


@pytest.mark.parametrize(
    "path,value",
    [
        ("action", {}),
        ("thesis.status", []),
        ("event_risk.level", {}),
        ("reasons.0.evidence_ids", ["ev_invented"]),
        ("summary", "上涨概率 90%"),
        ("valid_for_days", True),
    ],
)
def test_malformed_output_is_rejected_as_validation_error(store, ledger, path, value):
    service, _, _ = services(store, ledger, FakeModel())
    packet = asyncio.run(service.analyze("MU", now=NOW))["input"]
    data = copy.deepcopy(reply_data())
    parts = path.split(".")
    parent = data
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    parent[parts[-1]] = value
    with pytest.raises(ValidationError):
        validate_reply(ModelReply(json.dumps(data), {}), packet, NOW)


def test_duplicate_keys_and_news_after_last_close(store, ledger):
    service, _, evidence = services(store, ledger, FakeModel())
    item = Evidence(
        instrument="MU",
        url="https://example.com/late",
        publisher="test",
        provider="test",
        kind="news",
        title="盘后事件",
        content="变化",
        published_at=iso(NOW - timedelta(minutes=10)),
        observed_at=iso(NOW),
    )
    evidence.add(item)
    report = asyncio.run(service.analyze("MU", now=NOW))
    assert any("事件后" in value for value in report["input"]["portfolio"]["warnings"])
    text = json.dumps(reply_data()).replace(
        '"action": "watch"', '"action": "watch", "action": "consider_buy"'
    )
    with pytest.raises(ValidationError):
        validate_reply(ModelReply(text, {}), report["input"], NOW)


def test_future_market_snapshot_and_old_daily_data_do_not_become_current(store, ledger):
    from dataclasses import replace

    service, _, _ = services(store, ledger, FakeModel())

    class FutureMarket(FakeMarket):
        async def query(self, *args, **kwargs):
            result = await super().query(*args, **kwargs)
            return replace(result, fetched_at=NOW + timedelta(seconds=1))

    service.market = FutureMarket()
    with pytest.raises(ValidationError, match="未来"):
        asyncio.run(service.analyze("MU", now=NOW))

    class OldMarket(FakeMarket):
        async def query(self, *args, **kwargs):
            result = await super().query(*args, **kwargs)
            return replace(result, bars=result.bars[:-1])

    service.market = OldMarket()
    ledger.record("seed", symbol="MU", quantity="1", price="200", day="2026-01-01")
    result = asyncio.run(service.analyze("MU", now=NOW))
    assert not result["input"]["market_fresh"]
    assert result["result"]["final_action"] == "watch"


def test_user_target_reduce_preference_overrides_model_hold(store, ledger):
    service, profiles, _ = services(store, ledger, FakeModel(reply_data(action="hold")))
    ledger.record("seed", symbol="MU", quantity="1", price="100", day="2026-01-01")
    profiles.update({"target_price": "150", "target_action": "reduce"}, symbol="MU")
    assert asyncio.run(service.analyze("MU", now=NOW))["result"]["final_action"] == "reduce"
    service.model = None
    assert asyncio.run(service.analyze("MU", now=NOW))["result"]["final_action"] == "reduce"
