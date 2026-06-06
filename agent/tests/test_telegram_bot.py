from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import telegram_bot


class DummySettings:
    telegram_bot_token = "token"


def _make_filter_update(message_payload):
    normalized_payload = {
        "entities": [],
        "text": None,
        "voice": None,
        "audio": None,
        "photo": None,
        "document": None,
        **message_payload,
    }
    message = SimpleNamespace(**normalized_payload)
    return SimpleNamespace(
        message=message,
        effective_message=message,
        channel_post=None,
        edited_channel_post=None,
        edited_message=None,
        business_message=None,
        edited_business_message=None,
    )


@pytest.mark.asyncio
async def test_extract_message_text_returns_prefixed_transcript(monkeypatch):
    fake_update = SimpleNamespace(
        message=SimpleNamespace(text=None),
        effective_user=SimpleNamespace(id=123),
    )
    fake_settings = SimpleNamespace(
        azure_openai_deployment="test-chat-deployment",
        azure_openai_transcription_deployment="test-whisper-deployment",
    )
    fake_context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"settings": fake_settings, "openai_client": object()}
        )
    )

    download_mock = AsyncMock(return_value=(b"audio-bytes", "voice.ogg", "audio/ogg"))
    transcribe_mock = AsyncMock(return_value="hello from audio")

    monkeypatch.setattr(telegram_bot, "_download_audio_for_transcription", download_mock)
    monkeypatch.setattr(telegram_bot, "_transcribe_audio_with_azure", transcribe_mock)

    text = await telegram_bot._extract_message_text(fake_update, fake_context)

    assert text == f"{telegram_bot.TRANSCRIBED_AUDIO_PREFIX}hello from audio"


@pytest.mark.asyncio
async def test_extract_message_text_transcription_failure_replies_and_returns_none(monkeypatch):
    fake_update = SimpleNamespace(
        message=SimpleNamespace(text=None),
        effective_user=SimpleNamespace(id=321),
    )
    fake_context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"settings": object(), "openai_client": object()}
        )
    )

    download_mock = AsyncMock(return_value=(b"audio-bytes", "voice.ogg", "audio/ogg"))
    transcribe_mock = AsyncMock(side_effect=RuntimeError("transcription failed"))
    reply_mock = AsyncMock()

    monkeypatch.setattr(telegram_bot, "_download_audio_for_transcription", download_mock)
    monkeypatch.setattr(telegram_bot, "_transcribe_audio_with_azure", transcribe_mock)
    monkeypatch.setattr(telegram_bot, "_reply_with_typing", reply_mock)

    text = await telegram_bot._extract_message_text(fake_update, fake_context)

    assert text is None
    reply_mock.assert_awaited_once()


def test_build_application_registers_audio_capable_message_handler(monkeypatch):
    added_handlers = []

    class DummyApplication:
        def __init__(self):
            self.bot_data = {}

        def add_handler(self, handler):
            added_handlers.append(handler)

    class DummyBuilder:
        def token(self, _token):
            return self

        def build(self):
            return DummyApplication()

    fake_message_handler = SimpleNamespace(filters=None)

    def fake_message_handler_factory(message_filter, callback):
        fake_message_handler.filters = message_filter
        fake_message_handler.callback = callback
        return fake_message_handler

    monkeypatch.setattr(telegram_bot.Application, "builder", lambda: DummyBuilder())
    monkeypatch.setattr(telegram_bot, "MessageHandler", fake_message_handler_factory)

    app = telegram_bot.build_application(DummySettings())

    assert app.bot_data["settings"].telegram_bot_token == "token"
    assert added_handlers[-1] is fake_message_handler

    for sample in (
        {"text": "hello"},
        {"voice": object()},
        {"audio": object()},
        {"photo": [SimpleNamespace(file_id="ph", file_unique_id="uph")]},
        {"document": SimpleNamespace(mime_type="audio/mpeg")},
        {"document": SimpleNamespace(mime_type="application/pdf")},
    ):
        assert fake_message_handler.filters.check_update(_make_filter_update(sample))


@pytest.mark.asyncio
async def test_download_non_audio_document_returns_attachment_payload():
    fake_download = AsyncMock(return_value=bytearray(b"pdf-bytes"))
    fake_telegram_file = SimpleNamespace(download_as_bytearray=fake_download)
    fake_get_file = AsyncMock(return_value=fake_telegram_file)

    fake_document = SimpleNamespace(
        file_id="doc-id",
        file_unique_id="unique-id",
        file_name="Report",
        mime_type="application/pdf",
        file_size=10,
    )
    fake_message = SimpleNamespace(
        document=fake_document,
        date=datetime(2026, 3, 14, 10, 30, 0, tzinfo=UTC),
        caption="Quarterly update",
        get_bot=lambda: SimpleNamespace(get_file=fake_get_file),
    )
    fake_update = SimpleNamespace(message=fake_message)

    payload = await telegram_bot._download_non_audio_document(fake_update)

    assert payload is not None
    assert payload.file_name == "Report.pdf"
    assert payload.mime_type == "application/pdf"
    assert payload.file_size == 10
    assert payload.file_bytes == b"pdf-bytes"
    assert payload.caption == "Quarterly update"


@pytest.mark.asyncio
async def test_download_non_audio_document_ignores_audio_mime():
    fake_document = SimpleNamespace(
        file_id="doc-id",
        file_unique_id="unique-id",
        file_name="voice.ogg",
        mime_type="audio/ogg",
        file_size=10,
    )
    fake_message = SimpleNamespace(document=fake_document)
    fake_update = SimpleNamespace(message=fake_message)

    payload = await telegram_bot._download_non_audio_document(fake_update)

    assert payload is None


@pytest.mark.asyncio
async def test_download_photo_attachment_returns_payload():
    fake_download = AsyncMock(return_value=bytearray(b"image-bytes"))
    fake_telegram_file = SimpleNamespace(download_as_bytearray=fake_download)
    fake_get_file = AsyncMock(return_value=fake_telegram_file)

    fake_photo = SimpleNamespace(
        file_id="photo-id",
        file_unique_id="photo-unique",
        file_size=11,
    )
    fake_message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="small", file_unique_id="small", file_size=5), fake_photo],
        date=datetime(2026, 3, 14, 11, 45, 0, tzinfo=UTC),
        caption="Whiteboard",
        get_bot=lambda: SimpleNamespace(get_file=fake_get_file),
    )
    fake_update = SimpleNamespace(message=fake_message)

    payload = await telegram_bot._download_photo_attachment(fake_update)

    assert payload is not None
    assert payload.file_name == "photo_photo-unique.jpg"
    assert payload.mime_type == "image/jpeg"
    assert payload.file_size == 11
    assert payload.file_bytes == b"image-bytes"
    assert payload.caption == "Whiteboard"


def test_persist_attachment_to_vault_writes_bytes(tmp_path):
    settings = SimpleNamespace(
        attachments_folder="Attachments",
        mcp_vault_path=str(tmp_path),
    )
    payload = telegram_bot.AttachmentPayload(
        file_name="My Report.pdf",
        file_unique_id="abc-123",
        mime_type="application/pdf",
        file_size=12,
        file_bytes=b"hello world!",
        captured_at=datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC),
        caption=None,
    )

    relative_path = telegram_bot._persist_attachment_to_vault(settings, payload)

    assert relative_path.startswith("Attachments/2026/03/")
    output_path = Path(tmp_path, *relative_path.split("/"))
    assert output_path.exists()
    assert output_path.read_bytes() == b"hello world!"


@pytest.mark.asyncio
async def test_analyze_photo_with_azure_returns_text():
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="- Whiteboard with sprint tasks"))]
    )
    create_mock = AsyncMock(return_value=response)
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )

    text = await telegram_bot._analyze_photo_with_azure(
        client=fake_client,
        deployment="gpt-5",
        photo_bytes=b"image-bytes",
        mime_type="image/jpeg",
        caption="Sprint board",
    )

    assert "Whiteboard" in text
    create_mock.assert_awaited_once()


def test_build_photo_capture_prompt_includes_core_info():
    payload = telegram_bot.AttachmentPayload(
        file_name="photo_u.jpg",
        file_unique_id="u",
        mime_type="image/jpeg",
        file_size=10,
        file_bytes=b"img",
        captured_at=datetime(2026, 3, 14, 13, 0, 0, tzinfo=UTC),
        caption="planning",
    )

    prompt = telegram_bot._build_photo_capture_prompt(
        vault_relative_path="Attachments/2026/03/photo_u.jpg",
        attachment=payload,
        core_info="- Whiteboard mentions launch blockers",
    )

    assert "Core info extracted from the image" in prompt
    assert "launch blockers" in prompt
    assert telegram_bot.SECTION_NOTES in prompt


def test_build_photo_capture_prompt_includes_suggested_tags():
    payload = telegram_bot.AttachmentPayload(
        file_name="photo_u.jpg",
        file_unique_id="u",
        mime_type="image/jpeg",
        file_size=10,
        file_bytes=b"img",
        captured_at=datetime(2026, 3, 14, 13, 0, 0, tzinfo=UTC),
        caption=None,
    )

    prompt = telegram_bot._build_photo_capture_prompt(
        vault_relative_path="Attachments/2026/03/photo_u.jpg",
        attachment=payload,
        core_info="- Whiteboard",
        suggested_tags=["planning", "roadmap"],
    )

    assert "planning, roadmap" in prompt


def test_build_memory_capture_prompt_includes_suggested_tags():
    prompt = telegram_bot._build_memory_capture_prompt("met Sara", ["people", "work"])
    assert telegram_bot.SECTION_NOTES in prompt
    assert "people, work" in prompt
    # Without tags, no tag-merge instruction is added.
    assert "update_frontmatter" not in telegram_bot._build_memory_capture_prompt("hi", [])


@pytest.mark.parametrize(
    "text, expected",
    [
        ("what did I do today?", True),
        ("Search the vault for SOFIA", True),
        ("met Sara about the launch", False),
        ("[Transcribed audio] what is my plan?", True),
        ("[Transcribed audio] remember to call the bank", False),
        ("ask: my notes on SOFIA", True),
        ("? quick lookup", True),
        ("", False),
    ],
)
def test_is_direct_request_routing(text, expected):
    assert telegram_bot._is_direct_request(text) is expected


def test_strip_query_prefix_removes_override():
    assert telegram_bot._strip_query_prefix("ask: my notes on SOFIA") == "my notes on SOFIA"
    assert telegram_bot._strip_query_prefix("plain text") == "plain text"


def test_section_constants_match_system_prompt():
    """The SECTION_* constants must stay identical to the template's headings."""
    prompt_path = Path(__file__).resolve().parent.parent / "system_prompt.md"
    content = prompt_path.read_text(encoding="utf-8")
    for section in (
        telegram_bot.SECTION_NOTES,
        telegram_bot.SECTION_LINKS,
        telegram_bot.SECTION_ATTACHMENTS,
        telegram_bot.SECTION_TASKS,
    ):
        assert section in content, f"{section!r} missing from system_prompt.md"
