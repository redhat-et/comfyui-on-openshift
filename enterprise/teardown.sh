#!/usr/bin/env bash
#
# Remove the multi-user configuration, leaving the cluster and the model volume
# alone.
#
#   ./enterprise/teardown.sh          remove the app, keep Redis data and models
#   ./enterprise/teardown.sh --all    also delete Redis's volume and the secrets
#
# To stop paying for the GPU rather than remove the app, you want `make park`
# or `make down` from the repo root instead — see docs/02-cost.md.
#
# shellcheck source=../scripts/lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/lib/common.sh"

require_cluster
: "${APP_NAMESPACE:=comfyui}"

PURGE=false
[[ "${1:-}" == "--all" ]] && PURGE=true

log "Removing the multi-user stack from $APP_NAMESPACE"

# ScaledObject first. Deleting the Deployment while KEDA still owns it leaves an
# orphaned HPA that logs errors about a missing scale target forever.
oc delete scaledobject comfy-worker -n "$APP_NAMESPACE" --ignore-not-found
oc delete triggerauthentication comfy-redis-auth -n "$APP_NAMESPACE" --ignore-not-found

oc delete servicemonitor comfy-gateway -n "$APP_NAMESPACE" --ignore-not-found 2>/dev/null || true
oc delete route comfy -n "$APP_NAMESPACE" --ignore-not-found
oc delete deployment comfy-gateway comfy-worker -n "$APP_NAMESPACE" --ignore-not-found
oc delete service comfy-gateway -n "$APP_NAMESPACE" --ignore-not-found
oc delete pdb comfy-gateway -n "$APP_NAMESPACE" --ignore-not-found
oc delete sa comfy-gateway -n "$APP_NAMESPACE" --ignore-not-found
oc delete bc,is comfy-gateway comfy-worker -n "$APP_NAMESPACE" --ignore-not-found

ok "application removed"

if [[ "$PURGE" == "true" ]]; then
    log "Purging Redis and secrets"

    oc delete deployment redis -n "$APP_NAMESPACE" --ignore-not-found
    oc delete service redis -n "$APP_NAMESPACE" --ignore-not-found
    oc delete networkpolicy redis-allow-app-only -n "$APP_NAMESPACE" --ignore-not-found
    oc delete pvc redis-data -n "$APP_NAMESPACE" --ignore-not-found
    oc delete secret comfy-redis comfy-gateway-session comfy-gateway-tls \
        -n "$APP_NAMESPACE" --ignore-not-found

    ok "purged"
else
    info "Redis, its volume, and comfyui-models/comfyui-output were left in place."
    info "Re-run enterprise/setup.sh to bring the stack back with the same data."
fi

if [[ "$PLATFORM" == "rosa" ]] && command -v rosa >/dev/null 2>&1 && rosa whoami >/dev/null 2>&1; then
    log "GPU machine pool"
    info "Autoscaling is left as-is. To pin it back to a fixed size:"
    info "  rosa edit machinepool --cluster $CLUSTER_NAME gpu --replicas 0 --yes"
fi
