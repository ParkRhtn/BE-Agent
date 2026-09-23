import json
import logging
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)


def _read_connections(config_path: Path) -> dict | None:
    if not config_path.exists():
        logger.warning("MCP config not found: %s", config_path)
        return None
    return json.loads(config_path.read_text())["mcpServers"]


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
