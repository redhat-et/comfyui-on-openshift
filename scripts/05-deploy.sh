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
      # Without this the Containerfile's ARG default wins and the build
      # silently tracks whatever tag that default names, not the .env pin.
      buildArgs:
        - name: COMFYUI_REF
          value: "${COMFYUI_REF}"
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
else
    build_in_cluster
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
    if [[ "$RESOLVED_IMAGE" == *"@"* ]]; then
        printf '    newName: %s\n    digest: %s\n' \
            "${RESOLVED_IMAGE%@*}" "${RESOLVED_IMAGE#*@}"
    else
        image_tag="${RESOLVED_IMAGE##*:}"

        # A ref with no tag (or whose only colon is a registry port, in which
        # case the "tag" contains a slash) would otherwise emit the whole ref
        # as newTag and fail the pull with a baffling message.
        if [[ "$image_tag" == "$RESOLVED_IMAGE" || "$image_tag" == */* ]]; then
            printf '    newName: %s\n    newTag: latest\n' "$RESOLVED_IMAGE"
        else
            printf '    newName: %s\n    newTag: %s\n' \
                "${RESOLVED_IMAGE%:*}" "$image_tag"
        fi
    fi
} >> "${WORKDIR}/kustomization.yaml"

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
