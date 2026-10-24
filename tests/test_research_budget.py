"""Report #5 regressions: exact preference paths and an affordable whole research plan."""

import asyncio
import json
from dataclasses import replace

from research_helpers import NOW
from test_conversation import Context, grouped_config
from test_deep import RoleModel, service
from test_onboarding_chat import command
from test_plugin import plugin_module as plugin_module

from stockwatcher.budget import compact_packet
from stockwatcher.citations import citation_scope
from stockwatcher.config import Settings
from stockwatcher.deep import ROLE_SYSTEM, ROLES, synthesis_budget_prompt, synthesis_prompt
from stockwatcher.model import ModelReply
from stockwatcher.reporting import citation_diagnostics


def test_wrong_preference_path_is_rejected_with_existing_path_hint_not_rewritten(store, ledger):
    class WrongPreference(RoleModel):
        async def generate(self, system, prompt, origin, **kwargs):
            reply = await super().generate(system, prompt, origin, **kwargs)
            data = json.loads(reply.text)
            if data.get("role") == "event":
                data["arguments"][0]["evidence_ids"] = ["preference.horizon_days"]
            return ModelReply(json.dumps(data), reply.metadata)

    model = WrongPreference()
    analysis, budget = service(store, ledger, model)
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    report = analysis.load(report["id"])
    research = report["model"]["research"]
    assert "event" not in research["roles"]
    error = research["diagnostics"]["event"]["citation_error"]
    assert error["rejected_values"] == ["preference.horizon_days"]
    assert error["available_path_hints"] == {
        "preference.horizon_days": "preference.values.horizon_days"
    }
    assert "preference.values.horizon_days" in citation_diagnostics(report)
    assert len(model.calls) == budget.stats()["calls"] == 6  # no corrective model retry


def test_old_diagnostic_can_show_real_path_hint_without_changing_stored_record():
    report = {
        "id": 5,
        "model": {
            "research": {
                "shared_evidence_ids": ["preference", "preference.values.horizon_days"],
                "diagnostics": {
                    "event": {
                        "status": "rejected",
                        "citation_error": {
                            "field": "event.arguments[1].evidence_ids",
                            "code": "unknown_reference",
                            "rejected_values": ["preference.horizon_days"],
                        },
                    }
                },
            }
        },
    }
    original = json.dumps(report)
    text = citation_diagnostics(report)
    assert "preference.values.horizon_days" in text and "不自动替换" in text
    assert json.dumps(report) == original


def test_full_plan_token_shortage_stops_before_spending_one_affordable_role(store, ledger):
    model = RoleModel()
    settings = replace(Settings(), model_daily_token_budget=25000)
    analysis, budget = service(store, ledger, model, settings)
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    research = report["model"]["research"]
    plan = research["budget_preflight"]
    assert report["status"] == "degraded" and plan["status"] == "insufficient"
    assert plan["estimated_tokens"] > plan["remaining_tokens"] == 25000
    assert model.calls == [] and budget.stats()["calls"] == 0
    assert all(research["diagnostics"][role]["status"] == "not_started" for role in ROLES)
    # This reproduces the old hole: a first role fits, while the complete six-call plan does not.
    packet = compact_packet(
        {key: value for key, value in report["input"].items() if key != "market_snapshots"},
        settings.model_max_input_chars - 8000,
    )
    first_prompt = json.dumps(
        {"role": "fundamental", "focus": ROLES["fundamental"], "packet": packet},
        ensure_ascii=False,
    )
    assert len((ROLE_SYSTEM + first_prompt).encode()) + 1200 < 25000
    assert "本次未启动模型调用" in analysis.render(report)
    assert "整轮预算预检" in citation_diagnostics(report)


def test_preflight_estimate_bounds_actual_six_calls_but_does_not_charge_margin(store, ledger):
    model = RoleModel()
    analysis, budget = service(store, ledger, model)
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    plan = report["model"]["research"]["budget_preflight"]
    actual = budget.stats()["reserved_token_estimate"]
    assert plan["status"] == "ready"
    assert actual < plan["estimated_tokens"] <= plan["remaining_tokens"]
    assert budget.stats()["calls"] == len(model.calls) == 6


def test_synthesis_plan_bounds_json_escaping_long_refs_and_error_snippets():
    packet = {"preference": {"values": {"horizon_days": 60}}, "evidence": {"items": []}}
    known = citation_scope(packet)
    argument = {"text": "\0" * 200, "evidence_ids": ["preference.values.horizon_days"] * 3}
    roles = {
        role: {
            "role": role,
            "horizon_days": 3650,
            "stance": "unknown",
            "arguments": [argument] * 2,
            "uncertainties": [argument] * 2,
        }
        for role in ROLES
    }
    errors = {role: "\0" * 3000 for role in ROLES}
    actual = synthesis_prompt(packet, roles, errors)
    estimated = synthesis_budget_prompt(packet, known)
    assert len(actual.encode()) <= len(estimated.encode())
    assert all(len(error) == 160 for error in json.loads(actual)["unavailable_roles"].values())


def test_usage_command_is_readable_and_never_calls_model(plugin_module):
    async def scenario():
        context = Context({})
        plugin = plugin_module.StockWatcherPlugin(context, grouped_config())
        try:
            await plugin.initialize()
            before = len(context.requests)
            text = await command(plugin, "盯盘 用量", "budget-view")
            assert "调用次数：已用 0 / 上限 24" in text
            assert "剩余 200,000" in text and "每日00:00重置" in text
            assert "不是厂商实际token用量或账单" in text
            assert len(context.requests) == before
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
