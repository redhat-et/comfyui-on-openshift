"""
showback_period() and quota_period_reset() -- T1's named gap ("Dec->Jan
untested"). Both are pinned in hub.py only: quota (Q5) and showback (Q4) are
gateway-side accounting, so unlike the envelope/showback-accrual/workspace
blocks there is no worker_agent.py copy to mirror against.

Both read time.gmtime(now) -- UTC, not the host's local timezone. That is an
assumption stated in quota_period_reset()'s own docstring implicitly (it
never mentions a timezone at all, which is the tell: everything here is UTC
by construction, via calendar.timegm and time.gmtime rather than their local-
time counterparts) and is tested explicitly below rather than trusted.
"""

from __future__ import annotations

import calendar
import time

import pytest

try:
    time.tzset  # Unix only -- absent on Windows, where this file is skipped.
    _HAS_TZSET = True
except AttributeError:
    _HAS_TZSET = False

requires_tzset = pytest.mark.skipif(
    not _HAS_TZSET, reason="time.tzset() is POSIX-only; the underlying "
                           "UTC-vs-local behaviour is unchanged on Windows, "
                           "there is just no way to flip the local zone to "
                           "prove it from this test")


@pytest.fixture
def local_timezone_far_from_utc(monkeypatch):
    """
    Actually changes the process's local timezone to UTC+14 (Pacific/Kiritimati
    has no DST, so it is a stable, unambiguous offset) for the duration of one
    test, so a function that (bug) reads time.localtime() instead of
    time.gmtime() would disagree with the UTC-computed expectation by a full
    calendar day near midnight. Restores the original TZ afterwards via
    monkeypatch's own teardown, then re-applies it with tzset() so later
    tests are not left running in a shifted zone.
    """
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    yield
    time.tzset()


# ---------------------------------------------------------------------------
# showback_period -- the calendar-month bucket
# ---------------------------------------------------------------------------


def test_showback_period_format_is_year_dash_month(hub_module):
    # 2024-06-15 12:00:00 UTC
    now = calendar.timegm((2024, 6, 15, 12, 0, 0, 0, 0, 0))
    assert hub_module.showback_period(now) == "2024-06"


def test_showback_period_december_to_january_boundary(hub_module):
    dec_31_2359 = calendar.timegm((2025, 12, 31, 23, 59, 59, 0, 0, 0))
    jan_1_0000 = calendar.timegm((2026, 1, 1, 0, 0, 0, 0, 0, 0))

    assert hub_module.showback_period(dec_31_2359) == "2025-12"
    assert hub_module.showback_period(jan_1_0000) == "2026-01"


def test_showback_period_leap_year_february(hub_module):
    # 2024 is a leap year; Feb 29 exists and belongs to "2024-02".
    feb_29_2024 = calendar.timegm((2024, 2, 29, 0, 0, 0, 0, 0, 0))
    assert hub_module.showback_period(feb_29_2024) == "2024-02"


@requires_tzset
def test_showback_period_uses_utc_not_local_time(hub_module, local_timezone_far_from_utc):
    """The function is time.strftime(FORMAT, time.gmtime(now)) -- gmtime,
    not localtime. With the process TZ actually set to UTC+14, a moment ten
    hours before UTC midnight is already past local midnight the next day;
    if the function read local time it would report the wrong period here."""
    ten_hours_before_utc_midnight = calendar.timegm((2025, 12, 31, 14, 0, 0, 0, 0, 0))
    assert hub_module.showback_period(ten_hours_before_utc_midnight) == "2025-12"


# ---------------------------------------------------------------------------
# quota_period_reset -- 00:00 UTC on the first of next calendar month
# ---------------------------------------------------------------------------


def test_quota_period_reset_within_a_month(hub_module):
    now = calendar.timegm((2024, 6, 15, 12, 30, 0, 0, 0, 0))
    reset = hub_module.quota_period_reset(now)

    assert time.gmtime(reset)[:6] == (2024, 7, 1, 0, 0, 0)


def test_quota_period_reset_december_rolls_into_next_january(hub_module):
    now = calendar.timegm((2025, 12, 15, 8, 0, 0, 0, 0, 0))
    reset = hub_module.quota_period_reset(now)

    assert time.gmtime(reset)[:6] == (2026, 1, 1, 0, 0, 0)


def test_quota_period_reset_from_the_last_second_of_december(hub_module):
    now = calendar.timegm((2025, 12, 31, 23, 59, 59, 0, 0, 0))
    reset = hub_module.quota_period_reset(now)

    assert time.gmtime(reset)[:6] == (2026, 1, 1, 0, 0, 0)


def test_quota_period_reset_leap_year_february_rolls_into_march(hub_module):
    """calendar.timegm((year, month, 1, ...)) does not need to know how many
    days February had -- the reset is always day 1 of the FOLLOWING month --
    but this pins that a leap year's Feb 29 does not confuse it into
    Mar 2 or some other off-by-one."""
    now = calendar.timegm((2024, 2, 29, 23, 0, 0, 0, 0, 0))
    reset = hub_module.quota_period_reset(now)

    assert time.gmtime(reset)[:6] == (2024, 3, 1, 0, 0, 0)


def test_quota_period_reset_from_january_stays_in_the_same_year(hub_module):
    now = calendar.timegm((2026, 1, 10, 0, 0, 0, 0, 0, 0))
    reset = hub_module.quota_period_reset(now)

    assert time.gmtime(reset)[:6] == (2026, 2, 1, 0, 0, 0)


@requires_tzset
def test_quota_period_reset_is_utc_not_local_time(hub_module, local_timezone_far_from_utc):
    """Same assumption as showback_period, proven the same way: with the
    process TZ actually shifted to UTC+14, a moment that is late Dec 31 in
    UTC is already Jan 1 local. quota_period_reset() reads
    moment.tm_mon/tm_year off time.gmtime(now), so it must still answer
    "next month is January 2026" -- not "next month is February", which is
    what it would answer if it had used the already-rolled-over local
    month."""
    ten_hours_before_utc_midnight = calendar.timegm((2025, 12, 31, 14, 0, 0, 0, 0, 0))
    reset = hub_module.quota_period_reset(ten_hours_before_utc_midnight)

    assert time.gmtime(reset)[:6] == (2026, 1, 1, 0, 0, 0)


def test_quota_period_reset_is_not_a_rolling_window(hub_module):
    """Explicitly not "now + 30 days" -- from the 1st of a 31-day month the
    reset must land on the 1st of next month, not the 31st/1st a rolling
    window would give."""
    now = calendar.timegm((2024, 1, 1, 0, 0, 0, 0, 0, 0))
    reset = hub_module.quota_period_reset(now)

    assert time.gmtime(reset)[:6] == (2024, 2, 1, 0, 0, 0)
    thirty_days = 30 * 24 * 3600
    assert reset != now + thirty_days
