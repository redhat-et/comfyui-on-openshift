#!/usr/bin/env bash
#
# Shared helpers. Every script sources this, which is also what loads .env, so
# there is exactly one place to configure the whole repo.
#
# shellcheck shell=bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export REPO_ROOT

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_env()
{
    if [[ -f "${REPO_ROOT}/.env" ]]; then
        # shellcheck disable=SC1091
        set -a; source "${REPO_ROOT}/.env"; set +a
    fi

    # Where the cluster lives.
    : "${PLATFORM:=rosa}"
    : "${AWS_PROFILE:=rosa-admin}"
    : "${AWS_REGION:=us-east-2}"
    : "${CLUSTER_NAME:=comfy}"

    # Base worker pool. Two is the ROSA HCP minimum.
    : "${BASE_INSTANCE_TYPE:=m5.xlarge}"
    : "${BASE_REPLICAS:=2}"
    : "${VPC_CIDR:=10.0.0.0/16}"

    # GPU pool.
    : "${GPU_INSTANCE_TYPE:=g6.xlarge}"
    : "${GPU_REPLICAS:=1}"

    # Storage. rwo  = one gp3 volume, one pod, simplest and fastest.
    #          rwx  = EFS, shared across pods, needed only if you scale out.
    : "${STORAGE_MODE:=rwo}"
    : "${MODELS_SIZE:=100Gi}"
    : "${OUTPUT_SIZE:=20Gi}"

    # Workload.
    : "${APP_NAMESPACE:=comfyui}"
    : "${COMFYUI_IMAGE:=}"

    # Multi-user configuration (enterprise/).
    : "${AUTH_MODE:=oauth}"
    : "${MAX_GPU_WORKERS:=3}"
    : "${SCALE_TO_ZERO:=true}"
    : "${ENABLE_MANAGER:=false}"
    : "${COMFYUI_REF:=v0.32.0}"

    # Cost guardrails.
    : "${MONTHLY_BUDGET_USD:=600}"
    : "${BUDGET_ALERT_EMAIL:=}"
    : "${GPU_VCPU_REQUEST:=32}"

    NETWORK_STACK_NAME="${NETWORK_STACK_NAME:-${CLUSTER_NAME}-net}"
    OPERATOR_ROLE_PREFIX="${OPERATOR_ROLE_PREFIX:-${CLUSTER_NAME}}"

    export PLATFORM AWS_PROFILE AWS_REGION CLUSTER_NAME VPC_CIDR
    export BASE_INSTANCE_TYPE BASE_REPLICAS GPU_INSTANCE_TYPE GPU_REPLICAS
    export STORAGE_MODE MODELS_SIZE OUTPUT_SIZE
    export APP_NAMESPACE COMFYUI_IMAGE
    export AUTH_MODE MAX_GPU_WORKERS SCALE_TO_ZERO ENABLE_MANAGER COMFYUI_REF
    export MONTHLY_BUDGET_USD BUDGET_ALERT_EMAIL GPU_VCPU_REQUEST
    export NETWORK_STACK_NAME OPERATOR_ROLE_PREFIX
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

# Progress output goes to stderr, always. Several functions below return a value
# on stdout (an ARN, a filesystem id) and are consumed with $(...); if progress
# lines shared that stream, every one of those captures would silently include
# them. Keeping the split means command substitution is safe everywhere.

log()
{
    printf '\n\033[1;36m==> %s\033[0m\n' "$*" >&2
}

ok()
{
    printf '  \033[1;32mok\033[0m      %s\n' "$*" >&2
}

info()
{
    printf '          %s\n' "$*" >&2
}

warn()
{
    printf '  \033[1;33mwarn\033[0m    %s\n' "$*" >&2
}

die()
{
    printf '\n  \033[1;31mfail\033[0m    %s\n\n' "$*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

require_tools()
{
    local missing=()
    local tool

    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done

    if (( ${#missing[@]} > 0 )); then
        die "missing tools: ${missing[*]}
          Install them with: make tools"
    fi
}

require_aws()
{
    require_tools aws jq

    aws sts get-caller-identity >/dev/null 2>&1 \
        || die "AWS credentials not working for profile '$AWS_PROFILE'.
          Run: aws configure --profile $AWS_PROFILE"

    AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
    AWS_CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"
    export AWS_ACCOUNT_ID AWS_CALLER_ARN

    if [[ "$AWS_CALLER_ARN" == *":root" ]]; then
        die "You are the root user. Create an IAM admin first — docs/01-aws-account.md step 2."
    fi
}

require_cluster()
{
    require_tools oc

    oc whoami >/dev/null 2>&1 \
        || die "Not logged in to a cluster.
          ROSA:  eval \"\$(make --no-print-directory login)\"
          Other: oc login <api-url>"
}

require_rosa()
{
    require_tools rosa

    if [[ -n "${ROSA_TOKEN:-}" ]]; then
        rosa login --token "$ROSA_TOKEN" >/dev/null
    fi

    rosa whoami >/dev/null 2>&1 \
        || die "rosa CLI not logged in.
          Get a token at https://console.redhat.com/openshift/token/rosa
          then put it in .env as ROSA_TOKEN=..."
}

confirm_destructive()
{
    local what="$1"
    local reply

    printf '\n\033[1;31m%s\033[0m\n' "$what"
    read -r -p "Type the cluster name ($CLUSTER_NAME) to confirm: " reply

    [[ "$reply" == "$CLUSTER_NAME" ]] || die "Aborted, nothing changed."
}

load_env
