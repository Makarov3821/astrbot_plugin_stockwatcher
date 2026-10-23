"""Exact, supplied references are accepted; rejected output remains diagnosable offline."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from research_helpers import NOW, FakeMarket, FakeModel, reply_data
from test_analysis import services
from test_conversation import Context, grouped_config
from test_deep import RoleModel, service
from test_onboarding_chat import command, reply
from test_plugin import plugin_module as plugin_module

from stockwatcher.budget import compact_packet
from stockwatcher.citations import CitationError, citation_scope, validate_ids
from stockwatcher.config import Settings
from stockwatcher.evidence import Evidence
from stockwatcher.model import ModelReply
from stockwatcher.reporting import citation_diagnostics


def test_only_existing_input_field_paths_and_supplied_news_are_accepted():
    packet = {
        "metrics": {"ma20": "100"},
        "portfolio": {"cash": None},
        "preference": {"values": {"allow_add": False}},
        "source_gaps": ["未配置SEC"],
        "previous": {"evidence_versions": {"ev_history_only": 1}},
        "evidence": {"items": [{"evidence_id": "ev_supplied"}]},
    }
    known = citation_scope(packet)
    refs = [
        "metrics.ma20",
        "portfolio.cash",
        "preference.values.allow_add",
        "source_gaps.0",
        "ev_supplied",
    ]
    assert validate_ids(refs, known, nonempty=True) == refs
    for bad in (
        "metrics.nonexistent",
        "source_gaps.1",
        "previous.evidence_versions",
        "ev_history_only",
        "ev_invented",
        "evsupplied",
    ):
        with pytest.raises(CitationError) as caught:
            validate_ids([bad], known, field="technical.arguments[0].evidence_ids")
        assert caught.value.diagnostic == {
            "field": "technical.arguments[0].evidence_ids",
            "code": "unknown_reference",
            "rejected_values": [bad],
        }


def test_field_path_references_work_through_roles_and_synthesis_without_extra_calls(store, ledger):
    class FieldReferences(RoleModel):
        async def generate(self, system, prompt, origin, **kwargs):
            result = await super().generate(system, prompt, origin, **kwargs)
            data = json.loads(result.text)
            if "role" in data:
                data["arguments"][0]["evidence_ids"] = ["metrics.ma20"]
                data["uncertainties"][0]["evidence_ids"] = ["source_gaps"]
            else:
                data["reasons"][0]["evidence_ids"] = ["metrics.ma20"]
                data["counterarguments"][0]["evidence_ids"] = ["portfolio.cash", "source_gaps"]
            return ModelReply(json.dumps(data), result.metadata)

    model = FieldReferences()
    analysis, budget = service(store, ledger, model)
    report = asyncio.run(analysis.analyze("MU", now=NOW, deep=True))
    assert report["status"] == "validated" and report["model"]["research"]["quality"] == "full"
    assert len(model.calls) == budget.stats()["calls"] == 6
    assert report["result"]["reasons"][0]["evidence_ids"] == ["metrics.ma20"]
    research = report["model"]["research"]
    assert "metrics.ma20" in research["shared_evidence_ids"]
    assert all(item["raw_response"] for item in research["diagnostics"].values())


def test_failed_role_raw_output_metadata_and_exact_bad_reference_survive_storage(store, ledger):
    class BadReference(RoleModel):
        async def generate(self, system, prompt, origin, **kwargs):
            result = await super().generate(system, prompt, origin, **kwargs)
            data = json.loads(result.text)
            if data.get("role") == "technical":
                data["arguments"][0]["evidence_ids"] = ["metrics.imaginary"]
            return ModelReply(json.dumps(data), result.metadata)

    analysis, _ = service(store, ledger, BadReference())
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    loaded = analysis.load(report["id"])
    research = loaded["model"]["research"]
    failed = research["diagnostics"]["technical"]
    assert failed["status"] == "rejected" and "metrics.imaginary" in failed["raw_response"]
    assert failed["citation_error"]["field"] == "technical.arguments[0].evidence_ids"
    assert failed["citation_error"]["rejected_values"] == ["metrics.imaginary"]
    assert "technical" not in research["roles"]
    assert "technical" in research["role_metadata"]
    text = citation_diagnostics(loaded)
    assert "metrics.imaginary" in text and "出错位置" in text and len(text) < 1500
    assert '"arguments"' not in text


def test_old_record_diagnostic_does_not_invent_unrecorded_ids_or_raw_output():
    report = {
        "id": 3,
        "model": {
            "research": {
                "roles": {},
                "errors": {
                    "technical": "AI引用校验失败：evidence_ids 含有未提供的证据ID或格式错误。"
                },
            }
        },
    }
    text = citation_diagnostics(report)
    assert "未保存失败角色原文" in text and "无法追溯精确ID" in text
    assert "技术面" in text and "metrics.ma20" not in text


def test_model_context_omits_history_ids_but_stored_report_retains_audit_history(store, ledger):
    packet = {
        "evidence": {"items": [], "excluded": []},
        "previous": {"id": 1, "evidence_versions": {"ev_history": 1}},
    }
    compacted = compact_packet(packet, 10000)
    assert "evidence_versions" not in compacted["previous"]
    assert packet["previous"]["evidence_versions"] == {"ev_history": 1}
    analysis, _, _ = services(store, ledger, FakeModel())
    report = asyncio.run(analysis.analyze("MU", now=NOW))
    second = asyncio.run(analysis.analyze("MU", now=NOW + timedelta(seconds=1)))
    assert second["input"]["previous"]["id"] == report["id"]
    assert "evidence_versions" in second["input"]["previous"]
    assert "evidence_versions" not in analysis.model.calls[-1]["previous"]


def test_single_model_citation_failure_is_recorded_and_event_claim_needs_actual_material(
    store, ledger
):
    data = reply_data()
    data["reasons"][0]["evidence_ids"] = ["metrics.fake"]
    analysis, _, _ = services(store, ledger, FakeModel(data))
    report = asyncio.run(analysis.analyze("MU", now=NOW))
    assert report["status"] == "degraded"
    assert report["model"]["citation_error"]["rejected_values"] == ["metrics.fake"]
    assert "metrics.fake" in citation_diagnostics(report)
    data = reply_data(
        event_risk={
            "level": "high",
            "reason": "只有缺口记录不能证明事件",
            "evidence_ids": ["source_gaps"],
        }
    )
    analysis.model = FakeModel(data)
    report = asyncio.run(analysis.analyze("MU", now=NOW))
    assert report["status"] == "degraded"
    assert any("事件风险不能" in item for item in report["result"]["warnings"])


def test_reference_diagnostic_commands_read_existing_records_without_model_calls(plugin_module):
    async def scenario():
        context = Context(reply_data())
        plugin = plugin_module.StockWatcherPlugin(context, grouped_config())
        try:
            await plugin.initialize()
            plugin.research.analysis.market = FakeMarket()
            plugin.research.analysis.sources = None
            await reply(plugin, "分析一下MU", "analyze")
            before = len(context.requests)
            explicit = await command(plugin, "盯盘 报告 1 引用诊断", "diag-explicit")
            natural, _ = await reply(plugin, "报告1引用诊断", "diag-natural")
            recent, _ = await reply(plugin, "看看引用诊断", "diag-recent")
            assert explicit.startswith("报告 #1 引用诊断")
            assert "引用诊断" in natural and "引用诊断" in recent
            assert len(context.requests) == before
            assert "无法追溯" not in natural  # a single-model successful report has no failed role
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_recorded_role_manifest_matches_the_smaller_actual_input_not_outer_packet(store, ledger):
    model = RoleModel()
    analysis, _ = service(
        store,
        ledger,
        model,
        replace(Settings(), model_max_input_chars=18000, model_daily_token_budget=1000000),
    )
    for index in range(10):
        analysis.evidence.add(
            Evidence(
                instrument="MU",
                url=f"https://example.com/audit/{index}",
                publisher="fixture",
                provider="test",
                kind="news",
                title=f"fixture article {index}",
                content="x" * 3000,
                published_at=NOW.isoformat(),
                observed_at=NOW.isoformat(),
            )
        )
    report = asyncio.run(analysis.analyze("MU", deep=True, now=NOW))
    assert report["status"] == "validated"
    actual = model.calls[0][0]["packet"]["evidence"]["items"]
    recorded = report["model"]["research"]["role_input_evidence"]
    assert [item["evidence_id"] for item in recorded] == [item["evidence_id"] for item in actual]
    assert all(
        info["version"] == item["version"] and info["supplied_chars"] == len(item["content"])
        for info, item in zip(recorded, actual, strict=True)
    )
    outer_ids = {item["evidence_id"] for item in report["input"]["model_input_evidence"]}
    role_ids = {item["evidence_id"] for item in recorded}
    assert role_ids < outer_ids
    allowed = set(report["model"]["research"]["shared_evidence_ids"])
    for removed in outer_ids - role_ids:
        with pytest.raises(CitationError):
            validate_ids([removed], allowed, nonempty=True)
