"""
The remaining pure-enough helpers in hub.py that a one-line change could
silently break, and that are not already covered by test_envelope.py,
test_period_boundaries.py, test_workspace_name.py, test_output_paths.py or
test_wait_and_quota.py: caller_identity(), locate_output() (the resolve-and-
compare containment check check-66-output-scoping.py exercises end to end),
quota_headers(), and showback_accrue_call()'s argument order.
"""

from __future__ import annotations

import pytest


class FakeRequest:
    """The only thing caller_identity() touches on a Request: .headers.get().
    A plain dict already has that signature."""

    def __init__(self, headers=None):
        self.headers = headers or {}


# ---------------------------------------------------------------------------
# caller_identity
# ---------------------------------------------------------------------------


def test_caller_identity_reads_the_forwarded_user_header(hub_module):
    request = FakeRequest({"x-forwarded-user": "alice"})
    assert hub_module.caller_identity(request) == "alice"


def test_caller_identity_defaults_to_empty_string(hub_module):
    request = FakeRequest({})
    assert hub_module.caller_identity(request) == ""


def test_caller_identity_clamps_like_the_envelope_does(hub_module):
    """output_file() and showback() must scope by the SAME clamped identity
    generate() put in the envelope -- a longer raw header that happens to
    share a prefix must not compare unequal to the clamped value stored on
    the job."""
    long_header = "u" * 1000
    request = FakeRequest({"x-forwarded-user": long_header})

    identity = hub_module.caller_identity(request)

    assert len(identity) == hub_module.MAX_ENVELOPE_FIELD_CHARS
    assert identity == hub_module.envelope_text(long_header)


# ---------------------------------------------------------------------------
# locate_output -- the resolve-then-compare containment check
# ---------------------------------------------------------------------------


def test_locate_output_serves_an_existing_file_with_no_workspace_scope(hub_module, hub_output_root):
    (hub_output_root / "out.png").write_bytes(b"x")

    found = hub_module.locate_output("out.png", None)

    assert found == hub_output_root / "out.png"


def test_locate_output_rejects_traversal_outside_output_root(hub_module, hub_output_root):
    with pytest.raises(hub_module.HTTPException) as exc_info:
        hub_module.locate_output("../../etc/passwd", None)
    assert exc_info.value.status_code == 403


def test_locate_output_404s_on_a_missing_file(hub_module, hub_output_root):
    with pytest.raises(hub_module.HTTPException) as exc_info:
        hub_module.locate_output("nope.png", None)
    assert exc_info.value.status_code == 404


def test_locate_output_serves_a_file_inside_the_caller_own_workspace(hub_module, hub_output_root):
    (hub_output_root / "alice-abc").mkdir()
    (hub_output_root / "alice-abc" / "out.png").write_bytes(b"x")

    found = hub_module.locate_output("alice-abc/out.png", "alice-abc")

    assert found == hub_output_root / "alice-abc" / "out.png"


def test_locate_output_refuses_another_users_workspace(hub_module, hub_output_root):
    (hub_output_root / "bob-def").mkdir()
    (hub_output_root / "bob-def" / "out.png").write_bytes(b"x")

    with pytest.raises(hub_module.HTTPException) as exc_info:
        hub_module.locate_output("bob-def/out.png", "alice-abc")
    assert exc_info.value.status_code == 403


def test_locate_output_checks_containment_on_the_resolved_path_not_the_url(hub_module, hub_output_root):
    """/outputs/<mine>/../<theirs>/x names 'mine' in its first URL segment
    but resolves to 'theirs' on disk -- check-66's own regression case."""
    (hub_output_root / "alice-abc").mkdir()
    (hub_output_root / "bob-def").mkdir()
    (hub_output_root / "bob-def" / "secret.png").write_bytes(b"x")

    with pytest.raises(hub_module.HTTPException) as exc_info:
        hub_module.locate_output("alice-abc/../bob-def/secret.png", "alice-abc")
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# quota_headers
# ---------------------------------------------------------------------------


def test_quota_headers_retry_after_matches_the_period_reset(hub_module):
    now = 1700000000.0
    headers = hub_module.quota_headers(now)

    expected = int(hub_module.quota_period_reset(now) - now)
    assert headers == {"Retry-After": str(expected)}


def test_quota_headers_retry_after_is_never_less_than_one(hub_module):
    """Called an instant before the period boundary, the naive subtraction
    could round to 0 -- a client must never be told to retry with no
    backoff at all."""
    boundary = hub_module.quota_period_reset(1700000000.0)
    headers = hub_module.quota_headers(boundary - 0.001)

    assert int(headers["Retry-After"]) >= 1


# ---------------------------------------------------------------------------
# showback_accrue_call -- one place for the Lua keys/args, both call sites
# ---------------------------------------------------------------------------


def test_showback_accrue_call_keys_and_arg_order(hub_module):
    keys, args = hub_module.showback_accrue_call("comfy:job:x:state", "submitter", now=1700000000.0)

    assert keys == ["comfy:job:x:state",
                     hub_module.showback_key(hub_module.showback_period(1700000000.0))]
    assert args[0] == 1700000000.0
    assert args[1] == hub_module.showback_ttl_seconds()
    assert args[2] == hub_module.SHOWBACK_MAX_USERS
    assert args[-1] == hub_module.SHOWBACK_TO_SUBMITTER


def test_showback_accrue_call_defaults_now_to_the_current_time(hub_module):
    before = hub_module.time.time()
    _, args = hub_module.showback_accrue_call("state", "submitter")
    after = hub_module.time.time()

    assert before <= args[0] <= after
