#!/usr/bin/env bash
#
# Push local model files into the cluster's comfyui-models volume.
#
#   scripts/10-push-models.sh [SRC_DIR]      # default ./models
#   make push-models SRC=./models
#
# SRC_DIR's layout mirrors the volume: SRC/checkpoints, SRC/loras, SRC/vae, …
# (.gitignore already expects a local models/ staging directory.)
#
# If the single-user ComfyUI pod is running, this rsyncs straight into it.
# Otherwise — enterprise workers scaled to zero, or nothing deployed yet — it
# starts a short-lived helper pod that mounts the volume, rsyncs into that,
# and removes it. That is also why this works with ReadWriteOnce storage: the
# helper only exists while no other pod holds the volume.
#
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_cluster
require_tools oc

SRC="${1:-models}"
SRC="${SRC%/}"

[[ -d "$SRC" ]] || die "no such directory: $SRC
          Stage models in a directory mirroring the volume layout:
            $SRC/checkpoints/*.safetensors
            $SRC/loras/... etc
          then re-run. (Or pass the directory: make push-models SRC=path)"

oc get pvc comfyui-models -n "$APP_NAMESPACE" >/dev/null 2>&1 \
    || die "No comfyui-models volume in $APP_NAMESPACE. Run 'make storage' first."

# ---------------------------------------------------------------------------
# Find or create something that mounts the volume
# ---------------------------------------------------------------------------

HELPER_CREATED=false

running_comfyui_pod()
{
    oc get pods -n "$APP_NAMESPACE" -l app=comfyui \
        --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

TARGET_POD="$(running_comfyui_pod || true)"

if [[ -n "$TARGET_POD" ]]; then
    log "Using the running ComfyUI pod ($TARGET_POD)"
else
    log "No ComfyUI pod running — starting a temporary loader pod"

    oc delete pod model-loader -n "$APP_NAMESPACE" --ignore-not-found >/dev/null 2>&1

    # ubi9/ubi rather than ubi-minimal: oc rsync needs tar in the container.
    oc apply -n "$APP_NAMESPACE" -f - >/dev/null <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: model-loader
  labels:
    app: model-loader
spec:
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: loader
      image: registry.access.redhat.com/ubi9/ubi:latest
      command: ["sleep", "infinity"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: models
          mountPath: /models
  volumes:
    - name: models
      persistentVolumeClaim:
        claimName: comfyui-models
EOF

    HELPER_CREATED=true
    TARGET_POD=model-loader

    printf '          waiting for it ' >&2
    if ! oc wait --for=condition=Ready pod/model-loader -n "$APP_NAMESPACE" \
        --timeout=180s >/dev/null 2>&1; then
        printf '\n' >&2
        oc describe pod model-loader -n "$APP_NAMESPACE" | sed -n '/Events/,$p' >&2
        die "loader pod never became Ready — events above.
          With ReadWriteOnce storage, another pod may still be holding the volume."
    fi
    printf ' ready\n' >&2
fi

# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

log "Syncing $SRC/ -> $TARGET_POD:/models/"

# Trailing slash: copy SRC's contents, not SRC itself — a SRC named 'models'
# must not become /models/models (see the audit that birthed this script).
oc rsync "$SRC/" "${TARGET_POD}:/models/" -n "$APP_NAMESPACE" --progress

log "Volume now holds"
oc exec "$TARGET_POD" -n "$APP_NAMESPACE" -- \
    sh -c 'du -sh /models/* 2>/dev/null || ls /models' | sed 's/^/  /'

if [[ "$HELPER_CREATED" == "true" ]]; then
    oc delete pod model-loader -n "$APP_NAMESPACE" >/dev/null
    ok "loader pod removed"
fi

cat <<EOF

Done. If MODELS_S3_BUCKET is your canonical store, remember the volume is a
cache — upload there too, or the next 'make down' forgets what you just pushed:
  aws s3 sync "$SRC/" "s3://\${MODELS_S3_BUCKET}/"
EOF
