"""
The admission decision of the gateway's one-button model install (BEGIN MODEL
INSTALLS in hub.py): validate_model_install() is the entire policy — source
allowlist, folder enum, filename confinement, format-by-name — and
looks_like_safetensors() is the format-by-content check that runs at byte
nine of the stream. Both are pure exactly so this file can enumerate the
refusals without a server, the way the network policy's comment promises a
"job that validates the source and the format".
"""

from __future__ import annotations

import struct


HF = "https://huggingface.co/some-org/some-model/resolve/main/model.safetensors"


# ---------------------------------------------------------------------------
# validate_model_install — the admission policy
# ---------------------------------------------------------------------------


def test_the_happy_path_is_admitted(hub_module):
    assert hub_module.validate_model_install(HF, "checkpoints", "model.safetensors") is None


def test_http_is_refused_even_for_an_allowlisted_host(hub_module):
    problem = hub_module.validate_model_install(
        HF.replace("https://", "http://"), "checkpoints", "model.safetensors")
    assert problem == "url must be https"


def test_a_host_off_the_allowlist_is_refused_and_named(hub_module):
    problem = hub_module.validate_model_install(
        "https://evil.example/model.safetensors", "checkpoints", "model.safetensors")
    assert "evil.example" in problem and "allowlist" in problem


def test_a_lookalike_subdomain_is_not_the_allowlisted_host(hub_module):
    """Exact hostname match: huggingface.co.evil.example must not pass a
    check that a substring or suffix comparison would wave through."""
    problem = hub_module.validate_model_install(
        "https://huggingface.co.evil.example/m.safetensors", "checkpoints", "m.safetensors")
    assert problem is not None


def test_userinfo_cannot_smuggle_the_allowlisted_host(hub_module):
    """https://huggingface.co@evil.example/ — the allowlisted name in the
    userinfo position, the real host after the @. urlsplit().hostname sees
    evil.example; this test pins that we validate that, not a prefix."""
    problem = hub_module.validate_model_install(
        "https://huggingface.co@evil.example/m.safetensors", "checkpoints", "m.safetensors")
    assert problem is not None


def test_a_folder_outside_the_enum_is_refused(hub_module):
    problem = hub_module.validate_model_install(HF, "../output", "model.safetensors")
    assert "folder" in problem


def test_a_filename_with_a_separator_is_refused(hub_module):
    problem = hub_module.validate_model_install(HF, "checkpoints", "../../evil.safetensors")
    assert "single path component" in problem


def test_a_non_safetensors_extension_is_refused(hub_module):
    problem = hub_module.validate_model_install(HF, "checkpoints", "model.ckpt")
    assert "safetensors" in problem


# ---------------------------------------------------------------------------
# looks_like_safetensors — the format check at byte nine
# ---------------------------------------------------------------------------


def _header(length: int) -> bytes:
    return struct.pack("<Q", length) + b"{"


def test_a_real_safetensors_header_passes(hub_module):
    assert hub_module.looks_like_safetensors(_header(84) + b'"tensor":')


def test_a_pickle_zip_magic_fails(hub_module):
    """A torch .ckpt renamed to .safetensors starts with PK\\x03\\x04 — the
    little-endian read of that is astronomically large, and byte nine is not
    a brace. Either check alone kills it; this pins that at least one does."""
    assert not hub_module.looks_like_safetensors(b"PK\x03\x04" + b"\x00" * 20)


def test_fewer_than_nine_bytes_fails(hub_module):
    assert not hub_module.looks_like_safetensors(_header(84)[:8])


def test_a_zero_length_header_fails(hub_module):
    assert not hub_module.looks_like_safetensors(_header(0))


def test_an_implausibly_large_header_fails(hub_module):
    assert not hub_module.looks_like_safetensors(_header(2**40))


def test_a_header_length_that_promises_no_json_fails(hub_module):
    assert not hub_module.looks_like_safetensors(struct.pack("<Q", 84) + b"X")
