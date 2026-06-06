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
from typing import TYPE_CHECKING, Callable, Generic, TypeVar

import structlog

from .utils import sanitize_note_name

if TYPE_CHECKING:
    from openai import AsyncAzureOpenAI

logger = structlog.get_logger()

_T = TypeVar("_T")

# A leading `#tag` (not a markdown heading like `## Notes`, which is `#` + `#` + space).
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9][\w/-]*)")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
# A top-level `# Heading` (single hash + space) — used to read an entity note's name.
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# Entity kinds: (singular entity_type, EnrichmentResult attribute / catalog key, vault subfolder).
_ENTITY_KINDS = (
    ("person", "people", "People"),
    ("project", "projects", "Projects"),
)

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
    "When known people/projects are listed with the message, reuse their exact spelling "
    "whenever the message refers to the same entity, so links stay consistent.\n"
    "Do not invent information that is not in the message."
)


@dataclass
class EnrichmentResult:
    tags: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)


@dataclass
class ResolvedEntity:
    """An extracted person/project mapped to its singleton vault note path.

    Paths are computed in Python (never by the LLM); ``is_new`` tells the agent
    whether to create the note or append a mention to an existing one.
    """

    name: str
    entity_type: str  # "person" | "project"
    path: str  # vault-relative, e.g. "Entities/People/sara.md"
    is_new: bool


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a note into ``(frontmatter_text, body)``; ``("", text)`` when none."""
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        return fm_match.group(1), text[fm_match.end():]
    return "", text


def _extract_tags_from_markdown(text: str) -> list[str]:
    """Collect frontmatter `tags:` plus inline `#tags` from one note's text."""
    frontmatter, body = _split_frontmatter(text)
    tags = _fm_list(frontmatter, "tags")
    tags.extend(_INLINE_TAG_RE.findall(body))
    return [t.strip().lower() for t in tags if t.strip()]


def _split_inline_list(raw: str) -> list[str]:
    return [item.strip().strip("\"'") for item in raw.split(",") if item.strip()]


def _fm_list(frontmatter: str, key: str) -> list[str]:
    """Read a YAML list (`key: [a, b]` or a `- ` block) from raw frontmatter text."""
    inline = re.search(rf"^{key}:\s*\[(.*?)\]\s*$", frontmatter, re.MULTILINE)
    if inline:
        return _split_inline_list(inline.group(1))
    block = re.search(rf"^{key}:\s*\n((?:\s*-\s*.+\n?)+)", frontmatter, re.MULTILINE)
    if not block:
        return []
    items: list[str] = []
    for line in block.group(1).splitlines():
        item = line.strip().lstrip("-").strip().strip("\"'")
        if item:
            items.append(item)
    return items


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


class _TTLCache(Generic[_T]):
    """In-memory, TTL-bounded cache around a loader callable."""

    def __init__(
        self,
        loader: Callable[[], _T],
        ttl_seconds: int,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._time_fn = time_fn
        self._cache: _T | None = None
        self._loaded_at = 0.0

    def get(self) -> _T:
        now = self._time_fn()
        if self._cache is None or (now - self._loaded_at) > self._ttl:
            self._cache = self._loader()
            self._loaded_at = now
            logger.debug("Cache refreshed", cache=type(self).__name__)
        return self._cache


class TaxonomyCache(_TTLCache[list[str]]):
    """TTL-bounded cache of the vault's tag taxonomy."""

    def __init__(
        self,
        vault_path: str,
        limit: int,
        ttl_seconds: int,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(lambda: scan_taxonomy(vault_path, limit), ttl_seconds, time_fn)


def resolve_entities(
    result: EnrichmentResult,
    entities_folder: str,
    vault_path: str,
) -> list[ResolvedEntity]:
    """Map enriched people/projects to their singleton entity-note paths.

    Pure Python — the LLM never invents a path. ``is_new`` reflects whether the
    note already exists on disk so the agent knows to create it vs append a mention.
    """
    resolved: list[ResolvedEntity] = []
    seen: set[str] = set()
    for entity_type, attr, subfolder in _ENTITY_KINDS:
        for raw_name in getattr(result, attr):
            name = sanitize_note_name(raw_name, fallback="")
            if not name:
                continue
            path = f"{entities_folder}/{subfolder}/{name}.md"
            # Dedup case-insensitively so case variants of one name don't spawn
            # colliding hub notes (matches scan_entities' name.lower() dedup and
            # case-insensitive filesystems). Key on the full path, not the bare
            # name, so a person and a project sharing a name stay distinct.
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            is_new = not Path(vault_path, *path.split("/")).exists()
            resolved.append(ResolvedEntity(name, entity_type, path, is_new))
    return resolved


def _entity_names(text: str) -> list[str]:
    """Canonical name (first `# ` H1) plus frontmatter `aliases` for one entity note."""
    frontmatter, body = _split_frontmatter(text)
    names: list[str] = []
    h1 = _H1_RE.search(body)
    if h1:
        names.append(h1.group(1))
    names.extend(_fm_list(frontmatter, "aliases"))
    return [n.strip() for n in names if n.strip()]


def scan_entities(vault_path: str, entities_folder: str) -> dict[str, list[str]]:
    """Collect existing entity display names (H1 + aliases) per kind, deduped."""
    catalog: dict[str, list[str]] = {attr: [] for _, attr, _ in _ENTITY_KINDS}
    for _entity_type, attr, subfolder in _ENTITY_KINDS:
        folder = Path(vault_path, *entities_folder.split("/"), subfolder)
        if not folder.exists():
            continue
        names: list[str] = []
        seen: set[str] = set()
        for md in folder.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for name in _entity_names(text):
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    names.append(name)
        catalog[attr] = names
    return catalog


class EntityCatalog(_TTLCache[dict[str, list[str]]]):
    """TTL-bounded cache of existing People/Project names for dedup + normalization."""

    def __init__(
        self,
        vault_path: str,
        entities_folder: str,
        ttl_seconds: int,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            lambda: scan_entities(vault_path, entities_folder), ttl_seconds, time_fn
        )


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


def _build_user_content(
    text: str,
    taxonomy: list[str],
    existing_entities: dict[str, list[str]] | None = None,
) -> str:
    parts = [f"Message:\n{text.strip()}"]
    if taxonomy:
        parts.append(
            "Existing tags you may reuse (choose only the fitting ones): "
            + ", ".join(taxonomy)
        )
    for _entity_type, attr, _subfolder in _ENTITY_KINDS:
        names = (existing_entities or {}).get(attr)
        if names:
            parts.append(
                f"Known {attr} (reuse the exact spelling when the message refers to one): "
                + ", ".join(names)
            )
    return "\n\n".join(parts)


async def enrich_capture(
    client: AsyncAzureOpenAI,
    deployment: str,
    text: str,
    taxonomy: list[str] | None = None,
    existing_entities: dict[str, list[str]] | None = None,
) -> EnrichmentResult:
    """Extract tags/people/projects/topics/tasks from a capture (best-effort).

    Never raises: enrichment is optional, so any failure yields an empty result
    and the capture flow proceeds unchanged. ``existing_entities`` (a
    ``{"people": [...], "projects": [...]}`` map) is passed to the model so it
    reuses canonical names instead of coining near-duplicates.
    """
    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": ENRICHMENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_user_content(text, taxonomy or [], existing_entities),
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
