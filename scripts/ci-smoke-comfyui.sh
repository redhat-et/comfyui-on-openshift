#!/usr/bin/env bash
#
# Boot the real ComfyUI — the pinned tag, on CPU, with the same path flags the
# images use — and prove the path contract holds:
#
#   1. A checkpoint placed in $MODELS_DIR/checkpoints is visible to the
#      CheckpointLoaderSimple node. This is the assertion that fails if anyone
#      reverts --models-directory to --base-directory: under that flag ComfyUI
#      looks in <dir>/models/checkpoints instead, and every model loaded per
#      this repo's docs silently disappears from the UI.
#   2. A custom node in the install tree's custom_nodes/ actually loads —
#      the other half of the same regression (--base-directory relocates
#      custom_nodes lookup away from the image).
#
# Needs no GPU, no cluster, no AWS: CPU torch and ~a minute of runner time.
# CI runs it on every PR; run it locally the same way (any platform torch
# runs on):
#
#   MODELS_DIR=/tmp/m OUTPUT_DIR=/tmp/o scripts/ci-smoke-comfyui.sh
#
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

MODELS_DIR="${MODELS_DIR:-/models}"
OUTPUT_DIR="${OUTPUT_DIR:-/output}"
SMOKE_PORT="${SMOKE_PORT:-8188}"
WORK="${SMOKE_WORKDIR:-$(mktemp -d)}"

COMFY_PID=""

cleanup()
{
    [[ -n "$COMFY_PID" ]] && kill "$COMFY_PID" 2>/dev/null
    wait 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Guard the launch commands themselves. The smoke test below runs the flags
# this script chooses; these greps tie it to the flags the images actually
# ship, so editing a Containerfile out from under the test fails loudly.
# ---------------------------------------------------------------------------

log "Launch-flag contract in the images"

CMD_BLOCK="$(sed -n '/^CMD \[/,/\]/p' "${REPO_ROOT}/app/Containerfile")"
START_BLOCK="$(sed -n '/^python3 main.py/,/&$/p' "${REPO_ROOT}/enterprise/worker/start.sh")"

check_block()
{
    local label="$1" block="$2"

    if ! grep -q -- '--models-directory' <<< "$block"; then
        die "$label no longer passes --models-directory — the smoke test below would lie."
    fi

    if grep -q -- '--base-directory' <<< "$block"; then
        die "$label passes --base-directory, which relocates models AND custom_nodes.
          See the comment above CMD in app/Containerfile before changing this."
    fi

    ok "$label uses --models-directory"
}

check_block "app/Containerfile CMD" "$CMD_BLOCK"
check_block "worker start.sh launch" "$START_BLOCK"

# ---------------------------------------------------------------------------
# Fetch and install the pinned ComfyUI
# ---------------------------------------------------------------------------

COMFYUI_SMOKE_REF="$(grep '^COMFYUI_REF=' "${REPO_ROOT}/.env.example" | cut -d= -f2)"
[[ -n "$COMFYUI_SMOKE_REF" ]] || die "could not read COMFYUI_REF from .env.example"

log "ComfyUI $COMFYUI_SMOKE_REF on CPU"

# init + fetch + checkout rather than `git clone --branch`, for the reason the
# Containerfiles give: --branch takes a branch or tag and refuses a commit,
# and COMFYUI_REF is a commit now. This accepts either.
git init -q "${WORK}/ComfyUI"
git -C "${WORK}/ComfyUI" fetch --quiet --depth 1 \
    https://github.com/comfyanonymous/ComfyUI.git "$COMFYUI_SMOKE_REF"
git -C "${WORK}/ComfyUI" checkout -q FETCH_HEAD

# Same three version numbers app/Containerfile (and enterprise/worker/
# Containerfile) install via TORCH_VERSION/TORCHVISION_VERSION/
# TORCHAUDIO_VERSION — read from the Containerfile itself so a version bump
# there does not silently unpin this script from what CI actually runs.
# This is a real gate, not caution: below torch 2.8 ComfyUI's DynamicVRAM
# check (main.py, see the Containerfile comment above TORCH_VERSION) falls
# back to a legacy code path with only a log line to show for it, so an
# unpinned resolve here could pass while proving nothing about the pinned
# stack.
TORCH_VERSION="$(grep '^ARG TORCH_VERSION=' "${REPO_ROOT}/app/Containerfile" | cut -d= -f2)"
TORCHVISION_VERSION="$(grep '^ARG TORCHVISION_VERSION=' "${REPO_ROOT}/app/Containerfile" | cut -d= -f2)"
TORCHAUDIO_VERSION="$(grep '^ARG TORCHAUDIO_VERSION=' "${REPO_ROOT}/app/Containerfile" | cut -d= -f2)"

[[ -n "$TORCH_VERSION" && -n "$TORCHVISION_VERSION" && -n "$TORCHAUDIO_VERSION" ]] \
    || die "could not read TORCH_VERSION/TORCHVISION_VERSION/TORCHAUDIO_VERSION from app/Containerfile"

log "torch ${TORCH_VERSION} / torchvision ${TORCHVISION_VERSION} / torchaudio ${TORCHAUDIO_VERSION}"

# The Containerfiles use TORCH_INDEX=.../whl/cu128 (a CUDA index); this
# script has no GPU, so it installs the same three version numbers from the
# CPU wheel index instead of the CUDA one — same pins, no multi-GB CUDA
# download. The cpu wheel index is linux-only; macOS default wheels are
# already CPU/Metal, so the version pins apply there without an index-url.
if [[ "$(uname -s)" == "Linux" ]]; then
    python3 -m pip install --quiet \
        torch=="${TORCH_VERSION}" \
        torchvision=="${TORCHVISION_VERSION}" \
        torchaudio=="${TORCHAUDIO_VERSION}" \
        --index-url https://download.pytorch.org/whl/cpu
else
    python3 -m pip install --quiet \
        torch=="${TORCH_VERSION}" \
        torchvision=="${TORCHVISION_VERSION}" \
        torchaudio=="${TORCHAUDIO_VERSION}"
fi

python3 -m pip install --quiet -r "${WORK}/ComfyUI/requirements.txt"

ok "installed"

# ---------------------------------------------------------------------------
# Plant the probes: a fake checkpoint where the docs say models go, and a
# trivial custom node where the images bake them.
# ---------------------------------------------------------------------------

mkdir -p "${MODELS_DIR}/checkpoints" "$OUTPUT_DIR" "${WORK}/scratch"
touch "${MODELS_DIR}/checkpoints/ci-probe.safetensors"

mkdir -p "${WORK}/ComfyUI/custom_nodes/ci_probe"
cat > "${WORK}/ComfyUI/custom_nodes/ci_probe/__init__.py" <<'EOF'
class CIProbe:
    CATEGORY = "ci"
    RETURN_TYPES = ()
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def run(self):
        return ()


NODE_CLASS_MAPPINGS = {"CIProbe": CIProbe}
EOF

# ---------------------------------------------------------------------------
# Boot with the images' flags (plus --cpu, which is the whole point)
# ---------------------------------------------------------------------------

log "Booting"

(
    cd "${WORK}/ComfyUI" || exit 1
    exec python3 main.py --cpu \
        --listen 127.0.0.1 \
        --port "$SMOKE_PORT" \
        --models-directory "$MODELS_DIR" \
        --output-directory "$OUTPUT_DIR" \
        --temp-directory "${WORK}/scratch"
) > "${WORK}/comfy.log" 2>&1 &
COMFY_PID=$!

booted=false

for _ in $(seq 1 60); do
    if curl -sf -m 2 "http://127.0.0.1:${SMOKE_PORT}/system_stats" >/dev/null 2>&1; then
        booted=true
        break
    fi

    kill -0 "$COMFY_PID" 2>/dev/null || break
    sleep 2
done

if [[ "$booted" != "true" ]]; then
    tail -40 "${WORK}/comfy.log" >&2
    die "ComfyUI never answered on port ${SMOKE_PORT} — log tail above."
fi

ok "up"

# ---------------------------------------------------------------------------
# The assertions
# ---------------------------------------------------------------------------

log "Path contract"

SMOKE_PORT="$SMOKE_PORT" python3 - <<'EOF'
import json, os, sys, urllib.request

port = os.environ["SMOKE_PORT"]
info = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/object_info", timeout=15))

failures = []

# 1. Models: the probe checkpoint must be offered by CheckpointLoaderSimple.
try:
    choices = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
except (KeyError, IndexError):
    choices = []

if "ci-probe.safetensors" in choices:
    print("  PASS  checkpoint in <models-dir>/checkpoints is visible to the loader")
else:
    failures.append("models")
    print(f"  FAIL  checkpoint not visible — loader offers: {choices!r}")
    print("        (--base-directory regression? it looks in <dir>/models/checkpoints)")

# 2. Custom nodes: the probe node in the install tree must have loaded.
if "CIProbe" in info:
    print("  PASS  custom node in the install tree's custom_nodes/ loaded")
else:
    failures.append("custom_nodes")
    print("  FAIL  custom node did not load — custom_nodes lookup left the install tree")

sys.exit(1 if failures else 0)
EOF

log "Smoke test clean"
