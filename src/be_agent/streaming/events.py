"""에이전트 내부 공통 이벤트. 에이전트 코드는 이 이벤트만 만들고, FE 프로토콜 변환은 어댑터가 맡는다."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TextDelta:
    message_id: str
    delta: str


@dataclass(frozen=True, slots=True)
class ToolCallStart:
    tool_call_id: str
    tool_name: str


@dataclass(frozen=True, slots=True)
class ToolCallArgsDelta:
    tool_call_id: str
    delta: str


@dataclass(frozen=True, slots=True)
class ToolCallReady:
    tool_call_id: str
    tool_name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    output: Any
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class RunError:
    message: str


AgentEvent = TextDelta | ToolCallStart | ToolCallArgsDelta | ToolCallReady | ToolResult | RunError
