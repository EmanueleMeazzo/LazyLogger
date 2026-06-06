"""Structured vault retrieval: the ``smart_vault_search`` LangChain tool.

The MCP server's ``search_notes`` is keyword-only. This tool spends the
frontmatter Phases 1-2 wrote — ``type``/``tags``/``people``/``projects``/
``date``/``created`` plus the daily ``## Section`` headings — to filter notes
*before* ranking, then BM25-ranks the survivors. It scans the vault fresh on
every call (the bot is "capture, then ask"; a stale index would miss the note
you just added) and returns a JSON list of hits the agent then ``read_note``s.

Pure Python over the filesystem: the LLM never invents a path. The blocking
walk runs in a worker thread (``asyncio.to_thread``) so the agent event loop
stays free, mirroring the offload pattern in ``telegram_bot._maybe_enrich``.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from langchain_core.tools import tool
from rank_bm25 import BM25Okapi

from .utils import parse_frontmatter, split_frontmatter

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from .config import Settings

logger = structlog.get_logger()

# Snippet window is a presentation detail with no reason to be runtime-tunable.
SNIPPET_CHARS = 240
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _as_list(value: object) -> list[str]:
    """Coerce a parsed frontmatter value to a list of strings (``None`` -> ``[]``)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _norm_set(items: list[str]) -> set[str]:
    """Casefolded, stripped, non-empty set — for case-insensitive filter matching."""
    return {s.strip().casefold() for s in items if str(s).strip()}


def _normalize_date(value: object) -> str | None:
    """Coerce a date-ish value to a zero-padded ``YYYY-MM-DD`` string, else ``None``.

    Handles every shape PyYAML and the LLM produce: ``datetime``/``date`` objects
    (unquoted timestamps), and ``str`` (quoted or tz-offset). A bare ``[:10]``
    slice would misalign on non-zero-padded input like ``2026-6-6``; parsing via
    :meth:`date.fromisoformat` and re-formatting guarantees lexical comparison
    matches chronological order.
    """
    if isinstance(value, datetime):  # check before date — datetime subclasses date
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        candidate = value.strip()
        for attempt in (candidate, candidate[:10]):
            try:
                return date.fromisoformat(attempt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _normalize_heading(text: str) -> str:
    """Reduce a heading (or a requested section name) to comparable text.

    Strips leading ``#``, emoji/symbols, and surrounding whitespace, then
    casefolds — so ``"## ✍️ Notes"`` and a requested ``"notes"`` both become
    ``"notes"``. Keeps only alphanumerics and spaces (drops emoji + punctuation).
    """
    stripped = text.lstrip("#").strip()
    kept = "".join(ch for ch in stripped if ch.isalnum() or ch.isspace())
    return " ".join(kept.split()).casefold()


def _slice_section(body: str, section: str) -> str | None:
    """Return the body under the first matching ``## Section`` heading, or ``None``.

    The slice runs from the line after the matched H2 to the next H2 (a line
    starting with exactly ``"## "``); ``### `` subheadings stay inside. Returns
    ``None`` when no heading matches, so the caller can exclude the note.
    """
    target = _normalize_heading(section)
    if not target:
        # An empty/symbol-only request must not collide with an all-emoji H2
        # (which also normalizes to "") — treat it as "section not found".
        return None
    lines = body.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        if start is None:
            if _normalize_heading(line) == target:
                start = i
        else:
            return "\n".join(lines[start + 1:i]).strip()
    if start is not None:
        return "\n".join(lines[start + 1:]).strip()
    return None


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens for BM25 (Unicode-aware, drops punctuation/markdown)."""
    return _TOKEN_RE.findall(text.lower())


def _snippet(text: str, query_tokens: list[str], max_chars: int) -> str:
    """A whitespace-collapsed window around the first query hit (or the head)."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    if query_tokens:
        lowered = collapsed.lower()
        hits = [pos for tok in query_tokens if (pos := lowered.find(tok)) >= 0]
        if hits:
            start = max(0, min(hits) - max_chars // 3)
            window = collapsed[start:start + max_chars].strip()
            prefix = "…" if start > 0 else ""
            suffix = "…" if start + max_chars < len(collapsed) else ""
            return f"{prefix}{window}{suffix}"
    head = collapsed[:max_chars].strip()
    return f"{head}…" if len(collapsed) > max_chars else head


def _search_vault_sync(
    vault_path: str,
    *,
    query: str,
    note_type: str | None,
    tags: list[str] | None,
    people: list[str] | None,
    projects: list[str] | None,
    date_from: str | None,
    date_to: str | None,
    section: str | None,
    limit: int | None,
    max_results: int,
    scan_limit: int,
) -> dict:
    """Walk the vault, filter on frontmatter, BM25-rank survivors. Blocking I/O."""
    root = Path(vault_path)
    empty = {"query": query, "total_matches": 0, "hits": []}
    if not root.exists():
        logger.warning("smart_vault_search: vault path missing", path=vault_path)
        return empty

    want_type = note_type.strip().lower() if note_type else None
    want_tags = _norm_set(tags or [])
    want_people = _norm_set(people or [])
    want_projects = _norm_set(projects or [])
    bound_from = _normalize_date(date_from) if date_from else None
    bound_to = _normalize_date(date_to) if date_to else None

    survivors: list[dict] = []
    scanned = 0
    truncated = False
    for md in root.rglob("*.md"):
        rel_parts = md.relative_to(root).parts
        # Skip Obsidian/VCS system dirs (.obsidian, .trash, .git, …).
        if any(part.startswith(".") for part in rel_parts):
            continue
        scanned += 1
        if scanned > scan_limit:
            truncated = True
            break
        try:
            raw = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        fm = parse_frontmatter(raw)
        if want_type and str(fm.get("type") or "").strip().lower() != want_type:
            continue
        if want_tags and not want_tags <= _norm_set(_as_list(fm.get("tags"))):
            continue
        if want_people and not want_people <= _norm_set(_as_list(fm.get("people"))):
            continue
        if want_projects and not want_projects <= _norm_set(_as_list(fm.get("projects"))):
            continue

        effective_date = _normalize_date(fm.get("date")) or _normalize_date(fm.get("created"))
        if bound_from or bound_to:
            if effective_date is None:
                continue
            if bound_from and effective_date < bound_from:
                continue
            if bound_to and effective_date > bound_to:
                continue

        body = split_frontmatter(raw)[1]
        if section is not None:
            sliced = _slice_section(body, section)
            if sliced is None:
                continue
            rank_text = sliced
        else:
            rank_text = body

        h1 = _H1_RE.search(body)
        note_tags = _as_list(fm.get("tags"))
        searchable = " ".join(
            filter(
                None,
                [str(fm.get("title") or ""), h1.group(1) if h1 else "", " ".join(note_tags), rank_text],
            )
        )
        survivors.append(
            {
                "path": md.relative_to(root).as_posix(),
                "type": str(fm.get("type") or "") or None,
                "tags": note_tags,
                "created": effective_date or "",
                "rank_text": rank_text,
                "searchable": searchable,
            }
        )

    if truncated:
        logger.warning(
            "smart_vault_search: scan hit scan_limit; results may be partial",
            scan_limit=scan_limit,
        )
    if not survivors:
        return empty

    cap = min(limit if (limit and limit > 0) else max_results, max_results)
    query_tokens = _tokenize(query)

    if not query_tokens:
        # Pure metadata filter — the filter IS the query; surface newest first.
        # Path is a deterministic tie-break so same-date notes don't reorder
        # across runs/platforms (rglob order is filesystem-dependent).
        by_recent = sorted(survivors, key=lambda s: (s["created"], s["path"]), reverse=True)
        ranked = [(s, 0.0) for s in by_recent[:cap]]
    else:
        corpus = [_tokenize(s["searchable"]) for s in survivors]
        wanted = set(query_tokens)
        # A note is a hit only if it actually contains a query token; BM25 then
        # ORDERS the hits. Its score sign is unreliable on small/filtered corpora
        # (a term in the majority of docs yields a negative IDF), so we rank by the
        # score but never drop on it. Membership also guarantees BM25Okapi sees a
        # non-empty corpus with a non-empty doc, so it can't divide by zero.
        matching = [i for i, toks in enumerate(corpus) if wanted.intersection(toks)]
        if not matching:
            ranked = []
        else:
            scores = BM25Okapi(corpus).get_scores(query_tokens)
            ranked = sorted(
                ((survivors[i], float(scores[i])) for i in matching),
                key=lambda pair: (pair[1], pair[0]["path"]),  # path tie-break = deterministic
                reverse=True,
            )[:cap]

    hits = [
        {
            "path": s["path"],
            "type": s["type"],
            "score": round(score, 4),
            "section": section,
            "snippet": _snippet(s["rank_text"], query_tokens, SNIPPET_CHARS),
            "tags": s["tags"],
        }
        for s, score in ranked
    ]
    return {"query": query, "total_matches": len(survivors), "hits": hits}


def build_smart_search_tool(settings: Settings) -> BaseTool:
    """Build the ``smart_vault_search`` tool, closing over the vault path + limits."""
    vault_path = settings.mcp_vault_path
    max_results = settings.smart_search_max_results
    scan_limit = settings.smart_search_scan_limit

    @tool
    async def smart_vault_search(
        query: str = "",
        note_type: str | None = None,
        tags: list[str] | None = None,
        people: list[str] | None = None,
        projects: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        section: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Structured search over the Obsidian vault — filter, then rank by relevance.

        Prefer this over `search_notes` whenever the request implies a filter.
        Combine any of the arguments (they are ANDed together):
        - query: free text for relevance ranking (BM25). Omit for a pure filter.
        - note_type: one of "daily", "link", "attachment", "entity".
        - tags / people / projects: lists; a note must contain ALL requested
          values (case-insensitive). people/projects live on daily notes.
        - date_from / date_to: inclusive "YYYY-MM-DD" bounds on the note's date
          (daily `date`, else the date part of `created`).
        - section: a daily-note section name ("notes", "links", "attachments",
          "tasks", "ideas") or "mentions"/"open" — search only within it.
        - limit: max hits to return.

        Examples:
        - "notes about Project Atlas" -> query="Project Atlas", projects=["Atlas"]
        - "links I tagged work this month" -> note_type="link", tags=["work"],
          date_from="2026-06-01"
        - "what did I capture about Sara" -> query="Sara", people=["Sara"]

        Returns a JSON object {query, total_matches, hits:[{path, type, score,
        section, snippet, tags}]}. read_note the top paths before answering;
        never invent a path.
        """
        result = await asyncio.to_thread(
            _search_vault_sync,
            vault_path,
            query=query or "",
            note_type=note_type,
            tags=tags,
            people=people,
            projects=projects,
            date_from=date_from,
            date_to=date_to,
            section=section,
            limit=limit,
            max_results=max_results,
            scan_limit=scan_limit,
        )
        return json.dumps(result, ensure_ascii=False)

    return smart_vault_search
