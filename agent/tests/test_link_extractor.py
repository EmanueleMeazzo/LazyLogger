from src.link_extractor import LinkExtractor


class StubSettings:
    url_allow_private_nets = False
    link_notes_folder = "Links"

    def get_allowed_domains(self) -> set[str]:
        return set()

    def get_blocked_domains(self) -> set[str]:
        return set()


def test_extract_urls_deduplicates_and_strips_punctuation():
    extractor = LinkExtractor(StubSettings())
    text = (
        "Check this https://example.com/page, and again https://example.com/page "
        "and https://another.org/test!"
    )

    urls = extractor.extract_urls(text)

    assert urls == ["https://example.com/page", "https://another.org/test"]


def test_slugify_normalizes_and_limits_length():
    extractor = LinkExtractor(StubSettings())
    slug = extractor._slugify("A Very Long Title!!! with symbols ### and many words to trim")

    assert slug.startswith("a-very-long-title-with-symbols-and-many-words-to-trim")
    assert len(slug) <= 60
