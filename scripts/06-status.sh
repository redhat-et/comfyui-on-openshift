#!/usr/bin/env bash
#
# What is running, and what it is costing you per hour.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

# us-east-2 on-demand, August 2026. Close enough to make decisions with; check
# the Cost Explorer for what you were actually billed.
declare -A HOURLY_EC2=(
    [m5.xlarge]=0.192
    [m5.2xlarge]=0.384
    [g5.xlarge]=1.006
    [g6.xlarge]=0.805
    [g6.2xlarge]=1.087
    [g6e.xlarge]=1.861
    [g4dn.xlarge]=0.526
)

ROSA_FEE_PER_4_VCPU=0.171
ROSA_HCP_CLUSTER_FEE=0.25

total_hourly=0

add_cost()
{
    total_hourly="$(awk "BEGIN { printf \"%.4f\", $total_hourly + $1 }")"
}

# ---------------------------------------------------------------------------

if [[ "$PLATFORM" == "rosa" ]] && command -v rosa >/dev/null 2>&1 && rosa whoami >/dev/null 2>&1; then

    log "Cluster"

    if ! rosa describe cluster -c "$CLUSTER_NAME" >/dev/null 2>&1; then
        info "no cluster named '$CLUSTER_NAME' — nothing running"
        info "hourly cost: ~\$0.00 (plus the AWS support plan, which bills regardless)"
        exit 0
    fi

    state="$(rosa describe cluster -c "$CLUSTER_NAME" -o json | jq -r '.state')"
    printf '  %-24s %s\n' "$CLUSTER_NAME" "$state"
    add_cost "$ROSA_HCP_CLUSTER_FEE"
    printf '  %-24s \$%s/hr\n' "HCP control plane" "$ROSA_HCP_CLUSTER_FEE"

    log "Machine pools"
    printf '  %-10s %-14s %-8s %10s %10s %10s\n' POOL TYPE NODES EC2 ROSAFEE TOTAL

    while IFS=$'\t' read -r pool_id instance_type replicas; do
        [[ -n "$pool_id" ]] || continue

        vcpus="$(aws ec2 describe-instance-types --instance-types "$instance_type" \
            --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null || echo 4)"

        ec2_rate="${HOURLY_EC2[$instance_type]:-0}"
        ec2_cost="$(awk "BEGIN { printf \"%.3f\", $ec2_rate * $replicas }")"
        rosa_cost="$(awk "BEGIN { printf \"%.3f\", ($vcpus * $replicas / 4) * $ROSA_FEE_PER_4_VCPU }")"
        line_cost="$(awk "BEGIN { printf \"%.3f\", $ec2_cost + $rosa_cost }")"

        printf '  %-10s %-14s %-8s %10s %10s %10s\n' \
            "$pool_id" "$instance_type" "$replicas" "$ec2_cost" "$rosa_cost" "$line_cost"

        add_cost "$line_cost"
    done < <(rosa list machinepools -c "$CLUSTER_NAME" -o json \
        | jq -r '.[] | [.id, (.aws_node_pool.instance_type // .instance_type), (.replicas // 0)] | @tsv')

    # NAT gateway, ELB, EBS. Roughly fixed, and easy to forget.
    add_cost 0.088
    printf '  %-10s %-14s %-8s %10s %10s %10s\n' "infra" "nat+elb+ebs" "-" "0.088" "-" "0.088"
fi

# ---------------------------------------------------------------------------

if oc whoami >/dev/null 2>&1; then

    log "GPU nodes"
    oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true \
        -o custom-columns='NODE:.metadata.name,GPU:.status.capacity.nvidia\.com/gpu,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,READY:.status.conditions[-1].status' \
        2>/dev/null || info "none"

    log "Workload in $APP_NAMESPACE"
    oc get pods -n "$APP_NAMESPACE" 2>/dev/null || info "namespace not found"

    printf '\n'
    oc get pvc -n "$APP_NAMESPACE" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------

if awk "BEGIN { exit !($total_hourly > 0) }"; then
    daily="$(awk "BEGIN { printf \"%.0f\", $total_hourly * 24 }")"
    monthly="$(awk "BEGIN { printf \"%.0f\", $total_hourly * 730 }")"

    log "Burn rate"
    printf '  \033[1m$%s/hour   $%s/day   $%s/month if left running\033[0m\n' \
        "$total_hourly" "$daily" "$monthly"
    printf '\n'
    printf '  Not counted: AWS Business support (greater of $100/month or 10%% of usage),\n'
    printf '  data transfer, and EBS snapshots.\n'
    printf '\n'
    printf '  make park    GPU pool to 0\n'
    printf '  make down    delete the cluster\n'
fi
