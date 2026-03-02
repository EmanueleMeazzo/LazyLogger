from __future__ import annotations

import structlog
from langchain_mcp_adapters.client import MultiServerMCPClient

from .config import Settings

logger = structlog.get_logger()


def create_mcp_client(settings: Settings) -> MultiServerMCPClient:
    """Create a MultiServerMCPClient configured for the Obsidian MCP server."""
    logger.info(
        "Configuring MCP client",
        vault_path=settings.mcp_vault_path,
    )
    return MultiServerMCPClient(
        {
            "obsidian": {
                "command": "npx",
                "args": ["-y", "@mauricio.wolff/mcp-obsidian", settings.mcp_vault_path],
                "transport": "stdio",
            }
        }
    )
