#!/usr/bin/env bash
#
# Print how to log in to the cluster. Does not log you in — the password is not
# something to pass through a shell history.
#
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

if oc whoami >/dev/null 2>&1; then
    log "Already logged in"
    info "server $(oc whoami --show-server)"
    info "user   $(oc whoami)"
    exit 0
fi

if [[ "$PLATFORM" != "rosa" ]]; then
    log "PLATFORM=$PLATFORM"
    info "Log in to your own cluster:  oc login <api-url> -u <user>"
    exit 0
fi

require_rosa

if ! rosa describe cluster -c "$CLUSTER_NAME" >/dev/null 2>&1; then
    die "No cluster named '$CLUSTER_NAME'. Run: make cluster"
fi

API_URL="$(rosa describe cluster -c "$CLUSTER_NAME" -o json | jq -r '.api.url')"
CONSOLE_URL="$(rosa describe cluster -c "$CLUSTER_NAME" -o json | jq -r '.console.url')"

log "Cluster $CLUSTER_NAME"
info "api     $API_URL"
info "console $CONSOLE_URL"

if rosa describe admin -c "$CLUSTER_NAME" >/dev/null 2>&1; then
    log "cluster-admin exists"
    rosa describe admin -c "$CLUSTER_NAME"

    cat <<EOF

  If you no longer have the password, rotate it:
    rosa delete admin -c $CLUSTER_NAME && rosa create admin -c $CLUSTER_NAME

  Newly created admin users take a few minutes to propagate through the
  identity provider. A failed login right after 'rosa create admin' usually
  just means you were early.
EOF
else
    cat <<EOF

  No cluster-admin yet:
    rosa create admin -c $CLUSTER_NAME
EOF
fi
