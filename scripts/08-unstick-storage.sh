#!/usr/bin/env bash
#
# Diagnose and repair a volume that a dead pod never let go of.
#
#   ./scripts/08-unstick-storage.sh            diagnose only, changes nothing
#   ./scripts/08-unstick-storage.sh --repair   fix what is safe to fix, ask before the rest
#
# ---------------------------------------------------------------------------
# What is actually going on
#
# A PersistentVolume is released when the kubelet on the node that mounted it
# tells the control plane it has unmounted. Nothing else releases it. Not
# deleting the pod, not deleting the Deployment, not deleting the PVC.
#
# So when a node dies — a spot reclaim, a hard crash, a lost network partition —
# there is no kubelet left to say that. The control plane's position is "that
# volume is still mounted over there", forever, and the replacement pod waits
# forever. For a ReadWriteOnce volume it waits on `Multi-Attach error`; for EFS
# it waits on a mount that will never complete.
#
# The instinct at that point is:
#
#     oc delete pod ... --force --grace-period=0
#
# That makes it worse, and it is worth being precise about why. Force-delete
# does not terminate anything. It deletes the *API object* while the container
# may still be running, so the kubelet never gets to unmount and never gets to
# report the unmount. You have thrown away the only record of what still needs
# releasing. The volume is now stuck with no pod to point at.
#
# The supported mechanism is a taint that tells the control plane the node is
# genuinely gone and its volumes may be force-detached:
#
#     node.kubernetes.io/out-of-service=nodeshutdown:NoExecute
#
# The reason this is not the default, and the reason this script asks before
# applying it: if the node is actually still running and still has the
# filesystem mounted, force-detaching it while it writes will corrupt data. So
# the whole job here is establishing that the node is really dead before
# reaching for it — which this script does against the EC2 API, not by
# guessing from the node's Ready condition.
# ---------------------------------------------------------------------------
#
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_cluster
require_tools oc jq

REPAIR=false
[[ "${1:-}" == "--repair" ]] && REPAIR=true

STUCK_MINUTES="${STUCK_MINUTES:-5}"
FOUND_PROBLEM=false

now_epoch()
{
    date -u +%s
}

age_minutes()
{
    local timestamp="$1"
    local then_epoch

    then_epoch="$(date -u -d "$timestamp" +%s 2>/dev/null \
        || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$timestamp" +%s 2>/dev/null)"
    [[ -n "$then_epoch" ]] || { echo 0; return; }

    echo $(( ( $(now_epoch) - then_epoch ) / 60 ))
}

# ---------------------------------------------------------------------------
# 1. Nodes
# ---------------------------------------------------------------------------

log "Nodes"

NOT_READY_NODES=()

while IFS=$'\t' read -r node_name ready; do
    [[ -n "$node_name" ]] || continue

    if [[ "$ready" == "True" ]]; then
        continue
    fi

    NOT_READY_NODES+=("$node_name")
    printf '  %-52s Ready=%s\n' "$node_name" "$ready"
done < <(oc get nodes -o json \
    | jq -r '.items[] | [.metadata.name, ([.status.conditions[]|select(.type=="Ready")|.status]|first)] | @tsv')

if (( ${#NOT_READY_NODES[@]} == 0 )); then
    ok "all nodes Ready"
else
    FOUND_PROBLEM=true
    warn "${#NOT_READY_NODES[@]} node(s) not Ready — these are the ones that strand volumes"
fi

# ---------------------------------------------------------------------------
# 2. Pods stuck Terminating
# ---------------------------------------------------------------------------

log "Pods stuck Terminating for more than ${STUCK_MINUTES} minutes"

STUCK_PODS=()

while IFS=$'\t' read -r namespace pod node deletion_ts; do
    [[ -n "$pod" ]] || continue

    local_age="$(age_minutes "$deletion_ts")"
    (( local_age >= STUCK_MINUTES )) || continue

    STUCK_PODS+=("${namespace}/${pod}/${node}")
    printf '  %-40s on %-38s %s min\n' "${namespace}/${pod}" "$node" "$local_age"
done < <(oc get pods -A -o json \
    | jq -r '.items[] | select(.metadata.deletionTimestamp != null)
             | [.metadata.namespace, .metadata.name, (.spec.nodeName // "unscheduled"), .metadata.deletionTimestamp]
             | @tsv')

if (( ${#STUCK_PODS[@]} == 0 )); then
    ok "none"
else
    FOUND_PROBLEM=true
fi

# ---------------------------------------------------------------------------
# 3. VolumeAttachments pointing at nodes that are gone
#
# This is the smoking gun for a ReadWriteOnce volume that will not release.
# ---------------------------------------------------------------------------

log "VolumeAttachments"

ORPHANED_ATTACHMENTS=()
EXISTING_NODES="$(oc get nodes -o jsonpath='{.items[*].metadata.name}')"

while IFS=$'\t' read -r attachment node volume _attached; do
    [[ -n "$attachment" ]] || continue

    node_state="ok"

    if [[ " $EXISTING_NODES " != *" $node "* ]]; then
        node_state="NODE GONE"
    elif [[ " ${NOT_READY_NODES[*]-} " == *" $node "* ]]; then
        node_state="NODE NOT READY"
    fi

    if [[ "$node_state" != "ok" ]]; then
        ORPHANED_ATTACHMENTS+=("${attachment}|${node}|${volume}")
        printf '  %-46s %-30s %s\n' "${volume:0:46}" "$node" "$node_state"
    fi
done < <(oc get volumeattachments -o json 2>/dev/null \
    | jq -r '.items[] | [.metadata.name, .spec.nodeName, (.spec.source.persistentVolumeName // "?"), (.status.attached|tostring)] | @tsv')

if (( ${#ORPHANED_ATTACHMENTS[@]} == 0 )); then
    ok "none stranded"
else
    FOUND_PROBLEM=true
    warn "${#ORPHANED_ATTACHMENTS[@]} volume(s) still recorded as attached to a node that cannot release them"
fi

# ---------------------------------------------------------------------------
# 4. Pods that cannot start because of it
# ---------------------------------------------------------------------------

log "Pods blocked on a volume"

BLOCKED=0

while IFS=$'\t' read -r namespace pod _reason message; do
    [[ -n "$pod" ]] || continue

    case "$message" in
        *Multi-Attach*|*FailedAttachVolume*|*"timed out waiting for the condition"*|*"unable to attach or mount"*)
            printf '  %-40s %s\n' "${namespace}/${pod}" "${message:0:90}"
            BLOCKED=$(( BLOCKED + 1 ))
            ;;
    esac
done < <(oc get events -A --field-selector reason=FailedAttachVolume,type=Warning -o json 2>/dev/null \
    | jq -r '.items[] | [.metadata.namespace, .involvedObject.name, .reason, .message] | @tsv' 2>/dev/null)

# FailedMount carries the Multi-Attach text on some versions.
while IFS=$'\t' read -r namespace pod message; do
    [[ -n "$pod" ]] || continue

    if [[ "$message" == *Multi-Attach* ]]; then
        printf '  %-40s %s\n' "${namespace}/${pod}" "${message:0:90}"
        BLOCKED=$(( BLOCKED + 1 ))
    fi
done < <(oc get events -A --field-selector reason=FailedMount,type=Warning -o json 2>/dev/null \
    | jq -r '.items[] | [.metadata.namespace, .involvedObject.name, .message] | @tsv' 2>/dev/null)

(( BLOCKED == 0 )) && ok "none" || FOUND_PROBLEM=true

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

if [[ "$FOUND_PROBLEM" == "false" ]]; then
    log "Nothing stuck"
    exit 0
fi

if [[ "$REPAIR" != "true" ]]; then
    cat >&2 <<EOF

  Found something. Re-run with --repair to fix it:

    ./scripts/08-unstick-storage.sh --repair

  It will confirm each node is genuinely dead — against the EC2 API, not by
  guessing — before force-detaching anything.

  Do NOT reach for 'oc delete pod --force --grace-period=0'. That deletes the
  pod record while the container may still be running, which strands the volume
  permanently instead of releasing it.

EOF
    exit 1
fi

# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

instance_state_for_node()
{
    local node_name="$1"
    local provider_id instance_id

    command -v aws >/dev/null 2>&1 || { echo "unknown (no aws CLI)"; return; }

    # aws:///us-east-2a/i-0abc123
    provider_id="$(oc get node "$node_name" -o jsonpath='{.spec.providerID}' 2>/dev/null)"
    instance_id="${provider_id##*/}"

    if [[ -z "$instance_id" || "$instance_id" != i-* ]]; then
        echo "unknown (no instance id on the Node object)"
        return
    fi

    aws ec2 describe-instances --instance-ids "$instance_id" \
        --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null \
        || echo "not-found"
}

log "Repair"

for node_name in "${NOT_READY_NODES[@]-}"; do
    [[ -n "$node_name" ]] || continue

    state="$(instance_state_for_node "$node_name")"

    printf '\n  node %s\n  EC2 instance state: %s\n' "$node_name" "$state"

    # terminated / stopped / not-found means the machine is definitively not
    # writing to anything. Anything else and we do not touch it.
    case "$state" in
        terminated|stopped|shutting-down|not-found)
            info "confirmed gone — safe to force-detach its volumes"
            ;;
        *)
            warn "the instance is '$state', which is not confirmed dead."
            warn "Refusing to taint it. Force-detaching a node that is still writing"
            warn "to the filesystem corrupts data."
            info "If you know it is dead (console shows it terminated, say), apply the"
            info "taint by hand:"
            info "  oc adm taint node $node_name node.kubernetes.io/out-of-service=nodeshutdown:NoExecute"
            continue
            ;;
    esac

    if oc get node "$node_name" -o jsonpath='{.spec.taints[*].key}' 2>/dev/null \
        | grep -q 'node.kubernetes.io/out-of-service'; then
        ok "already tainted out-of-service"
        continue
    fi

    read -r -p "  Apply the out-of-service taint to ${node_name}? [yes/N] " reply

    if [[ "$reply" != "yes" ]]; then
        info "skipped"
        continue
    fi

    # This deletes every pod on the node and force-detaches its volumes.
    oc adm taint node "$node_name" \
        node.kubernetes.io/out-of-service=nodeshutdown:NoExecute

    ok "tainted — volumes will detach and pods will reschedule within a minute or two"

    cat <<EOF

  IMPORTANT: remove this taint if the node ever comes back Ready, or it will
  refuse to run anything:

    oc adm taint node ${node_name} node.kubernetes.io/out-of-service=nodeshutdown:NoExecute-

EOF
done

# ---------------------------------------------------------------------------

if (( ${#ORPHANED_ATTACHMENTS[@]} > 0 )); then
    log "Stranded VolumeAttachments"

    info "The taint above normally clears these within a minute. Check again"
    info "before deleting any by hand — a VolumeAttachment deleted while the"
    info "volume is genuinely still mounted somewhere is how you get two writers."
    info ""

    for entry in "${ORPHANED_ATTACHMENTS[@]}"; do
        IFS='|' read -r attachment node volume <<< "$entry"
        info "  oc delete volumeattachment $attachment   # ${volume} on ${node}"
    done
fi

log "Re-checking"
sleep 20
# `exec "$0"` alone depends on the file's own execute bit surviving however it
# got here (a fresh checkout, a copy, tar without permissions) and, more to
# the point, drops every argument — so a re-run started with --repair would
# silently fall back to diagnose-only on this self re-exec. Invoke it through
# bash explicitly and forward argv.
exec bash "$0" "$@"
