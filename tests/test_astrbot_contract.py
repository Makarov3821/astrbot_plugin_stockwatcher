"""Optional checks against real upstream Star/CommandFilter code, with I/O dependencies stubbed.

The shared source is a development reference, never a packaged runtime dependency.
These tests do not represent a running AstrBot instance or platform delivery.
"""

import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "astrbot-temp" / "astrbot"
pytestmark = pytest.mark.skipif(
    not REFERENCE.is_dir(), reason="Optional shared AstrBot source is absent"
)


def load_source(monkeypatch, name, relative):
    spec = importlib.util.spec_from_file_location(name, REFERENCE / relative)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def actual_contract(monkeypatch):
    class Event:
        is_at_or_wake_command = True

        def __init__(self, text):
            self.text, self.extra = text, {}

        def get_message_str(self):
            return self.text

        def set_extra(self, key, value):
            self.extra[key] = value

    for name, fields in {
        "astrbot": {},
        "astrbot.core": {},
        "astrbot.core.star": {},
        "astrbot.core.utils": {},
        "astrbot.core.platform": {},
        "astrbot.core.config": {"AstrBotConfig": dict},
        "astrbot.core.log": {
            "LogManager": types.SimpleNamespace(get_plugin_logger=logging.getLogger)
        },
        "astrbot.core.utils.command_parser": {
            "CommandParserMixin": type("CommandParserMixin", (), {})
        },
        "astrbot.core.utils.plugin_kv_store": {
            "PluginKVStoreMixin": type("PluginKVStoreMixin", (), {})
        },
        "astrbot.core.platform.astr_message_event": {"AstrMessageEvent": Event},
        "astrbot.core.star.star_handler": {"StarHandlerMetadata": types.SimpleNamespace},
        "astrbot.core.star.filter": {"HandlerFilter": type("HandlerFilter", (), {})},
        "astrbot.api": {"AstrBotConfig": dict, "logger": logging.getLogger("contract")},
    }.items():
        module = types.ModuleType(name)
        module.__path__ = []
        for key, value in fields.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    registry = load_source(monkeypatch, "astrbot.core.star.star", "core/star/star.py")
    star = load_source(monkeypatch, "astrbot.core.star.base", "core/star/base.py")
    load_source(
        monkeypatch, "astrbot.core.star.filter.custom_filter", "core/star/filter/custom_filter.py"
    )
    filters = load_source(
        monkeypatch, "astrbot.core.star.filter.command", "core/star/filter/command.py"
    )
    captured = []

    def command(name, **options):
        def decorate(function):
            captured.append(
                filters.CommandFilter(
                    name,
                    alias=options.get("alias"),
                    handler_md=types.SimpleNamespace(handler=function),
                )
            )
            return function

        return decorate

    for name, fields in {
        "astrbot.api.star": {"Star": star.Star, "Context": object, "StarTools": object},
        "astrbot.api.event": {
            "AstrMessageEvent": Event,
            "MessageChain": object,
            "filter": types.SimpleNamespace(
                command=command,
                EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE="private"),
                event_message_type=lambda *args, **kwargs: lambda handler: handler,
            ),
        },
    }.items():
        module = types.ModuleType(name)
        for key, value in fields.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    package = types.ModuleType("stockwatcher_contract")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "stockwatcher_contract", package)
    main = importlib.import_module("stockwatcher_contract.main")
    yield main, registry, captured, Event
    for name in list(sys.modules):
        if name.startswith("stockwatcher_contract."):
            del sys.modules[name]


def test_upstream_star_discovers_main_without_register_decorator(actual_contract):
    main, registry, captured, _ = actual_contract
    assert registry.star_map[main.__name__].star_cls_type is main.StockWatcherPlugin
    assert main.StockWatcherPlugin.stockwatch.__doc__
    assert len(captured) == 1


@pytest.mark.parametrize(
    "text", ["盯盘", '盯盘 设置 MU 理由="等待 回调"', "stockwatch 分析 MU 缓存"]
)
def test_upstream_command_filter_accepts_full_command_and_leaves_quotes_intact(
    actual_contract, text
):
    _, _, captured, Event = actual_contract
    event = Event(text)
    assert captured[0].filter(event, {})
    assert event.extra["parsed_params"] == {}
    assert event.get_message_str() == text
