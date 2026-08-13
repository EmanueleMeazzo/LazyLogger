from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

from .enrichment import EnrichmentResult, ResolvedEntity, enrich_capture, resolve_entities
from .utils import (
    format_local_time,
    local_date_stem,
    split_message,
    today_daily_note_path,
    today_daily_note_stem,
)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph
    from openai import AsyncAzureOpenAI, AsyncOpenAI

    from .config import Settings
    from .link_extractor import LinkExtractionResult, LinkExtractor

logger = structlog.get_logger()

TRANSCRIBED_AUDIO_PREFIX = "[Transcribed audio] "
SUPPORTED_AUDIO_MIME_PREFIX = "audio/"
ATTACHMENT_STEM_MAX_LENGTH = 50
_SAFE_ATTACHMENT_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")

# Daily-note section headings — kept identical to the template in system_prompt.md
# so the agent appends to existing sections instead of creating duplicates.
SECTION_NOTES = "## ✍️ Notes"
SECTION_LINKS = "## 🔗 Links"
SECTION_ATTACHMENTS = "## 📎 Attachments"
SECTION_TASKS = "## ✅ Tasks"

# Explicit "this is a question/command, not a memory" overrides for routing.
QUERY_OVERRIDE_PREFIXES = ("ask:", "q:", "search:", "find:")

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


def _resolve_thread_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the conversation thread id for this chat's current session.

    A session is a single back-and-forth: it rolls over when the chat has been
    idle for ``CONVERSATION_IDLE_MINUTES`` or when the local date changes. This
    is what keeps the checkpointed history — and therefore the context sent to
    the LLM — bounded; without it a chat accumulates one ever-growing thread.

    Session state lives in ``chat_data`` (in-memory, no persistence configured),
    so a restart simply starts a fresh session.
    """
    settings: Settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    now = (update.message.date if update.message else None) or datetime.now(timezone.utc)
    date_stem = local_date_stem(now)

    session = context.chat_data.get("session")
    if (
        session is None
        or session["date"] != date_stem
        or now - session["last_seen"]
        > timedelta(minutes=settings.conversation_idle_minutes)
    ):
        session = {"id": now.strftime("%Y%m%d-%H%M%S"), "date": date_stem}
    session["last_seen"] = now
    context.chat_data["session"] = session
    return f"{chat_id}:{session['id']}"


async def _invoke_agent(agent: CompiledStateGraph, thread_id: str, text: str) -> str:
    """Invoke the LangGraph agent and return the response text."""
    config = {"configurable": {"thread_id": thread_id}}
    logger.debug("Agent invocation started", thread_id=thread_id, input=text)

    last_content: str = ""

    async def _stream() -> None:
        nonlocal last_content
        async for event in agent.astream(
            {"messages": [{"role": "user", "content": text}]},
            config=config,
            stream_mode="updates",
        ):
            for node_name, node_output in event.items():
                # Middleware nodes emit None when they make no state update
                # (e.g. TrimConversation when the conversation already fits).
                messages = (node_output or {}).get("messages", [])
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
    logger.debug("Agent invocation finished", thread_id=thread_id)
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
        response = await _invoke_agent(
            agent, _resolve_thread_id(update, context), prompt
        )
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
    client: AsyncOpenAI,
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
        # These Azure deployments (gpt-5 family) reject `max_tokens`.
        max_completion_tokens=220,
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
    # Whisper is the one route that can't use the v1 client — see main.async_main.
    client: AsyncAzureOpenAI = context.application.bot_data["transcription_client"]
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
                response = await _invoke_agent(
                    agent, _resolve_thread_id(update, context), prompt
                )
                responses.append(response)

            if responses:
                await _send_response(update, "\n\n".join(responses))
            return

    if not _is_direct_request(text):
        enrichment = await _maybe_enrich(context, text)
        suggested_tags, entities, tasks = _resolve_capture_extras(context, enrichment)
        prompt = _build_memory_capture_prompt(
            text,
            suggested_tags,
            entities,
            tasks,
            settings.tasks_moc_path,
            update.message.date if update.message else None,
        )
        response = await _invoke_agent(
            agent, _resolve_thread_id(update, context), prompt
        )
        await _send_response(update, response)
        return

    response = await _invoke_agent(
        agent, _resolve_thread_id(update, context), _strip_query_prefix(text)
    )
    await _send_response(update, response)


def _build_link_capture_prompt(result: LinkExtractionResult) -> str:
    daily_path = today_daily_note_path()
    captured_at = result.captured_at
    title = result.title.replace("\n", " ").strip()
    return (
        "Process this captured web link and save it into Obsidian.\n\n"
        f"- Original URL: {result.url}\n"
        f"- Canonical URL: {result.canonical_url}\n"
        f"- Domain: {result.domain}\n"
        f"- Title candidate: {title}\n"
        f"- Captured at (UTC): {captured_at}\n"
        f"- Link note target path: {result.note_path}\n"
        f"- Daily note path for backlink: {daily_path}\n\n"
        "Required actions:\n"
        "1) Create or update the link note at the target path. Use `write_note` with a `frontmatter` "
        "object containing: type: link, created (the captured time above), source: telegram, url, "
        "canonical_url, domain, title, and a `tags` list that includes `link` plus any relevant "
        "topical tags drawn from the content.\n"
        "2) In the note body, write a concise synopsis (3-5 bullet points) based only on the "
        "extracted content below.\n"
        f"3) In today's daily note, append under the `{SECTION_LINKS}` section a bullet with the URL, "
        "a wikilink to the dedicated link note, and a one-line synopsis.\n"
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


def _strip_transcribed_prefix(text: str) -> str:
    """Strip the `[Transcribed audio]` marker so routing/enrichment see real content."""
    stripped = text.strip()
    if stripped.startswith(TRANSCRIBED_AUDIO_PREFIX):
        return stripped[len(TRANSCRIBED_AUDIO_PREFIX):].strip()
    return stripped


def _is_direct_request(text: str) -> bool:
    # Route a transcribed question on its real content, not the prefix.
    stripped = _strip_transcribed_prefix(text)
    if not stripped:
        return False

    lowered = stripped.lower()
    if lowered.startswith("?") or any(lowered.startswith(p) for p in QUERY_OVERRIDE_PREFIXES):
        return True

    if stripped.endswith("?"):
        return True

    first_word_match = _FIRST_WORD_RE.search(stripped)
    if first_word_match:
        return first_word_match.group(0).lower() in REQUEST_PREFIXES

    return False


def _strip_query_prefix(text: str) -> str:
    """Remove an explicit query-override prefix before sending to the agent."""
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in QUERY_OVERRIDE_PREFIXES:
        if lowered.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def _format_suggested_tags(tags: list[str] | None) -> str:
    if not tags:
        return ""
    return (
        "\nWhen these suggested tags genuinely fit, merge them into the note's frontmatter "
        "`tags` list (use update_frontmatter with merge: true): "
        f"{', '.join(tags)}.\n"
    )


def _merge_tag_suggestions(tags: list[str], topics: list[str]) -> list[str]:
    """Fold extracted topics into the tag suggestions (lowercased, order-preserving)."""
    normalized = (value.strip().lower() for value in (*tags, *topics))
    return list(dict.fromkeys(value for value in normalized if value))


def _format_entities(entities: list[ResolvedEntity] | None, daily_stem: str) -> str:
    """Instruction block telling the agent to create/append entity hub notes."""
    if not entities:
        return ""
    lines = [
        "\nEntities detected in this capture — connect them into the graph:",
    ]
    for entity in entities:
        if entity.is_new:
            action = (
                "create the entity note (frontmatter type: entity, entity_type: "
                f"{entity.entity_type}, plus a `# {entity.name}` heading and a `## Mentions` section)"
            )
        else:
            action = "open the existing entity note"
        lines.append(
            f"- {entity.entity_type} \"{entity.name}\": {action} at `{entity.path}`, then "
            f"append a `## Mentions` bullet `- [[{daily_stem}]] — <short context>` (never duplicate one)."
        )
    inline = ", ".join(f"[[{entity.name}]]" for entity in entities)
    lines.append(
        f"In today's daily note, wikilink these inline in the memory bullet ({inline}) and merge "
        "their names into the daily-note frontmatter `people`/`projects` lists with "
        "update_frontmatter (merge: true)."
    )
    return "\n".join(lines) + "\n"


def _format_tasks(tasks: list[str] | None, daily_stem: str, moc_path: str | None) -> str:
    """Instruction block for writing extracted to-dos into the day + the Tasks MOC."""
    if not tasks:
        return ""
    rendered = "\n".join(f"  - {task}" for task in tasks)
    return (
        f"\nAction items to record as tasks:\n{rendered}\n"
        f"For each, append `- [ ] <task>  (from [[{daily_stem}]])` under today's "
        f"`{SECTION_TASKS}` section, and also append `- [ ] <task> — [[{daily_stem}]]` under the "
        f"`## Open` section of the tasks map at `{moc_path}` (create that note with an `## Open` "
        "heading if it does not exist). Do not duplicate a task that is already listed.\n"
    )


async def _maybe_enrich(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> EnrichmentResult:
    """Best-effort capture enrichment (tags/people/projects/tasks); never blocks capture."""
    settings: Settings = context.application.bot_data["settings"]
    if not settings.enrichment_enabled:
        return EnrichmentResult()

    cleaned = _strip_transcribed_prefix(text)
    if len(cleaned) < settings.enrichment_min_chars:
        return EnrichmentResult()

    client: AsyncOpenAI = context.application.bot_data["openai_client"]
    taxonomy_cache = context.application.bot_data.get("taxonomy_cache")
    entity_catalog = context.application.bot_data.get("entity_catalog")
    taxonomy = await asyncio.to_thread(taxonomy_cache.get) if taxonomy_cache else []
    existing_entities = (
        await asyncio.to_thread(entity_catalog.get) if entity_catalog else None
    )
    return await enrich_capture(
        client,
        settings.azure_openai_deployment,
        cleaned,
        taxonomy,
        existing_entities,
    )


def _resolve_capture_extras(
    context: ContextTypes.DEFAULT_TYPE,
    enrichment: EnrichmentResult,
) -> tuple[list[str], list[ResolvedEntity], list[str]]:
    """Turn an enrichment result into (suggested_tags, entities, tasks), honoring flags."""
    settings: Settings = context.application.bot_data["settings"]
    suggested_tags = _merge_tag_suggestions(enrichment.tags, enrichment.topics)
    entities = (
        resolve_entities(enrichment, settings.entities_folder, settings.mcp_vault_path)
        if settings.entity_linking_enabled
        else []
    )
    tasks = enrichment.tasks if settings.task_extraction_enabled else []
    return suggested_tags, entities, tasks


def _build_memory_capture_prompt(
    text: str,
    suggested_tags: list[str] | None = None,
    entities: list[ResolvedEntity] | None = None,
    tasks: list[str] | None = None,
    moc_path: str | None = None,
    captured_at: datetime | None = None,
) -> str:
    daily_path = today_daily_note_path()
    daily_stem = today_daily_note_stem()
    time_hint = (
        "Capture time (use it as the bullet's `[HH:MM]` prefix; do not invent a time): "
        f"{format_local_time(captured_at)}\n"
        if captured_at
        else ""
    )
    return (
        "Treat this user message as a memory entry to store, not as a question to answer.\n\n"
        f"Daily note target path: {daily_path}\n"
        f"{time_hint}"
        "Required actions:\n"
        "1) Read or create today's daily note at the target path. When creating it, use the daily "
        "note template (including the YAML frontmatter: type, created, source, date, day, tags).\n"
        f"2) Append the message under `{SECTION_NOTES}` as a concise bullet memory.\n"
        "3) Then apply only the tagging/entity/task instructions below (if any); take no other "
        "actions on the memory's content.\n"
        "4) Confirm briefly what you stored.\n"
        f"{_format_suggested_tags(suggested_tags)}"
        f"{_format_entities(entities, daily_stem)}"
        f"{_format_tasks(tasks, daily_stem, moc_path)}"
        "\nMemory content:\n"
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
        "1) Read or create today's daily note at the target path (when creating it, include the "
        "template YAML frontmatter: type, created, source, date, day, tags).\n"
        f"2) Ensure there is a `{SECTION_ATTACHMENTS}` section (create it only if absent).\n"
        "3) Append a single bullet in that section using this exact markdown link format:\n"
        f"   - [{attachment.file_name}]({vault_relative_path})\n"
        "4) Keep any caption text short and optional in the same bullet.\n"
        "5) Confirm what was written and where.\n"
    )


def _build_photo_capture_prompt(
    vault_relative_path: str,
    attachment: AttachmentPayload,
    core_info: str,
    suggested_tags: list[str] | None = None,
    entities: list[ResolvedEntity] | None = None,
    tasks: list[str] | None = None,
    moc_path: str | None = None,
) -> str:
    daily_path = today_daily_note_path()
    daily_stem = today_daily_note_stem()
    caption = (attachment.caption or "").strip()
    caption_line = f"- User caption: {caption}\n" if caption else ""
    time_hint = (
        "Capture time (use it as the Notes bullet's `[HH:MM]` prefix; do not invent a time): "
        f"{format_local_time(attachment.captured_at)}\n"
        if attachment.captured_at
        else ""
    )
    return (
        "A user sent a photo that was already saved to the vault.\n\n"
        "Do not rewrite or move the image file.\n"
        f"- Saved image path: {vault_relative_path}\n"
        f"- Original filename: {attachment.file_name}\n"
        f"- MIME type: {attachment.mime_type}\n"
        f"- File size in bytes: {attachment.file_size}\n"
        f"- Received at (UTC): {attachment.captured_at.astimezone(timezone.utc).isoformat()}\n"
        f"{caption_line}"
        f"- Daily note target path: {daily_path}\n"
        f"{time_hint}\n"
        "Core info extracted from the image:\n"
        f"{core_info}\n\n"
        "Required actions:\n"
        "1) Read or create today's daily note at the target path (when creating it, include the "
        "template YAML frontmatter: type, created, source, date, day, tags).\n"
        f"2) Ensure there is a `{SECTION_ATTACHMENTS}` section and append this exact link bullet:\n"
        f"   - [{attachment.file_name}]({vault_relative_path})\n"
        f"3) Ensure there is a `{SECTION_NOTES}` section and append a concise bullet, prefixed with the `[HH:MM]` capture time above, that summarizes the extracted core info from the image.\n"
        "4) Keep the summary factual and short; do not invent details beyond the extracted info.\n"
        "5) Confirm what was written and where.\n"
        f"{_format_suggested_tags(suggested_tags)}"
        f"{_format_entities(entities, daily_stem)}"
        f"{_format_tasks(tasks, daily_stem, moc_path)}"
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
        '- "Create a note called Projects/NewIdea"\n\n'
        "Plain statements are saved as memories in today's note. To force a question/lookup "
        'instead, end with "?" or start with "ask:" (e.g. "ask: my notes on SOFIA").'
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

    await _invoke_and_reply(
        update,
        context,
        f"Find notes matching: {query}. Use structured filters "
        "(person/project/tag/type/date/section) when the query implies them.",
    )


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
        client: AsyncOpenAI = context.application.bot_data["openai_client"]
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
            enrichment = await _maybe_enrich(
                context,
                f"{photo_attachment.caption or ''}\n{core_info}".strip(),
            )
            suggested_tags, entities, tasks = _resolve_capture_extras(context, enrichment)
            prompt = _build_photo_capture_prompt(
                vault_relative_path=vault_relative_path,
                attachment=photo_attachment,
                core_info=core_info,
                suggested_tags=suggested_tags,
                entities=entities,
                tasks=tasks,
                moc_path=settings.tasks_moc_path,
            )
            response = await _invoke_agent(
                agent, _resolve_thread_id(update, context), prompt
            )
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
            response = await _invoke_agent(
                agent, _resolve_thread_id(update, context), prompt
            )
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
