#!/usr/bin/env bash
#
# Create a minimal ROSA HCP cluster with one GPU worker pool.
#
# HCP rather than Classic: Classic bills you for 3 control-plane nodes and 2-3
# infra nodes of your own, so a "two node test cluster" is really an eight node
# cluster. HCP hosts the control plane on Red Hat's side for a flat $0.25/hr and
# builds in ~15 minutes instead of ~40 — which is what makes nightly teardown
# and rebuild a realistic way to keep the bill down.
#
# Skipped entirely when PLATFORM != rosa.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

if [[ "$PLATFORM" != "rosa" ]]; then
    log "PLATFORM=$PLATFORM — nothing to create, bring your own cluster"
    info "Log in with oc, then: make gpu storage deploy"
    exit 0
fi

require_aws
require_rosa

# ---------------------------------------------------------------------------
# Fail fast on the two things that waste the most time
# ---------------------------------------------------------------------------

log "Preflight"

GPU_VCPUS_PER_NODE="$(aws ec2 describe-instance-types \
    --instance-types "$GPU_INSTANCE_TYPE" \
    --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null || echo 4)"
GPU_VCPUS_NEEDED=$(( GPU_REPLICAS * GPU_VCPUS_PER_NODE ))

GPU_VCPU_QUOTA="$(aws service-quotas get-service-quota \
    --service-code ec2 --quota-code L-DB2E81BA \
    --query 'Quota.Value' --output text)"

if awk "BEGIN { exit !($GPU_VCPU_QUOTA < $GPU_VCPUS_NEEDED) }"; then
    die "G/VT vCPU quota is $GPU_VCPU_QUOTA, need $GPU_VCPUS_NEEDED.
          The cluster would build fine and the GPU pool would sit unschedulable forever.
          Check:  aws service-quotas list-requested-service-quota-change-history --status PENDING"
fi

ok "G/VT quota $GPU_VCPU_QUOTA vCPUs (need $GPU_VCPUS_NEEDED)"

# Offerings tell you the instance type exists in an AZ. They do not tell you
# there is capacity. This catches the first failure mode, not the second.
#
# The tr is for display; the containment check itself (list_contains, in
# common.sh) handles the TAB separators `--output text` emits on its own.
GPU_AZS="$(aws ec2 describe-instance-type-offerings \
    --location-type availability-zone \
    --filters "Name=instance-type,Values=${GPU_INSTANCE_TYPE}" \
    --query 'InstanceTypeOfferings[].Location' --output text | tr '\t' ' ')"

[[ -n "$GPU_AZS" ]] || die "$GPU_INSTANCE_TYPE is not offered in $AWS_REGION. Try us-east-1 or us-west-2."
ok "$GPU_INSTANCE_TYPE offered in: $GPU_AZS"

# ---------------------------------------------------------------------------
# IAM: account roles, OIDC provider, operator roles
#
# Account roles are per-account; operator roles are keyed to the OIDC config.
# All three commands are idempotent.
# ---------------------------------------------------------------------------

log "Account-wide STS roles"
rosa create account-roles --mode auto --hosted-cp --force-policy-creation --yes

log "OIDC config"

# Reuse the one this repo created before, if it still exists. Never just take
# the first entry from `rosa list oidc-config` — that lists every config in your
# whole Red Hat organisation, which on a shared account means binding your
# operator roles to somebody else's issuer.
OIDC_CONFIG_ID=""

if [[ -f "${REPO_ROOT}/.cluster-state" ]]; then
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.cluster-state"

    if [[ -n "${OIDC_CONFIG_ID:-}" ]] \
        && rosa list oidc-config -o json | jq -e --arg id "$OIDC_CONFIG_ID" \
            'any(.[]; .id == $id)' >/dev/null 2>&1; then
        ok "reusing oidc-config $OIDC_CONFIG_ID from .cluster-state"
    else
        OIDC_CONFIG_ID=""
    fi
fi

if [[ -z "$OIDC_CONFIG_ID" ]]; then
    # Identify the new config by diffing the list before and after, rather than
    # scraping the CLI's human-readable output, which changes between releases.
    OIDC_BEFORE="$(rosa list oidc-config -o json 2>/dev/null | jq -r '.[].id' | sort)"

    rosa create oidc-config --mode auto --yes

    OIDC_CONFIG_ID="$(rosa list oidc-config -o json | jq -r '.[].id' | sort \
        | comm -13 <(printf '%s\n' "$OIDC_BEFORE") - | tail -1)"

    [[ -n "$OIDC_CONFIG_ID" ]] \
        || die "Created an OIDC config but could not identify it.
          Run 'rosa list oidc-config', then put the id in .cluster-state as
          OIDC_CONFIG_ID=... and re-run."

    ok "created oidc-config $OIDC_CONFIG_ID"
fi

# When operator roles are created by --prefix rather than --cluster, the CLI
# also requires the installer role ARN — it cannot infer it without a cluster to
# look at. Pull it from the account roles created in the previous step.
INSTALLER_ROLE_ARN="$(rosa list account-roles -o json \
    | jq -r '[.[] | select(.RoleType == "Installer" and (.RoleARN | test("HCP")))][0].RoleARN // empty')"

if [[ -z "$INSTALLER_ROLE_ARN" ]]; then
    INSTALLER_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ACCOUNT_ROLE_PREFIX:-ManagedOpenShift}-HCP-ROSA-Installer-Role"
    warn "could not read the installer role from 'rosa list account-roles'; assuming"
    info "$INSTALLER_ROLE_ARN"
fi

log "Operator roles (prefix $OPERATOR_ROLE_PREFIX)"
rosa create operator-roles \
    --prefix "$OPERATOR_ROLE_PREFIX" \
    --oidc-config-id "$OIDC_CONFIG_ID" \
    --installer-role-arn "$INSTALLER_ROLE_ARN" \
    --hosted-cp --mode auto --yes

# ---------------------------------------------------------------------------
# Network
#
# HCP will not build into the default VPC. It needs a public and a private
# subnet per AZ, with the private one egressing through a NAT gateway.
# `rosa create network` drives a CloudFormation stack that lays that out.
# ---------------------------------------------------------------------------

log "VPC (CloudFormation stack $NETWORK_STACK_NAME)"

if aws cloudformation describe-stacks --stack-name "$NETWORK_STACK_NAME" >/dev/null 2>&1; then
    ok "stack already exists"
else
    rosa create network \
        --param "Region=${AWS_REGION}" \
        --param "Name=${NETWORK_STACK_NAME}" \
        --param "AvailabilityZoneCount=1" \
        --param "VpcCidr=${VPC_CIDR:-10.0.0.0/16}"
fi

# Read subnets off the VPC rather than trusting stack output key names, which
# have drifted between template versions.
VPC_ID="$(aws cloudformation describe-stacks --stack-name "$NETWORK_STACK_NAME" \
    --query 'Stacks[0].Outputs[?contains(OutputKey,`VPC`)||contains(OutputKey,`Vpc`)].OutputValue|[0]' \
    --output text)"

[[ "$VPC_ID" != "None" && -n "$VPC_ID" ]] || die "Could not read VPC id from stack $NETWORK_STACK_NAME"

PUBLIC_SUBNET_ID="$(aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=map-public-ip-on-launch,Values=true" \
    --query 'Subnets[0].SubnetId' --output text)"

PRIVATE_SUBNET_ID="$(aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=map-public-ip-on-launch,Values=false" \
    --query 'Subnets[0].SubnetId' --output text)"

PRIVATE_SUBNET_AZ="$(aws ec2 describe-subnets --subnet-ids "$PRIVATE_SUBNET_ID" \
    --query 'Subnets[0].AvailabilityZone' --output text)"

ok "vpc     $VPC_ID"
ok "public  $PUBLIC_SUBNET_ID"
ok "private $PRIVATE_SUBNET_ID ($PRIVATE_SUBNET_AZ)"

if ! list_contains "$GPU_AZS" "$PRIVATE_SUBNET_AZ"; then
    die "VPC landed in $PRIVATE_SUBNET_AZ but $GPU_INSTANCE_TYPE is only in: $GPU_AZS
          Delete the stack and re-run, or change AWS_REGION."
fi

# Stash for later scripts so they do not have to re-derive any of this.
cat > "${REPO_ROOT}/.cluster-state" <<EOF
VPC_ID=$VPC_ID
PUBLIC_SUBNET_ID=$PUBLIC_SUBNET_ID
PRIVATE_SUBNET_ID=$PRIVATE_SUBNET_ID
PRIVATE_SUBNET_AZ=$PRIVATE_SUBNET_AZ
OIDC_CONFIG_ID=$OIDC_CONFIG_ID
EOF

# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------

log "Cluster $CLUSTER_NAME (~15 minutes)"

if rosa describe cluster -c "$CLUSTER_NAME" >/dev/null 2>&1; then
    ok "cluster already exists"
else
    rosa create cluster \
        --cluster-name "$CLUSTER_NAME" \
        --sts --hosted-cp --mode auto --yes \
        --region "$AWS_REGION" \
        --billing-account "${BILLING_ACCOUNT_ID:-$AWS_ACCOUNT_ID}" \
        --oidc-config-id "$OIDC_CONFIG_ID" \
        --operator-roles-prefix "$OPERATOR_ROLE_PREFIX" \
        --subnet-ids "${PUBLIC_SUBNET_ID},${PRIVATE_SUBNET_ID}" \
        --compute-machine-type "$BASE_INSTANCE_TYPE" \
        --replicas "$BASE_REPLICAS"

    rosa logs install -c "$CLUSTER_NAME" --watch
fi

# ---------------------------------------------------------------------------
# GPU machine pool
#
# A dedicated pool rather than resizing the default one. The taint keeps
# ordinary OpenShift workloads off an $0.80/hr node. The NVIDIA GPU Operator's
# daemonsets tolerate nvidia.com/gpu out of the box; the ComfyUI manifests in
# this repo carry the matching toleration.
# ---------------------------------------------------------------------------

log "GPU machine pool: ${GPU_REPLICAS} x ${GPU_INSTANCE_TYPE}"

if rosa list machinepools -c "$CLUSTER_NAME" -o json | jq -e '.[]|select(.id=="gpu")' >/dev/null 2>&1; then
    ok "machine pool 'gpu' already exists"
else
    rosa create machinepool \
        --cluster "$CLUSTER_NAME" \
        --name gpu \
        --instance-type "$GPU_INSTANCE_TYPE" \
        --replicas "$GPU_REPLICAS" \
        --subnet "$PRIVATE_SUBNET_ID" \
        --labels "node-role.kubernetes.io/gpu=,nvidia.com/gpu.present=true" \
        --taints "nvidia.com/gpu=true:NoSchedule" \
        --yes
fi

# ---------------------------------------------------------------------------

log "cluster-admin"

if rosa describe admin -c "$CLUSTER_NAME" >/dev/null 2>&1; then
    ok "admin exists — 'rosa delete admin -c $CLUSTER_NAME' to rotate"
    rosa describe admin -c "$CLUSTER_NAME"
else
    rosa create admin -c "$CLUSTER_NAME"
fi

CONSOLE_URL="$(rosa describe cluster -c "$CLUSTER_NAME" -o json | jq -r '.console.url')"

cat <<EOF

Console: $CONSOLE_URL

Log in with the oc command above (allow a few minutes for the admin user to
propagate through the identity provider), then:

  make gpu
EOF
