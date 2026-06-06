"""Capture enrichment: vault tag taxonomy scanning + LLM metadata extraction.

The agent writes the markdown; this module only *prepares* structured data
(tags now, plus people/projects/topics/tasks for later phases) from a single
Azure call, mirroring the `_analyze_photo_with_azure` pattern in telegram_bot.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import structlog

if TYPE_CHECKING:
    from openai import AsyncAzureOpenAI

logger = structlog.get_logger()

# A leading `#tag` (not a markdown heading like `## Notes`, which is `#` + `#` + space).
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9][\w/-]*)")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_FM_TAGS_INLINE_RE = re.compile(r"^tags:\s*\[(.*?)\]\s*$", re.MULTILINE)
_FM_TAGS_BLOCK_RE = re.compile(r"^tags:\s*\n((?:\s*-\s*.+\n?)+)", re.MULTILINE)

ENRICHMENT_SYSTEM_PROMPT = (
    "You extract structured metadata from a single personal note-taking message. "
    "Return ONLY a JSON object with exactly these keys: tags, people, projects, topics, tasks. "
    "Each value is an array of short strings (use [] when there is nothing to extract). "
    "Guidance:\n"
    "- tags: 1-5 lowercase topical tags WITHOUT a leading '#'. Prefer reusing the provided "
    "existing tags when they genuinely fit; otherwise propose concise new ones.\n"
    "- people: proper names of individual people mentioned.\n"
    "- projects: named projects, initiatives, or products.\n"
    "- topics: key subjects or concepts.\n"
    "- tasks: explicit to-dos / action items the user needs to do, phrased as short imperatives.\n"
    "Do not invent information that is not in the message."
)


@dataclass
class EnrichmentResult:
    tags: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)


def _extract_tags_from_markdown(text: str) -> list[str]:
    """Collect frontmatter `tags:` plus inline `#tags` from one note's text."""
    tags: list[str] = []

    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        frontmatter = fm_match.group(1)
        inline = _FM_TAGS_INLINE_RE.search(frontmatter)
        if inline:
            tags.extend(_split_inline_list(inline.group(1)))
        else:
            block = _FM_TAGS_BLOCK_RE.search(frontmatter)
            if block:
                for line in block.group(1).splitlines():
                    item = line.strip().lstrip("-").strip().strip("\"'")
                    if item:
                        tags.append(item)
        body = text[fm_match.end():]
    else:
        body = text

    tags.extend(_INLINE_TAG_RE.findall(body))
    return [t.strip().lower() for t in tags if t.strip()]


def _split_inline_list(raw: str) -> list[str]:
    return [item.strip().strip("\"'") for item in raw.split(",") if item.strip()]


def scan_taxonomy(vault_path: str, limit: int = 60) -> list[str]:
    """Return the most frequent tags across the vault's markdown notes."""
    root = Path(vault_path)
    if not root.exists():
        return []

    counter: Counter[str] = Counter()
    for md in root.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        counter.update(_extract_tags_from_markdown(text))

    return [tag for tag, _ in counter.most_common(limit)]


class TaxonomyCache:
    """In-memory, TTL-bounded cache of the vault's tag taxonomy."""

    def __init__(
        self,
        vault_path: str,
        limit: int,
        ttl_seconds: int,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._vault_path = vault_path
        self._limit = limit
        self._ttl = ttl_seconds
        self._time_fn = time_fn
        self._cache: list[str] | None = None
        self._loaded_at = 0.0

    def get(self) -> list[str]:
        now = self._time_fn()
        if self._cache is None or (now - self._loaded_at) > self._ttl:
            self._cache = scan_taxonomy(self._vault_path, self._limit)
            self._loaded_at = now
            logger.debug("Taxonomy cache refreshed", tag_count=len(self._cache))
        return self._cache


def _clean_list(value: object, max_items: int = 8, lowercase: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip().lstrip("#").strip()
        if lowercase:
            cleaned = cleaned.lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out[:max_items]


def _loads_json_object(raw: str) -> dict | None:
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def parse_enrichment(raw: str) -> EnrichmentResult:
    data = _loads_json_object(raw)
    if not data:
        return EnrichmentResult()
    return EnrichmentResult(
        tags=_clean_list(data.get("tags"), lowercase=True),
        people=_clean_list(data.get("people")),
        projects=_clean_list(data.get("projects")),
        topics=_clean_list(data.get("topics")),
        tasks=_clean_list(data.get("tasks")),
    )


def _build_user_content(text: str, taxonomy: list[str]) -> str:
    parts = [f"Message:\n{text.strip()}"]
    if taxonomy:
        parts.append(
            "Existing tags you may reuse (choose only the fitting ones): "
            + ", ".join(taxonomy)
        )
    return "\n\n".join(parts)


async def enrich_capture(
    client: AsyncAzureOpenAI,
    deployment: str,
    text: str,
    taxonomy: list[str] | None = None,
) -> EnrichmentResult:
    """Extract tags/people/projects/topics/tasks from a capture (best-effort).

    Never raises: enrichment is optional, so any failure yields an empty result
    and the capture flow proceeds unchanged.
    """
    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": ENRICHMENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_user_content(text, taxonomy or []),
                },
            ],
            # These Azure deployments (gpt-5 family) reject `max_tokens`.
            max_completion_tokens=400,
        )
        raw = response.choices[0].message.content or ""
        return parse_enrichment(raw)
    except Exception:
        logger.warning("Capture enrichment failed; continuing without it", exc_info=True)
        return EnrichmentResult()
