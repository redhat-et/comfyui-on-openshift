#!/usr/bin/env bash
#
# One script. Multi-user ComfyUI on an existing OpenShift cluster.
#
#   ./enterprise/setup.sh
#
# Assumes you already have a cluster with GPU nodes — that is `make cluster gpu`
# from the repo root, or any OpenShift 4.x cluster where the NVIDIA GPU Operator
# is installed. Everything after that is here: Redis, storage, both images,
# KEDA, autoscaling, SSO, and the route.
#
# Idempotent. Re-run it after changing .env or the code and it converges.
#
# Design rationale lives in docs/06-enterprise-architecture.md; what this
# configuration changed from the original design document and why is in
# docs/07-design-review.md. This file is deliberately just mechanism.
#
# shellcheck source=../scripts/lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/lib/common.sh"

ENTERPRISE_DIR="${REPO_ROOT}/enterprise"
MANIFESTS="${ENTERPRISE_DIR}/manifests"

: "${APP_NAMESPACE:=comfyui}"
: "${AUTH_MODE:=oauth}"
: "${MAX_GPU_WORKERS:=3}"
: "${SCALE_TO_ZERO:=true}"
: "${ENABLE_MANAGER:=false}"
: "${COMFYUI_REF:=v0.32.0}"
: "${GPU_NODE_LABEL:=nvidia.com/gpu.present=true}"
: "${QUOTA_GPU_SECONDS:=0}"

# Off unless the operator chose a number, and off rather than fatal if they
# chose something that is not one: this is a cost breaker, and a breaker that
# refuses to deploy over its own configuration is a worse outage than the
# spend it guards against. hub.py takes the same posture on the value it
# actually reads (BEGIN QUOTA BREAKER).
if ! printf '%s' "$QUOTA_GPU_SECONDS" | grep -qE '^[0-9]+(\.[0-9]+)?$'; then
    warn "QUOTA_GPU_SECONDS=${QUOTA_GPU_SECONDS} is not a number — the per-user GPU-second quota will be OFF"
    QUOTA_GPU_SECONDS=0
fi

KEDA_NAMESPACE="${KEDA_NAMESPACE:-openshift-keda}"

require_cluster
require_tools oc

log "Target"
info "cluster    $(oc whoami --show-server)"
info "namespace  $APP_NAMESPACE"
info "auth       $AUTH_MODE"
info "workers    0..${MAX_GPU_WORKERS} ($GPU_INSTANCE_TYPE)"
info "scale-to-0 $SCALE_TO_ZERO"

# Any spelling of zero, not the literal "0": hub.py treats <= 0 as off, and a
# banner that says "0.0 GPU-seconds per user" while the gateway is enforcing
# nothing is the kind of small lie an operator only finds out about later.
if [[ "$QUOTA_GPU_SECONDS" =~ ^0+(\.0+)?$ ]]; then
    info "quota      off (QUOTA_GPU_SECONDS=${QUOTA_GPU_SECONDS})"
else
    info "quota      ${QUOTA_GPU_SECONDS} GPU-seconds per user per UTC month"
fi

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

log "Checking preconditions"

if ! oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true \
    --no-headers 2>/dev/null | grep -q .; then
    warn "no GPU nodes are labelled yet."
    info "That is expected if the GPU pool is already scaled to zero."
    info "If you have never run it, do 'make gpu' first — nothing here installs"
    info "the NVIDIA GPU Operator."
else
    ok "$(oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true --no-headers | wc -l) GPU node(s) present"
fi

# The gateway and the workers land on different nodes and both need the output
# volume. A gp3 block volume is ReadWriteOnce and physically cannot do that.
if [[ "$STORAGE_MODE" != "rwx" ]]; then
    die "STORAGE_MODE is '$STORAGE_MODE' but the multi-user configuration requires 'rwx'.

          The gateway serves finished images off the same volume the workers
          write them to, and those pods are on different nodes. ReadWriteOnce
          block storage cannot be mounted by both.

          Set STORAGE_MODE=rwx in .env and run 'make storage' — that provisions
          EFS and the CSI driver. See docs/03-storage.md."
fi

ok "storage mode rwx"

# No `oc project`: every command below passes -n explicitly, and mutating the
# user's current kubeconfig context is a rude side effect for a setup script.
oc get namespace "$APP_NAMESPACE" >/dev/null 2>&1 || oc create namespace "$APP_NAMESPACE"

for claim in comfyui-models comfyui-output; do
    oc get pvc "$claim" -n "$APP_NAMESPACE" >/dev/null 2>&1 \
        || die "PVC '$claim' does not exist. Run 'make storage' first."
done

ok "volumes present"

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

log "Secrets"

if oc get secret comfy-redis -n "$APP_NAMESPACE" >/dev/null 2>&1; then
    ok "comfy-redis exists (delete it to rotate the password)"
else
    oc create secret generic comfy-redis -n "$APP_NAMESPACE" \
        --from-literal=password="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-32)"
    ok "comfy-redis created"
fi

if [[ "$AUTH_MODE" == "oauth" ]]; then
    if oc get secret comfy-gateway-session -n "$APP_NAMESPACE" >/dev/null 2>&1; then
        ok "comfy-gateway-session exists"
    else
        # oauth-proxy decodes this with base64.URLEncoding when it needs AES
        # (only with --pass-access-token or --cookie-refresh, neither of which
        # is set here), so use the URL-safe alphabet and no padding — standard
        # base64's + and / would fail that decode if those flags are ever added.
        oc create secret generic comfy-gateway-session -n "$APP_NAMESPACE" \
            --from-literal=session_secret="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"
        ok "comfy-gateway-session created"
    fi
fi

# ---------------------------------------------------------------------------
# KEDA — the pod half of scale-to-zero
# ---------------------------------------------------------------------------

install_keda()
{
    # Check for the KedaController, not the CRD. The CRDs ship with the CSV, so
    # a cluster where someone installed the operator from OperatorHub but never
    # created the operand passes a CRD check while having no controller at all —
    # ScaledObjects then apply cleanly and nothing ever scales.
    if oc get kedacontroller keda -n "$KEDA_NAMESPACE" >/dev/null 2>&1; then
        ok "KEDA is installed and the controller exists"
        return 0
    fi

    if oc get crd scaledobjects.keda.sh >/dev/null 2>&1; then
        warn "KEDA CRDs exist but there is no KedaController — creating it"
        create_keda_controller
        return 0
    fi

    log "Installing the Custom Metrics Autoscaler (KEDA)"

    # Package name has moved between OpenShift releases, so resolve it rather
    # than hardcoding.
    local package
    package="$(oc get packagemanifests -n openshift-marketplace \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
        | grep -iE 'custom-metrics-autoscaler' | head -1)"

    [[ -n "$package" ]] || die "Could not find the Custom Metrics Autoscaler operator in the catalog.
          Install 'Custom Metrics Autoscaler' from OperatorHub in the console, then re-run."

    info "package $package"

    local channel
    channel="$(oc get packagemanifest "$package" -n openshift-marketplace \
        -o jsonpath='{.status.defaultChannel}')"

    oc apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${KEDA_NAMESPACE}
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: keda
  namespace: ${KEDA_NAMESPACE}
spec:
  targetNamespaces:
    - ${KEDA_NAMESPACE}
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: ${package}
  namespace: ${KEDA_NAMESPACE}
spec:
  channel: ${channel}
  installPlanApproval: Automatic
  name: ${package}
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

    printf '          waiting for the KEDA CRDs ' >&2
    for _ in $(seq 1 60); do
        if oc get crd scaledobjects.keda.sh >/dev/null 2>&1; then
            printf ' ready\n' >&2
            break
        fi
        printf '.' >&2
        sleep 10
    done

    oc get crd scaledobjects.keda.sh >/dev/null 2>&1 \
        || die "KEDA did not install. oc get csv -n ${KEDA_NAMESPACE}"

    create_keda_controller
}

create_keda_controller()
{
    # The operator deploys no controller until this CR exists. The name must be
    # exactly "keda" and it must live in the operator's namespace.
    oc apply -f - <<EOF
apiVersion: keda.sh/v1alpha1
kind: KedaController
metadata:
  name: keda
  namespace: ${KEDA_NAMESPACE}
spec:
  watchNamespace: ""
EOF

    printf '          waiting for the keda-operator deployment ' >&2
    for _ in $(seq 1 40); do
        if oc get deployment keda-operator -n "$KEDA_NAMESPACE" \
            -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -q '[1-9]'; then
            printf ' ready\n' >&2
            ok "KEDA controller running"
            return 0
        fi
        printf '.' >&2
        sleep 10
    done

    printf '\n' >&2
    warn "the KEDA controller did not become ready. Autoscaling will not work."
    info "  oc get pods -n ${KEDA_NAMESPACE}"
    info "  oc get kedacontroller keda -n ${KEDA_NAMESPACE} -o yaml"
}

if [[ "$SCALE_TO_ZERO" == "true" ]]; then
    install_keda
else
    warn "SCALE_TO_ZERO=false — skipping KEDA. Workers stay at a fixed replica count."
fi

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

# Returns the resolved image reference on stdout. Everything else this function
# emits — including oc's own chatter and the entire container build log — must
# go to stderr, or it ends up concatenated into the caller's variable and the
# sed that substitutes the image into the manifests produces garbage.
build_image()
{
    local name="$1" context="$2"
    shift 2

    log "Building $name"

    oc apply -n "$APP_NAMESPACE" -f - >&2 <<EOF
apiVersion: image.openshift.io/v1
kind: ImageStream
metadata:
  name: ${name}
spec:
  lookupPolicy:
    local: true
---
apiVersion: build.openshift.io/v1
kind: BuildConfig
metadata:
  name: ${name}
spec:
  source:
    type: Binary
    binary: {}
  strategy:
    type: Docker
    dockerStrategy:
      dockerfilePath: Containerfile
      buildArgs:
$(for arg in "$@"; do printf '        - name: %s\n          value: "%s"\n' "${arg%%=*}" "${arg#*=}"; done)
  output:
    to:
      kind: ImageStreamTag
      name: ${name}:latest
  resources:
    limits:
      memory: 8Gi
      cpu: "4"
EOF

    oc start-build "$name" --from-dir "$context" --follow --wait -n "$APP_NAMESPACE" >&2 \
        || die "Build of $name failed. oc logs -n $APP_NAMESPACE bc/${name}"

    # The only thing on stdout.
    oc get imagestreamtag "${name}:latest" -n "$APP_NAMESPACE" \
        -o jsonpath='{.image.dockerImageReference}'
}

# Keep one copy of custom nodes. The single-user path reads app/src/custom_nodes;
# copy it into the worker build context so both images carry the same nodes.
mkdir -p "${ENTERPRISE_DIR}/worker/custom_nodes"

if [[ -d "${REPO_ROOT}/app/src/custom_nodes" ]]; then
    cp -r "${REPO_ROOT}/app/src/custom_nodes/." "${ENTERPRISE_DIR}/worker/custom_nodes/" 2>/dev/null || true
fi

# And one copy of their pip dependencies, for the same reason and by the same
# route. app/Containerfile has always installed app/requirements-extra.txt; the
# worker image never did, so a node pack that needs one pip package worked
# single-user and failed at import in the pool — reported by ComfyUI as a
# logged traceback and a missing node type, on a GPU node already provisioned
# and paid for.
if [[ -f "${REPO_ROOT}/app/requirements-extra.txt" ]]; then
    cp "${REPO_ROOT}/app/requirements-extra.txt" \
        "${ENTERPRISE_DIR}/worker/custom_nodes/requirements-extra.txt"
fi

GATEWAY_IMAGE="$(build_image comfy-gateway "${ENTERPRISE_DIR}/gateway")"
ok "gateway $GATEWAY_IMAGE"

WORKER_IMAGE="$(build_image comfy-worker "${ENTERPRISE_DIR}/worker" \
    "COMFYUI_REF=${COMFYUI_REF}" "ENABLE_MANAGER=${ENABLE_MANAGER}")"
ok "worker  $WORKER_IMAGE"

if [[ "$ENABLE_MANAGER" == "true" ]]; then
    warn "ComfyUI-Manager is baked in. Any user who reaches the UI can install"
    warn "arbitrary Python that runs on a GPU node with an instance role and a"
    warn "writable shared model volume. See docs/06-enterprise-architecture.md."
fi

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

log "Applying manifests"

# Two substitutions, not one. The image is built above and cannot be a literal
# in the manifest; QUOTA_GPU_SECONDS is Q5's per-user GPU-second quota
# (docs/10-roadmap.md), which is a per-deployment choice and so lives in .env
# beside the rest of the configuration rather than as a number somebody edits
# into a manifest — the same reason MAX_GPU_WORKERS is substituted into
# 03-autoscale.yaml below. Called from both places the gateway is applied,
# because a manifest applied from only one of them would carry the placeholder.
apply_gateway()
{
    sed -e "s#image: comfy-gateway:latest#image: ${GATEWAY_IMAGE}#" \
        -e "s#QUOTA_GPU_SECONDS_PLACEHOLDER#${QUOTA_GPU_SECONDS}#" \
        "${MANIFESTS}/01-gateway.yaml" \
        | oc apply -n "$APP_NAMESPACE" -f -
}

oc apply -n "$APP_NAMESPACE" -f "${MANIFESTS}/00-redis.yaml"
apply_gateway

# The worker's nodeSelector has to name a label the machine pool declares, or
# the cluster autoscaler cannot tell that a pending pod would fit on a node it
# has not created yet. See the long comment in 02-worker.yaml.
sed -e "s#image: comfy-worker:latest#image: ${WORKER_IMAGE}#" \
    -e "s#GPU_NODE_LABEL_KEY#${GPU_NODE_LABEL%%=*}#" \
    -e "s#GPU_NODE_LABEL_VALUE#${GPU_NODE_LABEL#*=}#" \
    "${MANIFESTS}/02-worker.yaml" | oc apply -n "$APP_NAMESPACE" -f -

ok "workers select ${GPU_NODE_LABEL}"

if [[ "$SCALE_TO_ZERO" == "true" ]]; then
    # Match the key, not the key plus its default value: matching a literal
    # "maxReplicaCount: 3" silently stops substituting the day someone edits
    # the manifest's default, and MAX_GPU_WORKERS quietly stops working.
    sed -E -e "s/^([[:space:]]*maxReplicaCount:).*/\1 ${MAX_GPU_WORKERS}/" \
        -e "s/NAMESPACE_PLACEHOLDER/${APP_NAMESPACE}/" \
        "${MANIFESTS}/03-autoscale.yaml" | oc apply -n "$APP_NAMESPACE" -f -
    ok "ScaledObject: 0..${MAX_GPU_WORKERS} workers on queue depth"
else
    oc scale deployment/comfy-worker --replicas 1 -n "$APP_NAMESPACE"
    ok "workers pinned at 1"
fi

# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------

log "Exposure ($AUTH_MODE)"

if [[ "$AUTH_MODE" == "oauth" ]]; then
    oc apply -n "$APP_NAMESPACE" -f "${MANIFESTS}/05-oauth-proxy.yaml"

    # The SAR in the patch has to name this namespace.
    PATCHED="$(mktemp)"
    trap 'rm -f "$PATCHED"' EXIT
    sed "s/NAMESPACE_PLACEHOLDER/${APP_NAMESPACE}/" \
        "${MANIFESTS}/05-oauth-proxy-patch.yaml" > "$PATCHED"

    oc patch deployment comfy-gateway -n "$APP_NAMESPACE" \
        --type strategic --patch-file "$PATCHED"

    ok "oauth-proxy sidecar added; gateway rebound to loopback"
else
    # A previous oauth run added the sidecar and rebound the gateway to
    # loopback via `oc patch` — changes `oc apply` of the base manifest can
    # never see or undo, because they were never in last-applied-configuration.
    # Left in place, the plain route below would point at a gateway nothing
    # can reach. Recreate the deployment from the base manifest instead.
    if oc get deployment comfy-gateway -n "$APP_NAMESPACE" \
        -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null \
        | grep -qw oauth-proxy; then
        warn "removing the oauth-proxy sidecar left over from AUTH_MODE=oauth"
        oc delete deployment comfy-gateway -n "$APP_NAMESPACE" --ignore-not-found
        oc delete route comfy -n "$APP_NAMESPACE" --ignore-not-found
        apply_gateway
    fi

    warn "AUTH_MODE=none — the gateway will be public with no login."
    oc apply -n "$APP_NAMESPACE" -f "${MANIFESTS}/04-route-plain.yaml"
fi

# ---------------------------------------------------------------------------
# Metrics
#
# The gateway serves /metrics (queue depth, live workers, estimated wait —
# three gauges, no job or user data). A ServiceMonitor lets OpenShift's
# user-workload monitoring scrape it, which turns "queue deeper than N for 30
# minutes" into an alert instead of a support ticket. In oauth mode the scrape
# goes through the proxy's 8443, which skips auth for /metrics only; in none
# mode, straight to the gateway port.
#
# No metricRelabelings here: the scrape is unfiltered, so a new gauge added to
# metrics() (hub.py) — comfy_estimated_wait_seconds, docs/10-roadmap.md Q6 —
# is picked up the moment the image ships it, with nothing to edit in this
# file. I4's Prometheus scaler trigger reads it the same way once it exists.
# ---------------------------------------------------------------------------

configure_metrics()
{
    if ! oc get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
        info "no ServiceMonitor CRD on this cluster — skipping metrics wiring"
        return 0
    fi

    log "Metrics"

    if [[ "$AUTH_MODE" == "oauth" ]]; then
        oc apply -n "$APP_NAMESPACE" -f - <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: comfy-gateway
  labels:
    app: comfy-gateway
spec:
  selector:
    matchLabels:
      app: comfy-gateway
  endpoints:
    - port: public
      scheme: https
      path: /metrics
      interval: 30s
      tlsConfig:
        # The serving cert names the Service; the scrape hits the pod IP.
        # In-namespace scrape of our own pod — skip hostname verification
        # rather than plumb the serving CA bundle through.
        insecureSkipVerify: true
EOF
    else
        oc apply -n "$APP_NAMESPACE" -f - <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: comfy-gateway
  labels:
    app: comfy-gateway
spec:
  selector:
    matchLabels:
      app: comfy-gateway
  endpoints:
    - port: http
      path: /metrics
      interval: 30s
EOF
    fi

    ok "ServiceMonitor applied — comfy_queue_depth, comfy_workers_registered, comfy_estimated_wait_seconds"

    # ServiceMonitors in user namespaces are scraped only once user-workload
    # monitoring is switched on, which is a one-time cluster-admin setting.
    if ! oc get configmap cluster-monitoring-config -n openshift-monitoring \
        -o jsonpath='{.data.config\.yaml}' 2>/dev/null | grep -q 'enableUserWorkload: *true'; then
        warn "user-workload monitoring is not enabled — the ServiceMonitor sits idle until it is."
        info "One-time enable (cluster admin): set 'enableUserWorkload: true' in the"
        info "cluster-monitoring-config ConfigMap in openshift-monitoring. Docs:"
        info "  https://docs.redhat.com/en/documentation/openshift_container_platform/latest/html/monitoring/enabling-monitoring-for-user-defined-projects"
    fi
}

configure_metrics

# ---------------------------------------------------------------------------
# Node-level scale to zero
#
# The half that actually saves money. KEDA removing a pod from an idle GPU node
# changes the bill by nothing at all; removing the node changes it by ~$0.98/hr.
# ---------------------------------------------------------------------------

configure_machinepool_autoscaling()
{
    [[ "$PLATFORM" == "rosa" ]] || {
        warn "PLATFORM=$PLATFORM — configure node autoscaling for your cluster yourself."
        info "Pods will scale to zero; the GPU node will not, and it bills either way."
        return 0
    }

    command -v rosa >/dev/null 2>&1 || { warn "rosa CLI not found; skipping node autoscaling."; return 0; }
    rosa whoami >/dev/null 2>&1 || { warn "rosa not logged in; skipping node autoscaling."; return 0; }

    log "GPU machine pool autoscaling"

    # min-replicas 0 is only permissible because this pool is tainted and is not
    # the cluster's only untainted pool — ROSA requires one untainted pool with
    # at least 2 replicas (single-AZ), which the base pool provides.
    if rosa edit machinepool --cluster "$CLUSTER_NAME" gpu \
        --enable-autoscaling --min-replicas 0 --max-replicas "$MAX_GPU_WORKERS" --yes 2>/dev/null; then
        ok "GPU pool autoscales 0..${MAX_GPU_WORKERS} — the node goes away when idle"
        return 0
    fi

    warn "the API refused min-replicas 0 on this cluster; falling back to 1."
    info "Pods will still scale to zero, but one GPU node stays up at roughly"
    info "\$0.98/hour (~\$700/month). If that is not acceptable, use 'make park'"
    info "or 'make down' on a schedule instead — see docs/02-cost.md."

    rosa edit machinepool --cluster "$CLUSTER_NAME" gpu \
        --enable-autoscaling --min-replicas 1 --max-replicas "$MAX_GPU_WORKERS" --yes \
        || warn "could not enable autoscaling on the GPU pool at all; it stays at a fixed size."
}

if [[ "$SCALE_TO_ZERO" == "true" ]]; then
    configure_machinepool_autoscaling
fi

# ---------------------------------------------------------------------------
# Wait and report
# ---------------------------------------------------------------------------

log "Waiting for Redis and the gateway"

oc rollout status deployment/redis -n "$APP_NAMESPACE" --timeout=300s || true

if ! oc rollout status deployment/comfy-gateway -n "$APP_NAMESPACE" --timeout=600s; then
    warn "the gateway did not become ready."
    oc get pods -n "$APP_NAMESPACE" -l app=comfy-gateway
    oc describe pod -n "$APP_NAMESPACE" -l app=comfy-gateway | sed -n '/Events/,$p' | head -30
    die "See docs/05-troubleshooting.md."
fi

ROUTE_HOST="$(oc get route comfy -n "$APP_NAMESPACE" -o jsonpath='{.spec.host}' 2>/dev/null)"

log "Up"

cat <<EOF

  https://${ROUTE_HOST}

  Workers:  0 right now — they start when you queue something.
            The first job after an idle period waits for a GPU node to be
            provisioned and a ~10 GB image to be pulled. Budget 8-17 minutes
            for it; subsequent jobs start in seconds.

  Watch it happen:
    oc get pods -n ${APP_NAMESPACE} -w
    oc get scaledobject,hpa -n ${APP_NAMESPACE}
    oc logs -n ${APP_NAMESPACE} -l app=comfy-worker -f

  Queue depth and worker count:
    curl -s https://${ROUTE_HOST}/api/stats

  Costs, and how to stop paying:
    make status
    docs/02-cost.md

EOF

if [[ "$AUTH_MODE" == "oauth" ]]; then
    cat <<EOF
  Access is gated on being able to 'get' the ${APP_NAMESPACE} namespace. Grant a
  colleague access with:

    oc adm policy add-role-to-user view <user> -n ${APP_NAMESPACE}

EOF
fi
