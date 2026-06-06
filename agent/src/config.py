from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_deployment: str = "gpt-5"
    azure_openai_transcription_deployment: str = "whisper-1"
    azure_openai_api_version: str = "2025-03-01-preview"

    # Telegram
    telegram_bot_token: str
    telegram_authorized_users: str  # comma-separated usernames

    # MCP
    mcp_vault_path: str = "/vault"

    # Agent
    system_prompt_path: str = "/app/system_prompt.md"
    llm_max_tokens: int = 4096
    log_level: str = "INFO"
    # Persistent conversation memory (kept off the synced vault on purpose).
    checkpointer_db_path: str = "/data/checkpoints.sqlite"

    # Capture enrichment master switch: one Azure call yields tags AND the
    # people/projects/tasks consumed by entity-linking + task-extraction below.
    enrichment_enabled: bool = True
    taxonomy_scan_limit: int = 60
    taxonomy_cache_ttl_seconds: int = 1800
    enrichment_min_chars: int = 12

    # Entity linking (people/projects → hub notes; topics fold into tags)
    entity_linking_enabled: bool = True
    entities_folder: str = "Entities"
    entity_cache_ttl_seconds: int = 1800

    # Task extraction (daily `## ✅ Tasks` section + central MOC)
    task_extraction_enabled: bool = True
    tasks_moc_path: str = "Tasks/Tasks.md"

    # Smart vault search (structured retrieval tool)
    smart_search_max_results: int = 5
    smart_search_scan_limit: int = 5000

    # URL extraction
    url_extraction_enabled: bool = True
    url_extractor_backend: str = "crawl4ai"
    url_extraction_max_urls_per_message: int = 3
    url_fetch_timeout_seconds: int = 25
    url_fetch_max_chars: int = 12000
    url_allow_private_nets: bool = False
    url_allowed_domains: str = ""
    url_blocked_domains: str = ""
    link_notes_folder: str = "Links"
    attachments_folder: str = "Attachments"

    # Timezone
    user_timezone: str = "UTC"

    # Health server
    health_port: int = 8080

    @field_validator("telegram_authorized_users", mode="after")
    @classmethod
    def validate_authorized_users(cls, v: str) -> str:
        users = [u.strip() for u in v.split(",") if u.strip()]
        if not users:
            raise ValueError(
                "TELEGRAM_AUTHORIZED_USERS must contain at least one username"
            )
        return v

    @field_validator("user_timezone", mode="after")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        normalized = v.strip()
        if normalized.upper() == "UTC":
            return "UTC"
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(
                f"Invalid USER_TIMEZONE: {normalized!r}. "
                "Use IANA timezone names (e.g. 'Europe/Rome', 'UTC')."
            )
        return normalized

    @field_validator("url_extractor_backend", mode="after")
    @classmethod
    def validate_url_extractor_backend(cls, v: str) -> str:
        allowed = {"crawl4ai"}
        normalized = v.strip().lower()
        if normalized not in allowed:
            raise ValueError("URL_EXTRACTOR_BACKEND must be one of: crawl4ai")
        return normalized

    @field_validator(
        "link_notes_folder",
        "attachments_folder",
        "entities_folder",
        "tasks_moc_path",
        mode="after",
    )
    @classmethod
    def validate_vault_relative_folder(cls, v: str) -> str:
        normalized = v.strip().replace("\\", "/").strip("/")
        if not normalized:
            raise ValueError("Folder path cannot be empty")
        parts = [part for part in normalized.split("/") if part]
        if any(part == ".." for part in parts):
            raise ValueError("Folder path cannot contain '..'")
        return "/".join(parts)

    @field_validator(
        "url_extraction_max_urls_per_message",
        "url_fetch_timeout_seconds",
        "url_fetch_max_chars",
        "taxonomy_scan_limit",
        "taxonomy_cache_ttl_seconds",
        "entity_cache_ttl_seconds",
        "smart_search_max_results",
        "smart_search_scan_limit",
        mode="after",
    )
    @classmethod
    def validate_positive_ints(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Value must be a positive integer")
        return v

    def get_authorized_users(self) -> set[str]:
        """Parse comma-separated usernames into a normalized set."""
        return {
            u.strip().lower().lstrip("@")
            for u in self.telegram_authorized_users.split(",")
            if u.strip()
        }

    def get_allowed_domains(self) -> set[str]:
        return {
            d.strip().lower()
            for d in self.url_allowed_domains.split(",")
            if d.strip()
        }

    def get_blocked_domains(self) -> set[str]:
        return {
            d.strip().lower()
            for d in self.url_blocked_domains.split(",")
            if d.strip()
        }

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
