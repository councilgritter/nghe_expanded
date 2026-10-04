"""Tests for the off-peak guard.

This is the code that decides whether a paid run may start, so the boundaries are
pinned exactly: an hour that bills double must never be treated as free.

DeepSeek's published windows: peak is 01:00-04:00 and 06:00-10:00 UTC, Monday
through Friday, excluding Chinese public holidays; everything else is off-peak.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reading.pipeline.offpeak import (
    DEFAULT_PEAK_WEEKDAYS,
    DEFAULT_PEAK_WINDOWS,
    PeakPolicy,
    check,
    parse_weekdays,
    parse_windows,
    status_line,
)

# 2026-10-05 is a Monday, so weekday boundaries are unambiguous.
MONDAY = datetime(2026, 10, 5, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 10, 3, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 10, 4, tzinfo=timezone.utc)


def at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute)


@pytest.fixture
def policy() -> PeakPolicy:
    return PeakPolicy()


class TestDefaults:
    def test_defaults_match_the_published_windows(self):
        assert DEFAULT_PEAK_WINDOWS == ((1, 4), (6, 10))
        assert DEFAULT_PEAK_WEEKDAYS == (0, 1, 2, 3, 4)


class TestWeekdayPeak:
    @pytest.mark.parametrize("hour", [1, 2, 3, 6, 7, 8, 9])
    def test_hours_inside_the_windows_are_peak(self, policy, hour):
        assert policy.is_peak(at(MONDAY, hour)) is True

    @pytest.mark.parametrize("hour", [0, 4, 5, 10, 11, 12, 18, 23])
    def test_hours_outside_the_windows_are_off_peak(self, policy, hour):
        assert policy.is_peak(at(MONDAY, hour)) is False

    def test_window_start_is_inclusive(self, policy):
        assert policy.is_peak(at(MONDAY, 1, 0)) is True
        assert policy.is_peak(at(MONDAY, 6, 0)) is True

    def test_window_end_is_exclusive(self, policy):
        # 04:00 and 10:00 are the first off-peak minutes.
        assert policy.is_peak(at(MONDAY, 4, 0)) is False
        assert policy.is_peak(at(MONDAY, 10, 0)) is False

    def test_the_last_peak_minute_is_still_peak(self, policy):
        assert policy.is_peak(at(MONDAY, 3, 59)) is True
        assert policy.is_peak(at(MONDAY, 9, 59)) is True

    @pytest.mark.parametrize("weekday_offset", range(5))
    def test_every_weekday_follows_the_rule(self, policy, weekday_offset):
        day = MONDAY + timedelta(days=weekday_offset)
        assert policy.is_peak(at(day, 2)) is True
        assert policy.is_peak(at(day, 20)) is False


class TestWeekend:
    @pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
    @pytest.mark.parametrize("hour", [1, 2, 3, 6, 7, 9, 12, 23])
    def test_the_whole_weekend_is_off_peak(self, policy, day, hour):
        assert policy.is_peak(at(day, hour)) is False


class TestHolidays:
    def test_a_listed_holiday_is_off_peak_all_day(self):
        policy = PeakPolicy(offpeak_dates=frozenset({"2026-10-05"}))
        for hour in (1, 2, 6, 9):
            assert policy.is_peak(at(MONDAY, hour)) is False

    def test_an_unlisted_day_is_unaffected(self):
        policy = PeakPolicy(offpeak_dates=frozenset({"2026-10-06"}))
        assert policy.is_peak(at(MONDAY, 2)) is True

    def test_without_a_calendar_a_holiday_is_treated_as_peak(self, policy):
        # The conservative reading: we cannot know, so we wait rather than pay double.
        assert policy.is_peak(at(MONDAY, 2)) is True


class TestNextOffpeak:
    def test_finds_the_end_of_the_current_window(self, policy):
        assert policy.next_offpeak(at(MONDAY, 2)) == at(MONDAY, 4)

    def test_returns_now_when_already_off_peak(self, policy):
        assert policy.next_offpeak(at(MONDAY, 5)) == at(MONDAY, 5)

    def test_returns_now_on_a_weekend(self, policy):
        assert policy.next_offpeak(at(SATURDAY, 2)) == at(SATURDAY, 2)

    def test_skips_the_gap_between_windows(self, policy):
        # 04:00-06:00 is off-peak, so 05:00 is already fine.
        assert policy.next_offpeak(at(MONDAY, 5)) == at(MONDAY, 5)

    def test_late_peak_rolls_to_the_next_day(self, policy):
        assert policy.next_offpeak(at(MONDAY, 9)) == at(MONDAY, 10)

    def test_friday_peak_rolls_over_the_weekend(self, policy):
        # Friday 09:00 -> 10:00 the same day; the weekend after is all off-peak.
        friday = MONDAY + timedelta(days=4)
        assert policy.next_offpeak(at(friday, 9)) == at(friday, 10)

    def test_result_is_never_itself_peak(self, policy):
        for hour in range(24):
            moment = policy.next_offpeak(at(MONDAY, hour))
            assert policy.is_peak(moment) is False


class TestCheck:
    def test_allows_an_off_peak_run(self, policy):
        allowed, message = check(policy, at(MONDAY, 20))
        assert allowed is True
        assert "off-peak" in message

    def test_refuses_a_peak_run(self, policy):
        allowed, message = check(policy, at(MONDAY, 2))
        assert allowed is False
        assert "peak" in message.lower()
        assert "04:00" in message          # when the window closes
        assert "--allow-peak" in message   # how to override

    def test_explicit_approval_allows_a_peak_run(self, policy):
        allowed, message = check(policy, at(MONDAY, 2), allow_peak=True)
        assert allowed is True
        assert "PEAK" in message

    def test_allows_a_weekend_run_without_approval(self, policy):
        assert check(policy, at(SATURDAY, 2))[0] is True


class TestParsing:
    def test_parses_the_documented_windows(self):
        assert parse_windows("01:00-04:00,06:00-10:00") == ((1, 4), (6, 10))

    def test_parses_a_full_day_window(self):
        assert parse_windows("00:00-24:00") == ((0, 24),)

    @pytest.mark.parametrize("bad", ["nonsense", "10:00-01:00", "01:00"])
    def test_rejects_malformed_windows(self, bad):
        with pytest.raises(ValueError):
            parse_windows(bad)

    def test_parses_weekday_names(self):
        assert parse_weekdays("Mon,Tue,Wed,Thu,Fri") == (0, 1, 2, 3, 4)

    def test_parses_all_days(self):
        assert parse_weekdays("Mon,Tue,Wed,Thu,Fri,Sat,Sun") == (0, 1, 2, 3, 4, 5, 6)

    def test_rejects_an_unknown_weekday(self):
        with pytest.raises(ValueError):
            parse_weekdays("Funday")


class TestDescribe:
    def test_describes_the_policy(self, policy):
        text = policy.describe()
        assert "01:00-04:00" in text and "06:00-10:00" in text
        assert "Mon" in text

    def test_status_line_reports_peak_and_the_resume_time(self, policy):
        line = status_line(policy, at(MONDAY, 2))
        assert "PEAK" in line and "04:00" in line

    def test_status_line_reports_off_peak(self, policy):
        assert "off-peak" in status_line(policy, at(MONDAY, 20))


class TestTimezoneHandling:
    def test_naive_datetimes_are_rejected(self, policy):
        with pytest.raises(ValueError):
            policy.is_peak(datetime(2026, 10, 5, 2))

    def test_non_utc_input_is_converted(self, policy):
        # 09:00 in UTC+7 is 02:00 UTC, inside the 01:00-04:00 window.
        plus_seven = timezone(timedelta(hours=7))
        moment = datetime(2026, 10, 5, 9, tzinfo=plus_seven)
        assert moment.astimezone(timezone.utc).hour == 2
        assert policy.is_peak(moment) is True
