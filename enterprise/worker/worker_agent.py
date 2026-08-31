"""
Worker agent: the sidecar-in-the-same-container that connects a locked-down
ComfyUI process to the Redis queue.

Runs alongside ComfyUI in the GPU pod. ComfyUI binds 127.0.0.1 only, so this
agent is the sole path in or out — no user can reach the GPU pod directly, and
the pod needs no Service, no Route, and no ingress rules.

Six things here that the obvious version of this script gets wrong, each of
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

  5. Move jobs, don't pop them, and heartbeat while running. SIGTERM is the
     polite case; SIGKILL (an OOM, a node reclaim) gives no warning at all.
     BLMOVE parks the in-flight job in a per-worker processing list and a
     TTL'd heartbeat key marks this worker alive; when the heartbeat lapses,
     the gateway's reaper acts on the stranded job loudly instead of letting
     it disappear.

  6. Write down HOW FAR the job got, before doing the thing. The reaper above
     can see that a worker died and cannot see what killed it, so what it is
     allowed to do about a stranded job depends entirely on the `phase`
     breadcrumb this agent leaves on the job's state hash: PHASE_DISPATCHED
     while ComfyUI has not been handed the workflow (retryable — nothing ran),
     PHASE_EXECUTING once it has (terminal — a workflow that OOM-killed one
     worker would OOM-kill the next one too). The breadcrumb is a promise about
     the past, so each write must happen BEFORE the transition it describes is
     observable: written after, there is a window in which the job is executing
     and the record says it is not, and that window is exactly when a retry
     replays a poison workflow. The queue entry cannot carry this — it is a
     static copy of what hub.py pushed and nothing rewrites it.
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

# The heartbeat is how the gateway distinguishes a busy worker from a dead one.
# It is refreshed on every pass through both the polling loop and the per-job
# event loop, so the TTL must comfortably exceed RECV_TIMEOUT — the longest
# this process legitimately goes without touching Redis.
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "180"))

WORKER_KEY = f"comfy:worker:{WORKER_ID}"

# The job currently being executed lives in this list (moved there from the
# queue by BLMOVE, removed on any terminal state). If this process dies without
# removing it, the gateway's reaper fails the job loudly instead of letting it
# vanish. Key shape is shared with hub.py — change both or neither.
PROCESSING_KEY = f"comfy:processing:{WORKER_ID}"


# ---------------------------------------------------------------------------
# BEGIN SHARED ENVELOPE — the queue payload contract (docs/10-roadmap.md, F2)
#
# One entry on the queue is one JSON object of this shape:
#
#     {"schema_version": 1,
#      "job_id":        "<uuid4>",
#      "workflow":      {...},                            # ComfyUI API format
#      "queue_key":     "",                               # reserved for Q1
#      "attempt":       {"count": 0, "phase": "queued"},   # Q2: retry + phase
#      "user":          "",                               # reserved for Q4
#      "submitted_at":  1756400000.0}                     # reserved for Q6
#
# This block is MIRRORED VERBATIM in enterprise/gateway/hub.py and
# enterprise/worker/worker_agent.py — change both or neither, the same rule the
# processing-list key shape already follows. There is no third file to import
# from: enterprise/setup.sh builds the two images from two different build
# contexts (enterprise/gateway and enterprise/worker), so a shared module would
# have to be copied into both of them anyway. scripts/lint.sh compares the two
# copies line for line, which is what makes "change both or neither" a check
# rather than a hope. Both directions are defined in both files even though
# each file uses only one of them: a block that is byte-identical is one a
# reviewer can diff, and one half of a contract is not a contract.
#
# Why the fields are here before anything reads them. Four roadmap items each
# want to add a key to a payload these two files must agree on: Q1 a
# fair-queueing lane key, Q2 an attempt count and a phase breadcrumb, Q4
# attribution, Q6 a submit timestamp. Adding them one item at a time opens four
# separate windows in which a gateway that writes a key runs beside a worker
# that does not, or the reverse — and this pool scales to zero and rolls
# constantly, so both halves of a rollout are live at once by construction.
# Each field therefore exists NOW: it has a default, it round-trips through
# Redis, and nothing reads it. The item that owns a field changes what reads
# it, not the wire format.
#
# schema_version is bumped only for a change that is NOT backwards compatible —
# a field removed, or one whose meaning changed under a name that stayed the
# same. Giving a reserved field its behaviour is neither, and must not bump it.
#
# Tolerant in both directions, for that same rolling-deploy reason:
#
#   - No schema_version at all is the pre-F2 shape, {"job_id", "workflow"}. It
#     is version 1 by definition and every reserved field takes its default, so
#     a queue entry written before the rollout still runs afterwards.
#   - A key this side has never heard of is carried, never rejected: it came
#     from a newer peer, and refusing it would strand exactly the work the
#     versioning exists to protect. The consumer reads the keys it knows.
#
# Nothing unbounded goes in here. The workflow is already tens of kilobytes,
# which is why four scalars are free — and why a fifth field that grows with
# use would not be. queue_key and user are clamped on the way in: `user` comes
# from a request header, and under AUTH_MODE=none that header is client-
# supplied, so its length is not ours to trust.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

# What a payload that declares no version is taken to be. 1 rather than 0,
# because the pre-F2 two-key shape is a valid version-1 envelope with every
# reserved field defaulted — not a broken one.
IMPLICIT_SCHEMA_VERSION = 1

# Long enough for any real username or lane name; short enough that a header
# nobody authenticated cannot make a queue entry interesting.
MAX_ENVELOPE_FIELD_CHARS = 256

# The phase vocabulary (docs/10-roadmap.md, Q2). Three values, and the only
# distinction any of them exists to draw is "has ComfyUI been handed this
# workflow yet?" — because that is the one question that decides whether a
# worker's death may be retried.
#
#   queued      nobody has picked it up. hub.py writes this at submit.
#   dispatched  a worker has it and has written its name on the job, but
#               ComfyUI has not answered. The agent is inside submit_prompt().
#   executing   ComfyUI accepted the prompt. From here the workflow has run,
#               at least partly, on a GPU.
#
# These are shared vocabulary rather than gateway-private constants because
# BOTH files use them and must use them identically: worker_agent.py writes the
# breadcrumb, hub.py's reaper reads it and decides a job's fate from it, and a
# rolling deploy always has one vintage of each running at once. A worker that
# wrote "submitted" against a gateway that reads "executing" would silently
# make every mid-generation death retryable — the exact poison-pill replay the
# narrowing exists to prevent — with nothing failing anywhere to say so.
PHASE_QUEUED = "queued"
PHASE_DISPATCHED = "dispatched"
PHASE_EXECUTING = "executing"

# The phases in which ComfyUI provably has not started on this workflow, so a
# worker that died here can be requeued without replaying anything onto a
# second GPU. Everything else — including a phase this side does not recognise
# and a state hash that has expired — is not retryable, which is the safe
# direction: the cost of not retrying is one user resubmitting, the cost of
# retrying wrongly is a poison workflow walking the whole pool.
RETRYABLE_PHASES = frozenset({PHASE_QUEUED, PHASE_DISPATCHED})

# Where the breadcrumb and the attempt counter live on the job's own state
# hash, beside status/worker/schema_version. The queue entry cannot carry the
# live phase: it is a static copy of what hub.py pushed, and the worker never
# rewrites it, so by the time the reaper is looking at a stranded entry the
# only thing that knows how far the job got is the state hash.
PHASE_FIELD = "phase"
ATTEMPT_COUNT_FIELD = "attempt_count"


def envelope_text(value) -> str:
    """A reserved string field: always a str, always bounded, never None."""
    return str(value)[:MAX_ENVELOPE_FIELD_CHARS] if value else ""


def new_attempt() -> dict:
    """A fresh, not-yet-tried breadcrumb. Q2 owns everything that moves it."""
    return {"count": 0, "phase": PHASE_QUEUED}


def attempt_count_of(envelope: dict) -> int:
    """
    How many times this entry has already been requeued, per the entry itself.

    Advisory only, and deliberately so: the queue entry is a copy, and two
    gateway replicas both holding a copy is exactly the case this number must
    not be trusted to bound. The authority is the atomic counter on the job's
    state hash (hub.py's reaper, HINCRBY); this is what an operator reads off
    a raw queue entry and what the worker logs.
    """
    count = (envelope.get("attempt") or {}).get("count", 0)

    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def build_envelope(job_id: str, workflow: dict, *, queue_key: str = "",
                   user: str = "", attempt: dict | None = None,
                   submitted_at: float | None = None) -> dict:
    """
    The producer side. Every reserved field is written with its default rather
    than omitted, so no consumer ever has to ask whether a key is present —
    which is the whole reason the absent-field case has only one shape (a
    pre-F2 payload) instead of one per item.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "workflow": workflow,
        "queue_key": envelope_text(queue_key),
        "attempt": dict(attempt) if isinstance(attempt, dict) else new_attempt(),
        "user": envelope_text(user),
        "submitted_at": time.time() if submitted_at is None else submitted_at,
    }


def parse_envelope(payload: dict) -> dict:
    """
    The consumer side, and the tolerant half of the contract.

    Raises KeyError for a payload carrying no job_id or no workflow — that is a
    malformed entry rather than an old one, and the caller drops it. Everything
    else is defaulted, so the caller reads the same keys whichever version
    arrived, and an unrecognised key is simply not one of the keys read.
    """
    if not isinstance(payload, dict):
        raise TypeError("queue entry is not a JSON object")

    version = payload.get("schema_version", IMPLICIT_SCHEMA_VERSION)

    if isinstance(version, bool) or not isinstance(version, int):
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = IMPLICIT_SCHEMA_VERSION

    attempt = payload.get("attempt")

    return {
        "schema_version": version,
        "job_id": payload["job_id"],
        "workflow": payload["workflow"],
        "queue_key": envelope_text(payload.get("queue_key")),
        "attempt": dict(attempt) if isinstance(attempt, dict) else new_attempt(),
        "user": envelope_text(payload.get("user")),
        "submitted_at": payload.get("submitted_at"),
    }

# END SHARED ENVELOPE
# ---------------------------------------------------------------------------


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


def heartbeat(conn: redis.Redis) -> None:
    """Tell the gateway this worker is alive. Expiry does the deregistering."""
    try:
        conn.set(WORKER_KEY, str(int(time.time())), ex=HEARTBEAT_TTL)
    except redis.RedisError:
        pass  # transient; the next refresh re-arms it well within the TTL


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

def run_job(conn: redis.Redis, job: dict) -> None:
    job_id = job["job_id"]
    workflow = job["workflow"]

    conn.hset(
        state_key(job_id),
        mapping={
            "status": "running",
            "worker": WORKER_ID,
            # Which envelope version this worker actually parsed, recorded
            # where an operator can read it. Mid rolling-deploy a job that
            # says 1 came either from a gateway older than F2 or from a queue
            # entry written before the rollout — which is the difference
            # between "the tolerant path worked" and "nobody looked".
            "schema_version": job["schema_version"],
            # Point 6: this job is now mine, and ComfyUI has not been handed
            # the workflow. Written in the SAME HSET as status/worker, not a
            # second round trip after it — the gateway's reaper reads status
            # and phase together, and a job that looked running with no phase
            # would be read as "how far it got is unknown" and refused a retry
            # it had earned.
            PHASE_FIELD: PHASE_DISPATCHED,
        },
    )
    conn.expire(state_key(job_id), EVENT_STREAM_TTL)
    emit(conn, job_id, {"type": "started", "data": {"worker": WORKER_ID}})

    # Connect first, submit second. See point 1 in the module docstring.
    ws = websocket.WebSocket()
    ws.connect(f"ws://{COMFY_ADDR}/ws?clientId={CLIENT_ID}", timeout=30)
    ws.settimeout(RECV_TIMEOUT)

    try:
        prompt_id = submit_prompt(workflow)

        # Point 6, the other half: ComfyUI has the workflow. Written BEFORE the
        # `accepted` event, because this is the flag that stops a death here
        # from being retried and the event is only a thing to look at. From
        # here on, every death this job suffers is terminal.
        conn.hset(state_key(job_id), PHASE_FIELD, PHASE_EXECUTING)
        conn.expire(state_key(job_id), EVENT_STREAM_TTL)

        emit(conn, job_id, {"type": "accepted", "data": {"prompt_id": prompt_id}})

        deadline = time.time() + JOB_TIMEOUT

        while True:
            # Refreshed here as well as in the polling loop: a generation can
            # legally run for JOB_TIMEOUT seconds, which is far longer than the
            # heartbeat TTL. Each pass is bounded by RECV_TIMEOUT, which is not.
            heartbeat(conn)

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
    heartbeat(conn)

    log(f"worker {WORKER_ID} ready, polling {QUEUE_KEY}")

    try:
        while not shutting_down:
            heartbeat(conn)

            # Move, don't pop. BLMOVE parks the job in this worker's processing
            # list, so if this process dies mid-job the gateway's reaper can
            # find and fail it. A plain BRPOP would make the job cease to exist
            # anywhere the moment it was picked up.
            #
            # Short timeout rather than blocking forever, so the SIGTERM flag
            # is noticed promptly instead of at the end of the grace period.
            raw = conn.blmove(QUEUE_KEY, PROCESSING_KEY, timeout=5, src="RIGHT", dest="LEFT")

            if raw is None:
                continue

            try:
                try:
                    # Tolerant by contract: a pre-F2 {"job_id", "workflow"}
                    # entry parses as version 1 with every reserved field
                    # defaulted, and a key from a newer gateway is carried and
                    # not read. Only a missing job_id or workflow is malformed.
                    job = parse_envelope(json.loads(raw))
                    job_id = job["job_id"]

                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    log(f"discarding malformed queue entry: {exc}")
                    continue

                retries = attempt_count_of(job)
                log(f"picked up {job_id} (envelope v{job['schema_version']}"
                    + (f", retry {retries}" if retries else "") + ")")

                try:
                    run_job(conn, job)

                except Exception as exc:  # noqa: BLE001 - every failure must reach the user
                    log(f"job {job_id} failed: {exc}")

                    try:
                        finish(conn, job_id, "failed", {"error": str(exc)})
                    except Exception:  # noqa: BLE001
                        pass

            finally:
                # The job reached a terminal state (or was malformed); take it
                # out of the processing list so the reaper never touches it.
                try:
                    conn.lrem(PROCESSING_KEY, 1, raw)
                except Exception:  # noqa: BLE001
                    pass

    finally:
        try:
            conn.delete(WORKER_KEY)
        except Exception:  # noqa: BLE001
            pass

    log("exiting cleanly")

    return 0


if __name__ == "__main__":
    sys.exit(main())
