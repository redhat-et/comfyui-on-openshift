"""
workspace_name() -- BEGIN SHARED WORKSPACE in both hub.py and worker_agent.py,
mirrored verbatim (scripts/lint.sh diffs the two copies). check-60-user-
workspaces.py already proves the end-to-end behaviour through a real submit;
this is the same hostile-input list, plus unicode identity questions the e2e
suite has no reason to ask, at the function level and against both copies.
"""

from __future__ import annotations

import re

import pytest

SLUG_SHAPE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{12}$")


@pytest.fixture(params=["hub_module", "worker_agent_module"])
def mod(request):
    return request.getfixturevalue(request.param)


# ---------------------------------------------------------------------------
# check-60's hostile strings
# ---------------------------------------------------------------------------

HOSTILE_USERS = [
    ("path traversal", "../../../../../../../../tmp/evil"),
    ("absolute path", "/etc/passwd"),
    ("very long (2000 chars)", "a" * 2000),
]


@pytest.mark.parametrize("desc,user", HOSTILE_USERS, ids=[d for d, _ in HOSTILE_USERS])
def test_hostile_username_produces_a_safe_single_component_name(mod, desc, user):
    name = mod.workspace_name(user)

    assert "/" not in name
    assert "\\" not in name
    assert "\0" not in name
    assert name not in (".", "..")
    assert SLUG_SHAPE.match(name), f"{desc}: {name!r} does not match <slug>-<12 hex>"


def test_empty_string_is_the_anonymous_workspace(mod):
    assert mod.workspace_name("") == mod.ANON_WORKSPACE


def test_anonymous_workspace_cannot_be_produced_by_a_real_username(mod):
    """ANON_WORKSPACE starts with '_', a character the slug rule never
    emits (WORKSPACE_UNSAFE collapses it to '-'), so no real username can
    alias onto the anonymous bucket."""
    for _, hostile in HOSTILE_USERS:
        assert mod.workspace_name(hostile) != mod.ANON_WORKSPACE
    assert mod.workspace_name("_anonymous") != mod.ANON_WORKSPACE


def test_oauth_shaped_username_is_not_over_sanitized(mod):
    name = mod.workspace_name("alice.smith@example.com")

    assert name.startswith("alice-smith-example-com-")
    assert SLUG_SHAPE.match(name)


def test_2000_char_username_is_bounded(mod):
    name = mod.workspace_name("a" * 2000)

    # MAX_WORKSPACE_SLUG_CHARS (40) + "-" + WORKSPACE_DIGEST_CHARS (12)
    assert len(name) <= mod.MAX_WORKSPACE_SLUG_CHARS + 1 + mod.WORKSPACE_DIGEST_CHARS


# ---------------------------------------------------------------------------
# the slug allowlist and the digest length, pinned directly
# ---------------------------------------------------------------------------


def test_digest_suffix_is_exactly_the_configured_length(mod):
    name = mod.workspace_name("alice")
    digest = name.rsplit("-", 1)[-1]

    assert len(digest) == mod.WORKSPACE_DIGEST_CHARS
    assert re.fullmatch(r"[0-9a-f]+", digest)


def test_slug_half_is_lowercase_alnum_and_hyphen_only(mod):
    name = mod.workspace_name("Alice.Smith_the-2nd!!")
    slug = name.rsplit("-", 1)[0]

    assert re.fullmatch(r"[a-z0-9-]*", slug)


def test_username_that_slugs_to_nothing_falls_back_to_user(mod):
    """A username made entirely of characters outside the allowlist (every
    run collapses to '-', which is then stripped) must not produce an empty
    or bare-hyphen slug half -- workspace_name() falls back to the literal
    string "user" so the digest still separates it from every other such
    name."""
    name = mod.workspace_name("!!!###???")

    assert name.startswith("user-")
    assert SLUG_SHAPE.match(name)


# ---------------------------------------------------------------------------
# two users, one collision surface: same slug, different digest
# ---------------------------------------------------------------------------


def test_two_users_sharing_a_truncated_slug_get_different_names(mod):
    """Two usernames that agree on their first 40 allowlisted characters
    (MAX_WORKSPACE_SLUG_CHARS) slug identically -- the digest is the only
    thing that can still tell them apart."""
    base = "a" * 40
    user_1 = base + "-one"
    user_2 = base + "-two"

    name_1 = mod.workspace_name(user_1)
    name_2 = mod.workspace_name(user_2)

    slug_1 = name_1[: mod.MAX_WORKSPACE_SLUG_CHARS]
    slug_2 = name_2[: mod.MAX_WORKSPACE_SLUG_CHARS]
    assert slug_1 == slug_2, "the premise of this test: the slugs must collide"
    assert name_1 != name_2, "the digest must still separate them"


def test_two_visually_similar_short_names_that_slug_identically_differ(mod):
    """"a/b" and "a-b" both slug to "a-b" (the separator collapses to the
    same '-' the literal hyphen already is) -- distinct usernames, identical
    readable half, and the digest is what keeps their directories apart."""
    name_1 = mod.workspace_name("a/b")
    name_2 = mod.workspace_name("a-b")

    assert name_1.rsplit("-", 1)[0] == name_2.rsplit("-", 1)[0] == "a-b"
    assert name_1 != name_2


# ---------------------------------------------------------------------------
# unicode: mixed script, and NFC vs NFD -- tested for what the code actually
# does, since WORKSPACE_UNSAFE = re.compile(r"[^A-Za-z0-9]+") is an ASCII-only
# allowlist and hashlib.sha256 hashes raw UTF-8 bytes with no normalization
# step anywhere in workspace_name().
# ---------------------------------------------------------------------------


def test_unicode_username_still_produces_a_valid_confined_name(mod):
    name = mod.workspace_name("wŏrker_日本語_ключ")

    assert SLUG_SHAPE.match(name)


def test_cyrillic_homoglyph_does_not_collide_with_the_ascii_lookalike(mod):
    """"аlice" opens with Cyrillic а (U+0430), not ASCII 'a' -- a
    classic homoglyph a phishing username or copy-paste from a compromised
    IdP could produce. The allowlist regex is ASCII-only, so the Cyrillic
    character collapses to '-' where "alice" would keep its 'a'; combined
    with the digest (computed over different UTF-8 bytes either way) the two
    must not name the same directory."""
    ascii_name = mod.workspace_name("alice")
    homoglyph_name = mod.workspace_name("аlice")

    assert ascii_name != homoglyph_name


def test_nfc_and_nfd_encodings_of_the_same_identity_share_one_workspace(mod):
    """
    "cafe" is not the interesting case here -- an accented "e" is, because
    Unicode has two ways to spell it that render identically:

      NFC: "caf\u00e9"        -- 4 code points, precomposed e-acute
      NFD: "cafe\u0301"       -- 5 code points, plain 'e' + combining acute

    A real IdP can hand back either form for what a human reading two support
    tickets would call the same username, and the proxy in front of the
    gateway and the worker behind the queue need not agree on which. Before
    workspace_name() normalised, the two forms hashed to two digests and the
    same person got two unrelated workspaces -- and, once the gateway scoped
    /outputs by the caller's computed name, a 403 on their own files whenever
    the spellings differed. NFC is applied before the slug and the digest, so
    both forms name one directory.
    """
    nfc = "caf\u00e9"        # precomposed e-acute, 4 code points
    nfd = "cafe\u0301"       # 'e' + combining acute accent, 5 code points
    assert nfc != nfd  # the premise: two distinct Python strings...
    import unicodedata
    assert unicodedata.normalize("NFC", nfd) == nfc  # ...that a human, or a
    # normalizing IdP, would call identical.

    assert mod.workspace_name(nfc) == mod.workspace_name(nfd)
    assert mod.workspace_name(nfc).startswith("caf-")  # the accent is still not slug-safe
