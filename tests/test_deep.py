import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from research_helpers import NOW, FakeMarket, reply_data

from stockwatcher.analysis import AnalysisService
from stockwatcher.budget import BudgetedModel
from stockwatcher.config import Settings
from stockwatcher.deep import ROLES, DeepResearch, validate_role
from stockwatcher.domain import ValidationError
from stockwatcher.evidence import EvidenceStore
from stockwatcher.model import ModelReply, ModelUnavailable
from stockwatcher.profiles import Profiles


class RoleModel:
    def __init__(self, failures=(), *, wait=0):
        self.calls, self.active, self.peak = [], 0, 0
        self.failures, self.wait = failures, wait

    async def generate(self, system, prompt, origin, **kwargs):
        data = json.loads(prompt)
        self.calls.append((data, kwargs))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.wait)
            role = data.get("role")
            if role in self.failures:
                raise ModelUnavailable("测试角色不可用")
            if role:
                data = {
                    "role": role,
                    "horizon_days": 180 if role in {"bull", "fundamental"} else 5,
                    "stance": "support"
                    if role == "bull"
                    else "oppose"
                    if role == "bear"
                    else "mixed",
                    "arguments": [{"text": "独立证据结论", "evidence_ids": ["metrics"]}],
                    "uncertainties": [{"text": "需要复核事件覆盖", "evidence_ids": ["metrics"]}],
                }
            else:
                assert "roles" in data
                data = reply_data()
            return ModelReply(
                json.dumps(data),
                {"provider_id": "host", "usage": {"input_other": 100, "output": 50}},
            )
        finally:
            self.active -= 1


def service(store, ledger, model, settings=None):
    settings = settings or Settings()
    budget = BudgetedModel(model, store, settings, clock=lambda: NOW)
    analysis = AnalysisService(
        store, Profiles(store), ledger, FakeMarket(), EvidenceStore(store), None, budget
    )
    analysis.deep_engine = DeepResearch(budget, settings)
    return analysis, budget


def test_six_call_research_shared_packet_preserves_disagreement_and_horizons(store, ledger):
    model = RoleModel(wait=0.005)
    analysis, budget = service(store, ledger, model)
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    assert report["status"] == "validated"
    assert len(model.calls) == 6
    assert model.peak == 2
    packets = [data["packet"] for data, _ in model.calls]
    assert all(packet == packets[0] for packet in packets)
    research = report["model"]["research"]
    assert research["quality"] == "full"
    assert research["roles"]["bull"]["stance"] == "support"
    assert research["roles"]["bear"]["stance"] == "oppose"
    assert research["roles"]["bull"]["horizon_days"] != research["roles"]["bear"]["horizon_days"]
    assert budget.stats()["calls"] == 6
    assert "完成 5/5 个角色" in analysis.render(report)
    assert "bear" in analysis.render(report, detailed=True)
    assert all(kwargs["max_output_tokens"] <= 2048 for _, kwargs in model.calls)


@pytest.mark.parametrize("failures", [("event",), tuple(ROLES)])
def test_partial_and_all_role_failures_are_explicit(store, ledger, failures):
    model = RoleModel(failures)
    analysis, _ = service(store, ledger, model)
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    research = report["model"]["research"]
    if len(failures) == 5:
        assert report["status"] == "degraded"
        assert set(research["errors"]) == {*failures, "synthesis"}
        assert research["quality"] == "failed"
        assert len(model.calls) == 5
        assert "未启动汇总" in research["errors"]["synthesis"]
        assert "单模型备选" not in analysis.render(report)
    else:
        assert report["status"] == "validated"
        assert set(research["errors"]) == set(failures)
        assert research["quality"] == "partial"
        assert any("缺失角色" in warning for warning in report["result"]["warnings"])
        assert len(model.calls) == 6


def test_unknown_role_citations_are_discarded(store, ledger):
    class Hallucinating(RoleModel):
        async def generate(self, system, prompt, origin, **kwargs):
            result = await super().generate(system, prompt, origin, **kwargs)
            data = json.loads(result.text)
            if data.get("role") == "bull":
                data["arguments"][0]["evidence_ids"] = ["ev_invented"]
                return ModelReply(json.dumps(data), result.metadata)
            return result

    analysis, _ = service(store, ledger, Hallucinating())
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    assert "bull" in report["model"]["research"]["errors"]
    assert "bull" not in report["model"]["research"]["roles"]


def test_deep_call_budget_preflight_avoids_wasted_partial_run(store, ledger):
    model = RoleModel()
    analysis, budget = service(store, ledger, model, replace(Settings(), model_daily_calls=5))
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    assert report["status"] == "degraded"
    assert budget.stats()["calls"] == 0
    assert model.calls == []


def test_daily_budget_atomic_under_concurrent_calls_and_failure_counts(store):
    class Model:
        async def generate(self, *args, **kwargs):
            await asyncio.sleep(0)
            raise ModelUnavailable("test failure")

    budget = BudgetedModel(
        Model(), store, replace(Settings(), model_daily_calls=1), clock=lambda: NOW
    )

    async def scenario():
        results = await asyncio.gather(
            *(budget.generate("s", "{}", "origin") for _ in range(3)), return_exceptions=True
        )
        assert all(isinstance(item, ModelUnavailable) for item in results)

    asyncio.run(scenario())
    assert budget.stats()["calls"] == 1
    assert store.rows("SELECT status FROM model_calls")[0][0] == "failed"
    budget.clock = lambda: NOW + timedelta(days=1)
    assert budget.stats()["calls"] == 0


def test_token_planning_budget_zero_and_input_limit_stop_calls(store):
    model = RoleModel()
    budget = BudgetedModel(
        model, store, replace(Settings(), model_daily_token_budget=0), clock=lambda: NOW
    )
    with pytest.raises(ModelUnavailable, match="预算"):
        asyncio.run(budget.generate("s", "{}", "origin"))
    budget.settings = Settings()
    with pytest.raises(ModelUnavailable, match="字符"):
        asyncio.run(budget.generate("s", "x" * 24001, "origin"))
    assert not model.calls


def test_deep_timeout_cancels_all_role_tasks(store, ledger):
    model = RoleModel(wait=1)
    analysis, budget = service(store, ledger, model)
    settings = SimpleNamespace(
        model_max_input_chars=24000,
        model_max_output_tokens=2048,
        deep_concurrency=2,
        deep_timeout_seconds=0.02,
    )
    analysis.deep_engine = DeepResearch(budget, settings)
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    assert report["status"] == "degraded"
    assert model.active == 0
    assert not any(row[0] == "reserved" for row in store.rows("SELECT status FROM model_calls"))
    assert any("总超时" in item for item in report["result"]["warnings"])


def test_two_successful_roles_are_exposed_as_partial_research_at_the_top(store, ledger):
    model = RoleModel(failures=("fundamental", "event", "bear"))
    analysis, budget = service(store, ledger, model)
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    brief = analysis.render(report)
    assert "深度研究未完整完成（2/5）" in brief.splitlines()[0]
    assert "AI分析已通过校验" not in brief
    assert "技术面｜5天｜利弊并存" in brief
    assert "多方｜180天｜偏支持" in brief
    assert "保留疑点：需要复核事件覆盖" in brief
    assert (
        "缺失角色：基本面" in brief
        and "事件（模型请求未完成）" in brief
        and "空方（模型请求未完成）" in brief
    )
    assert "趋势参考：20日均价" not in brief
    assert "所有角色共用本次材料" in brief
    assert budget.stats()["calls"] == 6
    before = budget.stats()["calls"]
    detail = analysis.render(report, detailed=True)
    assert "论点：独立证据结论 [metrics]" in detail
    assert "反证/缺口：需要复核事件覆盖 [metrics]" in detail
    assert "失败诊断 基本面（fundamental）" in detail
    assert budget.stats()["calls"] == before


@pytest.mark.parametrize("fence", ["json", "JSON", ""])
def test_standard_json_fences_are_accepted_without_weakening_role_validation(fence):
    data = {
        "role": "technical",
        "horizon_days": 5,
        "stance": "mixed",
        "arguments": [{"text": "价格仍弱", "evidence_ids": ["metrics"]}],
        "uncertainties": [{"text": "后续价格未知", "evidence_ids": ["metrics"]}],
    }
    text = f"```{fence}\n{json.dumps(data)}\n```"
    assert validate_role(text, "technical", {"metrics"}) == data
    data["arguments"][0]["evidence_ids"] = ["ev_not_supplied"]
    with pytest.raises(ValidationError, match="technical.arguments"):
        validate_role(f"```{fence}\n{json.dumps(data)}\n```", "technical", {"metrics"})


def test_duplicate_role_keys_and_extra_text_are_still_rejected():
    text = '{"role":"technical","role":"bull"}'
    with pytest.raises(ValidationError, match="JSON"):
        validate_role(text, "technical", {"metrics"})
    with pytest.raises(ValidationError, match="JSON"):
        validate_role("Here is the result: ```json\n{}\n```", "technical", {"metrics"})


def test_successful_roles_and_failed_synthesis_are_not_labelled_complete(store, ledger):
    class FailingSummary(RoleModel):
        async def generate(self, system, prompt, origin, **kwargs):
            response = await super().generate(system, prompt, origin, **kwargs)
            if "role" not in json.loads(prompt):
                raise ModelUnavailable("汇总请求超时")
            return response

    analysis, _ = service(store, ledger, FailingSummary())
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    text = analysis.render(report)
    assert "深度汇总未完成（角色完成5/5）" in text.splitlines()[0]
    assert "五个角色已完成，汇总未完成" in text
    assert "五个角色与汇总均已完成" not in text
    assert "汇总失败：请求超时" in text
    assert "多方｜180天" in text and "空方｜5天" in text


def test_preflight_budget_failure_remains_identified_as_deep_research(store, ledger):
    analysis, _ = service(store, ledger, RoleModel(), replace(Settings(), model_daily_calls=5))
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    text = analysis.render(report)
    assert "深度研究" in text.splitlines()[0]
    assert "0/5" in text and "调用预算不足" in text
    assert "没有已完成的研究角色" in text
