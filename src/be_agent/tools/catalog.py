"""화면에 보여 줄 도구 이름·설명 (한국어).

모델에게 넘기는 설명은 도구가 원래 가진 것(대부분 영어)을 그대로 쓴다. 여기 값은 화면 표시용이다.
목록에 없는 도구(새로 붙인 MCP 등)는 원래 이름과 설명을 그대로 보여 준다.
"""

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDisplay:
    label: str
    description: str
    # 워크플로우 도구 노드에서 이 도구를 고르면 인자 칸에 채워 줄 예시
    example_args: dict[str, Any] | None = None


TOOL_DISPLAY: dict[str, ToolDisplay] = {
    "get_current_time": ToolDisplay(
        "현재 시각", "지정한 지역(시간대)의 현재 날짜와 시각을 알려 줍니다.", {"timezone": "Asia/Seoul"}
    ),
    # DuckDuckGo MCP
    "search": ToolDisplay(
        "웹 검색", "웹을 검색해 제목, 링크, 요약을 가져옵니다.", {"query": "{{input}}", "max_results": 5}
    ),
    "fetch_content": ToolDisplay(
        "웹 페이지 읽기", "링크의 본문을 가져옵니다. 메뉴·광고는 빼고 글만 추립니다.", {"url": "{{input}}"}
    ),
    "expand_link": ToolDisplay(
        "검색 링크 펼치기",
        "검색 결과에 줄여서 나온 링크(ref://…)를 원래 주소로 되돌립니다. 웹 검색과 함께 씁니다.",
        {"token": "{{input}}"},
    ),
}


def display_for(name: str, description: str) -> ToolDisplay:
    return TOOL_DISPLAY.get(name) or ToolDisplay(name, description)


def example_args(name: str, schema: dict[str, Any]) -> str:
    """도구 노드 인자 예시 (JSON). 목록에 없으면 필수 인자를 {{input}} 으로 채운다."""
    known = TOOL_DISPLAY.get(name)
    if known and known.example_args is not None:
        args = known.example_args
    else:
        required = schema.get("required") or []
        args = {key: "{{input}}" for key in required}
    return json.dumps(args, ensure_ascii=False)
