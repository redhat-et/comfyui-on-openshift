#!/usr/bin/env bash
#
# Storage. This is the part that trips people up, so here is the short version:
#
#   STORAGE_MODE=rwo   One gp3 block volume, bound to one node, mounted by one
#                      pod. ~$0.08/GB-month. Fast local NVMe-backed reads, which
#                      matters a lot when you are loading a 7 GB checkpoint into
#                      VRAM. This is the right answer for a single GPU.
#
#   STORAGE_MODE=rwx   EFS. Shared across pods and nodes, so several replicas
#                      (or a separate model-loader job) can read the same models
#                      at once. ~$0.30/GB-month, and NFS-over-network model
#                      loads are noticeably slower. You need this only when you
#                      genuinely have more than one consumer.
#
# One GPU supports one ComfyUI pod, so rwo is the default and you should stay
# there until you have a concrete reason not to. The rwx path is fully scripted
# below because setting up the EFS CSI driver on an STS cluster by hand — IAM
# role, trust policy against the cluster OIDC provider, credentials secret,
# ClusterCSIDriver, filesystem, security group, mount targets, access point —
# is about eight steps that all have to be right at once.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_cluster

log "Namespace $APP_NAMESPACE"
oc get namespace "$APP_NAMESPACE" >/dev/null 2>&1 || oc create namespace "$APP_NAMESPACE"
ok "ready"

# ---------------------------------------------------------------------------
# RWO — gp3, the simple path
# ---------------------------------------------------------------------------

setup_rwo()
{
    local storage_class

    # ROSA ships gp3-csi as default. Self-managed OCP on AWS often calls it gp3.
    # Other platforms will have something else entirely, so resolve it rather
    # than guessing.
    storage_class="$(oc get storageclass \
        -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].metadata.name}' \
        2>/dev/null | awk '{print $1}')"

    [[ -n "$storage_class" ]] || storage_class="gp3-csi"

    log "Block storage via StorageClass '$storage_class'"

    oc apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: comfyui-models
  namespace: ${APP_NAMESPACE}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: ${MODELS_SIZE}
  storageClassName: ${storage_class}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: comfyui-output
  namespace: ${APP_NAMESPACE}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: ${OUTPUT_SIZE}
  storageClassName: ${storage_class}
EOF

    ok "comfyui-models  ${MODELS_SIZE}"
    ok "comfyui-output  ${OUTPUT_SIZE}"

    cat <<EOF

  These are ReadWriteOnce, so exactly one pod can mount them. That is a
  deliberate constraint, not a limitation to work around: a second ComfyUI
  replica has no GPU to run on anyway.

  Note that 'make down' destroys these along with the cluster. If re-downloading
  models is painful, push them to S3 first — see docs/03-storage.md.
EOF
}

# ---------------------------------------------------------------------------
# RWX — EFS, the shared path
# ---------------------------------------------------------------------------

efs_iam_role()
{
    local role_name="${CLUSTER_NAME}-efs-csi"
    local oidc_issuer oidc_host oidc_provider_arn trust_policy

    oidc_issuer="$(oc get authentication cluster -o jsonpath='{.spec.serviceAccountIssuer}')"
    oidc_host="${oidc_issuer#https://}"
    oidc_provider_arn="arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/${oidc_host}"

    if aws iam get-role --role-name "$role_name" >/dev/null 2>&1; then
        ok "IAM role $role_name exists"
        aws iam get-role --role-name "$role_name" --query 'Role.Arn' --output text
        return 0
    fi

    # Both the operator SA and the controller SA assume this role.
    trust_policy="$(jq -n \
        --arg provider "$oidc_provider_arn" \
        --arg host "$oidc_host" \
        '{
            Version: "2012-10-17",
            Statement: [{
                Effect: "Allow",
                Principal: { Federated: $provider },
                Action: "sts:AssumeRoleWithWebIdentity",
                Condition: {
                    StringEquals: {
                        ($host + ":sub"): [
                            "system:serviceaccount:openshift-cluster-csi-drivers:aws-efs-csi-driver-operator",
                            "system:serviceaccount:openshift-cluster-csi-drivers:aws-efs-csi-driver-controller-sa"
                        ]
                    }
                }
            }]
        }')"

    aws iam create-role \
        --role-name "$role_name" \
        --assume-role-policy-document "$trust_policy" \
        --query 'Role.Arn' --output text >/dev/null

    aws iam put-role-policy \
        --role-name "$role_name" \
        --policy-name efs-csi \
        --policy-document "$(jq -n '{
            Version: "2012-10-17",
            Statement: [
                { Effect: "Allow",
                  Action: [
                    "elasticfilesystem:DescribeAccessPoints",
                    "elasticfilesystem:DescribeFileSystems",
                    "elasticfilesystem:DescribeMountTargets",
                    "elasticfilesystem:TagResource",
                    "ec2:DescribeAvailabilityZones"
                  ],
                  Resource: "*" },
                { Effect: "Allow",
                  Action: ["elasticfilesystem:CreateAccessPoint"],
                  Resource: "*",
                  Condition: { StringLike: { "aws:RequestTag/efs.csi.aws.com/cluster": "true" } } },
                { Effect: "Allow",
                  Action: ["elasticfilesystem:DeleteAccessPoint"],
                  Resource: "*",
                  Condition: { StringEquals: { "aws:ResourceTag/efs.csi.aws.com/cluster": "true" } } }
            ]
        }')"

    ok "created IAM role $role_name"
    aws iam get-role --role-name "$role_name" --query 'Role.Arn' --output text
}

efs_filesystem()
{
    local fs_name="${CLUSTER_NAME}-models"
    local existing file_system_id vpc_id vpc_cidr security_group_id

    existing="$(aws efs describe-file-systems \
        --query "FileSystems[?Name=='${fs_name}'].FileSystemId|[0]" --output text 2>/dev/null)"

    if [[ "$existing" != "None" && -n "$existing" ]]; then
        file_system_id="$existing"
        ok "EFS $file_system_id exists"
    else
        file_system_id="$(aws efs create-file-system \
            --performance-mode generalPurpose \
            --throughput-mode elastic \
            --encrypted \
            --tags "Key=Name,Value=${fs_name}" \
            --query FileSystemId --output text)"

        # Progress to stderr — stdout is this function's return value.
        printf '          waiting for %s ' "$file_system_id" >&2
        while [[ "$(aws efs describe-file-systems --file-system-id "$file_system_id" \
            --query 'FileSystems[0].LifeCycleState' --output text)" != "available" ]]; do
            printf '.' >&2
            sleep 5
        done
        printf ' available\n' >&2
    fi

    # Mount targets need a security group that lets the cluster nodes reach NFS.
    #
    # Discovery is by Name tag, which works for the VPC 02-cluster.sh creates.
    # A bring-your-own cluster's VPC may be named anything, so an explicit
    # VPC_ID (from .env, the environment, or the .cluster-state this repo
    # wrote) always wins over discovery.
    if [[ -z "${VPC_ID:-}" && -f "${REPO_ROOT}/.cluster-state" ]]; then
        # shellcheck disable=SC1091
        source "${REPO_ROOT}/.cluster-state"
    fi

    if [[ -n "${VPC_ID:-}" ]]; then
        vpc_id="$VPC_ID"
    else
        vpc_id="$(aws ec2 describe-vpcs \
            --filters "Name=tag:Name,Values=*${CLUSTER_NAME}*" \
            --query 'Vpcs[0].VpcId' --output text)"
    fi

    [[ "$vpc_id" != "None" && -n "$vpc_id" ]] \
        || die "Could not find the cluster VPC by tag Name=*${CLUSTER_NAME}*.
          Set VPC_ID in .env to the VPC your cluster's nodes live in."

    vpc_cidr="$(aws ec2 describe-vpcs --vpc-ids "$vpc_id" \
        --query 'Vpcs[0].CidrBlock' --output text)"

    security_group_id="$(aws ec2 describe-security-groups \
        --filters "Name=vpc-id,Values=${vpc_id}" "Name=group-name,Values=${CLUSTER_NAME}-efs" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)"

    if [[ "$security_group_id" == "None" || -z "$security_group_id" ]]; then
        security_group_id="$(aws ec2 create-security-group \
            --group-name "${CLUSTER_NAME}-efs" \
            --description "NFS from cluster nodes to EFS" \
            --vpc-id "$vpc_id" --query GroupId --output text)"

        aws ec2 authorize-security-group-ingress \
            --group-id "$security_group_id" \
            --protocol tcp --port 2049 --cidr "$vpc_cidr" >/dev/null
    fi

    ok "security group $security_group_id (NFS 2049 from $vpc_cidr)"

    # One mount target per subnet the nodes live in.
    local subnet_id
    for subnet_id in $(aws ec2 describe-subnets \
        --filters "Name=vpc-id,Values=${vpc_id}" "Name=map-public-ip-on-launch,Values=false" \
        --query 'Subnets[].SubnetId' --output text); do

        if aws efs create-mount-target \
            --file-system-id "$file_system_id" \
            --subnet-id "$subnet_id" \
            --security-groups "$security_group_id" >/dev/null 2>&1; then
            ok "mount target in $subnet_id"
        else
            ok "mount target in $subnet_id already exists"
        fi
    done

    printf '%s' "$file_system_id"
}

setup_rwx()
{
    require_aws

    log "EFS CSI driver (STS cluster — this is the eight-step part)"

    local role_arn file_system_id

    role_arn="$(efs_iam_role)"
    ok "role $role_arn"

    log "Installing aws-efs-csi-driver-operator"

    # Resolve the channel off the package manifest like the other operator
    # installs in this repo, falling back to stable if the catalog is slow.
    local efs_channel
    efs_channel="$(oc get packagemanifest aws-efs-csi-driver-operator \
        -n openshift-marketplace -o jsonpath='{.status.defaultChannel}' 2>/dev/null)"
    efs_channel="${efs_channel:-stable}"

    oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: aws-efs-csi-driver-operator
  namespace: openshift-cluster-csi-drivers
spec:
  channel: ${efs_channel}
  installPlanApproval: Automatic
  name: aws-efs-csi-driver-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

    # The driver reads an AWS *config file* out of the `credentials` key of this
    # secret — not a bare role_arn field. Get the shape wrong and the controller
    # starts up perfectly happily, then fails every provisioning call with
    # AccessDenied and a PVC that sits in Pending with no useful event.
    #
    # web_identity_token_file is the projected service account token that the
    # operator mounts into the controller pod; it is what turns the role ARN
    # into actual credentials on an STS cluster.
    oc create secret generic aws-efs-cloud-credentials \
        --namespace openshift-cluster-csi-drivers \
        --from-literal=credentials="[default]
sts_regional_endpoints = regional
role_arn = ${role_arn}
web_identity_token_file = /var/run/secrets/openshift/serviceaccount/token" \
        --dry-run=client -o yaml | oc apply -f -

    ok "credentials secret written"

    printf '          waiting for the operator CSV '
    for _ in $(seq 1 60); do
        if oc get csv -n openshift-cluster-csi-drivers 2>/dev/null \
            | grep -q 'aws-efs.*Succeeded'; then
            printf ' ready\n'
            break
        fi
        printf '.'
        sleep 10
    done

    oc apply -f - <<'EOF'
apiVersion: operator.openshift.io/v1
kind: ClusterCSIDriver
metadata:
  name: efs.csi.aws.com
spec:
  managementState: Managed
EOF

    ok "ClusterCSIDriver created"

    log "EFS filesystem"
    file_system_id="$(efs_filesystem)"
    ok "filesystem $file_system_id"

    log "StorageClass and PVCs"

    oc apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com

# Retain, not the default Delete. Deleting a PVC by accident should not take a
# 100 GB model library with it. The cost is that removed PVCs leave Released PVs
# behind, which you clean up deliberately:
#   oc get pv | grep Released
reclaimPolicy: Retain

parameters:
  provisioningMode: efs-ap
  fileSystemId: ${file_system_id}
  directoryPerms: "775"
  gidRangeStart: "1000"
  gidRangeEnd: "2000"

mountOptions:
  # noresvport is the one that matters for the volume-release problem.
  #
  # By default an NFS client reconnecting after any network interruption comes
  # back on a new source port, and the server rejects it as a different client.
  # The mount does not fail — it hangs, permanently, and because NFS mounts are
  # 'hard' by default every process that touches it goes into uninterruptible
  # sleep. Uninterruptible means SIGKILL does not work: the pod sits in
  # Terminating forever, the kubelet can never unmount, and the volume is never
  # released. That is one of the two ways you end up here.
  #
  # noresvport lets the reconnect succeed instead.
  - noresvport
  # Bound how long a single NFS operation retries before returning an error, so
  # an EFS blip degrades into slow-and-erroring rather than wedged-forever.
  # Still 'hard' semantics — writes are not silently lost, which is why 'soft'
  # is deliberately not in this list.
  - timeo=600
  - retrans=2
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: comfyui-models
  namespace: ${APP_NAMESPACE}
spec:
  accessModes: [ReadWriteMany]
  resources:
    requests:
      storage: ${MODELS_SIZE}
  storageClassName: efs-sc
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: comfyui-output
  namespace: ${APP_NAMESPACE}
spec:
  accessModes: [ReadWriteMany]
  resources:
    requests:
      storage: ${OUTPUT_SIZE}
  storageClassName: efs-sc
EOF

    cat <<EOF

  EFS is elastic — the size on the PVC is a formality, you are billed for what
  you actually store (~\$0.30/GB-month) plus throughput.

  It also survives 'make down'. That is the real reason to pick it: your models
  outlive the cluster, so a rebuild does not mean a re-download.
EOF
}

# ---------------------------------------------------------------------------

case "$STORAGE_MODE" in
    rwo) setup_rwo ;;
    rwx) setup_rwx ;;
    *)   die "STORAGE_MODE must be 'rwo' or 'rwx', got '$STORAGE_MODE'" ;;
esac

log "Volumes in $APP_NAMESPACE"
oc get pvc -n "$APP_NAMESPACE"

cat <<EOF

Next:
  make deploy
EOF
