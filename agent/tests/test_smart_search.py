from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from src.config import Settings
from src.smart_search import (
    _normalize_date,
    _normalize_heading,
    _search_vault_sync,
    _slice_section,
    _snippet,
    _tokenize,
    build_smart_search_tool,
)

# --- Fixtures: a small vault spanning every note type the app writes ---

DAILY = """---
type: daily
created: 2026-06-06T09:00:00+00:00
source: telegram
date: 2026-06-06
day: Saturday
tags: [daily, work]
people: [Sara Rossi]
projects: [Atlas]
---

# 🌿 Daily Note — 2026-06-06 (Saturday)

## ✍️ Notes
- Met Sara about the Atlas roadmap and budget.
### sub-detail
- still inside the notes section
## 🔗 Links
- [[20260510-atlas]]
## ✅ Tasks
- [ ] send the deck
"""

LINK = """---
type: link
created: 2026-05-10T12:00:00+00:00
source: telegram
url: https://example.com/atlas
domain: example.com
title: Atlas Launch Plan
tags: [link, work, planning]
---

# Atlas Launch Plan

- Synopsis bullet about the launch and rollout schedule.
"""

ENTITY = """---
type: entity
created: 2026-06-01T08:00:00+00:00
source: telegram
entity_type: person
aliases: [Sara]
tags: []
---

# Sara Rossi

## Mentions
- [[20260606]] — Atlas roadmap
"""


def _make_vault(tmp_path, files):
    for relpath, content in files.items():
        p = tmp_path / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(tmp_path)


def _search(vault, **overrides):
    params = dict(
        query="",
        note_type=None,
        tags=None,
        people=None,
        projects=None,
        date_from=None,
        date_to=None,
        section=None,
        limit=None,
        max_results=5,
        scan_limit=5000,
    )
    params.update(overrides)
    return _search_vault_sync(vault, **params)


def _typical_vault(tmp_path):
    return _make_vault(
        tmp_path,
        {"2026/06/20260606.md": DAILY, "Links/atlas.md": LINK, "Entities/People/Sara Rossi.md": ENTITY},
    )


def _paths(result):
    return {h["path"] for h in result["hits"]}


# --- _normalize_date ---


class TestNormalizeDate:
    def test_datetime_object(self):
        assert _normalize_date(datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc)) == "2026-06-06"

    def test_date_object(self):
        assert _normalize_date(date(2026, 6, 6)) == "2026-06-06"

    def test_iso_string_with_time(self):
        assert _normalize_date("2026-06-06T09:00:00+00:00") == "2026-06-06"

    def test_iso_date_string(self):
        assert _normalize_date("2026-06-06") == "2026-06-06"

    def test_garbage_returns_none(self):
        assert _normalize_date("not-a-date") is None

    def test_non_padded_is_normalized_or_none(self):
        # Either rejected (None) or coerced to padded — never a misaligned slice.
        assert _normalize_date("2026-6-6") in (None, "2026-06-06")

    def test_none_and_int_return_none(self):
        assert _normalize_date(None) is None
        assert _normalize_date(12345) is None


# --- heading / section slicing ---


class TestNormalizeHeading:
    def test_strips_emoji_and_casefolds(self):
        assert _normalize_heading("## ✍️ Notes") == "notes"
        assert _normalize_heading("notes") == "notes"
        assert _normalize_heading("## 💡 Ideas") == "ideas"
        assert _normalize_heading("## Open") == "open"


class TestSliceSection:
    BODY = "## ✍️ Notes\n- a\n### sub\n- b\n## 🔗 Links\n- c\n"

    def test_slices_until_next_h2_keeping_subheadings(self):
        sliced = _slice_section(self.BODY, "notes")
        assert "- a" in sliced and "### sub" in sliced and "- b" in sliced
        assert "- c" not in sliced  # stopped at the next ## Links

    def test_missing_section_returns_none(self):
        assert _slice_section(self.BODY, "tasks") is None

    def test_first_of_duplicate_headings(self):
        body = "## Notes\nfirst\n## Notes\nsecond\n## Links\nx\n"
        assert _slice_section(body, "notes").strip() == "first"


class TestTokenizeAndSnippet:
    def test_tokenize_lowercases_and_drops_punctuation(self):
        assert _tokenize("Atlas, roadmap!") == ["atlas", "roadmap"]

    def test_snippet_centers_on_query_hit(self):
        text = "intro " * 30 + "the ATLAS budget review " + "outro " * 30
        snip = _snippet(text, ["atlas"], 60)
        assert "atlas" in snip.lower()
        assert len(snip) <= 62  # max_chars + ellipses

    def test_snippet_head_when_no_query(self):
        assert _snippet("hello world", [], 240) == "hello world"


# --- filter arms ---


class TestFilters:
    def test_filter_by_type(self, tmp_path):
        result = _search(_typical_vault(tmp_path), note_type="link")
        assert _paths(result) == {"Links/atlas.md"}

    def test_filter_by_type_is_case_insensitive(self, tmp_path):
        # The docstring asks for lowercase, but the LLM may capitalize — both sides
        # are lowercased, so "LINK"/"Daily" must still match.
        vault = _typical_vault(tmp_path)
        assert _paths(_search(vault, note_type="LINK")) == {"Links/atlas.md"}
        assert _paths(_search(vault, note_type="Daily")) == {"2026/06/20260606.md"}

    def test_filter_by_tags_and_semantics(self, tmp_path):
        vault = _typical_vault(tmp_path)
        # "work" is on both daily and link; case-insensitive.
        assert _paths(_search(vault, tags=["Work"])) == {
            "2026/06/20260606.md",
            "Links/atlas.md",
        }
        # AND across requested tags: only the link has both.
        assert _paths(_search(vault, tags=["work", "planning"])) == {"Links/atlas.md"}

    def test_filter_by_people_case_insensitive(self, tmp_path):
        result = _search(_typical_vault(tmp_path), people=["sara rossi"])
        assert _paths(result) == {"2026/06/20260606.md"}

    def test_filter_by_projects(self, tmp_path):
        result = _search(_typical_vault(tmp_path), projects=["atlas"])
        assert _paths(result) == {"2026/06/20260606.md"}

    def test_date_range_uses_date_then_created(self, tmp_path):
        vault = _typical_vault(tmp_path)
        # June only: daily (date 06-06) + entity (created 06-01); link is 05-10.
        result = _search(vault, date_from="2026-06-01", date_to="2026-06-30")
        assert _paths(result) == {"2026/06/20260606.md", "Entities/People/Sara Rossi.md"}

    def test_date_filter_excludes_note_with_no_date_or_created(self, tmp_path):
        # A note with neither `date` nor `created` has no effective date, so it
        # must drop out of any date-bounded search (but appear when unbounded).
        vault = _make_vault(tmp_path, {"undated.md": "---\ntype: daily\ntags: [x]\n---\n# Undated\n"})
        assert _search(vault)["total_matches"] == 1
        assert _search(vault, date_from="2026-01-01")["total_matches"] == 0

    def test_section_filter_matches_only_notes_with_that_section(self, tmp_path):
        # Only the daily note has a "## ✍️ Notes" section.
        result = _search(_typical_vault(tmp_path), section="notes")
        assert _paths(result) == {"2026/06/20260606.md"}

    def test_absent_section_yields_no_hits(self, tmp_path):
        result = _search(_typical_vault(tmp_path), section="groceries")
        assert result["total_matches"] == 0
        assert result["hits"] == []


# --- ranking ---


class TestRanking:
    def test_bm25_orders_by_relevance(self, tmp_path):
        vault = _make_vault(
            tmp_path,
            {
                "a.md": "atlas atlas atlas roadmap",
                "b.md": "atlas roadmap",
                # filler so "atlas" isn't in a majority of docs (avoids negative IDF)
                "c.md": "unrelated note",
                "d.md": "another filler",
                "e.md": "more filler text",
            },
        )
        result = _search(vault, query="atlas")
        paths = [h["path"] for h in result["hits"]]
        assert paths[0] == "a.md"
        assert "b.md" in paths
        assert result["hits"][0]["score"] > 0

    def test_empty_query_sorts_by_created_desc(self, tmp_path):
        result = _search(_typical_vault(tmp_path))
        paths = [h["path"] for h in result["hits"]]
        # daily 06-06 > entity 06-01 > link 05-10
        assert paths == [
            "2026/06/20260606.md",
            "Entities/People/Sara Rossi.md",
            "Links/atlas.md",
        ]

    def test_query_with_no_matches_returns_no_hits_without_crash(self, tmp_path):
        result = _search(_typical_vault(tmp_path), query="zzzznotpresent")
        assert result["total_matches"] == 3  # all survive the (absent) filters
        assert result["hits"] == []

    def test_blank_note_with_query_is_not_a_hit_and_does_not_crash(self, tmp_path):
        # A note with no tokenizable text can't match a query token (so no hit),
        # and the membership guard means BM25Okapi never sees an empty corpus.
        vault = _make_vault(tmp_path, {"blank.md": "---\ntype: daily\n---\n"})
        result = _search(vault, query="anything")
        assert result["total_matches"] == 1
        assert result["hits"] == []

    def test_single_survivor_with_common_term_still_returned(self, tmp_path):
        # Regression: with one filtered survivor, BM25's IDF for a term present in
        # it is negative — ranking must not drop it on score sign.
        result = _search(_typical_vault(tmp_path), query="atlas", projects=["Atlas"])
        assert _paths(result) == {"2026/06/20260606.md"}


# --- caps, hygiene, edges ---


class TestCapsAndHygiene:
    def test_limit_and_max_results_cap(self, tmp_path):
        vault = _typical_vault(tmp_path)
        assert len(_search(vault, max_results=2)["hits"]) == 2
        assert len(_search(vault, limit=1)["hits"]) == 1
        # limit above max_results is still capped at max_results
        assert len(_search(vault, limit=99, max_results=2)["hits"]) == 2

    def test_falsy_limit_falls_back_to_max_results(self, tmp_path):
        # limit=0 / negative (the LLM could emit either) must fall back to
        # max_results, not return zero hits — that's what `limit and limit > 0` guards.
        vault = _typical_vault(tmp_path)  # 3 notes, max_results default 5
        assert len(_search(vault, limit=0)["hits"]) == 3
        assert len(_search(vault, limit=-3)["hits"]) == 3

    def test_skips_dot_directories(self, tmp_path):
        vault = _make_vault(
            tmp_path,
            {
                "2026/06/20260606.md": DAILY,
                ".obsidian/plugin.md": "---\ntype: daily\n---\n# plugin readme\n",
                ".trash/old.md": "---\ntype: daily\n---\n# deleted\n",
            },
        )
        result = _search(vault, note_type="daily")
        assert _paths(result) == {"2026/06/20260606.md"}

    def test_scan_limit_truncates(self, tmp_path):
        files = {f"n{i}.md": f"---\ntype: daily\n---\nnote {i}\n" for i in range(10)}
        result = _search(_make_vault(tmp_path, files), scan_limit=3)
        assert result["total_matches"] == 3

    def test_missing_vault_returns_empty(self, tmp_path):
        result = _search(str(tmp_path / "does-not-exist"))
        assert result == {"query": "", "total_matches": 0, "hits": []}


# --- tool integration (async) ---


def _settings(vault):
    return Settings(
        _env_file=None,
        azure_openai_endpoint="https://test.openai.azure.com/",
        azure_openai_api_key="k",
        telegram_bot_token="t",
        telegram_authorized_users="alice",
        mcp_vault_path=vault,
    )


@pytest.mark.asyncio
async def test_tool_returns_json_shape(tmp_path):
    vault = _typical_vault(tmp_path)
    tool = build_smart_search_tool(_settings(vault))
    assert tool.name == "smart_vault_search"

    raw = await tool.ainvoke({"query": "atlas", "projects": ["Atlas"]})
    payload = json.loads(raw)
    assert payload["query"] == "atlas"
    assert payload["total_matches"] >= 1
    hit = payload["hits"][0]
    assert set(hit) == {"path", "type", "score", "section", "snippet", "tags"}
    assert hit["path"] == "2026/06/20260606.md"
    assert isinstance(hit["score"], float)


@pytest.mark.asyncio
async def test_tool_empty_query_is_valid(tmp_path):
    vault = _typical_vault(tmp_path)
    tool = build_smart_search_tool(_settings(vault))
    payload = json.loads(await tool.ainvoke({"note_type": "link"}))
    assert [h["path"] for h in payload["hits"]] == ["Links/atlas.md"]
