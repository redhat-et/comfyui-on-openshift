"""
The FOCUS export's pure half (docs/10-roadmap.md, N4): focus_period_bounds(),
focus_row(), focus_rows() and focus_csv() are all functions of their
arguments, so the calendar edges and the CSV-hostility cases live here where
they cost microseconds. check-91-focus-export.py owns the wire: content
type, scoping, the live gateway reading the real Hash.
"""

from __future__ import annotations

import csv
import io


REPORT = {
    "period": "2026-08",
    "users": {"alice@example.com": 7200.0,
              'comma,"quote"@example.com': 1800.0},
    "users_total_gpu_seconds": 9000.0,
    "anonymous_gpu_seconds": 900.0,
    "excluded_gpu_seconds": 3600.0,
    "other_gpu_seconds": 0.0,
    "billed_jobs": 12,
    "truncated": False,
    "periods_available": ["2026-08"],
}


def rows_of(hub_module, rate=3.6):
    return hub_module.focus_rows(REPORT, REPORT["period"], rate)


def test_period_bounds_are_end_exclusive_iso(hub_module):
    assert hub_module.focus_period_bounds("2026-08") == (
        "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z")


def test_period_bounds_cross_the_year(hub_module):
    # The December edge every hand-rolled month+1 gets wrong once.
    assert hub_module.focus_period_bounds("2025-12") == (
        "2025-12-01T00:00:00Z", "2026-01-01T00:00:00Z")


def test_one_row_per_user_plus_nonzero_buckets(hub_module):
    rows = rows_of(hub_module)
    subs = [row["SubAccountId"] for row in rows]

    # Two users, anonymous, excluded (empty SubAccountId) -- and NOT the
    # zero-valued `other` bucket: a $0.00 line for nothing is noise.
    assert sorted(subs) == ["", "alice@example.com", "anonymous",
                            'comma,"quote"@example.com']


def test_costs_are_seconds_at_the_rate_and_all_four_equal(hub_module):
    rows = {row["SubAccountId"]: row for row in rows_of(hub_module, rate=3.6)}
    alice = rows["alice@example.com"]

    # 7200s = 2h at $3.6/h.
    assert float(alice["BilledCost"]) == 7.2
    assert (alice["BilledCost"] == alice["EffectiveCost"]
            == alice["ListCost"] == alice["ContractedCost"])
    assert float(alice["ConsumedQuantity"]) == 7200.0
    assert float(alice["PricingQuantity"]) == 2.0


def test_unknowable_columns_are_empty_not_plausible(hub_module):
    for row in rows_of(hub_module):
        for column in ("RegionId", "RegionName", "ResourceId",
                       "ResourceName", "SkuId"):
            assert row[column] == ""


def test_excluded_bills_nobody_and_says_so(hub_module):
    row = next(r for r in rows_of(hub_module) if r["SubAccountId"] == "")

    assert float(row["BilledCost"]) == 3.6  # 1h at 3.6
    assert "nobody" in row["ChargeDescription"].lower()


def test_csv_round_trips_a_hostile_identity(hub_module):
    text = hub_module.focus_csv(rows_of(hub_module))
    parsed = list(csv.DictReader(io.StringIO(text)))

    assert [len(row) for row in parsed] == [len(hub_module.FOCUS_COLUMNS)] * len(parsed)
    assert 'comma,"quote"@example.com' in {row["SubAccountId"] for row in parsed}


def test_csv_header_is_the_pinned_column_set(hub_module):
    text = hub_module.focus_csv([])
    reader = csv.reader(io.StringIO(text))

    assert next(reader) == hub_module.FOCUS_COLUMNS
