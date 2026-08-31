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

log "enterprise/test/run.sh — check discovery (F3, docs/10-roadmap.md)"

# run.sh copies every enterprise/test/*.py into its work directory by glob
# (line 6), but invokes checks by hardcoded name around lines 69 and 85,
# threading only check.py/check2.py/check3.py's exit codes into RC by hand.
# A check added as a file but never wired into those two lines is copied and
# silently never run — the suite still exits 0. Prove it: drop an
# always-failing check into the suite's own discovery path (enterprise/test/,
# the directory run.sh's `cp *.py` reads) and assert the suite's own exit
# code goes non-zero.
#
# This runs the real e2e suite — real Redis, stub ComfyUI, real
# hub.py/worker_agent.py — so it needs the same dependencies `make test`
# already needs (see enterprise/test/README.md) and takes as long as one
# normal run.sh pass.

RUN_SH="$REPO_ROOT/enterprise/test/run.sh"
DISCOVERY_CHECK="$REPO_ROOT/enterprise/test/checkzz_always_fails.py"
DISCOVERY_LOG="$(mktemp -t comfy-run-sh-discovery.XXXXXX)"

cleanup_discovery_check()
{
    rm -f "$DISCOVERY_CHECK" "$DISCOVERY_LOG"
}
trap cleanup_discovery_check EXIT

cat > "$DISCOVERY_CHECK" <<'CHECKEOF'
# Deliberately-broken check dropped into enterprise/test/ (run.sh's `cp *.py`
# discovery path) by scripts/unit-tests.sh, to prove run.sh has no check
# discovery: this file is copied into the work directory but nothing in
# run.sh calls it by name, so its failure must not, by itself, change the
# suite's exit code.
import sys
print("  FAIL  checkzz_always_fails is deliberately broken")
sys.exit(1)
CHECKEOF

suite_fails_on_an_unwired_broken_check()
{
    local rc=0
    "$RUN_SH" > "$DISCOVERY_LOG" 2>&1 || rc=$?
    (( rc != 0 ))
}

expect_true "a check dropped into enterprise/test/ but never wired into run.sh still fails the suite" \
    suite_fails_on_an_unwired_broken_check

cleanup_discovery_check
trap - EXIT

# ---------------------------------------------------------------------------

log "scripts/lint.sh — manifest shape (F3, docs/10-roadmap.md)"

# scripts/lint.sh's manifest step only calls yaml.safe_load_all — it parses
# and asserts nothing about shape, so it is content with any syntactically
# valid YAML regardless of which invariant from
# docs/09-engineering-handoff.md section 3 it violates.
# scripts/lint-fixtures/manifests/ holds small, deliberately-broken fixtures
# (see the README there); each is copied under a zz-fixture- name into
# enterprise/manifests/, which scripts/lint.sh's own
# `enterprise/manifests/*.yaml` glob already scans — so the real, unmodified
# lint.sh is what runs — then the copy is removed.

LINT_SH="$REPO_ROOT/scripts/lint.sh"
LINT_FIXTURES="$REPO_ROOT/scripts/lint-fixtures/manifests"
LINT_LOG="$(mktemp -t comfy-lint-fixture.XXXXXX)"

cleanup_manifest_fixture_drops()
{
    rm -f "$REPO_ROOT"/enterprise/manifests/zz-fixture-*.yaml "$LINT_LOG"
}
trap cleanup_manifest_fixture_drops EXIT

lint_fails_on_manifest_fixture()
{
    local fixture="$1"
    local target="$REPO_ROOT/enterprise/manifests/zz-fixture-$(basename "$fixture")"
    local rc=0

    cp "$fixture" "$target"
    "$LINT_SH" > "$LINT_LOG" 2>&1 || rc=$?
    rm -f "$target"

    (( rc != 0 ))
}

expect_true "lint fails a worker manifest that lost its nvidia.com/gpu toleration" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/worker-no-gpu-toleration.yaml"

expect_true "lint fails a Route that lost timeout-tunnel" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/route-missing-timeout-tunnel.yaml"

expect_true "lint fails a gateway Service that regained the gateway's own container port" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/gateway-svc-exposes-container-port.yaml"

cleanup_manifest_fixture_drops
trap - EXIT

# ---------------------------------------------------------------------------

log "scripts/lint.sh — worker memory limit vs. smallest supported GPU instance (F1, docs/10-roadmap.md)"

# F1 (docs/10-roadmap.md): enterprise/manifests/02-worker.yaml requests
# memory: 8Gi and limits memory: 24Gi. The smallest GPU instance type this
# repo supports is a tie between g5.xlarge and g6.xlarge (scripts/06-status.sh
# lists m5.xlarge, m5.2xlarge, g5.xlarge, g6.xlarge, g6.2xlarge, g6e.xlarge and
# g4dn.xlarge; among the GPU families, g5.xlarge/g6.xlarge/g4dn.xlarge each
# have 16 GiB of system RAM, the smallest of the lot — g6.2xlarge and
# g6e.xlarge have 32 GiB. g6.xlarge is also .env.example's GPU_INSTANCE_TYPE
# default), and 16 GiB of *system* RAM has nothing to do with the 24 GB of
# *VRAM* the L4/A10G GPU itself carries. A 24Gi container memory limit is
# therefore unreachable on that node: the container can never hit its own
# cgroup ceiling, so the real ceiling is node memory pressure, which produces
# an eviction or a kernel OOM kill of the ComfyUI process instead of a clean
# container-level OOMKilled — and a burstable pod (requests 8Gi < limits
# 24Gi) whose limit exceeds node capacity is a prime eviction candidate to
# begin with.
#
# Unlike the three fixtures above, this is not a hypothetical regression —
# the real, unmodified enterprise/manifests/02-worker.yaml already has this
# shape, which the fixture mirrors byte-for-byte for the fields that matter.
# scripts/lint.sh has no check for it yet, so this assertion is written
# failing on purpose: F1 is the manifest/lint fix, not this commit.

trap cleanup_manifest_fixture_drops EXIT

expect_true "lint fails a worker manifest whose memory limit does not fit the smallest supported GPU instance type" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/worker-memory-exceeds-smallest-instance.yaml"

cleanup_manifest_fixture_drops
trap - EXIT

# ---------------------------------------------------------------------------

log "scripts/lint.sh — Containerfile arbitrary-UID block (F3, docs/10-roadmap.md)"

# The fourth shape case has no fixture file under scripts/lint-fixtures/:
# scripts/lint.sh does not scan Containerfiles by any glob or fixed list
# today (SHELL_FILES and PYTHON_FILES above both skip them), so a fixture
# dropped anywhere would not be "discovered" by lint either — that would
# test nothing. The only faithful test mutates one of the two real
# Containerfiles docs/09-engineering-handoff.md section 3 actually names,
# strips the chgrp 0 / chmod g=u block, runs lint, and restores the original
# content immediately after, regardless of the assertion's outcome.

CONTAINERFILE="$REPO_ROOT/app/Containerfile"
CONTAINERFILE_BACKUP="$(mktemp -t comfy-containerfile-backup.XXXXXX)"
cp "$CONTAINERFILE" "$CONTAINERFILE_BACKUP"

restore_containerfile()
{
    # Idempotent on purpose: the backup is left in place until the very end,
    # so this is safe to call more than once (including from the EXIT trap
    # after an explicit call already ran).
    [[ -f "$CONTAINERFILE_BACKUP" ]] && cp "$CONTAINERFILE_BACKUP" "$CONTAINERFILE"
}
trap restore_containerfile EXIT

python3 - "$CONTAINERFILE" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()
start = content.index("RUN chgrp -R 0")
end = content.index("\n", content.index("chmod g+s", start)) + 1
with open(path, "w") as f:
    f.write(content[:start] + content[end:])
PYEOF

lint_fails_on_containerfile_fixture()
{
    local rc=0
    "$LINT_SH" > "$LINT_LOG" 2>&1 || rc=$?
    (( rc != 0 ))
}

expect_true "lint fails a Containerfile that lost its chgrp 0 / chmod g=u block" \
    lint_fails_on_containerfile_fixture

restore_containerfile
trap - EXIT
rm -f "$CONTAINERFILE_BACKUP" "$LINT_LOG"

# ---------------------------------------------------------------------------

printf '\n'

if (( FAILURES == 0 )); then
    log "Unit tests clean"
else
    die "$FAILURES unit test(s) failed above."
fi
