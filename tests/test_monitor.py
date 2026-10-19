import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from research_helpers import NOW, FakeMarket, FakeModel, batch, reply_data

from stockwatcher.analysis import AnalysisService
from stockwatcher.config import Settings
from stockwatcher.delivery import Delivery, in_quiet_hours, message_parts
from stockwatcher.domain import Bar, MarketSession, ValidationError, iso
from stockwatcher.evidence import EvidenceStore
from stockwatcher.monitor import Monitor
from stockwatcher.profiles import Profiles

ENTRY = {
    "lower": "160",
    "upper": "169",
    "invalid_below": "130",
    "confirmation": "daily_close_above_ma20",
    "reason": "测试候选",
}


def setup(store, ledger, *, sender=None):
    async def send(origin, text):
        return True

    profiles = Profiles(store)
    profiles.update(
        {"max_stock_pct": "50", "max_total_pct": "80", "risk_budget_pct": "1", "allow_add": True}
    )
    ledger.record("cash_set", cash="10000", day="2026-01-01")
    analysis = AnalysisService(
        store,
        profiles,
        ledger,
        FakeMarket(),
        EvidenceStore(store),
        None,
        FakeModel(reply_data(entry=ENTRY, valid_for_days=7)),
    )
    delivery = Delivery(store, sender or send)
    monitor = Monitor(analysis, delivery, Settings())
    store.set_setting("notification_origin", "test:Friend:1")
    return monitor, analysis, profiles


def adopt(monitor, analysis):
    report = asyncio.run(analysis.analyze("MU", now=NOW))
    return monitor.adopt(report["id"], now=NOW)


def next_packet(analysis):
    next_day = NOW + timedelta(days=1)

    class NextMarket(FakeMarket):
        async def query(self, symbol, *, adjustment="raw", now=None):
            original = batch(symbol, adjustment)
            bar = replace(
                original.bars[-1],
                timestamp=original.bars[-1].timestamp + timedelta(days=1),
                low=D(161),
                high=D(169),
                close=D(168),
            )
            return replace(
                original,
                bars=original.bars[:-1] + (bar,),
                fetched_at=next_day,
                requested_end=next_day - timedelta(minutes=16),
            )

        async def calendar(self, *args):
            return [
                MarketSession(
                    next_day.date(), next_day.replace(hour=14, minute=30), next_day.replace(hour=21)
                )
            ]

    analysis.market = NextMarket()
    report = asyncio.run(analysis.analyze("MU", now=next_day, program_only=True))
    from stockwatcher.domain import MarketBatch

    return (
        report["input"],
        MarketBatch.from_dict(report["input"]["market_snapshots"]["raw"]),
        next_day,
    )


def test_explicit_subscription_and_disabled_tick_uses_no_network(store, ledger):
    monitor, _, _ = setup(store, ledger)
    store.set_setting("notification_origin", None)
    with pytest.raises(ValidationError, match="接收会话"):
        monitor.configure({"enabled": True})
    assert asyncio.run(monitor.tick(now=NOW)) == {"enabled": False}
    assert not store.rows("SELECT * FROM monitor_jobs")


def test_entry_lifecycle_dedup_and_actual_trade_does_not_happen_implicitly(store, ledger):
    monitor, analysis, _ = setup(store, ledger)
    plan_id = adopt(monitor, analysis)
    monitor.configure({"enabled": True})
    packet, raw, now = next_packet(analysis)
    monitor._evaluate("US:MU", raw, now, packet)
    assert (
        store.rows("SELECT state FROM monitor_plans WHERE id=?", (plan_id,))[0]["state"]
        == "triggered"
    )
    assert ledger.view().positions == {}
    monitor._evaluate("US:MU", raw, now, packet)
    assert len(store.rows("SELECT * FROM notification_outbox")) == 2  # armed, confirmed
    assert monitor.adopt(1, now=NOW) == plan_id


@pytest.mark.parametrize("kind", ["invalidated", "expired", "superseded", "completed", "cancelled"])
def test_plan_terminal_states(store, ledger, kind):
    monitor, analysis, profiles = setup(store, ledger)
    plan_id = adopt(monitor, analysis)
    if kind == "invalidated":
        packet, raw, now = next_packet(analysis)
        raw = replace(raw, bars=raw.bars[:-1] + (replace(raw.bars[-1], low=D(120)),))
        monitor._evaluate("US:MU", raw, now, packet)
    elif kind == "expired":
        monitor.reconcile(now=NOW + timedelta(days=8))
    elif kind == "superseded":
        profiles.update({"horizon_days": 180})
        monitor.reconcile(now=NOW)
    elif kind == "completed":
        ledger.record("buy", symbol="MU", quantity="1", price="100", day="2026-01-06")
        monitor.reconcile(now=NOW)
    else:
        assert monitor.cancel(plan_id)
    assert store.rows("SELECT state FROM monitor_plans WHERE id=?", (plan_id,))[0]["state"] == kind


def test_no_pullback_stays_waiting_and_missing_budget_never_confirms(store, ledger):
    monitor, analysis, _ = setup(store, ledger)
    adopt(monitor, analysis)
    packet, raw, now = next_packet(analysis)
    above = replace(
        raw, bars=raw.bars[:-1] + (replace(raw.bars[-1], low=D(170), high=D(180), close=D(175)),)
    )
    monitor._evaluate("US:MU", above, now, packet)
    assert store.rows("SELECT state FROM monitor_plans")[0][0] == "waiting"
    packet["portfolio"]["increase_allowed"] = False
    # A later observation may arm, but cannot confirm without complete constraints.
    packet["market_asof"] = iso(now + timedelta(minutes=1))
    monitor._evaluate("US:MU", raw, now, packet)
    assert store.rows("SELECT state FROM monitor_plans")[0][0] == "armed"


def test_daily_job_survives_restart_deduplicates_and_never_uses_model_by_default(store, ledger):
    monitor, analysis, _ = setup(store, ledger)
    ledger.watch("MU")
    monitor.configure(
        {"enabled": True, "daily": True, "quiet_start": "00:00", "quiet_end": "00:00"}
    )
    at = NOW + timedelta(hours=2)  # 08:00 Shanghai; default report 07:30
    first = asyncio.run(monitor.tick(now=at))
    assert first["sent"] == 1
    assert analysis.model.calls == []
    restarted = Monitor(analysis, monitor.delivery, Settings())
    asyncio.run(restarted.tick(now=at))
    assert len(store.rows("SELECT * FROM monitor_jobs")) == 1
    assert len(store.rows("SELECT * FROM notification_outbox")) == 1
    assert store.rows("SELECT status FROM monitor_jobs")[0][0] == "done"


def test_holding_exit_notification_deduplicates_and_does_not_sell(store, ledger):
    monitor, _, profiles = setup(store, ledger)
    ledger.record("seed", symbol="MU", quantity="2", price="200", day="2026-01-01")
    profiles.update({"hard_stop_price": "175"}, symbol="MU")
    monitor.configure({"enabled": True})
    packet = None
    minute = replace(
        batch(adjustment="raw"),
        timeframe="1Min",
        session="regular",
        bars=(Bar(NOW - timedelta(minutes=18), D(169), D(170), D(168), D(169), D(100)),),
    )
    monitor._evaluate("US:MU", minute, NOW, packet)
    monitor._evaluate("US:MU", minute, NOW, packet)
    assert len(store.rows("SELECT * FROM notification_outbox")) == 1
    assert ledger.view().positions["US:MU"].quantity == 2


def test_job_leases_retry_and_stale_owner_cannot_complete(store, ledger):
    monitor, _, _ = setup(store, ledger)
    delivery = monitor.delivery
    first = delivery.claim_job("job", NOW)
    assert first
    assert delivery.claim_job("job", NOW) is None
    second = delivery.claim_job("job", NOW + timedelta(minutes=31))
    assert second and second != first
    delivery.finish_job("job", first, NOW)
    assert store.rows("SELECT status FROM monitor_jobs")[0][0] == "running"
    delivery.finish_job("job", second, NOW, error="fixture failure")
    assert delivery.claim_job("job", NOW) is None
    third = delivery.claim_job("job", NOW + timedelta(hours=1))
    delivery.finish_job("job", third, NOW, error="fixture failure")
    assert store.rows("SELECT status FROM monitor_jobs")[0][0] == "failed"


def test_delivery_chunks_resume_from_unsent_part_and_redact_errors(store, ledger):
    calls = []
    fail = True

    async def sender(origin, text):
        calls.append(text)
        if fail and "2/3" in text:
            raise RuntimeError("private-secret")
        return True

    monitor, _, _ = setup(store, ledger, sender=sender)
    config = monitor.configure({"enabled": True, "quiet_start": "00:00", "quiet_end": "00:00"})
    with store.transaction() as db:
        monitor.delivery.enqueue(db, "event", "test:Friend:1", "x" * 3100, NOW)
    assert asyncio.run(monitor.delivery.flush(config, now=NOW)) == 0
    row = store.rows("SELECT * FROM notification_outbox")[0]
    assert row["next_part"] == 1
    assert "private" not in row["last_error"]
    fail = False
    assert asyncio.run(Delivery(store, sender).flush(config, now=NOW + timedelta(minutes=3))) == 1
    assert len([value for value in calls if "1/3" in value]) == 1
    assert store.rows("SELECT status FROM notification_outbox")[0][0] == "sent"


def test_quiet_hours_expiry_target_change_and_stop(store, ledger):
    sent = []

    async def sender(*args):
        sent.append(args)

    monitor, _, _ = setup(store, ledger, sender=sender)
    config = monitor.configure({"enabled": True, "quiet_start": "22:00", "quiet_end": "07:00"})
    with store.transaction() as db:
        monitor.delivery.enqueue(db, "event", "test:Friend:1", "text", NOW)
    assert asyncio.run(monitor.delivery.flush(config, now=NOW)) == 0  # 06:00 local
    store.set_setting("notification_origin", "test:Friend:other")
    assert asyncio.run(monitor.delivery.flush(config, now=NOW + timedelta(hours=2))) == 0
    assert store.rows("SELECT status FROM notification_outbox")[0][0] == "cancelled"
    assert not sent
    assert in_quiet_hours(NOW, "Asia/Shanghai", "22:00", "07:00")
    assert not in_quiet_hours(NOW, "Asia/Shanghai", "00:00", "00:00")
    assert "".join(message_parts("长" * 4501)) == "长" * 4501
    monitor.configure({"enabled": False})
    assert asyncio.run(monitor.tick(now=NOW))["enabled"] is False


def test_changed_preferences_cancel_pending_plan_trigger_messages(store, ledger):
    monitor, analysis, profiles = setup(store, ledger)
    adopt(monitor, analysis)
    monitor.configure({"enabled": True})
    packet, raw, now = next_packet(analysis)
    monitor._evaluate("US:MU", raw, now, packet)
    profiles.update({"allow_add": False})
    monitor.reconcile(now=now)
    assert {row[0] for row in store.rows("SELECT status FROM notification_outbox")} == {"cancelled"}
