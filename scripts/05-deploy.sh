#!/usr/bin/env bash
#
# Build (if needed) and deploy ComfyUI.
#
# If COMFYUI_IMAGE is set in .env, that image is deployed as-is. If it is empty,
# an in-cluster BuildConfig builds app/Containerfile and pushes to the internal
# registry — no local podman, no external registry account, no image push over
# your home connection.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_cluster
require_tools oc

log "Namespace $APP_NAMESPACE"
oc get namespace "$APP_NAMESPACE" >/dev/null 2>&1 || oc create namespace "$APP_NAMESPACE"

if ! oc get pvc comfyui-models -n "$APP_NAMESPACE" >/dev/null 2>&1; then
    die "No comfyui-models volume. Run 'make storage' first."
fi

ok "volumes present"

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build_in_cluster()
{
    log "Building in-cluster from app/Containerfile"

    oc apply -n "$APP_NAMESPACE" -f - <<EOF
apiVersion: image.openshift.io/v1
kind: ImageStream
metadata:
  name: comfyui
spec:
  lookupPolicy:
    local: true
---
apiVersion: build.openshift.io/v1
kind: BuildConfig
metadata:
  name: comfyui
spec:
  source:
    type: Binary
    binary: {}
  strategy:
    type: Docker
    dockerStrategy:
      dockerfilePath: Containerfile
      # Without this the Containerfile's ARG defaults win and the build
      # silently ignores what .env says.
      buildArgs:
        - name: COMFYUI_REF
          value: "${COMFYUI_REF}"
        - name: ENABLE_MANAGER
          value: "${ENABLE_MANAGER}"
        - name: MANAGER_REF
          value: "${MANAGER_REF}"
        - name: TORCH_INDEX
          value: "${TORCH_INDEX}"
        - name: TORCH_VERSION
          value: "${TORCH_VERSION}"
        - name: TORCHVISION_VERSION
          value: "${TORCHVISION_VERSION}"
        - name: TORCHAUDIO_VERSION
          value: "${TORCHAUDIO_VERSION}"
  output:
    to:
      kind: ImageStreamTag
      name: comfyui:latest
  resources:
    limits:
      # The CUDA base image plus torch is a large build. Starving it produces an
      # OOMKilled build pod with an unhelpful message.
      memory: 8Gi
      cpu: "4"
EOF

    ok "BuildConfig ready"

    info "streaming build logs — this takes 10-15 minutes on a cold cache"
    oc start-build comfyui \
        --from-dir "${REPO_ROOT}/app" \
        --follow --wait -n "$APP_NAMESPACE" \
        || die "Build failed. oc logs -n $APP_NAMESPACE bc/comfyui"

    RESOLVED_IMAGE="$(oc get imagestreamtag comfyui:latest -n "$APP_NAMESPACE" \
        -o jsonpath='{.image.dockerImageReference}')"

    ok "built $RESOLVED_IMAGE"
}

if [[ -n "$COMFYUI_IMAGE" ]]; then
    RESOLVED_IMAGE="$COMFYUI_IMAGE"
    log "Using image from .env"
    ok "$RESOLVED_IMAGE"

    if [[ "$ENABLE_MANAGER" == "true" ]]; then
        warn "ENABLE_MANAGER only affects images built here — your prebuilt"
        warn "image ships whatever it ships."
    fi
else
    build_in_cluster

    if [[ "$ENABLE_MANAGER" == "true" ]]; then
        warn "ComfyUI-Manager is baked in. Fine for one person behind 'make forward';"
        warn "never put a public Route in front of this pod — Manager installs and"
        warn "runs arbitrary code on a node holding cloud credentials."
        info "Model downloads land on the models volume and survive restarts."
        info "Custom-NODE installs do not — bake nodes into app/src/custom_nodes/."
    fi
fi

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

log "Applying manifests"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

cp -r "${REPO_ROOT}/manifests/base/." "$WORKDIR/"

{
    printf '\nnamespace: %s\n\nimages:\n  - name: comfyui\n' "$APP_NAMESPACE"

    # An ImageStreamTag resolves to a digest reference, not a tag. Kustomize
    # needs those expressed as `digest:`, and getting this wrong yields an
    # image ref like `registry/comfyui@sha256:sha256:abc...` that fails to pull
    # with a message that does not mention kustomize at all.
    # Digest-vs-tag handling lives in common.sh (kustomize_image_fields),
    # where scripts/unit-tests.sh pins its edge cases: digest refs, untagged
    # refs, and registries with a port.
    kustomize_image_fields "$RESOLVED_IMAGE"
} >> "${WORKDIR}/kustomization.yaml"

# S3 as the canonical model store: sync the bucket into the volume before
# ComfyUI starts, so 'make down' stops being a re-download event. The bucket,
# the read-only IAM role, and the annotated ServiceAccount come from
# scripts/09-s3-models.sh — this only wires them into the pod.
if [[ -n "${MODELS_S3_BUCKET:-}" ]]; then
    log "S3 model sync (s3://${MODELS_S3_BUCKET})"

    oc get sa comfyui -n "$APP_NAMESPACE" >/dev/null 2>&1 \
        || die "MODELS_S3_BUCKET is set but the comfyui ServiceAccount does not exist.
          Run scripts/09-s3-models.sh once — it creates the bucket, the IAM
          role, and the ServiceAccount the init container needs."

    cat >> "${WORKDIR}/kustomization.yaml" <<EOF

patches:
  - patch: |-
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: comfyui
      spec:
        template:
          spec:
            serviceAccountName: comfyui
            initContainers:
              - name: fetch-models
                image: docker.io/amazon/aws-cli:latest
                command: ["aws", "s3", "sync", "s3://${MODELS_S3_BUCKET}/", "/models", "--no-progress"]
                env:
                  # The arbitrary UID has no writable home; the CLI wants one
                  # for its credential cache.
                  - name: HOME
                    value: /tmp
                securityContext:
                  allowPrivilegeEscalation: false
                  capabilities:
                    drop: ["ALL"]
                volumeMounts:
                  - name: models
                    mountPath: /models
EOF

    ok "init container added — models sync on every pod start"
fi

oc apply -k "$WORKDIR"

# ---------------------------------------------------------------------------
# Wait, and explain the wait
# ---------------------------------------------------------------------------

log "Waiting for the pod"

info "First start pulls the image onto the GPU node and initialises CUDA."
info "Several minutes is normal. The startup probe allows 15."

if oc rollout status deployment/comfyui -n "$APP_NAMESPACE" --timeout=900s; then
    ok "ComfyUI is up"
else
    warn "Rollout did not complete. Most likely causes, in order:"
    info ""
    oc get pods -n "$APP_NAMESPACE" -l app=comfyui
    info ""
    oc describe pod -n "$APP_NAMESPACE" -l app=comfyui | sed -n '/Events/,$p' | head -30
    info ""
    info "  Pending           -> no GPU node ready. oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true"
    info "  CrashLoopBackOff  -> usually a permission error from the arbitrary UID."
    info "                       oc logs -n $APP_NAMESPACE -l app=comfyui --previous"
    info "  ImagePullBackOff  -> the image reference did not resolve."
    exit 1
fi

cat <<EOF

Open it:
  make forward          then http://localhost:8188

Load models:
  oc rsync ./checkpoints \$(oc get pod -n ${APP_NAMESPACE} -l app=comfyui -o name | head -1 | cut -d/ -f2):/models/checkpoints -n ${APP_NAMESPACE}

Stop paying when you are done:
  make park             GPU to 0 replicas,  ~\$2.04/hr -> ~\$1.06/hr
  make down             delete the cluster, ~\$2.04/hr -> ~\$0.05/hr
EOF
