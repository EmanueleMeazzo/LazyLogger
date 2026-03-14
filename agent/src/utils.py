from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Telegram message length limit
TELEGRAM_MAX_LENGTH = 4096


def split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split a long message into chunks that fit within Telegram's limit.

    Splits at paragraph boundaries (double newline) when possible,
    falling back to single newline, then hard cut.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try splitting at double newline
        cut = remaining.rfind("\n\n", 0, max_length)
        if cut == -1:
            # Try single newline
            cut = remaining.rfind("\n", 0, max_length)
        if cut == -1:
            # Hard cut at max length
            cut = max_length

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")

    return chunks


def today_daily_note_path() -> str:
    """Return the vault-relative path for today's daily note.

    Format: YYYY/MM/YYYYMMDD.md
    Example: 2026/03/20260302.md

    Timezone is read from the USER_TIMEZONE env var (default: UTC).
    """
    tz_name = os.environ.get("USER_TIMEZONE", "UTC")
    tz = timezone.utc if tz_name.upper() == "UTC" else ZoneInfo(tz_name)
    now = datetime.now(tz=tz)
    return f"{now.year}/{now.month:02d}/{now.year}{now.month:02d}{now.day:02d}.md"
