"""
is_bare_filename() (both files), scoped_prefix() and output_subfolder()
(worker_agent.py), output_url() and rewrite_image_urls() (hub.py) -- the
functions that decide whether a reported {subfolder, filename} can become a
URL, or a place on disk, without escaping OUTPUT_ROOT or another user's
workspace.

check-65-output-filename-confinement.py and check-60's part (d) already prove
this end to end (a real ComfyUI stub, a real worker, a real submit). This is
the same shapes at the function level, plus a planted symlink -- which the
e2e suite cannot easily arrange -- and the percent-encoded-separator question
neither file's docstring answers directly.
"""

from __future__ import annotations

import os
import pathlib

import pytest


@pytest.fixture(params=["hub_module", "worker_agent_module"])
def mod(request):
    return request.getfixturevalue(request.param)


# ---------------------------------------------------------------------------
# is_bare_filename -- shared by both files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "a\0b", "../etc"])
def test_is_bare_filename_rejects_unsafe_shapes(mod, name):
    assert mod.is_bare_filename(name) is False


@pytest.mark.parametrize("name", ["out.png", "ComfyUI_00001_.png", "a..b", "..hidden", "a.b.c"])
def test_is_bare_filename_accepts_ordinary_shapes(mod, name):
    assert mod.is_bare_filename(name) is True


def test_is_bare_filename_does_not_decode_percent_encoding(mod):
    """
    "%2e%2e" is the literal six-character string a manifest entry could
    carry -- it is not ".." (different characters) and it contains no raw
    "/", so is_bare_filename() passes it as bare. This is documented
    behaviour, not a gap this function is supposed to close: the guarantee
    that a percent-encoded traversal cannot escape OUTPUT_ROOT is enforced
    downstream, on the RESOLVED filesystem path -- hub.py's locate_output()
    (resolve() + is_relative_to(OUTPUT_ROOT)) and worker_agent.py's
    output_subfolder() (same pattern). is_bare_filename()'s only job is
    "no raw separator, no NUL, not a literal dot-segment"; a component that
    still needs URL-decoding before it means anything is exactly the shape
    that job does not cover.
    """
    assert mod.is_bare_filename("%2e%2e") is True
    assert mod.is_bare_filename("%2f") is True
    assert mod.is_bare_filename("..%2f..%2fetc") is True  # no RAW "/" or ".."


# ---------------------------------------------------------------------------
# output_url (hub.py)
# ---------------------------------------------------------------------------


def test_output_url_ordinary_case(hub_module):
    assert hub_module.output_url("", "out.png") == "/outputs/out.png"


def test_output_url_nested_subfolder(hub_module):
    assert (hub_module.output_url("sub1/sub2", "out.png")
            == "/outputs/sub1/sub2/out.png")


def test_output_url_none_on_traversal_subfolder(hub_module):
    assert hub_module.output_url("../etc", "out.png") is None


def test_output_url_none_on_absolute_subfolder(hub_module):
    assert hub_module.output_url("/etc", "out.png") is None


def test_output_url_none_on_separator_in_filename(hub_module):
    assert hub_module.output_url("", "a/b.png") is None


def test_output_url_none_on_empty_filename(hub_module):
    assert hub_module.output_url("", "") is None


def test_output_url_none_on_non_string_inputs(hub_module):
    assert hub_module.output_url(None, "out.png") is None
    assert hub_module.output_url("", None) is None
    assert hub_module.output_url(123, "out.png") is None


def test_output_url_percent_encoded_separator_in_a_component_still_builds_a_url(hub_module):
    """Same documented gap as is_bare_filename() above, one layer up: a
    subfolder component of the literal string "%2e%2e" is not rejected by
    output_url() either, because it delegates entirely to is_bare_filename().
    The URL this produces is /outputs/%2e%2e/out.png -- inert unless
    something downstream decodes it before re-checking containment; hub.py's
    own /outputs/{path:path} route receives an already-decoded path from
    Starlette and re-resolves it against OUTPUT_ROOT in locate_output(), so
    this is not independently exploitable through the URL alone. Recorded
    here as what output_url() does, not what locate_output() promises."""
    assert hub_module.output_url("%2e%2e", "out.png") == "/outputs/%2e%2e/out.png"


# ---------------------------------------------------------------------------
# rewrite_image_urls (hub.py) -- a raw ComfyUI `executed` event
# ---------------------------------------------------------------------------


def _executed_event(images):
    return {"type": "executed", "data": {"output": {"images": images}}}


def test_rewrite_image_urls_adds_a_url_for_an_ordinary_image(hub_module):
    event = _executed_event([{"filename": "out.png", "subfolder": "", "type": "output"}])
    result = hub_module.rewrite_image_urls(event)

    assert result["data"]["output"]["images"][0]["url"] == "/outputs/out.png"


def test_rewrite_image_urls_refuses_a_traversal_subfolder(hub_module):
    """This is G5/the check-10 regression: a live `executed` event is
    forwarded from ComfyUI verbatim, with none of the worker's own
    confinement applied first."""
    event = _executed_event(
        [{"filename": "out.png", "subfolder": "../../etc", "type": "output"}])
    result = hub_module.rewrite_image_urls(event)

    assert "url" not in result["data"]["output"]["images"][0]


def test_rewrite_image_urls_refuses_a_separator_inside_filename(hub_module):
    event = _executed_event(
        [{"filename": "../secret.txt", "subfolder": "", "type": "output"}])
    result = hub_module.rewrite_image_urls(event)

    assert "url" not in result["data"]["output"]["images"][0]


def test_rewrite_image_urls_refuses_an_empty_subfolder_component(hub_module):
    event = _executed_event(
        [{"filename": "out.png", "subfolder": "/etc", "type": "output"}])
    result = hub_module.rewrite_image_urls(event)

    assert "url" not in result["data"]["output"]["images"][0]


def test_rewrite_image_urls_drops_a_stale_url_on_refusal(hub_module):
    """A pre-populated 'url' key (however it got there) must not survive a
    refusal -- popped, not left stale, so nothing downstream reads an old
    value as if it were still valid for this event."""
    event = _executed_event(
        [{"filename": "out.png", "subfolder": "..", "url": "/outputs/stale.png",
          "type": "output"}])
    result = hub_module.rewrite_image_urls(event)

    assert "url" not in result["data"]["output"]["images"][0]


def test_rewrite_image_urls_leaves_ordinary_shapes_with_a_url(hub_module):
    event = _executed_event([
        {"filename": "a.png", "subfolder": "sub", "type": "output"},
        {"filename": "b.png", "subfolder": "", "type": "temp"},
    ])
    result = hub_module.rewrite_image_urls(event)
    images = result["data"]["output"]["images"]

    assert images[0]["url"] == "/outputs/sub/a.png"
    assert images[1]["url"] == "/outputs/b.png"


def test_rewrite_image_urls_no_images_key_is_a_no_op(hub_module):
    event = {"type": "executing", "data": {"node": "1"}}
    assert hub_module.rewrite_image_urls(event) is event


def test_rewrite_image_urls_skips_a_malformed_image_entry(hub_module):
    """An image list entry that is not a dict, or has no 'filename' at all,
    must not raise -- it is left exactly as ComfyUI (or a hostile custom
    node) sent it."""
    event = _executed_event(["not-a-dict", {"subfolder": "x", "type": "output"}])
    result = hub_module.rewrite_image_urls(event)
    images = result["data"]["output"]["images"]

    assert images[0] == "not-a-dict"
    assert "url" not in images[1]


# ---------------------------------------------------------------------------
# scoped_prefix (worker_agent.py)
# ---------------------------------------------------------------------------


def test_scoped_prefix_prepends_the_workspace(worker_agent_module):
    assert (worker_agent_module.scoped_prefix("alice-abc", "myrun")
            == "alice-abc/myrun")


def test_scoped_prefix_is_idempotent(worker_agent_module):
    """A prefix already inside this workspace is left alone -- a requeued
    job (Q2) must not nest one level deeper each retry."""
    once = worker_agent_module.scoped_prefix("alice-abc", "myrun")
    twice = worker_agent_module.scoped_prefix("alice-abc", once)

    assert once == twice == "alice-abc/myrun"


def test_scoped_prefix_defaults_a_blank_prefix(worker_agent_module):
    result = worker_agent_module.scoped_prefix("alice-abc", "   ")
    assert result == f"alice-abc/{worker_agent_module.DEFAULT_FILENAME_PREFIX}"


def test_scoped_prefix_defaults_a_non_string_prefix(worker_agent_module):
    result = worker_agent_module.scoped_prefix("alice-abc", None)
    assert result == f"alice-abc/{worker_agent_module.DEFAULT_FILENAME_PREFIX}"


def test_scoped_prefix_rejects_absolute_path(worker_agent_module):
    with pytest.raises(ValueError):
        worker_agent_module.scoped_prefix("alice-abc", "/etc/passwd")


def test_scoped_prefix_rejects_traversal(worker_agent_module):
    with pytest.raises(ValueError):
        worker_agent_module.scoped_prefix("alice-abc", "../../etc/passwd")


def test_scoped_prefix_rejects_backslash_and_nul(worker_agent_module):
    with pytest.raises(ValueError):
        worker_agent_module.scoped_prefix("alice-abc", "a\\b")
    with pytest.raises(ValueError):
        worker_agent_module.scoped_prefix("alice-abc", "a\0b")


def test_scoped_prefix_does_not_double_prefix_someone_elses_workspace_name(worker_agent_module):
    """A prefix that happens to spell a DIFFERENT workspace's name is not
    special-cased -- it is prefixed like anything else, landing inside the
    caller's own workspace under a name that looks like someone else's but
    resolves nowhere near them."""
    result = worker_agent_module.scoped_prefix("alice-abc", "bob-def/run")
    assert result == "alice-abc/bob-def/run"


def test_scope_workflow_outputs_rewrites_every_save_node(worker_agent_module):
    workflow = {
        "1": {"class_type": "KSampler", "inputs": {}},
        "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "myrun"}},
        "3": {"class_type": "SaveImage", "inputs": {"filename_prefix": "../evil"}},
    }
    with pytest.raises(ValueError):
        worker_agent_module.scope_workflow_outputs(workflow, "alice-abc")

    # Node 2, processed before node 3 raised, is left rewritten -- the caller
    # (run_job) fails the whole job on the exception, so a partially-rewritten
    # workflow is never submitted; this just pins that the function does not
    # try to undo node 2's rewrite on its own.
    assert workflow["2"]["inputs"]["filename_prefix"] == "alice-abc/myrun"


def test_scope_workflow_outputs_counts_only_nodes_with_the_input(worker_agent_module):
    workflow = {
        "1": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "a"}},
        "3": "not-a-node",
    }
    count = worker_agent_module.scope_workflow_outputs(workflow, "alice-abc")
    assert count == 1


# ---------------------------------------------------------------------------
# output_subfolder (worker_agent.py) -- touches the filesystem
# ---------------------------------------------------------------------------


def test_output_subfolder_empty_filename_is_passed_through(worker_agent_module, worker_output_root):
    subfolder, filename = worker_agent_module.output_subfolder("alice-abc", "sub", "")
    assert (subfolder, filename) == ("sub", "")


def test_output_subfolder_rejects_a_filename_that_is_not_bare(worker_agent_module, worker_output_root):
    subfolder, filename = worker_agent_module.output_subfolder(
        "alice-abc", "sub", "../escape.png")
    assert filename == ""


def test_output_subfolder_already_inside_the_workspace_is_left_alone(worker_agent_module, worker_output_root):
    """No file needs to exist on disk for this branch: reported is checked
    for containment inside ws_root before any exists()/replace() call."""
    subfolder, filename = worker_agent_module.output_subfolder(
        "alice-000000000000", "alice-000000000000/nested", "out.png")
    assert (subfolder, filename) == ("alice-000000000000/nested", "out.png")


def test_output_subfolder_nonexistent_file_is_named_but_not_moved(worker_agent_module, worker_output_root):
    """A manifest entry pointing at a file that is not actually on disk --
    nothing to move, nothing to serve -- is reported under the caller's
    workspace anyway, per output_subfolder()'s own docstring, rather than
    from the shared root it doesn't belong to."""
    subfolder, filename = worker_agent_module.output_subfolder(
        "alice-abc", "somewhere", "ghost.png")
    assert subfolder == "alice-abc/somewhere"
    assert filename == "ghost.png"


def test_output_subfolder_moves_a_real_file_into_the_workspace(worker_agent_module, worker_output_root):
    (worker_output_root / "somewhere").mkdir()
    source = worker_output_root / "somewhere" / "out.png"
    source.write_bytes(b"fake-png")

    subfolder, filename = worker_agent_module.output_subfolder(
        "alice-abc", "somewhere", "out.png")

    assert subfolder == "alice-abc/somewhere"
    assert filename == "out.png"
    assert not source.exists()
    assert (worker_output_root / "alice-abc" / "somewhere" / "out.png").read_bytes() == b"fake-png"


def test_output_subfolder_rejects_a_subfolder_that_resolves_outside_output_root(
        worker_agent_module, worker_output_root):
    subfolder, filename = worker_agent_module.output_subfolder(
        "alice-abc", "../../../../outside", "out.png")
    assert filename == ""


def test_output_subfolder_follows_and_rejects_a_symlink_escaping_output_root(
        worker_agent_module, worker_output_root, tmp_path_factory):
    """A symlink already sitting in the output volume -- planted by an
    earlier job, or a hostile custom node -- must not let a reported output
    resolve outside OUTPUT_ROOT. resolve() follows the symlink; the
    containment check runs on the RESOLVED path, same pattern as
    workspace_path()."""
    outside = tmp_path_factory.mktemp("outside-output-root")
    (outside / "secret.png").write_bytes(b"not yours")

    (worker_output_root / "escape-link").symlink_to(outside)

    subfolder, filename = worker_agent_module.output_subfolder(
        "alice-abc", "escape-link", "secret.png")

    assert filename == ""
    # And the planted file is untouched -- refusal, not a move-then-refuse.
    assert (outside / "secret.png").read_bytes() == b"not yours"


def test_output_subfolder_refuses_a_file_inside_another_users_workspace(
        worker_agent_module, worker_output_root):
    """
    W5 (AUDIT-AND-PLAN.md). output_subfolder() checks that the reported path
    resolves inside OUTPUT_ROOT and is not already inside THIS caller's own
    workspace; "not inside mine" used to be satisfied by "inside somebody
    else's", and the move is an os.replace -- so a manifest entry naming a
    file that belongs to another submitter was moved into the caller's
    workspace and served to them: a cross-user read that was also a
    deletion. Now a reported path whose first component looks like another
    workspace is refused: nothing moves, nothing is served, and the caller
    gets their own workspace back with an empty filename.
    """
    bob_dir = worker_output_root / "bob-111111111111"
    bob_dir.mkdir()
    bobs_file = bob_dir / "out.png"
    bobs_file.write_bytes(b"bobs-image")

    subfolder, filename = worker_agent_module.output_subfolder(
        "alice-000000000000", "bob-111111111111", "out.png")

    assert (subfolder, filename) == ("alice-000000000000", "")  # refused, not served
    assert bobs_file.read_bytes() == b"bobs-image"  # bob's file untouched
    assert not (worker_output_root / "alice-000000000000" / "bob-111111111111").exists()


# ---------------------------------------------------------------------------
# workspace_path (worker_agent.py) -- the symlink case one layer down
# ---------------------------------------------------------------------------


def test_workspace_path_rejects_a_symlinked_workspace_escaping_output_root(
        worker_agent_module, worker_output_root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-workspace")
    (worker_output_root / "evil-000000000000").symlink_to(outside)

    with pytest.raises(ValueError):
        worker_agent_module.workspace_path("evil-000000000000")


def test_workspace_path_accepts_an_ordinary_workspace_name(worker_agent_module, worker_output_root):
    resolved = worker_agent_module.workspace_path("alice-000000000000")
    assert resolved == worker_output_root / "alice-000000000000"
