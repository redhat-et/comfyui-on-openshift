#!/usr/bin/env bash
#
# Unit tests for the pure helpers in scripts/lib/common.sh — the parsing and
# formatting logic where this repo's subtlest bugs have lived. Each case here
# pins a behavior that once went wrong (or visibly could): the tab-separated
# AWS CLI list that defeated a space-padded pattern match, the untagged image
# ref that became its own kustomize tag.
#
# No cluster, no AWS, no network, sub-second. `make test` runs this before
# the e2e suite; CI runs `make test`.
#
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

FAILURES=0

pass()
{
    printf '  \033[1;32mPASS\033[0m  %s\n' "$*" >&2
}

fail()
{
    printf '  \033[1;31mFAIL\033[0m  %s\n' "$*" >&2
    FAILURES=$(( FAILURES + 1 ))
}

expect_true()
{
    local desc="$1"
    shift

    if "$@"; then pass "$desc"; else fail "$desc"; fi
}

expect_false()
{
    local desc="$1"
    shift

    if "$@"; then fail "$desc"; else pass "$desc"; fi
}

expect_output()
{
    local desc="$1" expected="$2" actual="$3"

    if [[ "$expected" == "$actual" ]]; then
        pass "$desc"
    else
        fail "$desc"
        info "expected: $(printf '%q' "$expected")"
        info "actual:   $(printf '%q' "$actual")"
    fi
}

# ---------------------------------------------------------------------------

log "list_contains — AWS CLI text output is TAB-separated"

TAB_LIST="$(printf 'us-east-2a\tus-east-2b\tus-east-2c')"

expect_true  "finds the first element of a tab-separated list"  list_contains "$TAB_LIST" us-east-2a
expect_true  "finds a middle element of a tab-separated list"   list_contains "$TAB_LIST" us-east-2b
expect_true  "finds the last element of a tab-separated list"   list_contains "$TAB_LIST" us-east-2c
expect_false "rejects an element that is not in the list"       list_contains "$TAB_LIST" us-west-2a
expect_false "rejects a prefix of a real element"               list_contains "$TAB_LIST" us-east-2
expect_true  "single-element list still matches"                list_contains "us-east-2a" us-east-2a
expect_true  "space-separated lists (kubectl jsonpath) work"    list_contains "node-a node-b" node-b
expect_true  "newline-separated lists work"                     list_contains "$(printf 'a\nb\nc')" b
expect_false "empty list contains nothing"                      list_contains "" us-east-2a

# ---------------------------------------------------------------------------

log "kustomize_image_fields — digest refs, tags, and the untagged edge cases"

expect_output "digest ref uses digest:, split at @" \
    "$(printf '    newName: registry/ns/comfyui\n    digest: sha256:abc123\n')" \
    "$(kustomize_image_fields "registry/ns/comfyui@sha256:abc123")"

expect_output "tagged ref uses newTag" \
    "$(printf '    newName: quay.io/you/comfyui\n    newTag: v1\n')" \
    "$(kustomize_image_fields "quay.io/you/comfyui:v1")"

expect_output "untagged ref defaults to latest instead of eating the ref" \
    "$(printf '    newName: quay.io/you/comfyui\n    newTag: latest\n')" \
    "$(kustomize_image_fields "quay.io/you/comfyui")"

expect_output "registry port is not mistaken for a tag" \
    "$(printf '    newName: registry:5000/ns/comfyui\n    newTag: latest\n')" \
    "$(kustomize_image_fields "registry:5000/ns/comfyui")"

expect_output "registry port plus a real tag still resolves the tag" \
    "$(printf '    newName: registry:5000/ns/comfyui\n    newTag: v2\n')" \
    "$(kustomize_image_fields "registry:5000/ns/comfyui:v2")"

# ---------------------------------------------------------------------------

printf '\n'

if (( FAILURES == 0 )); then
    log "Unit tests clean"
else
    die "$FAILURES unit test(s) failed above."
fi
