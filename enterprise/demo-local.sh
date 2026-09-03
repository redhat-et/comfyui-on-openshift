#!/usr/bin/env bash
#
# The multi-user pool, on the machine in front of you. No cluster, no GPU
# card, no AWS account: real Redis, the real gateway, and N real ComfyUI
# workers rendering on this machine's own silicon — Apple's Metal backend on
# an M-series Mac, CUDA on a Linux box with a card, CPU anywhere else.
#
#   ./enterprise/demo-local.sh              # bring it up, Ctrl-C tears it down
#   ./enterprise/demo-local.sh --selftest   # bring it up, render one image
#                                           # through the whole pipeline,
#                                           # verify it, tear down, exit 0/1
#   DEMO_WORKERS=3 ./enterprise/demo-local.sh
#   DEMO_WORKER_ARGS_1="--highvram" ./enterprise/demo-local.sh
#                                           # extra ComfyUI flags, per worker
#
# What this demonstrates, and what it honestly cannot:
#
#   SHOWN     the entire application architecture — the queue, fair pickup by
#             multiple workers, replayable progress over WebSockets, the
#             gateway UI, showback, outputs served off shared storage. Two
#             browser windows queueing against two workers is the pitch's
#             slide 2, live, at a cost of zero dollars.
#
#   NOT SHOWN a laptop has one GPU, so N workers time-share it — this
#             simulates the pods, not the silicon. And the cluster halves
#             (SSO, KEDA, machine pools scaling to zero) need the cluster.
#
# First run downloads CPU/Metal torch (~a few hundred MB) and an SD 1.5
# checkpoint (~2.1 GB) into DEMO_HOME (default ~/.cache/comfyui-local-demo).
# Later runs reuse both. A 48 GB unified-memory Mac runs two or three
# workers comfortably; each holds the checkpoint plus activations, roughly
# 8-12 GB at SD-1.5/SDXL scale.
#
# shellcheck source=../scripts/lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/lib/common.sh"

DEMO_HOME="${DEMO_HOME:-$HOME/.cache/comfyui-local-demo}"
DEMO_WORKERS="${DEMO_WORKERS:-2}"
GATEWAY_PORT="${GATEWAY_PORT:-8199}"
REDIS_PORT="${REDIS_PORT:-6398}"        # e2e tests own 6399; stay clear
COMFY_BASE_PORT="${COMFY_BASE_PORT:-8291}"

MODEL_FILE="v1-5-pruned-emaonly-fp16.safetensors"
MODEL_URL="https://huggingface.co/Comfy-Org/stable-diffusion-v1-5-archive/resolve/main/${MODEL_FILE}"
MODEL_URL_FALLBACK="https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors"

SELFTEST=false
[[ "${1:-}" == "--selftest" ]] && SELFTEST=true

PIDS=()

cleanup()
{
    log "Stopping"

    # Stop tracking the children as jobs before killing them, or bash
    # asynchronously prints a "Killed: 9" line per worker into the middle of
    # the teardown output. kill(1) works on bare pids regardless.
    disown -a 2>/dev/null || true

    # `|| true` on every kill: a child that already exited makes kill fail,
    # this trap runs under common.sh's set -e, and a failed command that is
    # the tail of an && list is NOT exempt from it — without the guard a
    # passed selftest exits 1 from its own teardown.
    local pid
    for pid in "${PIDS[@]-}"; do
        [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    done
    sleep 1
    for pid in "${PIDS[@]-}"; do
        [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null || true
    done

    redis-cli -p "$REDIS_PORT" -a demo-local --no-auth-warning shutdown nosave 2>/dev/null || true
    ok "down"
}
# Signals convert to an exit and only EXIT runs cleanup: trapping all three
# on cleanup directly runs the whole teardown twice on Ctrl-C (once for INT,
# again for EXIT), printing a double "Stopping" at the end of every demo.
trap 'exit 130' INT TERM
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

require_tools python3 git curl

command -v redis-server >/dev/null 2>&1 \
    || die "redis-server is not installed.
          macOS:  brew install redis
          Linux:  apt-get install redis-server / dnf install redis"

mkdir -p "$DEMO_HOME"/{models/checkpoints,output,tmp}

# ---------------------------------------------------------------------------
# Python environment — torch for whatever silicon this machine has
# ---------------------------------------------------------------------------

VENV="${DEMO_HOME}/venv"
PY="${VENV}/bin/python"

if [[ ! -x "$PY" ]]; then
    log "Creating the demo Python environment (first run only)"
    python3 -m venv "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
fi

if ! "$PY" -c "import torch" 2>/dev/null; then
    log "Installing torch (first run only — a few hundred MB)"

    # macOS default wheels carry the Metal (MPS) backend. On Linux, take the
    # CPU wheel unless the machine clearly has NVIDIA silicon.
    if [[ "$(uname -s)" == "Darwin" ]]; then
        "$PY" -m pip install --quiet torch torchvision torchaudio
    elif command -v nvidia-smi >/dev/null 2>&1; then
        "$PY" -m pip install --quiet torch torchvision torchaudio
    else
        "$PY" -m pip install --quiet torch torchvision torchaudio \
            --index-url https://download.pytorch.org/whl/cpu
    fi
fi

# ---------------------------------------------------------------------------
# ComfyUI, at the same pinned ref the images build
# ---------------------------------------------------------------------------

if [[ ! -d "${DEMO_HOME}/ComfyUI" ]]; then
    log "Fetching ComfyUI ${COMFYUI_REF}"
    # init + fetch + checkout rather than `git clone --branch`: --branch takes
    # a ref name and refuses a bare commit SHA, which is what COMFYUI_REF is
    # by default. Same single-commit download, same reasoning as the
    # Containerfiles (see enterprise/worker/Containerfile).
    git init -q "${DEMO_HOME}/ComfyUI"
    git -C "${DEMO_HOME}/ComfyUI" remote add origin \
        https://github.com/comfyanonymous/ComfyUI.git
    git -C "${DEMO_HOME}/ComfyUI" fetch -q --depth 1 origin "$COMFYUI_REF"
    git -C "${DEMO_HOME}/ComfyUI" checkout -q FETCH_HEAD
fi

if ! "$PY" -c "import yaml, aiohttp" 2>/dev/null; then
    log "Installing ComfyUI and gateway dependencies (first run only)"
    "$PY" -m pip install --quiet -r "${DEMO_HOME}/ComfyUI/requirements.txt"
    "$PY" -m pip install --quiet -r "${REPO_ROOT}/enterprise/gateway/requirements.txt" websocket-client
fi

# ComfyUI-Manager. The shared cluster ships it OFF by default — Manager
# installs arbitrary Python with a nice button, and on a cluster node that
# button is remote code execution (the worker Containerfile's argument).
# This demo is the single-user sandbox that argument contrasts against: the
# person clicking the button already owns this laptop. It is also half the
# designer's loop — "see missing models, one-click load" goes through
# Manager — so leaving it out would demo a worse product than the platform
# ships. DEMO_ENABLE_MANAGER=false leaves it out.
#
# Installed as the pip package ComfyUI itself pins (manager_requirements.txt
# says comfyui_manager==4.2.2 — the same form both Containerfiles use), NOT as
# a custom_nodes checkout: at this ComfyUI ref, main.py imports
# comfyui_manager as a module behind --enable-manager, and a bare checkout
# in custom_nodes has no top-level __init__.py to import at all.
DEMO_ENABLE_MANAGER="${DEMO_ENABLE_MANAGER:-true}"
MANAGER_ARGS=()
if [[ "$DEMO_ENABLE_MANAGER" == "true" ]]; then
    if ! "$PY" -c "import comfyui_manager" 2>/dev/null; then
        log "Installing ComfyUI-Manager (first run only)"
        "$PY" -m pip install --quiet -r "${DEMO_HOME}/ComfyUI/manager_requirements.txt"
    fi
    MANAGER_ARGS=(--enable-manager)
fi

# ---------------------------------------------------------------------------
# A model to render with
# ---------------------------------------------------------------------------

if ! ls "${DEMO_HOME}/models/checkpoints/"*.safetensors >/dev/null 2>&1; then
    log "Downloading SD 1.5 (~2.1 GB, first run only)"

    if ! curl -fL --progress-bar "$MODEL_URL" \
        -o "${DEMO_HOME}/models/checkpoints/${MODEL_FILE}"; then
        warn "primary model URL failed; trying the fallback (~4.3 GB)"
        curl -fL --progress-bar "$MODEL_URL_FALLBACK" \
            -o "${DEMO_HOME}/models/checkpoints/v1-5-pruned-emaonly.safetensors" \
            || die "could not download a checkpoint. Put any .safetensors in
          ${DEMO_HOME}/models/checkpoints/ and re-run."
    fi
fi

CKPT_NAME="$(ls "${DEMO_HOME}/models/checkpoints/" | head -1)"
ok "checkpoint: $CKPT_NAME"

# ---------------------------------------------------------------------------
# Bring the stack up: Redis, N workers (ComfyUI + agent), the gateway
# ---------------------------------------------------------------------------

export REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
export REDIS_PASSWORD="demo-local"
export QUEUE_KEY="comfy:queue"
export OUTPUT_ROOT="${DEMO_HOME}/output"

# common.sh exported the cluster default, AUTH_MODE=oauth — a promise that
# oauth-proxy sits in front stripping X-Forwarded-User. Nothing sits in front
# here, so under oauth the gateway would scope output reads to an identity no
# proxy ever sets. "none" is hub.py's own definition of a directly-reached
# gateway.
export AUTH_MODE=none

log "Redis on :${REDIS_PORT}"
redis-server --port "$REDIS_PORT" --requirepass demo-local \
    --appendonly no --daemonize yes >/dev/null
for _ in $(seq 1 20); do
    redis-cli -p "$REDIS_PORT" -a demo-local --no-auth-warning ping 2>/dev/null \
        | grep -q PONG && break
    sleep 0.5
done

log "${DEMO_WORKERS} ComfyUI worker(s) — sharing this machine's one GPU"

for i in $(seq 1 "$DEMO_WORKERS"); do
    port=$(( COMFY_BASE_PORT + i - 1 ))

    # Per-worker extra ComfyUI flags: DEMO_WORKER_ARGS_1="--highvram", etc.
    # There is no per-worker VRAM to carve up on unified-memory Apple
    # silicon — ComfyUI forces its SHARED vram state there and the vram
    # flags are inert — but on a CUDA box this same knob runs one worker
    # --highvram (models stay resident) beside a --lowvram neighbor, the
    # laptop-scale stand-in for the cluster's VRAM-tier routing; flags like
    # --force-fp16 bite on every platform. Appended LAST for the same
    # reason start.sh appends "$@" last: ComfyUI's argparse takes the last
    # occurrence of a repeated flag, so a per-worker value can also
    # override any default above it.
    args_var="DEMO_WORKER_ARGS_${i}"
    read -r -a extra_args <<< "${!args_var:-}"

    # --database-url per worker: all N workers share one ComfyUI checkout,
    # and its default SQLite file (user/comfyui.db) takes an exclusive
    # lock — every worker but the first then boots with a dead database,
    # which quietly breaks the features built on it, model downloads
    # included. In the cluster each pod has its own filesystem, so only
    # this shared-checkout demo needs the split.
    ( cd "${DEMO_HOME}/ComfyUI" && exec "$PY" main.py \
        --listen 127.0.0.1 --port "$port" \
        --models-directory "${DEMO_HOME}/models" \
        --output-directory "${DEMO_HOME}/output" \
        --temp-directory "${DEMO_HOME}/tmp" \
        --database-url "sqlite:///${DEMO_HOME}/comfy-db-${i}.sqlite" \
        ${MANAGER_ARGS[@]+"${MANAGER_ARGS[@]}"} \
        ${extra_args[@]+"${extra_args[@]}"} \
      ) > "${DEMO_HOME}/comfy-${i}.log" 2>&1 &
    PIDS+=($!)

    COMFY_PORT="$port" HOSTNAME="local-worker-${i}" \
        "$PY" "${REPO_ROOT}/enterprise/worker/worker_agent.py" \
        > "${DEMO_HOME}/agent-${i}.log" 2>&1 &
    PIDS+=($!)
done

for i in $(seq 1 "$DEMO_WORKERS"); do
    port=$(( COMFY_BASE_PORT + i - 1 ))
    printf '          worker %s on :%s ' "$i" "$port" >&2
    for _ in $(seq 1 120); do
        if curl -sf -m 2 "http://127.0.0.1:${port}/system_stats" >/dev/null 2>&1; then
            printf ' up\n' >&2
            break
        fi
        printf '.' >&2
        sleep 2
    done
done

log "Gateway on :${GATEWAY_PORT}"
"${VENV}/bin/uvicorn" hub:app --host 127.0.0.1 --port "$GATEWAY_PORT" \
    --app-dir "${REPO_ROOT}/enterprise/gateway" --log-level warning \
    > "${DEMO_HOME}/gateway.log" 2>&1 &
PIDS+=($!)

for _ in $(seq 1 30); do
    curl -sf -m 2 "http://127.0.0.1:${GATEWAY_PORT}/healthz" >/dev/null 2>&1 && break
    sleep 0.5
done
curl -sf -m 2 "http://127.0.0.1:${GATEWAY_PORT}/healthz" >/dev/null \
    || { tail -20 "${DEMO_HOME}/gateway.log" >&2; die "gateway did not start"; }

# ---------------------------------------------------------------------------
# Selftest: one real render through the whole pipeline
# ---------------------------------------------------------------------------

if [[ "$SELFTEST" == "true" ]]; then
    log "Selftest: queueing one real render through the gateway"

    # A timestamp to compare outputs against, so a PNG left by a previous
    # run cannot pass this run's selftest.
    touch "${DEMO_HOME}/tmp/selftest-started"

    job_id="$(CKPT="$CKPT_NAME" GW="http://127.0.0.1:${GATEWAY_PORT}" "$PY" - <<'PYEOF'
import json, os, urllib.request
workflow = {
  "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": os.environ["CKPT"]}},
  "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": "a lighthouse on a basalt coast, storm light"}},
  "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": "blurry, lowres"}},
  "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
  "3": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
        "latent_image": ["5", 0], "seed": 7, "steps": 12, "cfg": 7.0,
        "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
  "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
  "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "demo"}},
}
req = urllib.request.Request(os.environ["GW"] + "/api/generate",
    data=json.dumps({"workflow": workflow}).encode(),
    headers={"Content-Type": "application/json"})
print(json.loads(urllib.request.urlopen(req, timeout=15).read())["job_id"])
PYEOF
)"

    info "job ${job_id} — first render loads the checkpoint, allow a few minutes"

    # `|| true` because this whole file runs under common.sh's set -e -o
    # pipefail: one transient poll failure (a 404 before the state hash
    # lands, a gateway blip) would otherwise abort the selftest through the
    # EXIT trap with no diagnostic at all, not merely miss one sample.
    status=""
    for _ in $(seq 1 150); do
        status="$(curl -sf "http://127.0.0.1:${GATEWAY_PORT}/api/jobs/${job_id}" \
            | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null \
            || true)"
        [[ "$status" == "completed" || "$status" == "failed" ]] && break
        sleep 4
    done

    if [[ "$status" != "completed" ]]; then
        warn "job ended as '${status:-unknown}'"
        tail -20 "${DEMO_HOME}"/agent-*.log >&2
        die "selftest failed"
    fi

    # find, not a top-level glob: the agent rewrites every job into a
    # per-submitter workspace (worker_agent.py, BEGIN OUTPUT WORKSPACES), so
    # the image lands at output/_anonymous/demo_*.png, one level down.
    rendered="$(find "${DEMO_HOME}/output" -name 'demo*.png' \
        -newer "${DEMO_HOME}/tmp/selftest-started" 2>/dev/null | head -1 || true)"
    [[ -n "$rendered" && "$(wc -c < "$rendered")" -gt 50000 ]] \
        || die "job completed but no plausible output image found in ${DEMO_HOME}/output"

    ok "rendered $(basename "$rendered") ($(du -h "$rendered" | cut -f1)) through queue -> worker -> shared output"
    log "Selftest passed"
    exit 0
fi

# ---------------------------------------------------------------------------

cat <<EOF

  The pool is up, on this machine:

    open http://127.0.0.1:${GATEWAY_PORT}

  Each worker's stock ComfyUI canvas — the frontend designers already
  know, Manager included — is one tab away (the local-demo bonus; in the
  cluster the workers are unreachable by design):

    open http://127.0.0.1:${COMFY_BASE_PORT}    # +1 per additional worker

  Authoring stays in ComfyUI; the gateway is where a finished workflow
  goes to run on the pool. Paste a workflow in API format, or drive it
  from a terminal:

    curl -s -X POST http://127.0.0.1:${GATEWAY_PORT}/api/generate \\
        -H 'Content-Type: application/json' -d @workflow_api.json
    curl -s http://127.0.0.1:${GATEWAY_PORT}/api/stats

  ${DEMO_WORKERS} worker(s) are sharing this machine's one GPU — pods are
  simulated, silicon is not. Logs live in ${DEMO_HOME}/.

  Ctrl-C tears everything down.
EOF

wait
