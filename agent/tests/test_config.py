import os

import pytest

from src.config import Settings


class TestSettings:
    def test_validate_authorized_users_valid(self):
        """Comma-separated string passes validation."""
        val = Settings.validate_authorized_users("alice,bob,charlie")
        assert val == "alice,bob,charlie"

    def test_validate_authorized_users_single(self):
        val = Settings.validate_authorized_users("alice")
        assert val == "alice"

    def test_validate_authorized_users_empty_string_raises(self):
        with pytest.raises(ValueError, match="at least one username"):
            Settings.validate_authorized_users("")

    def test_get_authorized_users_parses_and_normalizes(self):
        """get_authorized_users strips @, lowercases, and splits."""
        env = {
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
            "AZURE_OPENAI_API_KEY": "test-key",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_AUTHORIZED_USERS": "@Alice, Bob, @CHARLIE",
        }
        for k, v in env.items():
            os.environ[k] = v
        try:
            s = Settings(_env_file=None)
            assert s.get_authorized_users() == {"alice", "bob", "charlie"}
        finally:
            for k in env:
                os.environ.pop(k, None)

    def test_defaults(self):
        """Verify default values without loading .env."""
        env = {
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
            "AZURE_OPENAI_API_KEY": "test-key",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_AUTHORIZED_USERS": "alice",
        }
        for k, v in env.items():
            os.environ[k] = v
        try:
            s = Settings(_env_file=None)
            assert s.azure_openai_deployment == "gpt-5"
            assert s.azure_openai_transcription_deployment == "whisper-1"
            assert s.llm_max_tokens == 4096
            assert s.health_port == 8080
            assert s.url_extraction_enabled is True
            assert s.url_extractor_backend == "crawl4ai"
            assert s.url_extraction_max_urls_per_message == 3
            assert s.get_authorized_users() == {"alice"}
        finally:
            for k in env:
                os.environ.pop(k, None)

    def test_validate_url_backend_raises_for_invalid_value(self):
        with pytest.raises(ValueError, match="URL_EXTRACTOR_BACKEND"):
            Settings.validate_url_extractor_backend("unsupported")
