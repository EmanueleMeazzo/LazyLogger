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


# --- Phase 2: entity resolution + catalog ---


def test_resolve_entities_marks_new_and_existing(tmp_path):
    proj_dir = tmp_path / "Entities" / "Projects"
    proj_dir.mkdir(parents=True)
    (proj_dir / "Atlas.md").write_text("# Atlas\n", encoding="utf-8")

    result = enrichment.EnrichmentResult(people=["Sara"], projects=["Atlas"])
    resolved = enrichment.resolve_entities(result, "Entities", str(tmp_path))

    by_name = {e.name: e for e in resolved}
    assert by_name["Sara"].entity_type == "person"
    assert by_name["Sara"].path == "Entities/People/Sara.md"
    assert by_name["Sara"].is_new is True
    assert by_name["Atlas"].entity_type == "project"
    assert by_name["Atlas"].path == "Entities/Projects/Atlas.md"
    assert by_name["Atlas"].is_new is False


def test_resolve_entities_sanitizes_and_dedups(tmp_path):
    # "Sara/Rossi" sanitizes to "Sara Rossi", colliding with the explicit entry.
    result = enrichment.EnrichmentResult(people=["Sara/Rossi", "Sara Rossi"])
    resolved = enrichment.resolve_entities(result, "Entities", str(tmp_path))
    assert [e.path for e in resolved] == ["Entities/People/Sara Rossi.md"]


def test_resolve_entities_dedups_case_variants(tmp_path):
    # Case variants of one name must collapse to a single hub note (they would
    # collide on case-insensitive filesystems and split the graph otherwise).
    result = enrichment.EnrichmentResult(people=["Sara", "sara", "SARA"])
    resolved = enrichment.resolve_entities(result, "Entities", str(tmp_path))
    assert [e.path for e in resolved] == ["Entities/People/Sara.md"]


def test_scan_entities_reads_h1_and_aliases(tmp_path):
    people = tmp_path / "Entities" / "People"
    people.mkdir(parents=True)
    (people / "Sara Rossi.md").write_text(
        "---\ntype: entity\naliases: [Sara, S. Rossi]\n---\n\n# Sara Rossi\n\n## Mentions\n",
        encoding="utf-8",
    )
    projects = tmp_path / "Entities" / "Projects"
    projects.mkdir(parents=True)
    (projects / "Atlas.md").write_text("# Atlas\n", encoding="utf-8")

    catalog = enrichment.scan_entities(str(tmp_path), "Entities")

    assert catalog["people"] == ["Sara Rossi", "Sara", "S. Rossi"]
    assert catalog["projects"] == ["Atlas"]


def test_scan_entities_missing_folders_returns_empty(tmp_path):
    assert enrichment.scan_entities(str(tmp_path), "Entities") == {
        "people": [],
        "projects": [],
    }


def test_entity_catalog_refreshes_on_ttl(tmp_path):
    people = tmp_path / "Entities" / "People"
    people.mkdir(parents=True)
    (people / "Sara.md").write_text("# Sara\n", encoding="utf-8")
    clock = {"t": 0.0}
    cache = enrichment.EntityCatalog(
        str(tmp_path), "Entities", ttl_seconds=30, time_fn=lambda: clock["t"]
    )

    assert cache.get()["people"] == ["Sara"]

    (people / "Mike.md").write_text("# Mike\n", encoding="utf-8")
    clock["t"] = 10.0
    assert cache.get()["people"] == ["Sara"]  # cached within TTL

    clock["t"] = 100.0
    assert set(cache.get()["people"]) == {"Sara", "Mike"}


def test_build_user_content_lists_known_entities():
    content = enrichment._build_user_content(
        "met Sara", ["work"], {"people": ["Sara Rossi"], "projects": []}
    )
    assert "Known people" in content
    assert "Sara Rossi" in content
    # An empty kind is omitted entirely.
    assert "Known projects" not in content


@pytest.mark.asyncio
async def test_enrich_capture_passes_existing_entities_to_model():
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
    )
    create_mock = AsyncMock(return_value=response)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )

    await enrichment.enrich_capture(
        client,
        "gpt-5",
        "met Sara about Atlas",
        taxonomy=[],
        existing_entities={"people": ["Sara Rossi"], "projects": ["Atlas"]},
    )

    kwargs = create_mock.await_args.kwargs
    user_content = kwargs["messages"][1]["content"]
    assert "Sara Rossi" in user_content
    assert "Atlas" in user_content
    # gpt-5 deployments reject max_tokens/temperature — pin the SDK contract.
    assert kwargs["max_completion_tokens"] == 400
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs
