from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from langchain_openai import AzureChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langchain.agents import create_agent

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from langgraph.graph.graph import CompiledGraph

    from .config import Settings

logger = structlog.get_logger()


def load_system_prompt(path: str) -> str:
    """Load the system prompt from a markdown file."""
    text = Path(path).read_text(encoding="utf-8")
    logger.info("Loaded system prompt", path=path, length=len(text))
    return text


def create_llm(settings: Settings) -> AzureChatOpenAI:
    """Create the Azure OpenAI LLM instance."""
    return AzureChatOpenAI(
        azure_deployment=settings.azure_openai_deployment,
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        max_tokens=settings.llm_max_tokens,
    )


def build_agent(
    settings: Settings,
    tools: list[BaseTool],
    system_prompt: str,
) -> CompiledGraph:
    """Create the LangGraph ReAct agent with MCP tools and conversation memory."""
    llm = create_llm(settings)
    memory = InMemorySaver()

    # Let the LLM see tool errors and recover, instead of crashing
    for tool in tools:
        tool.handle_tool_error = True

    agent = create_agent(
        llm,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=memory,
    )

    logger.info(
        "Agent created",
        tool_count=len(tools),
        model=settings.azure_openai_deployment,
    )
    return agent
