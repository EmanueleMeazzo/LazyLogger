from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Telegram message length limit
TELEGRAM_MAX_LENGTH = 4096

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
# Characters illegal in a filename or an Obsidian `[[wikilink]]` target.
_ILLEGAL_NOTE_NAME_RE = re.compile(r'[\\/:*?"<>|#^\[\]]+')


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


def _user_tz() -> timezone | ZoneInfo:
    """Resolve the user's configured timezone (USER_TIMEZONE, default UTC)."""
    tz_name = os.environ.get("USER_TIMEZONE", "UTC")
    return timezone.utc if tz_name.upper() == "UTC" else ZoneInfo(tz_name)


def _now_in_user_tz() -> datetime:
    """Current time in the user's configured timezone."""
    return datetime.now(tz=_user_tz())


def format_local_time(dt: datetime) -> str:
    """Format a timezone-aware datetime as ``HH:MM`` in the user's timezone."""
    return dt.astimezone(_user_tz()).strftime("%H:%M")


def _stem(now: datetime) -> str:
    return f"{now.year}{now.month:02d}{now.day:02d}"


def today_daily_note_stem() -> str:
    """Return today's daily-note basename ``YYYYMMDD`` (no folders, no extension).

    Used for ``[[YYYYMMDD]]`` wikilinks; Obsidian resolves links by basename.
    """
    return _stem(_now_in_user_tz())


def today_daily_note_path() -> str:
    """Return the vault-relative path for today's daily note.

    Format: YYYY/MM/YYYYMMDD.md
    Example: 2026/03/20260302.md

    Timezone is read from the USER_TIMEZONE env var (default: UTC).
    """
    now = _now_in_user_tz()
    return f"{now.year}/{now.month:02d}/{_stem(now)}.md"


def slugify(text: str, fallback: str = "note", max_len: int = 60) -> str:
    """Lowercase, collapse non-alphanumerics to hyphens, trim to ``max_len``.

    Returns ``fallback`` when the input has no slug-able characters. Used for
    link-note filenames (which are referenced by explicit path, not by name).
    """
    normalized = _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    if not normalized:
        return fallback
    return normalized[:max_len]


def sanitize_note_name(name: str, fallback: str = "untitled", max_len: int = 80) -> str:
    """Make a string safe as an Obsidian note basename while keeping it readable.

    Strips characters illegal in filenames or wikilinks and collapses whitespace,
    but—unlike :func:`slugify`—preserves case and spaces so the note resolves from
    a natural ``[[Name]]`` link (e.g. ``"Sara Rossi"`` → ``Sara Rossi.md``).
    """
    cleaned = _ILLEGAL_NOTE_NAME_RE.sub(" ", name)
    # Truncate before the final strip so the slice can't re-expose a trailing
    # space/dot — Windows and Obsidian silently drop those from filenames, which
    # would desync the file from its computed path and `[[Name]]` wikilink.
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")[:max_len].strip(" .")
    if not cleaned:
        return fallback
    return cleaned
