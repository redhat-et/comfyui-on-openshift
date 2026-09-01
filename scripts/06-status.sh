#!/usr/bin/env bash
#
# What is running, and what it is costing you per hour.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

# us-east-2 on-demand, August 2026. Close enough to make decisions with; check
# the Cost Explorer for what you were actually billed.
#
# A case statement, not `declare -A`: associative arrays need bash 4, and the
# stock macOS bash is 3.2 — this script has to run on the laptop driving the
# cluster, not just in CI.
ec2_hourly_rate()
{
    case "$1" in
        m5.xlarge)   echo 0.192 ;;
        m5.2xlarge)  echo 0.384 ;;
        g5.xlarge)   echo 1.006 ;;
        g6.xlarge)   echo 0.805 ;;
        g6.2xlarge)  echo 1.087 ;;
        g6e.xlarge)  echo 1.861 ;;
        g4dn.xlarge) echo 0.526 ;;
        # An unknown type used to print as $0.00 with nothing to say why —
        # indistinguishable from an actually-free line, and the exact instance
        # type where under-reporting matters most: someone changed
        # GPU_INSTANCE_TYPE to something not in this table. Warn loudly and
        # return nothing, so the caller can mark the line instead of silently
        # folding it into the total as zero.
        *)
            warn "no on-demand rate on file for instance type '$1' — add it to ec2_hourly_rate() in scripts/06-status.sh"
            echo ""
            ;;
    esac
}

ROSA_FEE_PER_4_VCPU=0.171
ROSA_HCP_CLUSTER_FEE=0.25

total_hourly=0

add_cost()
{
    total_hourly="$(awk "BEGIN { printf \"%.4f\", $total_hourly + $1 }")"
}

# ---------------------------------------------------------------------------
# Live GPU node count, queried once up front so it can back both the
# machine-pool cost table below and the GPU nodes listing further down,
# rather than two independent queries that could disagree.
# ---------------------------------------------------------------------------

OC_LOGGED_IN=false
GPU_NODE_ROWS=""
LIVE_GPU_NODE_COUNT=0

if oc whoami >/dev/null 2>&1; then
    OC_LOGGED_IN=true

    # jq rather than custom-columns: the Ready condition has to be selected by
    # type — conditions[-1] is not guaranteed to be Ready on every version.
    GPU_NODE_ROWS="$(oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true -o json 2>/dev/null \
        | jq -r '.items[] | [
              .metadata.name,
              (.status.capacity."nvidia.com/gpu" // "0"),
              (.metadata.labels."node.kubernetes.io/instance-type" // "?"),
              ([.status.conditions[] | select(.type == "Ready") | .status] | first // "?")
          ] | @tsv')"

    [[ -n "$GPU_NODE_ROWS" ]] \
        && LIVE_GPU_NODE_COUNT="$(printf '%s\n' "$GPU_NODE_ROWS" | wc -l | tr -d ' ')"
fi

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
    printf '  %-10s %-14s %-9s %-8s %10s %10s %10s\n' POOL TYPE SCALE NODES EC2 ROSAFEE TOTAL

    while IFS=$'\t' read -r pool_id instance_type replicas as_min as_max; do
        [[ -n "$pool_id" ]] || continue

        # An autoscaling pool's `replicas` field is the last size ROSA was
        # told to request, not what is actually running right now — KEDA's
        # cron/queue-depth ScaledObject (enterprise/manifests/03-autoscale.yaml)
        # drives the real number independently. Prefer the live count from
        # `oc get nodes`, the way 00-preflight.sh already does; fall back to
        # the requested count (with a warning) when not logged in to oc.
        scale_label="-"
        nodes="$replicas"

        if [[ -n "$as_min" || -n "$as_max" ]]; then
            scale_label="${as_min:-?}..${as_max:-?}"

            if [[ "$OC_LOGGED_IN" == "true" ]]; then
                nodes="$LIVE_GPU_NODE_COUNT"
            else
                warn "pool '$pool_id' autoscales $scale_label — not logged in to oc, showing the last-requested count ($replicas) instead of the live node count"
            fi
        fi

        vcpus="$(aws ec2 describe-instance-types --instance-types "$instance_type" \
            --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null || echo 4)"

        ec2_rate="$(ec2_hourly_rate "$instance_type")"
        rosa_cost="$(awk "BEGIN { printf \"%.3f\", ($vcpus * $nodes / 4) * $ROSA_FEE_PER_4_VCPU }")"

        if [[ -z "$ec2_rate" ]]; then
            # ec2_hourly_rate already warned by name; this just keeps the
            # unknown line visible in the table instead of folding it into
            # the total as an unlabelled $0.00.
            printf '  %-10s %-14s %-9s %-8s %10s %10s %10s\n' \
                "$pool_id" "$instance_type" "$scale_label" "$nodes" "unknown rate" "$rosa_cost" "unknown rate"
            add_cost "$rosa_cost"
        else
            ec2_cost="$(awk "BEGIN { printf \"%.3f\", $ec2_rate * $nodes }")"
            line_cost="$(awk "BEGIN { printf \"%.3f\", $ec2_cost + $rosa_cost }")"

            printf '  %-10s %-14s %-9s %-8s %10s %10s %10s\n' \
                "$pool_id" "$instance_type" "$scale_label" "$nodes" "$ec2_cost" "$rosa_cost" "$line_cost"

            add_cost "$line_cost"
        fi
    done < <(rosa list machinepools -c "$CLUSTER_NAME" -o json \
        | jq -r '.[] | [
              .id,
              (.aws_node_pool.instance_type // .instance_type),
              (.replicas // 0),
              ((.autoscaling.min_replica // .autoscaling.min_replicas) // ""),
              ((.autoscaling.max_replica // .autoscaling.max_replicas) // "")
          ] | @tsv')

    # NAT gateway, ELB, EBS. Roughly fixed, and easy to forget.
    add_cost 0.088
    printf '  %-10s %-14s %-9s %-8s %10s %10s %10s\n' "infra" "nat+elb+ebs" "-" "-" "0.088" "-" "0.088"
fi

# ---------------------------------------------------------------------------

if [[ "$OC_LOGGED_IN" == "true" ]]; then

    log "GPU nodes"

    if [[ -n "$GPU_NODE_ROWS" ]]; then
        printf '  %-52s %-5s %-14s %s\n' NODE GPU TYPE READY
        while IFS=$'\t' read -r node_name gpu_count instance_type ready; do
            printf '  %-52s %-5s %-14s %s\n' "$node_name" "$gpu_count" "$instance_type" "$ready"
        done <<< "$GPU_NODE_ROWS"
    else
        info "none"
    fi

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
