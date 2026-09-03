"""
F2 -- the versioned queue payload envelope (BEGIN SHARED ENVELOPE in both
hub.py and worker_agent.py). check-40-envelope.py already proves this end to
end through Redis; this is the same contract at the function level -- every
field, the clamp, and the tolerant-parse rules parse_envelope() promises.

Both files carry a byte-identical copy of this block (scripts/lint.sh diffs
them), so every test here runs against both modules.
"""

from __future__ import annotations

import pytest


@pytest.fixture(params=["hub_module", "worker_agent_module"])
def mod(request):
    """The gateway and the worker's own copy of the shared envelope block,
    in turn -- a test that only passes for one of them is a divergence
    lint.sh should have caught, so run everything against both."""
    return request.getfixturevalue(request.param)


# ---------------------------------------------------------------------------
# build_envelope / parse_envelope round trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_every_field(mod):
    envelope = mod.build_envelope(
        "job-1", {"1": {"class_type": "KSampler"}},
        queue_key="lane-a", user="alice", attempt={"count": 2, "phase": "executing"},
        submitted_at=1700000000.0, tier="l40s",
    )

    parsed = mod.parse_envelope(envelope)

    assert parsed["job_id"] == "job-1"
    assert parsed["workflow"] == {"1": {"class_type": "KSampler"}}
    assert parsed["queue_key"] == "lane-a"
    assert parsed["user"] == "alice"
    assert parsed["attempt"] == {"count": 2, "phase": "executing"}
    assert parsed["submitted_at"] == 1700000000.0
    assert parsed["tier"] == "l40s"
    assert parsed["schema_version"] == mod.SCHEMA_VERSION


def test_build_envelope_defaults_every_reserved_field(mod):
    envelope = mod.build_envelope("job-2", {})

    assert envelope["queue_key"] == ""
    assert envelope["user"] == ""
    assert envelope["attempt"] == {"count": 0, "phase": mod.PHASE_QUEUED}
    assert isinstance(envelope["submitted_at"], float)
    # tier defaults to "" — the same lane as the default tier, and what a
    # pre-N2 consumer's silence about the field already meant (N2).
    assert envelope["tier"] == ""
    assert envelope["schema_version"] == mod.SCHEMA_VERSION


def test_round_trip_through_json_still_needs_no_second_parse_path(mod):
    """The envelope is JSON on the wire; parse_envelope() must read back
    exactly what build_envelope() wrote once json has round-tripped it
    (dict keys, float timestamps -- nothing json.dumps/loads silently
    changes the shape of)."""
    import json

    envelope = mod.build_envelope("job-3", {"a": 1}, user="bob", queue_key="q")
    wire = json.loads(json.dumps(envelope))
    parsed = mod.parse_envelope(wire)

    assert parsed["job_id"] == "job-3"
    assert parsed["user"] == "bob"
    assert parsed["queue_key"] == "q"
    assert parsed["workflow"] == {"a": 1}


# ---------------------------------------------------------------------------
# MAX_ENVELOPE_FIELD_CHARS clamp
# ---------------------------------------------------------------------------


def test_envelope_text_clamps_to_the_char_limit(mod):
    long_value = "x" * (mod.MAX_ENVELOPE_FIELD_CHARS + 500)

    assert len(mod.envelope_text(long_value)) == mod.MAX_ENVELOPE_FIELD_CHARS


def test_envelope_text_below_the_limit_is_unchanged(mod):
    assert mod.envelope_text("alice") == "alice"


def test_envelope_text_falsy_values_become_empty_string(mod):
    assert mod.envelope_text(None) == ""
    assert mod.envelope_text("") == ""
    assert mod.envelope_text(0) == ""


def test_envelope_text_coerces_non_string_truthy_values(mod):
    # str(value) first, THEN clamp -- a non-string reserved field (an int
    # smuggled in on a hand-pushed payload) must not raise.
    assert mod.envelope_text(12345) == "12345"


def test_build_envelope_clamps_queue_key_and_user(mod):
    envelope = mod.build_envelope(
        "job-4", {}, queue_key="q" * 1000, user="u" * 1000)

    assert len(envelope["queue_key"]) == mod.MAX_ENVELOPE_FIELD_CHARS
    assert len(envelope["user"]) == mod.MAX_ENVELOPE_FIELD_CHARS


def test_parse_envelope_clamps_a_field_that_arrived_oversized(mod):
    """The clamp has to hold on the CONSUMER side too -- a payload can be
    hand-pushed onto the queue, or written by a future gateway with a looser
    cap, so parse_envelope() re-clamps rather than trusting the producer."""
    payload = {"job_id": "job-5", "workflow": {}, "user": "u" * 1000}

    parsed = mod.parse_envelope(payload)

    assert len(parsed["user"]) == mod.MAX_ENVELOPE_FIELD_CHARS


# ---------------------------------------------------------------------------
# malformed / missing fields
# ---------------------------------------------------------------------------


def test_parse_envelope_rejects_a_non_dict_payload(mod):
    with pytest.raises(TypeError):
        mod.parse_envelope(["not", "a", "dict"])


def test_parse_envelope_requires_job_id(mod):
    with pytest.raises(KeyError):
        mod.parse_envelope({"workflow": {}})


def test_parse_envelope_requires_workflow(mod):
    with pytest.raises(KeyError):
        mod.parse_envelope({"job_id": "job-6"})


def test_parse_envelope_tolerates_the_pre_f2_shape(mod):
    """The one shape the whole envelope exists to keep working: an old
    gateway (or a hand-pushed test payload) that carries only the two
    original keys."""
    parsed = mod.parse_envelope({"job_id": "job-7", "workflow": {"a": 1}})

    assert parsed["schema_version"] == mod.IMPLICIT_SCHEMA_VERSION
    assert parsed["queue_key"] == ""
    assert parsed["user"] == ""
    assert parsed["attempt"] == mod.new_attempt()
    assert parsed["submitted_at"] is None
    # An entry with no tier at all is a default-tier job (N2) — the empty
    # string routes to the bare QUEUE_KEY, hub.tier_queue_key().
    assert parsed["tier"] == ""


def test_parse_envelope_ignores_an_unrecognised_field(mod):
    """A payload from a NEWER gateway, carrying a field this vintage does
    not define, must not raise -- forward compatibility during a rolling
    deploy."""
    parsed = mod.parse_envelope(
        {"job_id": "job-8", "workflow": {}, "future_field": "surprise"})

    assert "future_field" not in parsed
    assert parsed["job_id"] == "job-8"


@pytest.mark.parametrize("bad_attempt", [None, "not-a-dict", 42, ["count", 0]])
def test_parse_envelope_defaults_a_malformed_attempt_field(mod, bad_attempt):
    parsed = mod.parse_envelope(
        {"job_id": "job-9", "workflow": {}, "attempt": bad_attempt})

    assert parsed["attempt"] == mod.new_attempt()


# ---------------------------------------------------------------------------
# schema_version handling
# ---------------------------------------------------------------------------


def test_schema_version_defaults_when_absent(mod):
    parsed = mod.parse_envelope({"job_id": "j", "workflow": {}})
    assert parsed["schema_version"] == mod.IMPLICIT_SCHEMA_VERSION


@pytest.mark.parametrize("raw_version,expected", [
    (2, 2),
    ("3", 3),
    (True, 1),    # bool is an int subclass; int(True) == 1
    (False, 0),   # int(False) == 0 -- documented here because it looks like
                  # a bug at a glance and is not one: a payload spelling its
                  # version as the JSON literal `false` is vanishingly
                  # unlikely, and the function's contract is "coerce to int,
                  # fall back to the implicit version only when that itself
                  # raises" -- False coerces cleanly, so it is taken at
                  # face value like any other in-range int.
    ("not-a-number", 1),  # falls back to IMPLICIT_SCHEMA_VERSION
    (None, 1),
])
def test_schema_version_coercion(mod, raw_version, expected):
    parsed = mod.parse_envelope(
        {"job_id": "j", "workflow": {}, "schema_version": raw_version})
    assert parsed["schema_version"] == expected


# ---------------------------------------------------------------------------
# needs_payload / with_workflow -- the pointer-entry split
# ---------------------------------------------------------------------------


def test_needs_payload_true_for_a_pointer_entry(mod):
    record = mod.queue_record(mod.build_envelope("job-10", {"a": 1}))
    assert mod.needs_payload(record) is True


def test_needs_payload_false_once_workflow_is_present(mod):
    envelope = mod.build_envelope("job-11", {"a": 1})
    assert mod.needs_payload(envelope) is False


def test_needs_payload_false_for_non_dict_input(mod):
    assert mod.needs_payload(["not", "a", "dict"]) is False
    assert mod.needs_payload(None) is False


def test_with_workflow_rejoins_a_pointer_entry(mod):
    import json

    envelope = mod.build_envelope("job-12", {"nodes": True}, user="carol")
    record = mod.queue_record(envelope)
    stored = json.dumps(envelope["workflow"])

    rejoined = mod.with_workflow(record, stored)

    assert rejoined["workflow"] == {"nodes": True}
    # Rejoining reads as an ordinary envelope through parse_envelope(), same
    # as any entry that carried its own workflow all along.
    parsed = mod.parse_envelope(rejoined)
    assert parsed["job_id"] == "job-12"
    assert parsed["user"] == "carol"


def test_with_workflow_raises_on_non_json_payload(mod):
    record = mod.queue_record(mod.build_envelope("job-13", {}))

    with pytest.raises(Exception):
        mod.with_workflow(record, "{not json")


def test_queue_record_drops_only_the_workflow(mod):
    envelope = mod.build_envelope("job-14", {"big": "workflow"}, user="dan")
    record = mod.queue_record(envelope)

    assert "workflow" not in record
    assert record["job_id"] == "job-14"
    assert record["user"] == "dan"
    assert record["schema_version"] == envelope["schema_version"]


def test_payload_key_is_namespaced_under_the_job(mod):
    key = mod.payload_key("job-15")
    assert key == "comfy:job:job-15:payload"


# ---------------------------------------------------------------------------
# attempt / retry bookkeeping helpers
# ---------------------------------------------------------------------------


def test_new_attempt_starts_at_zero_and_queued(mod):
    assert mod.new_attempt() == {"count": 0, "phase": mod.PHASE_QUEUED}


@pytest.mark.parametrize("attempt,expected", [
    ({"count": 3, "phase": "executing"}, 3),
    ({}, 0),
    ({"count": "3"}, 0),     # not an int -- advisory field, not trusted
    ({"count": True}, 0),    # bool explicitly excluded despite being an int
    (None, 0),
])
def test_attempt_count_of_is_defensive_about_its_input(mod, attempt, expected):
    assert mod.attempt_count_of({"attempt": attempt}) == expected


def test_attempt_count_of_missing_attempt_key(mod):
    assert mod.attempt_count_of({}) == 0
