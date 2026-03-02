from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .utils import split_message, today_daily_note_path

if TYPE_CHECKING:
    from langgraph.graph.graph import CompiledGraph

    from .config import Settings

logger = structlog.get_logger()


def _check_authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user.username and user.username.lower() in settings.get_authorized_users():
        return True
    logger.warning(
        "Unauthorized access attempt",
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    return False


async def _invoke_agent(agent: CompiledGraph, chat_id: int, text: str) -> str:
    """Invoke the LangGraph agent and return the response text."""
    config = {"configurable": {"thread_id": str(chat_id)}}
    logger.debug("Agent invocation started", chat_id=chat_id, input=text)

    last_content: str = ""

    async def _stream() -> None:
        nonlocal last_content
        async for event in agent.astream(
            {"messages": [{"role": "user", "content": text}]},
            config=config,
            stream_mode="updates",
        ):
            for node_name, node_output in event.items():
                messages = node_output.get("messages", [])
                for msg in messages:
                    msg_type = msg.type if hasattr(msg, "type") else type(msg).__name__

                    # LLM decided to call tool(s)
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tc in msg.tool_calls:
                            logger.debug(
                                "Tool call",
                                node=node_name,
                                tool=tc.get("name"),
                                args=tc.get("args"),
                            )

                    # Tool result came back
                    elif msg_type == "tool":
                        content_preview = str(msg.content)[:500]
                        logger.debug(
                            "Tool result",
                            node=node_name,
                            tool=getattr(msg, "name", "?"),
                            content=content_preview,
                        )

                    # Final AI response
                    elif msg_type == "ai" and msg.content:
                        last_content = msg.content
                        logger.debug(
                            "LLM response",
                            node=node_name,
                            content=msg.content[:300],
                        )

    await asyncio.wait_for(_stream(), timeout=120.0)
    logger.debug("Agent invocation finished", chat_id=chat_id)
    return last_content or "I processed your request but have nothing to report."


async def _send_response(update: Update, text: str) -> None:
    """Send a response, splitting into multiple messages if needed."""
    for chunk in split_message(text):
        await update.message.reply_text(chunk)


# --- Command Handlers ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await update.message.reply_text("Sorry, I'm not available for public use.")
        return

    await update.message.reply_text(
        "Hi! I'm your Obsidian vault assistant.\n\n"
        "Send me any message and I'll help you take notes, "
        "search your vault, or organize your thoughts.\n\n"
        "Commands:\n"
        "/today - Show or create today's daily note\n"
        "/search <query> - Search the vault\n"
        "/read <path> - Read a specific note\n"
        "/status - Check agent health\n"
        "/help - Show this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await update.message.reply_text("Sorry, I'm not available for public use.")
        return

    await update.message.reply_text(
        "Available commands:\n"
        "/today - Show or create today's daily note\n"
        "/search <query> - Search the vault\n"
        "/read <path> - Read a specific note\n"
        "/status - Check agent health\n"
        "/help - Show this message\n\n"
        "Or just send a natural language message:\n"
        '- "Add to today\'s notes: meeting with Silvia"\n'
        '- "What did I write about SOFIA last week?"\n'
        '- "Create a note called Projects/NewIdea"'
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await update.message.reply_text("Sorry, I'm not available for public use.")
        return

    agent: CompiledGraph = context.application.bot_data["agent"]
    await update.message.chat.send_action(ChatAction.TYPING)

    path = today_daily_note_path()
    prompt = (
        f"Read today's daily note at '{path}'. "
        "If it doesn't exist, create it using the daily note template."
    )

    try:
        response = await _invoke_agent(agent, update.effective_chat.id, prompt)
        await _send_response(update, response)
    except Exception:
        logger.exception("Error handling /today command")
        await update.message.reply_text(
            "I'm having trouble accessing the vault right now. Please try again."
        )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await update.message.reply_text("Sorry, I'm not available for public use.")
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /search <query>")
        return

    agent: CompiledGraph = context.application.bot_data["agent"]
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        response = await _invoke_agent(
            agent, update.effective_chat.id, f"Search the vault for: {query}"
        )
        await _send_response(update, response)
    except Exception:
        logger.exception("Error handling /search command")
        await update.message.reply_text(
            "I'm having trouble searching right now. Please try again."
        )


async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await update.message.reply_text("Sorry, I'm not available for public use.")
        return

    path = " ".join(context.args) if context.args else ""
    if not path:
        await update.message.reply_text("Usage: /read <path/to/note>")
        return

    agent: CompiledGraph = context.application.bot_data["agent"]
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        response = await _invoke_agent(
            agent, update.effective_chat.id, f"Read the note at: {path}"
        )
        await _send_response(update, response)
    except Exception:
        logger.exception("Error handling /read command")
        await update.message.reply_text(
            "I can't access the vault right now. Please try again."
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await update.message.reply_text("Sorry, I'm not available for public use.")
        return

    tool_names = [t.name for t in context.application.bot_data.get("tools", [])]
    await update.message.reply_text(
        "Status: Running\n"
        f"MCP tools loaded: {len(tool_names)}\n"
        f"Tools: {', '.join(tool_names) if tool_names else 'none'}"
    )


# --- Natural Language Handler ---


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user_id = update.effective_user.id

    if not _check_authorized(update, settings):
        await update.message.reply_text("Sorry, I'm not available for public use.")
        return

    text = update.message.text
    if not text:
        return

    agent: CompiledGraph = context.application.bot_data["agent"]
    await update.message.chat.send_action(ChatAction.TYPING)

    logger.info("Received message", user_id=user_id, text_length=len(text))

    try:
        response = await _invoke_agent(agent, update.effective_chat.id, text)
        await _send_response(update, response)
    except Exception:
        logger.exception("Error invoking agent", user_id=user_id)
        await update.message.reply_text(
            "I'm having trouble thinking right now. Please try again in a moment."
        )


def build_application(settings: Settings) -> Application:
    """Build the Telegram Application with all handlers registered."""
    app = Application.builder().token(settings.telegram_bot_token).build()

    # Store settings in bot_data for access in handlers
    app.bot_data["settings"] = settings

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("status", cmd_status))

    # Natural language fallback (any text that isn't a command)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
