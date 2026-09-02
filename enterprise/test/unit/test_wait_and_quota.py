"""
estimated_wait_seconds() and quota_gpu_seconds_used() (hub.py, Q5/Q6) --
both `async def`, both take a redis.asyncio.Redis connection and do exactly
one read call on it. check-80-estimated-wait.py and check-95-quota-
breaker.py already prove these against a real Redis; here they are driven
with a small fake connection instead, so the FAIL-OPEN / malformed-data
branches (which are awkward to arrange against a real Redis without a
dedicated fault-injection fixture) are exercised directly and quickly.

No pytest-asyncio dependency: both functions are plain `async def` with no
event-loop-specific behaviour of their own, so `asyncio.run(...)` inside an
ordinary sync test is enough.
"""

from __future__ import annotations

import asyncio
import json

import pytest


class FakeAsyncRedis:
    """The minimal async surface estimated_wait_seconds()/
    quota_gpu_seconds_used() call: one `lindex` or `hget`. Configure a
    return value or an exception to raise instead."""

    def __init__(self, *, lindex_return=None, lindex_exc=None,
                 hget_return=None, hget_exc=None):
        self._lindex_return = lindex_return
        self._lindex_exc = lindex_exc
        self._hget_return = hget_return
        self._hget_exc = hget_exc
        self.hget_calls = []

    async def lindex(self, key, index):
        if self._lindex_exc is not None:
            raise self._lindex_exc
        return self._lindex_return

    async def hget(self, key, field):
        self.hget_calls.append((key, field))
        if self._hget_exc is not None:
            raise self._hget_exc
        return self._hget_return


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# estimated_wait_seconds
# ---------------------------------------------------------------------------


def test_empty_queue_reads_as_zero_not_unknown(hub_module):
    conn = FakeAsyncRedis(lindex_return=None)
    assert run(hub_module.estimated_wait_seconds(conn)) == 0.0


def test_reads_the_age_of_the_tail_entry(hub_module):
    submitted_at = hub_module.time.time() - 42.0
    conn = FakeAsyncRedis(lindex_return=json.dumps({"submitted_at": submitted_at}))

    waited = run(hub_module.estimated_wait_seconds(conn))

    assert waited is not None
    assert 41.0 <= waited <= 60.0  # generous upper bound for test-runner jitter


def test_malformed_json_reads_as_unknown(hub_module):
    conn = FakeAsyncRedis(lindex_return="{not json")
    assert run(hub_module.estimated_wait_seconds(conn)) is None


def test_entry_missing_submitted_at_reads_as_unknown(hub_module):
    """Absence is a distinct value from zero -- a pre-F2 or malformed entry
    must not report a fabricated 0.0 wait."""
    conn = FakeAsyncRedis(lindex_return=json.dumps({"job_id": "x"}))
    assert run(hub_module.estimated_wait_seconds(conn)) is None


def test_submitted_at_as_bool_is_rejected_despite_being_an_int_subclass(hub_module):
    conn = FakeAsyncRedis(lindex_return=json.dumps({"submitted_at": True}))
    assert run(hub_module.estimated_wait_seconds(conn)) is None


def test_submitted_at_as_string_is_rejected(hub_module):
    conn = FakeAsyncRedis(lindex_return=json.dumps({"submitted_at": "42.0"}))
    assert run(hub_module.estimated_wait_seconds(conn)) is None


def test_future_submitted_at_clamps_to_zero_not_negative(hub_module):
    """Clock skew between processes, or a queue entry re-timestamped in the
    future by a bug, must not surface as a negative wait."""
    far_future = hub_module.time.time() + 10_000
    conn = FakeAsyncRedis(lindex_return=json.dumps({"submitted_at": far_future}))

    assert run(hub_module.estimated_wait_seconds(conn)) == 0.0


def test_non_dict_json_reads_as_unknown(hub_module):
    """`json.loads(raw)` can succeed on a bare JSON scalar (e.g. "42") that
    has no .get() at all -- AttributeError, not a crash."""
    conn = FakeAsyncRedis(lindex_return="42")
    assert run(hub_module.estimated_wait_seconds(conn)) is None


# ---------------------------------------------------------------------------
# quota_gpu_seconds_used
# ---------------------------------------------------------------------------


def test_absent_field_is_zero_not_none(hub_module):
    """A submitter with nothing accrued yet this period is not an
    unreadable submitter -- only a genuine failure to read is None."""
    conn = FakeAsyncRedis(hget_return=None)
    used = run(hub_module.quota_gpu_seconds_used(conn, "alice", 1700000000.0))
    assert used == 0.0


def test_numeric_string_field_parses(hub_module):
    conn = FakeAsyncRedis(hget_return="12.5")
    used = run(hub_module.quota_gpu_seconds_used(conn, "alice", 1700000000.0))
    assert used == 12.5


def test_non_numeric_field_fails_open_as_none(hub_module):
    conn = FakeAsyncRedis(hget_return="not-a-number")
    used = run(hub_module.quota_gpu_seconds_used(conn, "alice", 1700000000.0))
    assert used is None


def test_redis_exception_fails_open_as_none(hub_module):
    conn = FakeAsyncRedis(hget_exc=ConnectionError("redis is gone"))
    used = run(hub_module.quota_gpu_seconds_used(conn, "alice", 1700000000.0))
    assert used is None


def test_reads_the_key_and_field_showback_would_write(hub_module):
    """quota_field()/showback_key() have to name the same Hash and field the
    accrual side (showback_accrue_call) writes, or the breaker enforces
    against a number nobody ever accrues into -- this pins the read side's
    half of that agreement."""
    conn = FakeAsyncRedis(hget_return="1.0")
    now = 1700000000.0
    run(hub_module.quota_gpu_seconds_used(conn, "alice", now))

    expected_key = hub_module.showback_key(hub_module.showback_period(now))
    expected_field = hub_module.quota_field("alice")
    assert conn.hget_calls == [(expected_key, expected_field)]


def test_anonymous_submitter_uses_the_shared_anonymous_field(hub_module):
    assert hub_module.quota_field("") == hub_module.SHOWBACK_ANONYMOUS_FIELD
    assert hub_module.quota_field("alice") == f"{hub_module.SHOWBACK_USER_PREFIX}alice"


# ---------------------------------------------------------------------------
# quota_enabled / quota_refusal_text -- small pure helpers beside the above
# ---------------------------------------------------------------------------


def test_quota_enabled_reflects_the_module_level_ceiling(hub_module):
    # QUOTA_GPU_SECONDS is read from the environment at import time; this
    # process imported with none set, so the breaker is off by default
    # (quota_enabled() is False) -- the documented "off unless somebody
    # chose a positive ceiling" behaviour.
    assert hub_module.quota_enabled() == (hub_module.QUOTA_GPU_SECONDS > 0)


def test_quota_refusal_text_names_the_period_and_the_reset_time(hub_module, monkeypatch):
    monkeypatch.setattr(hub_module, "QUOTA_GPU_SECONDS", 100.0)
    now = 1700000000.0

    text = hub_module.quota_refusal_text(45.0, now)

    assert "45.0" in text
    assert "100.0" in text
    assert hub_module.showback_period(now) in text
