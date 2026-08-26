"""
Worker agent: the sidecar-in-the-same-container that connects a locked-down
ComfyUI process to the Redis queue.

Runs alongside ComfyUI in the GPU pod. ComfyUI binds 127.0.0.1 only, so this
agent is the sole path in or out — no user can reach the GPU pod directly, and
the pod needs no Service, no Route, and no ingress rules.

Four things here that the obvious version of this script gets wrong, each of
which produces an intermittent bug that is miserable to diagnose in a cluster:

  1. Connect the WebSocket BEFORE submitting the prompt. ComfyUI starts emitting
     progress the moment it accepts the job. Submit-then-connect drops every
     event in that window, so short jobs appear to produce no progress at all
     and long ones start at 30%.

  2. Filter every event by prompt_id. One ComfyUI instance multiplexes all
     prompts onto one socket. Without the filter, another prompt's terminal
     "executing: node=None" ends the wrong job's stream.

  3. Time out the recv(). A bare ws.recv() blocks forever, so a ComfyUI process
     that dies or wedges takes the worker with it — the pod stays Running, stops
     consuming the queue, and nothing reports an error.

  4. Handle SIGTERM. This pool is autoscaled to zero, so pods get terminated as
     a matter of routine, not as a failure. Without a handler the worker is
     killed mid-generation and the job vanishes with no terminal event, leaving
     the user's browser on a progress bar that never moves.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid

import redis
import websocket  # websocket-client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None

QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
EVENT_STREAM_TTL = int(os.environ.get("EVENT_STREAM_TTL", "3600"))

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_ADDR = f"{COMFY_HOST}:{COMFY_PORT}"

# How long a single generation may run before we give up on it. Long enough for
# a big batch on a slow card, short enough that a wedged worker recovers within
# a coffee break.
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
RECV_TIMEOUT = int(os.environ.get("RECV_TIMEOUT", "60"))
BOOT_TIMEOUT = int(os.environ.get("BOOT_TIMEOUT", "600"))

WORKER_ID = os.environ.get("HOSTNAME") or f"worker-{uuid.uuid4().hex[:8]}"
CLIENT_ID = str(uuid.uuid4())

shutting_down = False


def log(message: str) -> None:
    print(f"[agent] {message}", flush=True)


def handle_sigterm(_signum, _frame) -> None:
    """
    Stop accepting new work, let the current job finish.

    Kubernetes sends SIGTERM and then waits terminationGracePeriodSeconds before
    SIGKILL. The Deployment sets that generously for exactly this reason — a
    half-finished generation is wasted GPU time and a confused user.
    """
    global shutting_down

    log("SIGTERM received — finishing the current job, then exiting")
    shutting_down = True


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

def connect_redis() -> redis.Redis:
    conn = redis.from_url(
        REDIS_URL,
        password=REDIS_PASSWORD,
        decode_responses=True,
        health_check_interval=30,
    )
    conn.ping()

    return conn


def stream_key(job_id: str) -> str:
    return f"comfy:job:{job_id}:events"


def state_key(job_id: str) -> str:
    return f"comfy:job:{job_id}:state"


def emit(conn: redis.Redis, job_id: str, event: dict) -> None:
    """Append one event to the job's stream. The gateway tails this."""
    key = stream_key(job_id)

    conn.xadd(key, {"data": json.dumps(event)})
    conn.expire(key, EVENT_STREAM_TTL)


def cancelled(conn: redis.Redis, job_id: str) -> bool:
    return conn.hget(state_key(job_id), "cancel_requested") == "1"


# ---------------------------------------------------------------------------
# ComfyUI HTTP API
# ---------------------------------------------------------------------------

def comfy_get(path: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(f"http://{COMFY_ADDR}{path}", timeout=timeout) as response:
        return json.loads(response.read())


def submit_prompt(workflow: dict) -> str:
    body = json.dumps({"prompt": workflow, "client_id": CLIENT_ID}).encode()
    request = urllib.request.Request(
        f"http://{COMFY_ADDR}/prompt",
        data=body,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["prompt_id"]

    except urllib.error.HTTPError as exc:
        # A 400 here is a malformed workflow, and ComfyUI puts the actual reason
        # in the body. Surfacing it is the difference between "failed" and
        # "failed: required input is missing: ckpt_name".
        detail = exc.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"ComfyUI rejected the workflow ({exc.code}): {detail}") from exc


def wait_for_comfy() -> None:
    """
    Poll until ComfyUI answers. Replaces the usual `time.sleep(5)`, which is
    both too long on a warm start and far too short on a cold one — first boot
    on a new node loads CUDA and scans custom nodes.
    """
    deadline = time.time() + BOOT_TIMEOUT

    while time.time() < deadline:
        try:
            comfy_get("/system_stats", timeout=5)
            log("ComfyUI is up")
            return

        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
            time.sleep(2)

    raise RuntimeError(f"ComfyUI did not answer on {COMFY_ADDR} within {BOOT_TIMEOUT}s")


# ---------------------------------------------------------------------------
# One job
# ---------------------------------------------------------------------------

def run_job(conn: redis.Redis, job_id: str, workflow: dict) -> None:
    conn.hset(state_key(job_id), mapping={"status": "running", "worker": WORKER_ID})
    conn.expire(state_key(job_id), EVENT_STREAM_TTL)
    emit(conn, job_id, {"type": "started", "data": {"worker": WORKER_ID}})

    # Connect first, submit second. See point 1 in the module docstring.
    ws = websocket.WebSocket()
    ws.connect(f"ws://{COMFY_ADDR}/ws?clientId={CLIENT_ID}", timeout=30)
    ws.settimeout(RECV_TIMEOUT)

    try:
        prompt_id = submit_prompt(workflow)
        emit(conn, job_id, {"type": "accepted", "data": {"prompt_id": prompt_id}})

        deadline = time.time() + JOB_TIMEOUT

        while True:
            if time.time() > deadline:
                raise TimeoutError(f"job exceeded {JOB_TIMEOUT}s")

            if cancelled(conn, job_id):
                interrupt()
                finish(conn, job_id, "cancelled", {})
                return

            try:
                raw = ws.recv()

            except websocket.WebSocketTimeoutException:
                # No news for RECV_TIMEOUT. Confirm ComfyUI is still alive and
                # that our prompt has not quietly completed while we were not
                # looking, then keep waiting.
                if prompt_finished(prompt_id):
                    finish(conn, job_id, "completed", collect_outputs(prompt_id))
                    return

                emit(conn, job_id, {"type": "waiting", "data": {"prompt_id": prompt_id}})
                continue

            # Binary frames are live preview images. Forwarding them through
            # Redis would work but multiplies traffic by the preview rate for
            # very little benefit, so they are dropped.
            if not isinstance(raw, str):
                continue

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            data = message.get("data") or {}

            # Point 2: one socket carries every prompt on this instance.
            if data.get("prompt_id") not in (None, prompt_id):
                continue

            emit(conn, job_id, message)

            kind = message.get("type")

            if kind == "execution_error":
                finish(conn, job_id, "failed", {"error": data.get("exception_message", "execution error")})
                return

            if kind == "execution_interrupted":
                finish(conn, job_id, "cancelled", {})
                return

            # Newer ComfyUI emits execution_success; older builds signal
            # completion with executing/node=None. Accept either.
            if kind == "execution_success" or (kind == "executing" and data.get("node") is None):
                finish(conn, job_id, "completed", collect_outputs(prompt_id))
                return

    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def prompt_finished(prompt_id: str) -> bool:
    try:
        return bool(comfy_get(f"/history/{prompt_id}").get(prompt_id))
    except Exception:  # noqa: BLE001
        return False


def collect_outputs(prompt_id: str) -> dict:
    """
    Pull the output manifest from /history. The WebSocket 'executed' events also
    carry it, but history is authoritative and survives a reconnect.
    """
    try:
        entry = comfy_get(f"/history/{prompt_id}").get(prompt_id, {})
    except Exception as exc:  # noqa: BLE001
        return {"warning": f"could not read history: {exc}"}

    images = []

    for node_output in (entry.get("outputs") or {}).values():
        for image in node_output.get("images", []):
            subfolder = image.get("subfolder") or ""
            images.append(
                {
                    "filename": image.get("filename"),
                    "subfolder": subfolder,
                    "type": image.get("type"),
                    "url": f"/outputs/{subfolder}/{image.get('filename')}".replace("//", "/"),
                }
            )

    return {"images": images}


def interrupt() -> None:
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"http://{COMFY_ADDR}/interrupt", data=b"{}"), timeout=10
        )
    except Exception:  # noqa: BLE001
        pass


def finish(conn: redis.Redis, job_id: str, status: str, payload: dict) -> None:
    conn.hset(state_key(job_id), mapping={"status": status})
    conn.expire(state_key(job_id), EVENT_STREAM_TTL)
    emit(conn, job_id, {"type": status, "data": payload})

    log(f"job {job_id} -> {status}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    wait_for_comfy()

    conn = connect_redis()
    conn.sadd("comfy:workers", WORKER_ID)

    log(f"worker {WORKER_ID} ready, polling {QUEUE_KEY}")

    try:
        while not shutting_down:
            # Short BRPOP timeout rather than blocking forever, so the SIGTERM
            # flag is noticed promptly instead of at the end of the grace period.
            item = conn.brpop(QUEUE_KEY, timeout=5)

            if item is None:
                continue

            _key, raw = item

            try:
                payload = json.loads(raw)
                job_id = payload["job_id"]
                workflow = payload["workflow"]

            except (json.JSONDecodeError, KeyError) as exc:
                log(f"discarding malformed queue entry: {exc}")
                continue

            log(f"picked up {job_id}")

            try:
                run_job(conn, job_id, workflow)

            except Exception as exc:  # noqa: BLE001 - every failure must reach the user
                log(f"job {job_id} failed: {exc}")

                try:
                    finish(conn, job_id, "failed", {"error": str(exc)})
                except Exception:  # noqa: BLE001
                    pass

    finally:
        try:
            conn.srem("comfy:workers", WORKER_ID)
        except Exception:  # noqa: BLE001
            pass

    log("exiting cleanly")

    return 0


if __name__ == "__main__":
    sys.exit(main())
