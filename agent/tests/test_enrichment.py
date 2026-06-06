from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import enrichment


def test_scan_taxonomy_counts_frontmatter_and_inline_tags(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntype: daily\ntags: [daily, work]\n---\n\nMet about #work and #project-x.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntags:\n  - work\n  - personal\n---\n\nNo inline tags here.\n",
        encoding="utf-8",
    )
    # A markdown heading must NOT be picked up as a tag.
    (tmp_path / "c.md").write_text("## Notes\nplain body\n", encoding="utf-8")

    tags = enrichment.scan_taxonomy(str(tmp_path), limit=10)

    assert "work" in tags  # appears most often
    assert tags[0] == "work"
    assert "project-x" in tags
    assert "personal" in tags
    assert "notes" not in tags  # the `## Notes` heading is not a tag


def test_scan_taxonomy_missing_vault_returns_empty(tmp_path):
    assert enrichment.scan_taxonomy(str(tmp_path / "does-not-exist")) == []


def test_taxonomy_cache_refreshes_on_ttl(tmp_path):
    (tmp_path / "a.md").write_text("body #alpha\n", encoding="utf-8")
    clock = {"t": 100.0}
    cache = enrichment.TaxonomyCache(
        str(tmp_path), limit=10, ttl_seconds=30, time_fn=lambda: clock["t"]
    )

    assert cache.get() == ["alpha"]

    # Add a new tag; within TTL the cached value is reused.
    (tmp_path / "b.md").write_text("body #beta\n", encoding="utf-8")
    clock["t"] = 120.0
    assert cache.get() == ["alpha"]

    # Past the TTL it rescans.
    clock["t"] = 200.0
    assert set(cache.get()) == {"alpha", "beta"}


def test_parse_enrichment_handles_plain_json():
    result = enrichment.parse_enrichment(
        '{"tags": ["Work", "#work"], "people": ["Sara"], '
        '"projects": ["Atlas"], "topics": [], "tasks": ["send the deck"]}'
    )
    assert result.tags == ["work"]  # deduped + normalized
    assert result.people == ["Sara"]
    assert result.projects == ["Atlas"]
    assert result.tasks == ["send the deck"]


def test_parse_enrichment_handles_code_fence_and_prose():
    raw = "Sure!\n```json\n{\"tags\": [\"a\"], \"tasks\": []}\n```"
    result = enrichment.parse_enrichment(raw)
    assert result.tags == ["a"]


def test_parse_enrichment_garbage_returns_empty():
    result = enrichment.parse_enrichment("not json at all")
    assert result.tags == []
    assert result.people == []


@pytest.mark.asyncio
async def test_enrich_capture_parses_model_output():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"tags": ["work"], "tasks": ["call Sara"]}')
            )
        ]
    )
    create_mock = AsyncMock(return_value=response)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )

    result = await enrichment.enrich_capture(
        client, "gpt-5", "meeting notes", taxonomy=["work"]
    )

    assert result.tags == ["work"]
    assert result.tasks == ["call Sara"]
    create_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_enrich_capture_never_raises_on_api_error():
    create_mock = AsyncMock(side_effect=RuntimeError("boom"))
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )

    result = await enrichment.enrich_capture(client, "gpt-5", "text", taxonomy=[])

    assert result == enrichment.EnrichmentResult()
