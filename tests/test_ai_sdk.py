import json
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from be_agent.streaming.ai_sdk import encode_ai_sdk_stream, to_ui_messages
from be_agent.streaming.events import AgentEvent, TextDelta, ToolCallReady, ToolResult


async def _events(*items: AgentEvent) -> AsyncIterator[AgentEvent]:
    for item in items:
        yield item


async def _encode(*items: AgentEvent) -> list:
    chunks = [c async for c in encode_ai_sdk_stream(_events(*items))]
    data = [c.removeprefix("data: ").strip() for c in chunks]
    return [d if d == "[DONE]" else json.loads(d) for d in data]


async def test_text_only_stream() -> None:
    events = await _encode(TextDelta("m1", "안녕"), TextDelta("m1", "하세요"))
    assert [e if isinstance(e, str) else e["type"] for e in events] == [
        "start",
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
        "[DONE]",
    ]


async def test_tool_call_then_answer_uses_two_steps() -> None:
    events = await _encode(
        ToolCallReady("call_1", "get_current_time", {"timezone": "UTC"}),
        ToolResult("call_1", "2026-01-01T00:00:00+00:00"),
        TextDelta("m2", "지금은 자정입니다"),
    )
    types = [e if isinstance(e, str) else e["type"] for e in events]
    assert types == [
        "start",
        "start-step",
        "tool-input-start",
        "tool-input-available",
        "tool-output-available",
        "finish-step",
        "start-step",
        "text-start",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
        "[DONE]",
    ]


def test_to_ui_messages_groups_run_into_one_assistant_message() -> None:
    messages = [
        HumanMessage(content="몇 시야?", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[{"name": "get_current_time", "args": {}, "id": "call_1"}]),
        ToolMessage(content="12:00", tool_call_id="call_1"),
        AIMessage(content="12시입니다", id="a2"),
    ]
    ui = to_ui_messages(messages)
    assert [m["role"] for m in ui] == ["user", "assistant"]
    parts = ui[1]["parts"]
    assert parts[1] == {
        "type": "tool-get_current_time",
        "toolCallId": "call_1",
        "state": "output-available",
        "input": {},
        "output": "12:00",
    }
    assert parts[-1] == {"type": "text", "text": "12시입니다", "state": "done"}
