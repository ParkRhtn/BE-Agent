import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any, env: dict[str, str | None]) -> Any:
    """설정 값 안의 ${이름} 을 .env·환경 변수 값으로 바꾼다. 키는 .env 에 두고 설정 파일은 git 에 올릴 수 있다."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if not env.get(name):
                logger.warning("MCP 설정의 ${%s} 값이 없습니다. .env 에 넣으세요.", name)
            return env.get(name) or ""

        return _VAR.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


def _read_connections(config_path: Path) -> dict | None:
    if not config_path.exists():
        logger.warning("MCP config not found: %s", config_path)
        return None
    env = {**dotenv_values(".env"), **os.environ}
    return _expand(json.loads(config_path.read_text())["mcpServers"], env)


async def load_mcp_tools(config_path: Path | None) -> list[BaseTool]:
    """mcp_servers.json 에 정의된 MCP 서버들의 도구를 불러온다. 실패해도 서버는 뜨도록 빈 목록을 돌려준다."""
    connections = _read_connections(config_path) if config_path else None
    if not connections:
        return []
    try:
        tools = await MultiServerMCPClient(connections).get_tools()
    except Exception:
        logger.exception("Failed to load MCP tools from %s", config_path)
        return []
    logger.info("Loaded %d MCP tools: %s", len(tools), [t.name for t in tools])
    return tools
