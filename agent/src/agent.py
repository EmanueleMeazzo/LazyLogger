from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from langchain_core.messages import RemoveMessage, trim_messages
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import before_model
from langgraph.graph.message import REMOVE_ALL_MESSAGES

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.tools import BaseTool
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from .config import Settings

logger = structlog.get_logger()


def load_system_prompt(path: str) -> str:
    """Load the system prompt from a markdown file."""
    text = Path(path).read_text(encoding="utf-8")
    logger.info("Loaded system prompt", path=path, length=len(text))
    return text


def create_llm(settings: Settings) -> ChatOpenAI:
    """Create the Azure OpenAI LLM instance (v1 API).

    The v1 API speaks the stock OpenAI protocol, so this is ``ChatOpenAI`` with
    the resource's ``/openai/v1/`` base URL — the deployment name goes in
    ``model``. No ``temperature``: modern deployments reject it.
    """
    return ChatOpenAI(
        base_url=settings.azure_v1_base_url,
        api_key=settings.azure_openai_api_key,
        model=settings.azure_openai_deployment,
        max_tokens=settings.llm_max_tokens,
    )


def build_trim_middleware(max_tokens: int) -> AgentMiddleware:
    """Cap the conversation state at ``max_tokens`` before every model call.

    A backstop only: `telegram_bot._resolve_thread_id` already scopes history to
    a short session, so this should rarely fire. ``start_on="human"`` is what
    keeps it safe — slicing between an AI message and its tool results orphans
    the tool call and the API rejects the request with a 400.
    """

    @before_model(name="TrimConversation")
    def trim_conversation(state, runtime) -> dict | None:
        messages = state["messages"]
        trimmed = trim_messages(
            messages,
            max_tokens=max_tokens,
            token_counter="approximate",
            strategy="last",
            start_on="human",
            include_system=False,  # create_agent injects the system prompt, it isn't in state
        )
        # Empty means even the latest turn blows the budget — leave state alone
        # rather than wipe the conversation and send the model nothing.
        if not trimmed or len(trimmed) == len(messages):
            return None
        logger.info(
            "Trimmed conversation", before=len(messages), after=len(trimmed)
        )
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *trimmed]}

    return trim_conversation


def build_agent(
    settings: Settings,
    tools: list[BaseTool],
    system_prompt: str,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """Create the LangGraph ReAct agent with MCP tools and conversation memory.

    The checkpointer is created and owned by the caller (see main.async_main) so
    its connection lives for the whole process lifetime.
    """
    llm = create_llm(settings)

    # Let the LLM see tool errors and recover, instead of crashing
    for tool in tools:
        tool.handle_tool_error = True

    agent = create_agent(
        llm,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
        middleware=[build_trim_middleware(settings.conversation_max_tokens)],
    )

    logger.info(
        "Agent created",
        tool_count=len(tools),
        model=settings.azure_openai_deployment,
        max_context_tokens=settings.conversation_max_tokens,
    )
    return agent
