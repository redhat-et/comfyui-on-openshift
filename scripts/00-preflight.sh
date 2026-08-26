#!/usr/bin/env bash
#
# Read-only. Tells you exactly which of the prerequisites are done and which
# are not, so you find out about the GPU quota before, not after, a 20-minute
# cluster build.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

FAILURES=0

check()
{
    local label="$1"
    local status="$2"      # ok | warn | fail
    local detail="${3:-}"

    case "$status" in
        ok)   printf '  \033[1;32m  ok  \033[0m  %-46s %s\n' "$label" "$detail" ;;
        warn) printf '  \033[1;33m warn \033[0m  %-46s %s\n' "$label" "$detail" ;;
        fail) printf '  \033[1;31m fail \033[0m  %-46s %s\n' "$label" "$detail"
              FAILURES=$(( FAILURES + 1 )) ;;
    esac
}

log "Configuration"
info "platform      $PLATFORM"
info "region        $AWS_REGION"
info "cluster       $CLUSTER_NAME"
info "gpu           ${GPU_REPLICAS} x ${GPU_INSTANCE_TYPE}"
info "storage       $STORAGE_MODE"

# ---------------------------------------------------------------------------

log "Tools"

for tool in aws rosa oc jq; do
    if command -v "$tool" >/dev/null 2>&1; then
        check "$tool" ok "$(command -v "$tool")"
    else
        check "$tool" fail "run: make tools"
    fi
done

if [[ ! -f "${REPO_ROOT}/.env" ]]; then
    check ".env" warn "not created — using defaults. cp .env.example .env"
else
    check ".env" ok ""
fi

# ---------------------------------------------------------------------------

if [[ "$PLATFORM" != "rosa" ]]; then
    log "Cluster (PLATFORM=$PLATFORM, skipping all AWS checks)"

    if oc whoami >/dev/null 2>&1; then
        check "oc logged in" ok "$(oc whoami --show-server)"

        gpu_nodes="$(oc get nodes -l nvidia.com/gpu.present=true --no-headers 2>/dev/null | wc -l)"
        if (( gpu_nodes > 0 )); then
            check "GPU nodes labelled" ok "$gpu_nodes"
        else
            check "GPU nodes labelled" warn "none yet — 'make gpu' installs NFD which labels them"
        fi
    else
        check "oc logged in" fail "oc login <api-url>"
    fi

    printf '\n'
    (( FAILURES == 0 )) && { log "Ready — next: make gpu"; exit 0; } || exit 1
fi

# ---------------------------------------------------------------------------

log "AWS account"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
    check "credentials" fail "aws configure --profile $AWS_PROFILE"
    printf '\n'
    die "Cannot continue without AWS credentials."
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"

if [[ "$CALLER_ARN" == *":root" ]]; then
    check "identity" fail "running as root — create an IAM admin (docs/01-aws-account.md)"
else
    check "identity" ok "$CALLER_ARN"
fi

check "account id" ok "$ACCOUNT_ID"

# The AWS Support API only answers for Business and above, so a failed call is
# the check.
#
# Warn rather than fail: AWS's ROSA setup page lists Business support under
# prerequisites, while Red Hat's own prerequisites say it is *recommended*.
# Cluster creation does generally succeed on Basic, so blocking here would stop
# a legitimately provisionable account. But without it you have no escalation
# path when Red Hat SRE needs one, which is exactly when you will care.
if aws support describe-severity-levels --region us-east-1 >/dev/null 2>&1; then
    check "AWS support plan" ok "Business or higher"
else
    check "AWS support plan" warn "Basic/Developer — AWS lists Business+ as a ROSA prerequisite; Red Hat calls it recommended"
fi

# ---------------------------------------------------------------------------

log "Service quotas in $AWS_REGION"

quota_check()
{
    local service_code="$1" quota_code="$2" needed="$3" label="$4"

    local applied pending
    applied="$(aws service-quotas get-service-quota \
        --service-code "$service_code" --quota-code "$quota_code" \
        --query 'Quota.Value' --output text 2>/dev/null || echo 0)"

    if awk "BEGIN { exit !($applied >= $needed) }"; then
        check "$label" ok "$applied (need $needed)"
        return
    fi

    pending="$(aws service-quotas list-requested-service-quota-change-history-by-quota \
        --service-code "$service_code" --quota-code "$quota_code" \
        --query "RequestedQuotas[?Status=='PENDING'||Status=='CASE_OPENED']|[0].DesiredValue" \
        --output text 2>/dev/null || echo None)"

    if [[ "$pending" != "None" && -n "$pending" ]]; then
        check "$label" warn "$applied now, $pending requested and pending"
    else
        check "$label" fail "$applied (need $needed) — run: make account"
    fi
}

GPU_VCPUS_PER_NODE="$(aws ec2 describe-instance-types \
    --instance-types "$GPU_INSTANCE_TYPE" \
    --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null || echo 4)"
GPU_VCPUS_NEEDED=$(( GPU_REPLICAS * GPU_VCPUS_PER_NODE ))

quota_check ec2 L-1216C47A 100                "Standard on-demand vCPUs"
quota_check ec2 L-DB2E81BA "$GPU_VCPUS_NEEDED" "G/VT on-demand vCPUs (GPU)"
quota_check ec2 L-0263D0A3 5                  "Elastic IPs"
quota_check vpc L-F678F1CE 5                  "VPCs per region"

# ---------------------------------------------------------------------------

log "GPU capacity"

GPU_AZS="$(aws ec2 describe-instance-type-offerings \
    --location-type availability-zone \
    --filters "Name=instance-type,Values=${GPU_INSTANCE_TYPE}" \
    --query 'InstanceTypeOfferings[].Location' --output text 2>/dev/null || echo "")"

if [[ -n "$GPU_AZS" ]]; then
    check "$GPU_INSTANCE_TYPE offered in" ok "$GPU_AZS"
else
    check "$GPU_INSTANCE_TYPE offered in" fail "nowhere in $AWS_REGION — try us-east-1 or us-west-2"
fi

# ---------------------------------------------------------------------------

log "Red Hat"

if command -v rosa >/dev/null 2>&1; then
    if [[ -n "${ROSA_TOKEN:-}" ]] && rosa login --token "$ROSA_TOKEN" >/dev/null 2>&1; then
        check "rosa login" ok "$(rosa whoami 2>/dev/null | awk '/Red Hat Username/ {print $NF}')"
    elif rosa whoami >/dev/null 2>&1; then
        check "rosa login" ok "$(rosa whoami 2>/dev/null | awk '/Red Hat Username/ {print $NF}')"
    else
        check "rosa login" fail "set ROSA_TOKEN in .env — https://console.redhat.com/openshift/token/rosa"
    fi
fi

# ---------------------------------------------------------------------------

printf '\n'

if (( FAILURES == 0 )); then
    log "Ready"
    info "Next: make cluster"
else
    log "$FAILURES blocker(s) above"
    info "Most common first run: 'make account' to file quota requests, then wait for approval."
    exit 1
fi
