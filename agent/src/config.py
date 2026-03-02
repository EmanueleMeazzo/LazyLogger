from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_deployment: str = "gpt-4o"
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

    def get_authorized_users(self) -> set[str]:
        """Parse comma-separated usernames into a normalized set."""
        return {
            u.strip().lower().lstrip("@")
            for u in self.telegram_authorized_users.split(",")
            if u.strip()
        }

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
