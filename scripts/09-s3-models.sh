#!/usr/bin/env bash
#
# Make S3 the canonical home for your models, so they outlive the cluster, the
# volumes, and every teardown — at ~$0.023/GB-month.
#
# What this creates (idempotent, like everything here):
#   1. A private, encrypted S3 bucket.
#   2. An IAM role the cluster's OIDC provider can hand to the `comfyui`
#      service account — read-only on that one bucket.
#   3. The `comfyui` ServiceAccount, annotated so the pod identity webhook
#      injects the role's credentials.
#
# Then set MODELS_S3_BUCKET in .env and run `make deploy`: the deployment gains
# an init container that syncs s3://bucket -> /models before ComfyUI starts.
# Uploading stays on your side of the fence, with your own credentials:
#
#   aws s3 sync ./models "s3://<bucket>/"
#
# The bucket layout mirrors /models: checkpoints/, loras/, vae/, ...
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_aws
require_cluster

BUCKET="${MODELS_S3_BUCKET:-${CLUSTER_NAME}-comfyui-models-${AWS_ACCOUNT_ID}}"
ROLE_NAME="${CLUSTER_NAME}-comfyui-s3"

log "Bucket $BUCKET"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    ok "exists"
else
    # us-east-1 rejects a LocationConstraint naming itself; everywhere else
    # requires one. The S3 API's oldest wart.
    if [[ "$AWS_REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$BUCKET" >/dev/null
    else
        aws s3api create-bucket --bucket "$BUCKET" \
            --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    fi

    aws s3api put-public-access-block --bucket "$BUCKET" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

    aws s3api put-bucket-encryption --bucket "$BUCKET" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

    ok "created — private, encrypted"
fi

# ---------------------------------------------------------------------------
# IAM role, trusted by the cluster OIDC provider for the comfyui SA only.
# Read-only: pods pull models; uploading happens from your machine with your
# own credentials, which keeps a compromised pod from corrupting the canon.
# ---------------------------------------------------------------------------

log "IAM role $ROLE_NAME"

OIDC_ISSUER="$(oc get authentication cluster -o jsonpath='{.spec.serviceAccountIssuer}')"
OIDC_HOST="${OIDC_ISSUER#https://}"
OIDC_PROVIDER_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/${OIDC_HOST}"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    ok "exists"
else
    trust_policy="$(jq -n \
        --arg provider "$OIDC_PROVIDER_ARN" \
        --arg host "$OIDC_HOST" \
        --arg sub "system:serviceaccount:${APP_NAMESPACE}:comfyui" \
        '{
            Version: "2012-10-17",
            Statement: [{
                Effect: "Allow",
                Principal: { Federated: $provider },
                Action: "sts:AssumeRoleWithWebIdentity",
                Condition: { StringEquals: { ($host + ":sub"): $sub } }
            }]
        }')"

    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$trust_policy" >/dev/null

    ok "created"
fi

aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name s3-models-read \
    --policy-document "$(jq -n --arg bucket "$BUCKET" '{
        Version: "2012-10-17",
        Statement: [
            { Effect: "Allow",
              Action: ["s3:ListBucket"],
              Resource: ("arn:aws:s3:::" + $bucket) },
            { Effect: "Allow",
              Action: ["s3:GetObject"],
              Resource: ("arn:aws:s3:::" + $bucket + "/*") }
        ]
    }')"

ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)"
ok "read-only policy attached"

# ---------------------------------------------------------------------------
# ServiceAccount. The eks.amazonaws.com/role-arn annotation is what the pod
# identity webhook (present on OpenShift STS clusters) reads to inject the
# web-identity token and AWS_ROLE_ARN into pods using this SA.
# ---------------------------------------------------------------------------

log "ServiceAccount comfyui in $APP_NAMESPACE"

oc get namespace "$APP_NAMESPACE" >/dev/null 2>&1 || oc create namespace "$APP_NAMESPACE"
oc get sa comfyui -n "$APP_NAMESPACE" >/dev/null 2>&1 \
    || oc create sa comfyui -n "$APP_NAMESPACE"

oc annotate sa comfyui -n "$APP_NAMESPACE" --overwrite \
    "eks.amazonaws.com/role-arn=${ROLE_ARN}"

ok "annotated with $ROLE_ARN"

cat <<EOF

Next:
  1. In .env:          MODELS_S3_BUCKET=${BUCKET}
  2. Upload models:    aws s3 sync ./models "s3://${BUCKET}/"
                       (layout mirrors /models: checkpoints/, loras/, vae/, ...)
  3. Redeploy:         make deploy
                       — the pod now syncs the bucket into /models on start.

Your models now survive 'make down', 'make destroy', and the cluster itself.
EOF
