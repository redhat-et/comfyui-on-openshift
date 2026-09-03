"""
N2 -- VRAM-tier routing (BEGIN VRAM TIERS in hub.py), at the function level.

The queue KEY SHAPE is the load-bearing thing here (docs/09-engineering-
handoff.md section 3): the default tier's queue is the bare QUEUE_KEY, every
other tier's is QUEUE_KEY:<name>, and the empty string an old envelope
carries is the same lane as the default tier. check-58-tier-routing.py proves
the routing end to end through Redis; this pins the pure functions every
routing decision goes through, including the one property no single e2e
scenario states directly: fair_enqueue_call() and the reaper's early depth
check derive the SAME key from the same envelope, so a submit, a requeue and
its backpressure check can never disagree about which list a job belongs on.

GPU_TIERS is read once at import (the conftest imports hub with no
environment, so the module-level default is tiering OFF); tiered cases set
the module globals directly rather than re-importing.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def tiered(hub_module, monkeypatch):
    """hub with three tiers configured, smallest first, like a real .env's
    GPU_TIERS=l4,l40s,h100."""
    monkeypatch.setattr(hub_module, "GPU_TIERS", ("l4", "l40s", "h100"))
    monkeypatch.setattr(hub_module, "DEFAULT_TIER", "l4")
    return hub_module


# ---------------------------------------------------------------------------
# parse_gpu_tiers -- the import-time refusal
# ---------------------------------------------------------------------------


def test_parse_gpu_tiers_empty_is_tiering_off(hub_module):
    assert hub_module.parse_gpu_tiers("") == ()
    assert hub_module.parse_gpu_tiers("  ,  ") == ()


def test_parse_gpu_tiers_keeps_order_and_trims(hub_module):
    """Order is load-bearing: the FIRST name is the default tier."""
    assert hub_module.parse_gpu_tiers(" l4 , l40s ,h100") == ("l4", "l40s", "h100")


@pytest.mark.parametrize("bad", ["L4", "l4 0s", "l4,l4", "-l4", "x" * 33, "l4:big"])
def test_parse_gpu_tiers_refuses_names_that_cannot_travel(hub_module, bad):
    """A tier name becomes a Redis key suffix, a metric label value, a KEDA
    listName and a machine pool name -- refused at import, not clamped,
    because a silently rewritten name routes jobs to a queue no worker
    polls."""
    with pytest.raises(ValueError):
        hub_module.parse_gpu_tiers(bad)


# ---------------------------------------------------------------------------
# tier_queue_key -- the key shape itself
# ---------------------------------------------------------------------------


def test_default_tier_queue_is_the_bare_queue_key(tiered):
    """The migration-path asymmetry: the default tier rides the pre-N2 list,
    so a backlog queued before tiering was switched on keeps draining."""
    assert tiered.tier_queue_key("l4") == tiered.QUEUE_KEY


def test_empty_tier_is_the_same_lane_as_the_default(tiered):
    """An old envelope with tier "" (or none at all) must land where a
    default-tier job lands -- absence is not a fourth routing state."""
    assert tiered.tier_queue_key("") == tiered.tier_queue_key("l4")


def test_non_default_tiers_get_suffixed_queues(tiered):
    assert tiered.tier_queue_key("l40s") == f"{tiered.QUEUE_KEY}:l40s"
    assert tiered.tier_queue_key("h100") == f"{tiered.QUEUE_KEY}:h100"


def test_untiered_pool_has_one_queue(hub_module):
    assert hub_module.tier_queues() == [("", hub_module.QUEUE_KEY)]


def test_tiered_pool_lists_every_queue_default_first(tiered):
    assert tiered.tier_queues() == [
        ("l4", tiered.QUEUE_KEY),
        ("l40s", f"{tiered.QUEUE_KEY}:l40s"),
        ("h100", f"{tiered.QUEUE_KEY}:h100"),
    ]


# ---------------------------------------------------------------------------
# resolve_tier -- what a submission may ask for
# ---------------------------------------------------------------------------


def test_resolve_tier_defaults_to_the_smallest(tiered):
    """Absent, empty or null means the cheap lane, never a guess."""
    assert tiered.resolve_tier(None) == "l4"
    assert tiered.resolve_tier("") == "l4"


def test_resolve_tier_accepts_exactly_the_configured_names(tiered):
    assert tiered.resolve_tier("l40s") == "l40s"
    assert tiered.resolve_tier("h100") == "h100"


def test_resolve_tier_refuses_an_unknown_tier_loudly(tiered):
    """A typo must fail at submit, where the caller can fix it -- accepted,
    it would be a job parked forever on a list nothing polls. The refusal
    names the valid tiers so the 400 is actionable."""
    with pytest.raises(ValueError, match="l4, l40s, h100"):
        tiered.resolve_tier("a100")


def test_resolve_tier_refuses_a_non_string(tiered):
    with pytest.raises(ValueError):
        tiered.resolve_tier(["l4"])


def test_resolve_tier_refuses_any_tier_on_an_untiered_pool(hub_module):
    """Accepting "l40s" where every queue is the same queue would be a
    routing promise the deployment cannot keep."""
    with pytest.raises(ValueError, match="no GPU tiers configured"):
        hub_module.resolve_tier("l40s")

    assert hub_module.resolve_tier(None) == ""


# ---------------------------------------------------------------------------
# the property the two call sites must share
# ---------------------------------------------------------------------------


def test_fair_enqueue_call_routes_by_the_envelope_tier(tiered):
    """fair_enqueue_call() derives the queue from the ENVELOPE, so a first
    submission and the reaper's requeue cannot disagree with the entry about
    where it belongs -- for every tier, including the default and the
    old-envelope empty string."""
    for tier in ("", "l4", "l40s", "h100"):
        envelope = tiered.build_envelope("job-t", {"a": 1}, tier=tier)
        keys, _args = tiered.fair_enqueue_call(envelope)

        assert keys[0] == tiered.tier_queue_key(tier)


def test_fair_enqueue_call_tolerates_an_envelope_with_no_tier_key(tiered):
    """A dict that predates the field entirely (a hand-built envelope, a
    pre-N2 caller) routes to the default lane rather than raising."""
    keys, _args = tiered.fair_enqueue_call(
        {"job_id": "job-old", "queue_key": "", "workflow": {}})

    assert keys[0] == tiered.QUEUE_KEY


# ---------------------------------------------------------------------------
# the worker's half of the key shape
# ---------------------------------------------------------------------------


def test_worker_defaults_to_polling_the_bare_queue(worker_agent_module):
    """WORKER_TIER unset (the conftest imports with no environment) polls
    QUEUE_KEY itself -- the untiered pool, and the default-tier pool on a
    tiered one, are the same worker configuration. The suffixed shape is
    exercised for real by check-58-tier-routing.py, which runs an agent
    under WORKER_TIER and proves which list it drains."""
    assert worker_agent_module.WORKER_TIER == ""
    assert worker_agent_module.POLL_QUEUE_KEY == worker_agent_module.QUEUE_KEY
