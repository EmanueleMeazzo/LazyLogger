from __future__ import annotations

import asyncio
import re
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
    from .link_extractor import LinkExtractionResult, LinkExtractor

logger = structlog.get_logger()

REQUEST_PREFIXES = {
    "add",
    "append",
    "create",
    "search",
    "read",
    "show",
    "find",
    "summarize",
    "summarise",
    "update",
    "organize",
    "organise",
    "what",
    "when",
    "where",
    "why",
    "how",
    "can",
    "could",
    "should",
    "would",
    "do",
    "does",
    "did",
    "is",
    "are",
    "was",
    "were",
    "please",
}


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


async def _reply_with_typing(update: Update, text: str) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    await _send_response(update, text)


def _build_link_capture_prompt(result: LinkExtractionResult) -> str:
    daily_path = today_daily_note_path()
    captured_at = result.captured_at
    title = result.title.replace("\n", " ").strip()
    return (
        "Process this captured web link and save it into Obsidian.\n\n"
        f"- Original URL: {result.url}\n"
        f"- Canonical URL: {result.canonical_url}\n"
        f"- Title candidate: {title}\n"
        f"- Captured at (UTC): {captured_at}\n"
        f"- Link note target path: {result.note_path}\n"
        f"- Daily note path for backlink: {daily_path}\n\n"
        "Required actions:\n"
        "1) Create or update the link note at the target path.\n"
        "2) In that link note, store:\n"
        "   - Title\n"
        "   - Source URL\n"
        "   - Captured timestamp\n"
        "   - A concise synopsis (3-5 bullet points) based only on the extracted content below\n"
        "   - Tags: #link #synopsis\n"
        "3) In today's daily note, append under a `## Links` section a bullet with:\n"
        "   - URL\n"
        "   - wikilink to the dedicated link note\n"
        "   - one-line synopsis\n"
        "4) Confirm what was written and where.\n\n"
        "Extracted content begins below:\n"
        "---\n"
        f"{result.extracted_text}\n"
        "---"
    )


def _build_link_extraction_error_prompt(result: LinkExtractionResult) -> str:
    return (
        "A link was received but extraction failed.\n"
        f"URL: {result.url}\n"
        f"Error: {result.error or 'unknown error'}\n"
        "Respond with a short explanation and suggest sending another link."
    )


def _is_direct_request(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    if "?" in stripped:
        return True

    first_word_match = re.match(r"^[A-Za-z]+", stripped)
    if first_word_match:
        return first_word_match.group(0).lower() in REQUEST_PREFIXES

    return False


def _build_memory_capture_prompt(text: str) -> str:
    daily_path = today_daily_note_path()
    return (
        "Treat this user message as a memory entry to store, not as a question to answer.\n\n"
        f"Daily note target path: {daily_path}\n"
        "Required actions:\n"
        "1) Read or create today's daily note at the target path.\n"
        "2) Append this message under `## Notes` as a concise bullet memory with a timestamp.\n"
        "3) Do not perform extra tasks unless explicitly requested.\n"
        "4) Confirm the memory was stored.\n\n"
        "Memory content:\n"
        f"{text.strip()}"
    )


# --- Command Handlers ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await _reply_with_typing(update, "Sorry, I'm not available for public use.")
        return

    await _reply_with_typing(
        update,
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
        await _reply_with_typing(update, "Sorry, I'm not available for public use.")
        return

    await _reply_with_typing(
        update,
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
        await _reply_with_typing(update, "Sorry, I'm not available for public use.")
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
        await _reply_with_typing(
            update,
            "I'm having trouble accessing the vault right now. Please try again."
        )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await _reply_with_typing(update, "Sorry, I'm not available for public use.")
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        await _reply_with_typing(update, "Usage: /search <query>")
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
        await _reply_with_typing(
            update,
            "I'm having trouble searching right now. Please try again."
        )


async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await _reply_with_typing(update, "Sorry, I'm not available for public use.")
        return

    path = " ".join(context.args) if context.args else ""
    if not path:
        await _reply_with_typing(update, "Usage: /read <path/to/note>")
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
        await _reply_with_typing(
            update,
            "I can't access the vault right now. Please try again."
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _check_authorized(update, settings):
        await _reply_with_typing(update, "Sorry, I'm not available for public use.")
        return

    tool_names = [t.name for t in context.application.bot_data.get("tools", [])]
    await _reply_with_typing(
        update,
        "Status: Running\n"
        f"MCP tools loaded: {len(tool_names)}\n"
        f"Tools: {', '.join(tool_names) if tool_names else 'none'}"
    )


# --- Natural Language Handler ---


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user_id = update.effective_user.id

    if not _check_authorized(update, settings):
        await _reply_with_typing(update, "Sorry, I'm not available for public use.")
        return

    text = update.message.text
    if not text:
        return

    agent: CompiledGraph = context.application.bot_data["agent"]
    link_extractor: LinkExtractor | None = context.application.bot_data.get(
        "link_extractor"
    )
    await update.message.chat.send_action(ChatAction.TYPING)

    logger.info("Received message", user_id=user_id, text_length=len(text))

    try:
        if settings.url_extraction_enabled and link_extractor:
            urls = link_extractor.extract_urls(text)
            if urls:
                logger.info("Processing links", user_id=user_id, url_count=len(urls))
                responses: list[str] = []
                for url in urls[: settings.url_extraction_max_urls_per_message]:
                    extraction = await link_extractor.extract(url)
                    prompt = (
                        _build_link_capture_prompt(extraction)
                        if extraction.success
                        else _build_link_extraction_error_prompt(extraction)
                    )
                    response = await _invoke_agent(agent, update.effective_chat.id, prompt)
                    responses.append(response)

                await _send_response(update, "\n\n".join(responses))
                return

        if not _is_direct_request(text):
            prompt = _build_memory_capture_prompt(text)
            response = await _invoke_agent(agent, update.effective_chat.id, prompt)
            await _send_response(update, response)
            return

        response = await _invoke_agent(agent, update.effective_chat.id, text)
        await _send_response(update, response)
    except Exception:
        logger.exception("Error invoking agent", user_id=user_id)
        await _reply_with_typing(
            update,
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
