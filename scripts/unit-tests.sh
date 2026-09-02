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

log "enterprise/test/run.sh — a check's failure reaches the suite's exit code"

# TWO independent folds, and one fixture each, because a fixture that breaks
# both rules at once cannot say which one caught it — which is precisely how
# the second fold below stayed missing for so long. The old fixture here both
# printed a FAIL line and exited 1; only the exit code was ever folded, so the
# printed half was decoration and nobody could tell.
#
#   1. EXIT STATUS. run.sh discovers checks by glob rather than by name (F3,
#      docs/10-roadmap.md): a check that is dropped into enterprise/test/ and
#      mentioned nowhere else must still run, and its non-zero exit must still
#      become the suite's. Before F3 the files were copied by a glob but
#      invoked by hardcoded name, so a new check was copied, never run, and
#      the suite stayed green.
#
#   2. A PRINTED FAILURE. Exit status is a contract a check can hold up to and
#      still lie: every check prints its assertions through the same check()
#      helper and turns the collected failures into its exit code in its last
#      few lines. Lose those lines and the check prints its FAIL lines exactly
#      as loudly as ever, exits 0, and the suite — and CI, which reads the same
#      status — stays green over output that says otherwise. run.sh therefore
#      reads each check's output for the helper's own line format as well.
#
# Both fixtures are named check-00-* so they sort AHEAD of every real check.
# run.sh stops at the first failing check, so each run here costs the suite's
# startup and one check rather than a full pass — and discovery is proven just
# as well, since nothing in run.sh names either file.
#
# These two run the real e2e suite — real Redis, stub ComfyUI, real
# hub.py/worker_agent.py — so they need the same dependencies `make test`
# already needs (see enterprise/test/README.md).

RUN_SH="$REPO_ROOT/enterprise/test/run.sh"
EXIT_STATUS_CHECK="$REPO_ROOT/enterprise/test/check-00-zz-exit-status.py"
PRINTED_FAIL_CHECK="$REPO_ROOT/enterprise/test/check-00-zz-printed-fail.py"
DISCOVERY_LOG="$(mktemp -t comfy-run-sh-discovery.XXXXXX)"

cleanup_discovery_checks()
{
    rm -f "$EXIT_STATUS_CHECK" "$PRINTED_FAIL_CHECK" "$DISCOVERY_LOG"
}
trap cleanup_discovery_checks EXIT

suite_fails()
{
    local rc=0
    "$RUN_SH" > "$DISCOVERY_LOG" 2>&1 || rc=$?
    (( rc != 0 ))
}

# Fixture 1: a non-zero exit and NOT ONE printed FAIL line, so the only fold
# that can catch it is the exit status.
cat > "$EXIT_STATUS_CHECK" <<'CHECKEOF'
# Deliberately-failing check written into enterprise/test/ (run.sh's `cp *.py`
# discovery path) by scripts/unit-tests.sh and removed again immediately.
# It exits non-zero and prints NO failure line, so it isolates one fold: a
# suite that still exits 0 with this file present has no check discovery.
import sys
print("  this check exits non-zero and deliberately prints no failure line")
sys.exit(1)
CHECKEOF

expect_true "a check dropped into enterprise/test/ but never wired into run.sh still fails the suite when it exits non-zero" \
    suite_fails

rm -f "$EXIT_STATUS_CHECK"

# Fixture 2: a printed FAIL line and a CLEAN exit, so the only fold that can
# catch it is the one that reads the output.
cat > "$PRINTED_FAIL_CHECK" <<'CHECKEOF'
# The other half, written and removed by scripts/unit-tests.sh: a check that
# reports a failed assertion in the format every check in this directory
# reports one, and then exits 0 — a check whose exit status has stopped
# reporting what its own assertions found. The suite must not be green.
import sys
print("  FAIL  a printed failure that exits 0 must still fail the suite")
sys.exit(0)
CHECKEOF

expect_true "a check that prints a FAIL line and exits 0 anyway still fails the suite" \
    suite_fails

rm -f "$PRINTED_FAIL_CHECK"

# And the marker that fold uses, read out of run.sh itself rather than
# restated here: a marker widened to a bare search for the word would fail a
# suite on its own checks' prose, which is a green suite's opposite failure
# mode and just as bad. Sub-millisecond, so every one of these is a separate
# case rather than one line of alternation.
FAIL_MARKER="$(sed -n "s/^FAIL_MARKER='\(.*\)'\$/\1/p" "$RUN_SH")"

matches_fail_marker()
{
    printf '%s\n' "$1" | grep -q "$FAIL_MARKER"
}

expect_true  "run.sh names the printed-failure marker in one place, where this can read it" \
    test -n "$FAIL_MARKER"
expect_true  "the marker fires on the check() helper's own failure line" \
    matches_fail_marker "  FAIL  the assertion name"
expect_false "the marker ignores the helper's PASS line" \
    matches_fail_marker "  PASS  the assertion name"
expect_false "the marker ignores the 'N FAILED: [...]' summary every check prints" \
    matches_fail_marker "3 FAILED: ['one', 'two', 'three']"
expect_false "the marker ignores an assertion NAME that merely contains the word" \
    matches_fail_marker "  PASS  a wedged ComfyUI must FAIL the job rather than park on it"
expect_false "the marker ignores a FAIL echoed back in a detail field" \
    matches_fail_marker "  PASS  job state  {'status': 'FAILED', 'phase': 'executing'}"
expect_false "the marker ignores run.sh's own indented progress lines" \
    matches_fail_marker "    check-10-stream.py FAILED (rc 1, 2 printed FAIL line(s))"

cleanup_discovery_checks
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

# Drop a deliberately-broken manifest into the directory lint already scans,
# and assert lint not only FAILS but fails for the stated reason.
#
# The second argument is not optional politeness. Asserting only on the exit
# code makes every fixture less specific each time lint gains a rule: a fixture
# written to test one thing eventually violates four, and then the assertion
# passes with its own rule deleted. That happened here — the toleration fixture
# was written before the F1 sizing rules existed, still carried the old
# 8Gi/24Gi block, and kept passing when the toleration check was replaced with
# `if False:`. Matching the message is what keeps a fixture honest as lint
# grows around it.
lint_fails_on_manifest_fixture()
{
    local fixture="$1"
    local expect="$2"
    # Declared and assigned separately: `local x="$(...)"` masks the command
    # substitution's exit status, which shellcheck flags (SC2155).
    local target
    target="$REPO_ROOT/enterprise/manifests/zz-fixture-$(basename "$fixture")"
    local rc=0

    cp "$fixture" "$target"
    "$LINT_SH" > "$LINT_LOG" 2>&1 || rc=$?
    rm -f "$target"

    (( rc != 0 )) && grep -qF "$expect" "$LINT_LOG"
}

expect_true "lint fails a worker manifest that lost its nvidia.com/gpu toleration" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/worker-no-gpu-toleration.yaml" \
    "tolerates no nvidia.com/gpu taint"

expect_true "lint fails a Route that lost timeout-tunnel" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/route-missing-timeout-tunnel.yaml" \
    "has no haproxy.router.openshift.io/timeout-tunnel"

expect_true "lint fails a gateway Service that regained the gateway's own container port" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/gateway-svc-exposes-container-port.yaml" \
    "bypasses the login"

cleanup_manifest_fixture_drops
trap - EXIT

# ---------------------------------------------------------------------------

log "scripts/lint.sh — worker memory limit vs. smallest supported GPU instance (F1, docs/10-roadmap.md)"

# F1 (docs/10-roadmap.md): enterprise/manifests/02-worker.yaml requested
# memory: 8Gi and limited memory: 24Gi, and manifests/base/deployment.yaml
# carried the identical block. The smallest GPU instance type this
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
# Unlike the three fixtures above, this is not a hypothetical regression:
# the fixture mirrors byte-for-byte, for the fields that matter, the shape
# both manifests shipped with until F1 fixed them. They are now 10Gi/10Gi
# and 2/2 — sized to what a 16 GiB node can actually give one pod, with
# requests equal to limits so the pod is Guaranteed QoS rather than the
# first thing evicted. scripts/lint.sh holds both halves; this assertion is
# what proves it still does.

trap cleanup_manifest_fixture_drops EXIT

expect_true "lint fails a worker manifest whose memory limit does not fit the smallest supported GPU instance type" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/worker-memory-exceeds-smallest-instance.yaml" \
    "limits memory to '24Gi'"

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

log "scripts/lint.sh — the queue payload envelope is mirrored (F2, docs/10-roadmap.md)"

# F2 defines the queue payload envelope once and duplicates it verbatim between
# the BEGIN/END SHARED ENVELOPE markers in enterprise/gateway/hub.py and
# enterprise/worker/worker_agent.py: the two files ship in two images built from
# two different contexts, so there is nowhere to import a shared definition
# from. "Change both or neither" is the rule the processing-list key shape
# already followed, and it is exactly the kind of rule that survives review and
# dies six months later — so scripts/lint.sh diffs the two copies.
#
# Like the Containerfile case above, this has no fixture file: a copy of hub.py
# dropped somewhere else would not be one of the two files lint compares, so it
# would test nothing. The only faithful test drifts the real hub.py the way a
# later item plausibly would — one side gains a reserved field the other has
# never heard of, which is precisely the rolling-deploy hazard F2 exists to
# prevent — runs lint, and restores the file immediately after, whatever the
# assertion's outcome.

HUB_PY="$REPO_ROOT/enterprise/gateway/hub.py"
HUB_PY_BACKUP="$(mktemp -t comfy-hub-backup.XXXXXX)"
ENVELOPE_LOG="$(mktemp -t comfy-envelope-lint.XXXXXX)"
cp "$HUB_PY" "$HUB_PY_BACKUP"

restore_hub_py()
{
    # Idempotent, for the same reason restore_containerfile is.
    [[ -f "$HUB_PY_BACKUP" ]] && cp "$HUB_PY_BACKUP" "$HUB_PY"
}
trap restore_hub_py EXIT

python3 - "$HUB_PY" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()
line = '        "submitted_at": time.time() if submitted_at is None else submitted_at,\n'
assert content.count(line) == 1, "the envelope's producer side moved — update this test"
with open(path, "w") as f:
    f.write(content.replace(line, line + '        "lane_priority": 0,\n', 1))
PYEOF

lint_fails_on_diverged_envelope()
{
    local rc=0
    "$LINT_SH" > "$ENVELOPE_LOG" 2>&1 || rc=$?
    (( rc != 0 ))
}

expect_true "lint fails when one copy of the shared envelope block gains a field the other lacks" \
    lint_fails_on_diverged_envelope

restore_hub_py
trap - EXIT
rm -f "$HUB_PY_BACKUP" "$ENVELOPE_LOG"

# ---------------------------------------------------------------------------

log "scripts/lint.sh — namespace hardening shapes (W4, P8, P9)"

# The rules added with the Redis ACL, the ServiceAccount-token removal and the
# namespace NetworkPolicy. Same mechanism as the manifest fixtures above — a
# deliberately-broken file copied under a zz-fixture- name into the directory
# lint already scans, then removed — and the same rule about the expected
# message: each fixture below violates exactly one invariant and the assertion
# names it, so a fixture cannot keep passing once the rule it was written for
# is deleted.
#
# Every one of these is invisible to enterprise/test/run.sh by construction. It
# runs one Redis with one password on one laptop, no Kubernetes at all, so it
# can see neither a pod's ServiceAccount token, nor a NetworkPolicy, nor which
# Redis user a connection authenticated as.

LINT_LOG="$(mktemp -t comfy-lint-fixture.XXXXXX)"

trap cleanup_manifest_fixture_drops EXIT

expect_true "lint fails a worker manifest that mounts a ServiceAccount token nothing in the pod uses" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/worker-mounts-sa-token.yaml" \
    "does not set automountServiceAccountToken: false"

# W4's two halves, one fixture each. The Secret key is the loud half — a
# reviewer can see `key: password` in a diff — and the URL is the silent one:
# a redis:// URL with no username authenticates as `default` however correct
# the password beside it, so the least-privilege user is bypassed with every
# pod healthy and every job running. A single fixture breaking both would pass
# with either rule deleted.
expect_true "lint fails a worker manifest that takes the ADMIN Redis password" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/worker-holds-admin-redis-password.yaml" \
    "the ADMIN Redis credential"

expect_true "lint fails a worker manifest whose REDIS_URL names no user, silently authenticating as default" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/worker-redis-url-has-no-user.yaml" \
    "with no username"

# The regression the namespace default-deny creates, and the only one it
# creates. A Deployment nothing selects is not left unrestricted, it is cut
# off — and it fails silently: Ready pod, passing probes, every connection
# timing out, nothing in the events naming a network policy.
expect_true "lint fails a Deployment in the multi-user namespace that no NetworkPolicy selects" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/deployment-no-network-policy.yaml" \
    "is not selected by any NetworkPolicy"

# I1's warm floor. The failure is not that KEDA rejects this — it accepts it
# happily, clamps to maxReplicaCount, and the floor simply never arrives, with
# no error anywhere and a person waiting out a cold start at nine in the
# morning that the setting existed to have already paid for.
expect_true "lint fails a ScaledObject whose warm floor asks for more workers than maxReplicaCount allows" \
    lint_fails_on_manifest_fixture "$LINT_FIXTURES/warm-floor-above-max-replicas.yaml" \
    "warm floor that never arrives"

cleanup_manifest_fixture_drops
trap - EXIT

# ---------------------------------------------------------------------------

printf '\n'

if (( FAILURES == 0 )); then
    log "Unit tests clean"
else
    die "$FAILURES unit test(s) failed above."
fi
