import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from be_agent.core import telegram
from tests.test_workflows import _create, _run, edge, node


class FakeTelegram:
    """텔레그램 봇 API 흉내. 받은 요청과 보낸 메시지를 기록한다."""

    def __init__(self, *, token: str = "123456:good-token", updates: list[dict[str, Any]] | None = None) -> None:
        self.token = token
        self.updates = (
            updates
            if updates is not None
            else [{"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "길동"}}}]
        )
        self.sent: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        _, bot, method = request.url.path.split("/")
        if bot != f"bot{self.token}":
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        body = json.loads(request.content or b"{}")
        match method:
            case "getMe":
                return httpx.Response(
                    200, json={"ok": True, "result": {"username": "my_news_bot", "first_name": "뉴스"}}
                )
            case "getUpdates":
                return httpx.Response(200, json={"ok": True, "result": self.updates})
            case "sendMessage":
                self.sent.append(body)
                return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})


@pytest.fixture
def fake_telegram(monkeypatch: pytest.MonkeyPatch) -> FakeTelegram:
    fake = FakeTelegram()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        telegram.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(fake.handler), **kw)
    )
    return fake


def test_markdown_to_telegram_html() -> None:
    html = telegram.to_html("# 오늘 뉴스\n\n- **삼성** <발표> & [기사](https://ex.com/a?b=1&c=2)\n- `code`")
    assert html == (
        "<b>오늘 뉴스</b>\n\n• <b>삼성</b> &lt;발표&gt; &amp; "
        '<a href="https://ex.com/a?b=1&amp;c=2">기사</a>\n• <code>code</code>'
    )


def test_long_text_is_split() -> None:
    text = "\n\n".join(f"문단 {i} " + "가" * 1000 for i in range(8))
    chunks = telegram.split_text(text)
    assert len(chunks) > 1 and all(len(c) <= 3500 for c in chunks)
    assert "\n\n".join(chunks) == text


def test_connect_test_and_disconnect(client: TestClient, fake_telegram: FakeTelegram) -> None:
    assert client.get("/api/v1/integrations/telegram").json() == {
        "connected": False,
        "bot_username": None,
        "chat_name": None,
        "token_hint": None,
    }

    bad = client.put("/api/v1/integrations/telegram", json={"bot_token": "123456:wrong-token"})
    assert bad.status_code == 400 and "토큰이 올바르지" in bad.json()["detail"]

    ok = client.put("/api/v1/integrations/telegram", json={"bot_token": " 123456:good-token "})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"connected": True, "bot_username": "my_news_bot", "chat_name": "길동", "token_hint": "…oken"}
    assert fake_telegram.sent[-1]["chat_id"] == "42"  # 연결되면 확인 메시지를 보낸다

    assert client.post("/api/v1/integrations/telegram/test").status_code == 204
    assert "테스트" in fake_telegram.sent[-1]["text"]

    assert client.delete("/api/v1/integrations/telegram").status_code == 204
    assert client.get("/api/v1/integrations/telegram").json()["connected"] is False
    assert client.post("/api/v1/integrations/telegram/test").status_code == 404


def test_connect_needs_a_message_to_the_bot(client: TestClient, fake_telegram: FakeTelegram) -> None:
    fake_telegram.updates = []
    response = client.put("/api/v1/integrations/telegram", json={"bot_token": "123456:good-token"})
    assert response.status_code == 400 and "@my_news_bot" in response.json()["detail"]


SEND = (
    [
        node("start", "start"),
        node("tool_1", "tool", tool="send_telegram", args='{"text": "**요약**: {{input}}"}'),
        node("end", "end"),
    ],
    [edge("start", "tool_1"), edge("tool_1", "end")],
)


def test_workflow_sends_to_telegram(client: TestClient, fake_telegram: FakeTelegram) -> None:
    workflow_id = _create(client, *SEND)

    # 연결 전에는 노드가 실패하고 이유를 알려 준다
    events = _run(client, workflow_id, "hello")
    error = next(e for e in events if e["type"] == "node_error")
    assert error["node_id"] == "tool_1" and error["error"].startswith("텔레그램이 연결되어 있지 않습니다")

    client.put("/api/v1/integrations/telegram", json={"bot_token": "123456:good-token"})
    events = _run(client, workflow_id, "hello")
    assert events[-1]["type"] == "run_finish" and "보냈습니다" in events[-1]["output"]
    assert fake_telegram.sent[-1] == {
        "chat_id": "42",
        "text": "<b>요약</b>: hello",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }


def test_tool_is_listed_in_korean(client: TestClient) -> None:
    tools = {t["name"]: t for t in client.get("/api/v1/tools").json()}
    assert tools["send_telegram"]["label"] == "텔레그램 보내기"
