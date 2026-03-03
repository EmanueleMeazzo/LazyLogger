from __future__ import annotations

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
        {"document": SimpleNamespace(mime_type="audio/mpeg")},
    ):
        assert fake_message_handler.filters.check_update(_make_filter_update(sample))
