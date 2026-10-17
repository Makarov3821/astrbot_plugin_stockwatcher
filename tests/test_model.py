import asyncio
from types import SimpleNamespace

import pytest

from stockwatcher.model import AstrBotModel, ModelUnavailable


def test_reuses_host_provider_without_tools_or_conversation_history():
    class Context:
        async def get_current_chat_provider_id(self, origin):
            assert origin == "session"
            return "configured"

        async def llm_generate(self, **kwargs):
            assert kwargs == {
                "chat_provider_id": "configured",
                "prompt": "packet",
                "system_prompt": "system",
                "contexts": [],
                "tools": None,
            }
            return SimpleNamespace(
                role="assistant",
                completion_text="{}",
                usage=SimpleNamespace(input_other=12, output=3, secret="hidden"),
            )

    reply = asyncio.run(AstrBotModel(Context()).generate("system", "packet", "session"))
    assert reply.metadata["usage"] == {"input_other": 12, "output": 3}
    assert "hidden" not in str(reply)


def test_timeout_provider_error_redacted_and_cancellation_propagates():
    class Context:
        async def llm_generate(self, **kwargs):
            raise RuntimeError("private-key")

    model = AstrBotModel(Context(), provider_id="fixed")
    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(model.generate("s", "p", ""))
    assert "private-key" not in str(captured.value)

    class SlowContext:
        async def llm_generate(self, **kwargs):
            await asyncio.Event().wait()

    async def scenario():
        model = AstrBotModel(SlowContext(), provider_id="fixed", timeout_seconds=0.01)
        with pytest.raises(ModelUnavailable):
            await model.generate("s", "p", "")
        task = asyncio.create_task(model.generate("s", "p", ""))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
