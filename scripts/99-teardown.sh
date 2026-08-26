#!/usr/bin/env bash
#
# Cost control, three levels, least destructive first.
#
#   park      GPU pool -> 0 replicas.  ~$2.04/hr -> ~$1.06/hr.
#             Keeps the cluster, the volumes, and your models. Back in ~5 min.
#             Right choice at the end of a workday.
#
#   cluster   Delete the cluster, keep the VPC and IAM roles.
#             ~$2.04/hr -> ~$0.05/hr. Rebuild in ~15 min with 'make cluster'.
#             Destroys gp3 volumes — EFS volumes survive.
#             Right choice on a Friday.
#
#   all       Delete everything: cluster, VPC, NAT gateway, operator roles,
#             OIDC config. Back to near-zero.
#
#             Deliberately left in place even by 'all': the account-wide ROSA
#             roles (shared by every cluster in the account), the budget alarm
#             (you want it precisely when nothing should be running), EFS
#             filesystems, and the support plan. Each is listed on the way out.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

MODE="${1:-}"

# --yes skips the type-the-cluster-name confirmation. It exists for cron
# (docs/02-cost.md schedules nightly teardown); interactive use should not
# pass it.
if [[ "${2:-}" == "--yes" ]]; then
    export ASSUME_YES=true
fi

require_rosa_platform()
{
    [[ "$PLATFORM" == "rosa" ]] || die "PLATFORM is '$PLATFORM'.
          These teardown modes manage a ROSA cluster. For a cluster you brought
          yourself, scale the GPU nodes down however that cluster does it, or:
            oc scale deployment/comfyui -n $APP_NAMESPACE --replicas 0"
}

park_gpu_pool()
{
    require_rosa_platform
    require_rosa

    log "Scaling GPU machine pool to 0 on $CLUSTER_NAME"
    rosa edit machinepool --cluster "$CLUSTER_NAME" gpu --replicas 0 --yes

    cat <<EOF

  Parked. Still billing: HCP control plane (\$0.25/hr), ${BASE_REPLICAS} base
  workers (~\$0.73/hr with the ROSA fee), NAT gateway and load balancer
  (~\$0.09/hr). About \$1.06/hr, \$26/day.

  If that is more than you want to pay overnight, use 'make down' instead —
  HCP rebuilds in ~15 minutes.

  Bring the GPU back:
    rosa edit machinepool --cluster $CLUSTER_NAME gpu --replicas ${GPU_REPLICAS} --yes
EOF
}

delete_cluster()
{
    require_rosa_platform
    require_rosa

    if ! rosa describe cluster -c "$CLUSTER_NAME" >/dev/null 2>&1; then
        ok "no cluster named $CLUSTER_NAME"
        return 0
    fi

    confirm_destructive "This deletes the cluster and every gp3 volume on it, models included."

    log "Deleting cluster $CLUSTER_NAME"
    rosa delete cluster --cluster "$CLUSTER_NAME" --yes
    rosa logs uninstall --cluster "$CLUSTER_NAME" --watch

    log "Deleting operator roles"
    rosa delete operator-roles --prefix "$OPERATOR_ROLE_PREFIX" --mode auto --yes || true
}

delete_network()
{
    require_aws

    if ! aws cloudformation describe-stacks --stack-name "$NETWORK_STACK_NAME" >/dev/null 2>&1; then
        ok "no stack named $NETWORK_STACK_NAME"
        return 0
    fi

    log "Deleting CloudFormation stack $NETWORK_STACK_NAME"
    aws cloudformation delete-stack --stack-name "$NETWORK_STACK_NAME"

    info "waiting for the VPC and NAT gateway to go away"
    aws cloudformation wait stack-delete-complete --stack-name "$NETWORK_STACK_NAME"
    ok "gone"
}

report_stragglers()
{
    require_aws

    log "Anything still billing in $AWS_REGION"

    printf '\n  Running EC2 instances:\n'
    aws ec2 describe-instances --filters "Name=instance-state-name,Values=running" \
        --query 'Reservations[].Instances[].[InstanceId,InstanceType]' --output text \
        | sed 's/^/    /' || true

    printf '\n  NAT gateways (~$32/month each, idle or not):\n'
    aws ec2 describe-nat-gateways --filter "Name=state,Values=available" \
        --query 'NatGateways[].[NatGatewayId,VpcId]' --output text | sed 's/^/    /' || true

    printf '\n  Load balancers (~$16/month each):\n'
    aws elbv2 describe-load-balancers \
        --query 'LoadBalancers[].[LoadBalancerName,Type]' --output text | sed 's/^/    /' || true

    printf '\n  Unattached EBS volumes:\n'
    aws ec2 describe-volumes --filters "Name=status,Values=available" \
        --query 'Volumes[].[VolumeId,Size,VolumeType]' --output text | sed 's/^/    /' || true

    printf '\n  EFS filesystems (survive cluster deletion by design):\n'
    aws efs describe-file-systems \
        --query 'FileSystems[].[FileSystemId,Name,SizeInBytes.Value]' --output text \
        | sed 's/^/    /' || true

    cat <<'EOF'

  Also remember: the AWS Business support plan bills whether or not anything is
  running. If you are done with ROSA entirely, downgrade it in the console.
EOF
}

case "$MODE" in
    park)
        park_gpu_pool
        ;;

    cluster)
        delete_cluster
        report_stragglers
        ;;

    all)
        delete_cluster
        delete_network

        log "Deleting OIDC config"
        require_rosa

        # The id is required — without it the CLI has nothing to act on and
        # drops to an interactive prompt that never gets answered in a script.
        if [[ -f "${REPO_ROOT}/.cluster-state" ]]; then
            # shellcheck disable=SC1091
            source "${REPO_ROOT}/.cluster-state"
        fi

        if [[ -n "${OIDC_CONFIG_ID:-}" ]]; then
            rosa delete oidc-config --oidc-config-id "$OIDC_CONFIG_ID" --mode auto --yes || true
            rm -f "${REPO_ROOT}/.cluster-state"
        else
            warn "no OIDC_CONFIG_ID recorded — leaving the OIDC config in place."
            info "List and delete it by hand if you want a clean account:"
            info "  rosa list oidc-config"
            info "  rosa delete oidc-config --oidc-config-id <id> --mode auto --yes"
        fi

        log "Kept on purpose"
        info "Account-wide ROSA roles — shared by every cluster in this account."
        info "  If this was the only one: rosa delete account-roles --mode auto"
        info "The ${CLUSTER_NAME}-monthly budget alarm — free, and most useful"
        info "  exactly when nothing is supposed to be running."

        report_stragglers
        ;;

    *)
        # Print the header comment block as usage, stopping at the first line
        # of actual code.
        awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
        exit 1
        ;;
esac
