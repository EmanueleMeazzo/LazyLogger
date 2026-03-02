from datetime import datetime, timezone
from unittest.mock import patch

from src.utils import split_message, today_daily_note_path


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
        assert result == "2026/03/2026Mar02.md"

    def test_single_digit_day(self):
        fake_now = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with patch("src.utils.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = today_daily_note_path()
        assert result == "2026/01/2026Jan05.md"
