from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Callable

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
    from langgraph.graph.state import CompiledStateGraph
    from openai import AsyncAzureOpenAI

    from .config import Settings
    from .link_extractor import LinkExtractionResult, LinkExtractor

logger = structlog.get_logger()

TRANSCRIBED_AUDIO_PREFIX = "[Transcribed audio] "
SUPPORTED_AUDIO_MIME_PREFIX = "audio/"
ATTACHMENT_STEM_MAX_LENGTH = 50
_SAFE_ATTACHMENT_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")

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

_FIRST_WORD_RE = re.compile(r"[A-Za-z]+")


@dataclass
class AttachmentPayload:
    file_name: str
    file_unique_id: str
    mime_type: str
    file_size: int
    file_bytes: bytes
    captured_at: datetime
    caption: str | None = None


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


def _require_auth(handler: Callable) -> Callable:
    """Decorator that rejects unauthorized users before the handler runs."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        settings: Settings = context.application.bot_data["settings"]
        if not _check_authorized(update, settings):
            await _reply_with_typing(update, "Sorry, I'm not available for public use.")
            return
        return await handler(update, context)

    return wrapper


async def _invoke_agent(agent: CompiledStateGraph, chat_id: int, text: str) -> str:
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


async def _invoke_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
) -> None:
    """Send typing indicator, invoke the agent, and reply (with error handling)."""
    agent: CompiledStateGraph = context.application.bot_data["agent"]
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        response = await _invoke_agent(agent, update.effective_chat.id, prompt)
        await _send_response(update, response)
    except Exception:
        logger.exception("Error invoking agent")
        await _reply_with_typing(
            update,
            "I'm having trouble right now. Please try again in a moment.",
        )


async def _send_response(update: Update, text: str) -> None:
    """Send a response, splitting into multiple messages if needed."""
    for chunk in split_message(text):
        await update.message.reply_text(chunk)


async def _reply_with_typing(update: Update, text: str) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    await _send_response(update, text)


async def _download_audio_for_transcription(
    update: Update,
) -> tuple[bytes, str, str] | None:
    message = update.message
    if not message:
        return None

    file_id: str | None = None
    filename = "audio_input"
    mime_type = "application/octet-stream"

    if message.voice:
        file_id = message.voice.file_id
        filename = "voice.ogg"
        mime_type = message.voice.mime_type or "audio/ogg"
    elif message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or "audio_input.mp3"
        mime_type = message.audio.mime_type or "audio/mpeg"
    elif message.document and message.document.mime_type:
        if message.document.mime_type.startswith(SUPPORTED_AUDIO_MIME_PREFIX):
            file_id = message.document.file_id
            filename = message.document.file_name or "audio_document"
            mime_type = message.document.mime_type

    if not file_id:
        return None

    telegram_file = await message.get_bot().get_file(file_id)
    file_bytes = bytes(await telegram_file.download_as_bytearray())
    return file_bytes, filename, mime_type


async def _download_non_audio_document(update: Update) -> AttachmentPayload | None:
    message = update.message
    if not message or not message.document:
        return None

    document = message.document
    mime_type = document.mime_type or "application/octet-stream"
    if mime_type.startswith(SUPPORTED_AUDIO_MIME_PREFIX):
        return None

    filename = document.file_name or "attachment"
    if "." not in filename:
        guessed_ext = mimetypes.guess_extension(mime_type) or ".bin"
        filename = f"{filename}{guessed_ext}"

    telegram_file = await message.get_bot().get_file(document.file_id)
    file_bytes = bytes(await telegram_file.download_as_bytearray())
    captured_at = message.date or datetime.now(tz=timezone.utc)

    return AttachmentPayload(
        file_name=filename,
        file_unique_id=document.file_unique_id,
        mime_type=mime_type,
        file_size=document.file_size or len(file_bytes),
        file_bytes=file_bytes,
        captured_at=captured_at,
        caption=message.caption,
    )


async def _download_photo_attachment(update: Update) -> AttachmentPayload | None:
    message = update.message
    if not message or not message.photo:
        return None

    # Telegram sends multiple sizes; use the last (largest) variant.
    photo = message.photo[-1]
    mime_type = "image/jpeg"
    filename = f"photo_{photo.file_unique_id}.jpg"

    telegram_file = await message.get_bot().get_file(photo.file_id)
    file_bytes = bytes(await telegram_file.download_as_bytearray())
    captured_at = message.date or datetime.now(tz=timezone.utc)

    return AttachmentPayload(
        file_name=filename,
        file_unique_id=photo.file_unique_id,
        mime_type=mime_type,
        file_size=photo.file_size or len(file_bytes),
        file_bytes=file_bytes,
        captured_at=captured_at,
        caption=message.caption,
    )


def _sanitize_attachment_stem(filename: str) -> str:
    stem = Path(filename).stem or "attachment"
    ascii_stem = (
        unicodedata.normalize("NFKD", stem)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_stem).strip(".-").lower()
    if not safe:
        return "attachment"
    return safe[:ATTACHMENT_STEM_MAX_LENGTH]


def _safe_attachment_extension(filename: str, mime_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if not suffix:
        suffix = mimetypes.guess_extension(mime_type) or ".bin"
    if not _SAFE_ATTACHMENT_EXT_RE.match(suffix):
        return ".bin"
    return suffix


def _persist_attachment_to_vault(settings: Settings, attachment: AttachmentPayload) -> str:
    captured_utc = attachment.captured_at.astimezone(timezone.utc)
    stem = _sanitize_attachment_stem(attachment.file_name)
    ext = _safe_attachment_extension(attachment.file_name, attachment.mime_type)
    unique_id = re.sub(r"[^A-Za-z0-9]", "", attachment.file_unique_id or "")[:8]
    if not unique_id:
        unique_id = hashlib.sha1(attachment.file_bytes).hexdigest()[:8]

    filename = f"{captured_utc:%Y%m%d-%H%M%S}-{stem}-{unique_id}{ext}"
    relative_path = (
        f"{settings.attachments_folder}/{captured_utc:%Y}/{captured_utc:%m}/{filename}"
    )
    absolute_path = Path(settings.mcp_vault_path, *relative_path.split("/"))
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_bytes(attachment.file_bytes)
    return relative_path


def _normalize_audio_filename(filename: str, mime_type: str) -> str:
    if "." in filename:
        return filename
    guessed_ext = mimetypes.guess_extension(mime_type) or ".bin"
    return f"{filename}{guessed_ext}"


async def _transcribe_audio_with_azure(
    client: AsyncAzureOpenAI,
    deployment: str,
    audio_bytes: bytes,
    filename: str,
    mime_type: str,
) -> str:
    normalized_filename = _normalize_audio_filename(filename, mime_type)
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = normalized_filename

    transcription = await client.audio.transcriptions.create(
        model=deployment,
        file=audio_file,
    )

    text = (getattr(transcription, "text", "") or "").strip()
    if not text:
        raise ValueError("Transcription returned empty text")
    return text


async def _analyze_photo_with_azure(
    client: AsyncAzureOpenAI,
    deployment: str,
    photo_bytes: bytes,
    mime_type: str,
    caption: str | None = None,
) -> str:
    encoded = base64.b64encode(photo_bytes).decode("ascii")
    prompt_text = (
        "Analyze this image and extract only the core factual information. "
        "Return 2-4 concise bullet points, each on its own line, no markdown heading. "
        "Focus on observable content, text in the image, or actionable context."
    )
    if caption and caption.strip():
        prompt_text += f" User caption context: {caption.strip()}"

    response = await client.chat.completions.create(
        model=deployment,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a precise visual note extraction assistant. "
                    "Only report grounded observations from the image."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                    },
                ],
            },
        ],
        max_tokens=220,
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise ValueError("Photo analysis returned empty text")
    return content


async def _extract_message_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    message = update.message
    if not message:
        return None

    if message.text:
        return message.text

    audio_payload = await _download_audio_for_transcription(update)
    if not audio_payload:
        return None

    audio_bytes, filename, mime_type = audio_payload
    settings: Settings = context.application.bot_data["settings"]
    client: AsyncAzureOpenAI = context.application.bot_data["openai_client"]
    try:
        transcript = await _transcribe_audio_with_azure(
            client=client,
            deployment=settings.azure_openai_transcription_deployment,
            audio_bytes=audio_bytes,
            filename=filename,
            mime_type=mime_type,
        )
        return f"{TRANSCRIBED_AUDIO_PREFIX}{transcript}"
    except Exception:
        logger.exception(
            "Audio transcription failed",
            user_id=update.effective_user.id if update.effective_user else None,
            mime_type=mime_type,
            filename=filename,
            byte_size=len(audio_bytes),
        )
        await _reply_with_typing(
            update,
            "I couldn't transcribe that audio message. Please try a shorter or different audio format.",
        )
        return None


async def _process_user_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    user_id = update.effective_user.id
    agent: CompiledStateGraph = context.application.bot_data["agent"]
    settings: Settings = context.application.bot_data["settings"]
    link_extractor: LinkExtractor | None = context.application.bot_data.get(
        "link_extractor"
    )

    logger.info("Received message", user_id=user_id, text_length=len(text))

    if settings.url_extraction_enabled and link_extractor:
        urls = link_extractor.extract_urls(text)
        if urls:
            logger.info("Processing links", user_id=user_id, url_count=len(urls))
            urls = urls[: settings.url_extraction_max_urls_per_message]

            # Extract all URLs in parallel (independent I/O)
            extractions = await asyncio.gather(
                *(link_extractor.extract(url) for url in urls),
                return_exceptions=True,
            )

            responses: list[str] = []
            for extraction in extractions:
                if isinstance(extraction, BaseException):
                    logger.exception("Link extraction failed", exc_info=extraction)
                    continue
                prompt = (
                    _build_link_capture_prompt(extraction)
                    if extraction.success
                    else _build_link_extraction_error_prompt(extraction)
                )
                response = await _invoke_agent(agent, update.effective_chat.id, prompt)
                responses.append(response)

            if responses:
                await _send_response(update, "\n\n".join(responses))
            return

    if not _is_direct_request(text):
        prompt = _build_memory_capture_prompt(text)
        response = await _invoke_agent(agent, update.effective_chat.id, prompt)
        await _send_response(update, response)
        return

    response = await _invoke_agent(agent, update.effective_chat.id, text)
    await _send_response(update, response)


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

    if stripped.endswith("?"):
        return True

    first_word_match = _FIRST_WORD_RE.search(stripped)
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


def _build_attachment_capture_prompt(
    vault_relative_path: str,
    attachment: AttachmentPayload,
) -> str:
    daily_path = today_daily_note_path()
    caption = (attachment.caption or "").strip()
    caption_line = f"- User caption: {caption}\n" if caption else ""
    return (
        "A user sent a file attachment that was already saved to the vault.\n\n"
        "Do not rewrite or move the file.\n"
        f"- Saved attachment path: {vault_relative_path}\n"
        f"- Original filename: {attachment.file_name}\n"
        f"- MIME type: {attachment.mime_type}\n"
        f"- File size in bytes: {attachment.file_size}\n"
        f"- Received at (UTC): {attachment.captured_at.astimezone(timezone.utc).isoformat()}\n"
        f"{caption_line}"
        f"- Daily note target path: {daily_path}\n\n"
        "Required actions:\n"
        "1) Read or create today's daily note at the target path.\n"
        "2) Ensure there is a `## Attachments` section (create it if missing).\n"
        "3) Append a single bullet in that section using this exact markdown link format:\n"
        f"   - [{attachment.file_name}]({vault_relative_path})\n"
        "4) Keep any caption text short and optional in the same bullet.\n"
        "5) Confirm what was written and where.\n"
    )


def _build_photo_capture_prompt(
    vault_relative_path: str,
    attachment: AttachmentPayload,
    core_info: str,
) -> str:
    daily_path = today_daily_note_path()
    caption = (attachment.caption or "").strip()
    caption_line = f"- User caption: {caption}\n" if caption else ""
    return (
        "A user sent a photo that was already saved to the vault.\n\n"
        "Do not rewrite or move the image file.\n"
        f"- Saved image path: {vault_relative_path}\n"
        f"- Original filename: {attachment.file_name}\n"
        f"- MIME type: {attachment.mime_type}\n"
        f"- File size in bytes: {attachment.file_size}\n"
        f"- Received at (UTC): {attachment.captured_at.astimezone(timezone.utc).isoformat()}\n"
        f"{caption_line}"
        f"- Daily note target path: {daily_path}\n\n"
        "Core info extracted from the image:\n"
        f"{core_info}\n\n"
        "Required actions:\n"
        "1) Read or create today's daily note at the target path.\n"
        "2) Ensure there is a `## Attachments` section and append this exact link bullet:\n"
        f"   - [{attachment.file_name}]({vault_relative_path})\n"
        "3) Ensure there is a `## Notes` section and append a concise bullet that summarizes the extracted core info from the image.\n"
        "4) Keep the summary factual and short; do not invent details beyond the extracted info.\n"
        "5) Confirm what was written and where.\n"
    )


# --- Command Handlers ---


@_require_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


@_require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


@_require_auth
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    path = today_daily_note_path()
    prompt = (
        f"Read today's daily note at '{path}'. "
        "If it doesn't exist, create it using the daily note template."
    )
    await _invoke_and_reply(update, context, prompt)


@_require_auth
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args) if context.args else ""
    if not query:
        await _reply_with_typing(update, "Usage: /search <query>")
        return

    await _invoke_and_reply(update, context, f"Search the vault for: {query}")


@_require_auth
async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    path = " ".join(context.args) if context.args else ""
    if not path:
        await _reply_with_typing(update, "Usage: /read <path/to/note>")
        return

    await _invoke_and_reply(update, context, f"Read the note at: {path}")


@_require_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tool_names = [t.name for t in context.application.bot_data.get("tools", [])]
    await _reply_with_typing(
        update,
        "Status: Running\n"
        f"MCP tools loaded: {len(tool_names)}\n"
        f"Tools: {', '.join(tool_names) if tool_names else 'none'}"
    )


# --- Natural Language Handler ---


@_require_auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo_attachment = await _download_photo_attachment(update)
    if photo_attachment:
        await update.message.chat.send_action(ChatAction.TYPING)
        settings: Settings = context.application.bot_data["settings"]
        agent: CompiledStateGraph = context.application.bot_data["agent"]
        client: AsyncAzureOpenAI = context.application.bot_data["openai_client"]
        try:
            vault_relative_path = _persist_attachment_to_vault(
                settings, photo_attachment
            )
            core_info = await _analyze_photo_with_azure(
                client=client,
                deployment=settings.azure_openai_deployment,
                photo_bytes=photo_attachment.file_bytes,
                mime_type=photo_attachment.mime_type,
                caption=photo_attachment.caption,
            )
            prompt = _build_photo_capture_prompt(
                vault_relative_path=vault_relative_path,
                attachment=photo_attachment,
                core_info=core_info,
            )
            response = await _invoke_agent(agent, update.effective_chat.id, prompt)
            await _send_response(update, response)
        except Exception:
            logger.exception(
                "Photo attachment processing failed",
                user_id=update.effective_user.id if update.effective_user else None,
                file_name=photo_attachment.file_name,
                mime_type=photo_attachment.mime_type,
                byte_size=photo_attachment.file_size,
            )
            await _reply_with_typing(
                update,
                "I couldn't store that photo. Please try sending it again.",
            )
        return

    attachment = await _download_non_audio_document(update)
    if attachment:
        await update.message.chat.send_action(ChatAction.TYPING)
        settings: Settings = context.application.bot_data["settings"]
        agent: CompiledStateGraph = context.application.bot_data["agent"]
        try:
            vault_relative_path = _persist_attachment_to_vault(settings, attachment)
            prompt = _build_attachment_capture_prompt(vault_relative_path, attachment)
            response = await _invoke_agent(agent, update.effective_chat.id, prompt)
            await _send_response(update, response)
        except Exception:
            logger.exception(
                "Attachment processing failed",
                user_id=update.effective_user.id if update.effective_user else None,
                file_name=attachment.file_name,
                mime_type=attachment.mime_type,
                byte_size=attachment.file_size,
            )
            await _reply_with_typing(
                update,
                "I couldn't store that attachment. Please try sending it again.",
            )
        return

    text = await _extract_message_text(update, context)
    if not text:
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        await _process_user_text(update, context, text)
    except Exception:
        logger.exception(
            "Error invoking agent",
            user_id=update.effective_user.id if update.effective_user else None,
        )
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

    # Natural language, audio, photos, and non-audio file attachments
    message_filter = (
        (
            filters.TEXT
            | filters.VOICE
            | filters.AUDIO
            | filters.PHOTO
            | filters.Document.AUDIO
            | filters.Document.ALL
        )
        & ~filters.COMMAND
    )
    app.add_handler(MessageHandler(message_filter, handle_message))

    return app
