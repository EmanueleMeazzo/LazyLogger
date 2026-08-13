from __future__ import annotations

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from src.agent import build_trim_middleware


def _run(middleware, messages):
    """Invoke the middleware's before_model hook the way the agent graph does."""
    return middleware.before_model({"messages": messages}, None)


def _long_exchange(turns: int) -> list:
    """``turns`` human/AI pairs, each chunky enough to blow a small token budget."""
    messages: list = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"question {i} " + "padding " * 200))
        messages.append(AIMessage(content=f"answer {i} " + "padding " * 200))
    return messages


def test_no_op_when_under_budget():
    """Nothing is rewritten while the conversation still fits."""
    middleware = build_trim_middleware(60000)
    messages = [HumanMessage(content="hi"), AIMessage(content="hello")]

    assert _run(middleware, messages) is None


def test_trims_and_replaces_state_when_over_budget():
    middleware = build_trim_middleware(5000)
    messages = _long_exchange(20)

    update = _run(middleware, messages)

    assert update is not None
    kept = update["messages"]
    # State is replaced wholesale: a REMOVE_ALL sentinel followed by the survivors.
    assert isinstance(kept[0], RemoveMessage)
    assert kept[0].id == REMOVE_ALL_MESSAGES
    survivors = kept[1:]
    assert 0 < len(survivors) < len(messages)
    # The most recent turn is what survives.
    assert survivors[-1] is messages[-1]


def test_never_returns_an_empty_conversation():
    """A single turn larger than the whole budget leaves state untouched.

    Trimming to nothing would send the model an empty message list, which is a
    worse failure than an oversized request.
    """
    middleware = build_trim_middleware(10)
    messages = [HumanMessage(content="padding " * 5000)]

    assert _run(middleware, messages) is None


def test_window_never_starts_on_a_tool_message():
    """Orphaning a ToolMessage from its tool_call is what makes the API 400."""
    middleware = build_trim_middleware(5000)
    messages: list = []
    for i in range(10):
        messages.append(HumanMessage(content=f"q{i} " + "padding " * 200))
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "search", "args": {"q": str(i)}, "id": f"call-{i}"}
                ],
            )
        )
        messages.append(
            ToolMessage(content="result " + "padding " * 200, tool_call_id=f"call-{i}")
        )
        messages.append(AIMessage(content=f"a{i} " + "padding " * 200))

    update = _run(middleware, messages)

    assert update is not None
    survivors = update["messages"][1:]
    assert survivors, "expected a non-empty trimmed window"
    assert isinstance(survivors[0], HumanMessage)
    # Every surviving tool result still has its originating tool call in the window.
    call_ids = {
        tc["id"]
        for m in survivors
        if isinstance(m, AIMessage)
        for tc in (m.tool_calls or [])
    }
    for msg in survivors:
        if isinstance(msg, ToolMessage):
            assert msg.tool_call_id in call_ids


def test_system_messages_are_not_counted_or_kept():
    """create_agent injects the system prompt outside state; trimming ignores it."""
    middleware = build_trim_middleware(5000)
    messages = [SystemMessage(content="you are a bot"), *_long_exchange(20)]

    update = _run(middleware, messages)

    assert update is not None
    survivors = update["messages"][1:]
    assert not any(isinstance(m, SystemMessage) for m in survivors)
