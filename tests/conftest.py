import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from be_agent.core.config import Settings
from be_agent.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db",
        checkpoint_sqlite_path=str(tmp_path / "checkpoints.db"),
        default_model="fake:echo",
        mcp_config_path=None,  # 테스트에서 외부 MCP 서버를 띄우지 않는다
    )


@pytest.fixture
def anon_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def login_as(client: TestClient, email: str, password: str = "password123") -> None:
    response = client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"


@pytest.fixture
def client(anon_client: TestClient) -> TestClient:
    """로그인된 클라이언트."""
    login_as(anon_client, "user@example.com")
    return anon_client


def parse_sse(body: str) -> list[Any]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            data = line.removeprefix("data: ")
            events.append(data if data == "[DONE]" else json.loads(data))
    return events
