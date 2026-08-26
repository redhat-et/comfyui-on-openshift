#!/usr/bin/env bash
#
# Run this the moment the AWS account activates. Nothing else in this repo works
# until the GPU quota request it files here is approved, and that approval is the
# only step with a lead time measured in days rather than minutes.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_aws

log "Account $AWS_ACCOUNT_ID, region $AWS_REGION"
info "$AWS_CALLER_ARN"

# ---------------------------------------------------------------------------
# Service quotas
#
# service_code | quota_code | desired | human name
# ---------------------------------------------------------------------------

GPU_VCPUS_PER_NODE="$(aws ec2 describe-instance-types \
    --instance-types "$GPU_INSTANCE_TYPE" \
    --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null || echo 4)"

QUOTA_REQUESTS=(
    "ec2|L-1216C47A|100|Standard on-demand instances (vCPUs)"
    "ec2|L-DB2E81BA|${GPU_VCPU_REQUEST}|G and VT on-demand instances (vCPUs)"
    "ec2|L-0263D0A3|10|EC2-VPC Elastic IPs"
    "vpc|L-F678F1CE|10|VPCs per region"
    "vpc|L-FE5A380F|10|NAT gateways per AZ"
    "ebs|L-7A658B76|300|gp3 volume storage (TiB)"
    "ebs|L-D18FCD1D|300|gp2 volume storage (TiB)"
)

request_quota()
{
    local service_code="$1" quota_code="$2" desired="$3" label="$4"

    local applied pending
    applied="$(aws service-quotas get-service-quota \
        --service-code "$service_code" --quota-code "$quota_code" \
        --query 'Quota.Value' --output text 2>/dev/null || echo 0)"

    if awk "BEGIN { exit !($applied >= $desired) }"; then
        printf '  already   %-42s %s\n' "$label" "$applied"
        return 0
    fi

    # A second request while one is open is an error, so check for it.
    pending="$(aws service-quotas list-requested-service-quota-change-history-by-quota \
        --service-code "$service_code" --quota-code "$quota_code" \
        --query "RequestedQuotas[?Status=='PENDING'||Status=='CASE_OPENED']|[0].DesiredValue" \
        --output text 2>/dev/null || echo None)"

    if [[ "$pending" != "None" && -n "$pending" ]]; then
        printf '  pending   %-42s %s requested\n' "$label" "$pending"
        return 0
    fi

    if aws service-quotas request-service-quota-increase \
        --service-code "$service_code" --quota-code "$quota_code" \
        --desired-value "$desired" >/dev/null 2>&1; then
        printf '  FILED     %-42s %s -> %s\n' "$label" "$applied" "$desired"
    else
        warn "could not file $label automatically. Request it here:"
        info "https://${AWS_REGION}.console.aws.amazon.com/servicequotas/home/services/${service_code}/quotas/${quota_code}"
    fi
}

log "Filing service quota increases"

for entry in "${QUOTA_REQUESTS[@]}"; do
    IFS='|' read -r service_code quota_code desired label <<< "$entry"
    request_quota "$service_code" "$quota_code" "$desired" "$label"
done

info ""
info "GPU pool needs $(( GPU_REPLICAS * GPU_VCPUS_PER_NODE )) G-family vCPUs;"
info "requesting $GPU_VCPU_REQUEST leaves room to resize without re-filing."

# ---------------------------------------------------------------------------
# Budget alarm
#
# A running cluster is ~$2/hour. A forgotten weekend is ~$100. A forgotten month
# is ~$1,500. This alarm is the cheapest thing in the repo.
# ---------------------------------------------------------------------------

log "Budget alarm"

create_budget()
{
    if [[ -z "$BUDGET_ALERT_EMAIL" ]]; then
        warn "BUDGET_ALERT_EMAIL is not set in .env — skipping."
        warn "This is the one guardrail between you and a four-figure surprise. Set it."
        return 0
    fi

    if aws budgets describe-budget --account-id "$AWS_ACCOUNT_ID" \
        --budget-name "${CLUSTER_NAME}-monthly" >/dev/null 2>&1; then
        ok "budget ${CLUSTER_NAME}-monthly already exists"
        return 0
    fi

    local budget_json notifications_json
    budget_json="$(jq -n --arg name "${CLUSTER_NAME}-monthly" --arg limit "$MONTHLY_BUDGET_USD" \
        '{ BudgetName: $name,
           BudgetLimit: { Amount: $limit, Unit: "USD" },
           TimeUnit: "MONTHLY",
           BudgetType: "COST" }')"

    notifications_json="$(jq -n --arg email "$BUDGET_ALERT_EMAIL" \
        '[ 50, 80, 100 ]
         | map({ Notification: { NotificationType: "ACTUAL",
                                ComparisonOperator: "GREATER_THAN",
                                Threshold: ., ThresholdType: "PERCENTAGE" },
                 Subscribers: [ { SubscriptionType: "EMAIL", Address: $email } ] })
         + [ { Notification: { NotificationType: "FORECASTED",
                               ComparisonOperator: "GREATER_THAN",
                               Threshold: 100, ThresholdType: "PERCENTAGE" },
               Subscribers: [ { SubscriptionType: "EMAIL", Address: $email } ] } ]')"

    aws budgets create-budget \
        --account-id "$AWS_ACCOUNT_ID" \
        --budget "$budget_json" \
        --notifications-with-subscribers "$notifications_json"

    ok "alerts at 50/80/100% of \$${MONTHLY_BUDGET_USD}/month to $BUDGET_ALERT_EMAIL"
}

create_budget

# Cost Explorer must be switched on once per account before any cost view
# populates. Free, and there is no reason not to.
if aws ce get-cost-and-usage \
    --time-period "Start=$(date -u -d '2 days ago' +%F 2>/dev/null || date -u -v-2d +%F),End=$(date -u +%F)" \
    --granularity DAILY --metrics UnblendedCost >/dev/null 2>&1; then
    ok "Cost Explorer active"
else
    warn "Cost Explorer not active yet — enable once in the Billing console (free)."
fi

# ---------------------------------------------------------------------------

log "Where things stand"

for entry in "${QUOTA_REQUESTS[@]}"; do
    IFS='|' read -r service_code quota_code _ label <<< "$entry"
    value="$(aws service-quotas get-service-quota \
        --service-code "$service_code" --quota-code "$quota_code" \
        --query 'Quota.Value' --output text 2>/dev/null || echo '?')"
    printf '  %-44s %s\n' "$label" "$value"
done

cat <<EOF

Watch the GPU request with:
  aws service-quotas list-requested-service-quota-change-history --status PENDING \\
    --query 'RequestedQuotas[].[QuotaName,DesiredValue,Status]' --output table

When "G and VT" is at or above $(( GPU_REPLICAS * GPU_VCPUS_PER_NODE )):
  make preflight && make cluster
EOF
