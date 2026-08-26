#!/usr/bin/env bash
#
# Install Node Feature Discovery and the NVIDIA GPU Operator, then prove the
# cluster can actually schedule a pod onto the GPU.
#
# Works on ROSA and on any other OpenShift 4.x cluster — nothing here is ROSA
# specific. Channels and CR shapes for both operators move between releases, so
# instead of hardcoding them this reads the default channel off the package
# manifest and the CR off the installed CSV's alm-examples annotation, which is
# exactly what the console's "Create instance" button does.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_cluster
require_tools jq

NFD_NAMESPACE="openshift-nfd"
GPU_NAMESPACE="nvidia-gpu-operator"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-1800}"

log "Cluster $(oc whoami --show-server)"
info "as $(oc whoami)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

default_channel_for()
{
    oc get packagemanifest "$1" -n openshift-marketplace \
        -o jsonpath='{.status.defaultChannel}' 2>/dev/null
}

# Both helpers filter CSVs by name prefix, never `.items[0]` or "any
# Succeeded": an operator installed AllNamespaces-mode anywhere on the cluster
# copies its CSV into every namespace, so on a bring-your-own cluster the
# first list entry can be somebody else's operator entirely.

wait_for_csv()
{
    local namespace="$1" csv_prefix="$2"
    local deadline=$(( SECONDS + WAIT_TIMEOUT ))

    printf '          waiting for the %s CSV in %s ' "$csv_prefix" "$namespace"

    while (( SECONDS < deadline )); do
        if oc get csv -n "$namespace" -o json 2>/dev/null \
            | jq -e --arg prefix "$csv_prefix" \
                '.items[] | select(.metadata.name | startswith($prefix))
                          | select(.status.phase == "Succeeded")' >/dev/null 2>&1; then
            printf ' ready\n'
            return 0
        fi

        printf '.'
        sleep 10
    done

    printf '\n'
    die "CSV ${csv_prefix}* in $namespace never reached Succeeded.
          oc get csv -n $namespace
          oc get installplan -n $namespace"
}

# Apply the vendor's own example CR straight out of the installed CSV, so the
# shape always matches the operator version that actually got installed.
apply_alm_example()
{
    local namespace="$1" csv_prefix="$2" kind="$3"
    local csv_name example

    csv_name="$(oc get csv -n "$namespace" -o json \
        | jq -r --arg prefix "$csv_prefix" \
            '[.items[] | select(.metadata.name | startswith($prefix))][0].metadata.name // empty')"

    [[ -n "$csv_name" ]] || die "No CSV named ${csv_prefix}* in $namespace"

    example="$(oc get csv "$csv_name" -n "$namespace" \
        -o jsonpath='{.metadata.annotations.alm-examples}' \
        | jq --arg kind "$kind" '.[] | select(.kind == $kind)')"

    [[ -n "$example" ]] || die "No $kind example in CSV $csv_name"

    printf '%s' "$example" | oc apply -n "$namespace" -f -
}

# ---------------------------------------------------------------------------
# Node Feature Discovery
#
# NFD labels nodes with the hardware they actually have. The GPU Operator keys
# off the PCI vendor label (10de = NVIDIA) to decide where to run its driver
# daemonset, so this has to land first.
# ---------------------------------------------------------------------------

log "Node Feature Discovery"

NFD_CHANNEL="$(default_channel_for nfd)"
[[ -n "$NFD_CHANNEL" ]] || die "Could not resolve the nfd default channel. Is the redhat-operators catalog healthy?"
ok "channel $NFD_CHANNEL"

oc apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NFD_NAMESPACE}
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: nfd
  namespace: ${NFD_NAMESPACE}
spec:
  targetNamespaces:
    - ${NFD_NAMESPACE}
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: nfd
  namespace: ${NFD_NAMESPACE}
spec:
  channel: ${NFD_CHANNEL}
  installPlanApproval: Automatic
  name: nfd
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

wait_for_csv "$NFD_NAMESPACE" nfd
apply_alm_example "$NFD_NAMESPACE" nfd NodeFeatureDiscovery
ok "NodeFeatureDiscovery created"

# ---------------------------------------------------------------------------
# NVIDIA GPU Operator
# ---------------------------------------------------------------------------

log "NVIDIA GPU Operator"

GPU_CHANNEL="$(default_channel_for gpu-operator-certified)"
[[ -n "$GPU_CHANNEL" ]] || die "Could not resolve gpu-operator-certified. Is the certified-operators catalog enabled?"
ok "channel $GPU_CHANNEL"

oc apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${GPU_NAMESPACE}
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: nvidia-gpu-operator-group
  namespace: ${GPU_NAMESPACE}
spec:
  targetNamespaces:
    - ${GPU_NAMESPACE}
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: gpu-operator-certified
  namespace: ${GPU_NAMESPACE}
spec:
  channel: ${GPU_CHANNEL}
  installPlanApproval: Automatic
  name: gpu-operator-certified
  source: certified-operators
  sourceNamespace: openshift-marketplace
EOF

wait_for_csv "$GPU_NAMESPACE" gpu-operator-certified
apply_alm_example "$GPU_NAMESPACE" gpu-operator-certified ClusterPolicy
ok "ClusterPolicy created"

# The driver container compiles against the running RHCOS kernel and pulls
# multi-gigabyte images. On a fresh node this is genuinely slow. It is not hung.
log "Waiting for the driver stack (10-20 minutes on a fresh node)"

deadline=$(( SECONDS + WAIT_TIMEOUT ))
policy_ready=0

while (( SECONDS < deadline )); do
    state="$(oc get clusterpolicy -o jsonpath='{.items[0].status.state}' 2>/dev/null || echo pending)"

    if [[ "$state" == "ready" ]]; then
        ok "ClusterPolicy ready"
        policy_ready=1
        break
    fi

    printf '          state=%-12s %s\n' "$state" \
        "$(oc get pods -n "$GPU_NAMESPACE" --no-headers 2>/dev/null \
            | awk '{print $3}' | sort | uniq -c | tr '\n' ' ')"
    sleep 30
done

(( policy_ready == 1 )) || die "ClusterPolicy never became ready.
          oc get pods -n $GPU_NAMESPACE
          oc logs -n $GPU_NAMESPACE -l app=nvidia-driver-daemonset"

# ---------------------------------------------------------------------------
# Verify — the only claim that matters is "a pod can get a GPU"
# ---------------------------------------------------------------------------

log "GPU capacity by node"
oc get nodes -o custom-columns=\
'NODE:.metadata.name,GPU:.status.capacity.nvidia\.com/gpu,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type'

log "nvidia-smi in a pod"

oc delete pod gpu-smoke-test --ignore-not-found >/dev/null 2>&1

oc apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: gpu-smoke-test
spec:
  restartPolicy: Never
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
  containers:
    - name: smi
      image: nvcr.io/nvidia/cuda:12.6.2-base-ubi9
      command: ["nvidia-smi"]
      resources:
        limits:
          nvidia.com/gpu: 1
EOF

if oc wait --for=jsonpath='{.status.phase}'=Succeeded pod/gpu-smoke-test --timeout=600s 2>/dev/null; then
    oc logs gpu-smoke-test
    oc delete pod gpu-smoke-test >/dev/null
    ok "GPU is schedulable"
else
    oc describe pod gpu-smoke-test | sed -n '/Events/,$p'
    die "Smoke test did not succeed — see events above."
fi

cat <<EOF

Next:
  make storage
EOF
