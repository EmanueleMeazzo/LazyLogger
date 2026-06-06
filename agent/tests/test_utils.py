from datetime import datetime, timezone
from unittest.mock import patch

from src.utils import (
    format_local_time,
    sanitize_note_name,
    slugify,
    split_message,
    today_daily_note_path,
    today_daily_note_stem,
)


class TestSplitMessage:
    def test_short_message_unchanged(self):
        assert split_message("hello") == ["hello"]

    def test_exact_limit(self):
        text = "a" * 4096
        assert split_message(text) == [text]

    def test_splits_at_double_newline(self):
        chunk1 = "a" * 2000
        chunk2 = "b" * 2000
        text = chunk1 + "\n\n" + chunk2
        result = split_message(text, max_length=2050)
        assert len(result) == 2
        assert result[0] == chunk1
        assert result[1] == chunk2

    def test_splits_at_single_newline(self):
        chunk1 = "a" * 2000
        chunk2 = "b" * 2000
        text = chunk1 + "\n" + chunk2
        result = split_message(text, max_length=2050)
        assert len(result) == 2

    def test_hard_cut_when_no_newline(self):
        text = "a" * 5000
        result = split_message(text, max_length=2000)
        assert len(result) == 3
        assert all(len(chunk) <= 2000 for chunk in result)

    def test_empty_string(self):
        assert split_message("") == [""]


class TestTodayDailyNotePath:
    def test_format(self):
        fake_now = datetime(2026, 3, 2, tzinfo=timezone.utc)
        with patch("src.utils.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = today_daily_note_path()
        assert result == "2026/03/20260302.md"

    def test_single_digit_day(self):
        fake_now = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with patch("src.utils.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = today_daily_note_path()
        assert result == "2026/01/20260105.md"

    def test_respects_user_timezone_env(self):
        # 2026-03-01 23:30 UTC == 2026-03-02 in Europe/Rome (+1h)
        from datetime import timedelta

        rome_tz = timezone(timedelta(hours=1))
        fake_now = datetime(2026, 3, 2, 0, 30, tzinfo=rome_tz)
        with (
            patch.dict("os.environ", {"USER_TIMEZONE": "Europe/Rome"}),
            patch("src.utils.ZoneInfo", return_value=rome_tz) as mock_zi,
            patch("src.utils.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = today_daily_note_path()
        mock_zi.assert_called_once_with("Europe/Rome")
        assert result == "2026/03/20260302.md"


class TestTodayDailyNoteStem:
    def test_format(self):
        fake_now = datetime(2026, 3, 2, tzinfo=timezone.utc)
        with patch("src.utils.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = today_daily_note_stem()
        assert result == "20260302"

    def test_single_digit_day_and_month(self):
        fake_now = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with patch("src.utils.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = today_daily_note_stem()
        assert result == "20260105"

    def test_respects_user_timezone_env(self):
        from datetime import timedelta

        rome_tz = timezone(timedelta(hours=1))
        # 2026-03-01 23:30 UTC == 2026-03-02 00:30 in Europe/Rome (+1h)
        fake_now = datetime(2026, 3, 2, 0, 30, tzinfo=rome_tz)
        with (
            patch.dict("os.environ", {"USER_TIMEZONE": "Europe/Rome"}),
            patch("src.utils.ZoneInfo", return_value=rome_tz) as mock_zi,
            patch("src.utils.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = today_daily_note_stem()
        mock_zi.assert_called_once_with("Europe/Rome")
        assert result == "20260302"


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World!") == "hello-world"

    def test_fallback_on_empty(self):
        assert slugify("!!!", fallback="link") == "link"

    def test_truncates(self):
        assert len(slugify("a" * 200)) == 60


class TestSanitizeNoteName:
    def test_preserves_case_and_spaces(self):
        assert sanitize_note_name("Sara Rossi") == "Sara Rossi"

    def test_strips_illegal_chars(self):
        # Slashes, colons, and wikilink-breaking chars collapse to spaces.
        assert sanitize_note_name("Sara/Rossi: [lead]") == "Sara Rossi lead"

    def test_fallback_on_empty(self):
        assert sanitize_note_name("///", fallback="entity") == "entity"

    def test_truncation_never_leaves_trailing_space_or_dot(self):
        # Truncation runs before the final strip, so an over-length name can't
        # return a value ending in a space/dot (Windows/Obsidian drop those).
        assert not sanitize_note_name("A" * 79 + " Bcd").endswith((" ", "."))
        assert not sanitize_note_name("X" * 79 + ".abc").endswith((" ", "."))
        assert len(sanitize_note_name("A" * 200)) == 80


class TestFormatLocalTime:
    def test_utc(self):
        with patch.dict("os.environ", {"USER_TIMEZONE": "UTC"}):
            dt = datetime(2026, 6, 6, 9, 30, tzinfo=timezone.utc)
            assert format_local_time(dt) == "09:30"

    def test_converts_to_user_timezone(self):
        # 09:30 UTC -> 11:30 in a UTC+2 zone; exercises the astimezone conversion.
        from datetime import timedelta

        rome_tz = timezone(timedelta(hours=2))
        dt = datetime(2026, 6, 6, 9, 30, tzinfo=timezone.utc)
        with (
            patch.dict("os.environ", {"USER_TIMEZONE": "Europe/Rome"}),
            patch("src.utils.ZoneInfo", return_value=rome_tz) as mock_zi,
        ):
            assert format_local_time(dt) == "11:30"
        mock_zi.assert_called_once_with("Europe/Rome")
