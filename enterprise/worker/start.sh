#!/usr/bin/env bash
#
# Start ComfyUI and the agent in one container.
#
# The naive form of this is:
#
#     CMD python3 main.py --listen 127.0.0.1 & python3 worker_agent.py
#
# which is broken in three ways that all show up only in a cluster:
#
#   - PID 1 is the shell, so SIGTERM from Kubernetes goes to the shell and not
#     to the agent. The agent never gets to finish its job; it just dies at the
#     end of the grace period.
#   - If ComfyUI crashes, the agent keeps running and the pod stays Ready while
#     consuming jobs it cannot possibly execute.
#   - Nothing reaps the background process, so a crashed ComfyUI becomes a
#     zombie and the container never restarts.
#
# This version traps SIGTERM, forwards it to both children, and exits if either
# one dies — which lets the kubelet restart the pod instead of leaving a
# half-dead worker in the pool.

set -uo pipefail

COMFY_HOST="${COMFY_HOST:-127.0.0.1}"
COMFY_PORT="${COMFY_PORT:-8188}"
COMFY_ROOT="${COMFY_ROOT:-/opt/comfyui}"

# One variable for both halves. ComfyUI is a long-lived process with a single
# fixed --output-directory, and worker_agent.py computes each submitter's
# output workspace underneath that same directory (docs/10-roadmap.md, Q3) from
# its own OUTPUT_ROOT. Two variables would let the pod come up with the agent
# naming paths under a directory ComfyUI is not writing to — every generation
# would 404 and nothing would log an error.
OUTPUT_ROOT="${OUTPUT_ROOT:-/output}"
export OUTPUT_ROOT

comfy_pid=""
agent_pid=""

shutdown()
{
    echo "[start] SIGTERM — stopping children"

    # Agent first: it stops accepting work and finishes the job in flight.
    [[ -n "$agent_pid" ]] && kill -TERM "$agent_pid" 2>/dev/null
    wait "$agent_pid" 2>/dev/null

    [[ -n "$comfy_pid" ]] && kill -TERM "$comfy_pid" 2>/dev/null
    wait "$comfy_pid" 2>/dev/null

    echo "[start] done"
    exit 0
}

trap shutdown TERM INT

# 127.0.0.1 only. The GPU pod has no Service and no Route; the only way to reach
# ComfyUI is the agent in this same container, which is the isolation the whole
# hub-and-spoke design rests on. Changing this to 0.0.0.0 quietly turns every
# GPU pod into an unauthenticated remote code execution endpoint.
echo "[start] launching ComfyUI on ${COMFY_HOST}:${COMFY_PORT}"

cd "$COMFY_ROOT" || exit 1

# --models-directory, NOT --base-directory: --base-directory would relocate
# custom_nodes lookup to /models/custom_nodes, silently ignoring every node
# baked into this image, and put checkpoints at /models/models/checkpoints.
python3 main.py \
    --listen "$COMFY_HOST" \
    --port "$COMFY_PORT" \
    --models-directory /models \
    --output-directory "$OUTPUT_ROOT" \
    --temp-directory /tmp &
comfy_pid=$!

echo "[start] launching agent"
python3 "${COMFY_ROOT}/worker_agent.py" &
agent_pid=$!

# Exit as soon as either child exits, so the kubelet can restart us.
wait -n "$comfy_pid" "$agent_pid"
exit_code=$?

echo "[start] a child exited (status ${exit_code}) — shutting the pod down"

kill -TERM "$comfy_pid" "$agent_pid" 2>/dev/null
wait 2>/dev/null

exit "$exit_code"
