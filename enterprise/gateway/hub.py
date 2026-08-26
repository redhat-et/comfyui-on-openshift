"""
Gateway: the CPU-side front door for the multi-user ComfyUI cluster.

Responsibilities, and nothing else:
  - accept a workflow, put it on a Redis queue, hand back a job id
  - stream that job's progress to the browser over a WebSocket
  - serve the finished images off the shared EFS volume

What it deliberately does NOT do: talk to a GPU worker directly. The workers
are unaddressable (they bind 127.0.0.1 only). Redis is the entire interface,
which is what lets the worker pool scale between zero and N without any
connection state to fix up.

Design note — why Redis Streams rather than pub/sub:

Pub/sub is fire-and-forget. A browser that connects to /ws/<job> a beat after
the worker started publishing has already missed those messages, and a browser
that reconnects after a dropped connection has missed everything. Both are the
common case, not the edge case: the POST and the WebSocket open are two separate
round trips from the client.

A Stream is an append-only log with an id per entry, so XREAD from "0-0" replays
everything that already happened and then blocks for what comes next — replay
and live tail in one primitive, with no window where an event can fall between
them. Streams are given a TTL so finished jobs do not accumulate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import uuid

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None

QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
EVENT_STREAM_TTL = int(os.environ.get("EVENT_STREAM_TTL", "3600"))
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "500"))

# Real API-format workflows are tens of KB. The cap exists because uvicorn
# imposes no body limit of its own, the whole body is buffered in memory and
# then stored in Redis, and with AUTH_MODE=none this endpoint is public.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(2 * 1024 * 1024)))

# Workers announce themselves with a heartbeat key (TTL-based, so a SIGKILLed
# worker disappears from the count on its own) and park the job they are
# running in a per-worker processing list. The reaper below fails jobs whose
# worker's heartbeat is gone — without it, a worker OOM-kill or node death
# leaves the browser on a progress bar that never moves.
WORKER_KEY_PREFIX = "comfy:worker:"
PROCESSING_KEY_PREFIX = "comfy:processing:"
REAPER_INTERVAL = int(os.environ.get("REAPER_INTERVAL", "30"))

# The same EFS volume the workers write into, mounted read-only here. This is
# why the enterprise configuration requires STORAGE_MODE=rwx: two pods on two
# different nodes need the same filesystem, and a gp3 block volume cannot do
# that.
OUTPUT_ROOT = pathlib.Path(os.environ.get("OUTPUT_ROOT", "/output")).resolve()
STATIC_ROOT = pathlib.Path(__file__).parent / "static"

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # One reaper per gateway replica is fine: RPOP is atomic, so two reapers
    # racing over the same processing list each fail different entries.
    reaper = asyncio.create_task(reap_orphaned_jobs())
    yield
    reaper.cancel()


app = FastAPI(title="ComfyUI Gateway", lifespan=lifespan)

_redis: redis.Redis | None = None


def client() -> redis.Redis:
    global _redis

    if _redis is None:
        _redis = redis.from_url(
            REDIS_URL,
            password=REDIS_PASSWORD,
            decode_responses=True,
            health_check_interval=30,
        )

    return _redis


def stream_key(job_id: str) -> str:
    return f"comfy:job:{job_id}:events"


def state_key(job_id: str) -> str:
    return f"comfy:job:{job_id}:state"


# ---------------------------------------------------------------------------
# Reaping jobs whose worker died
#
# A worker moves each job from the queue into its own processing list before
# running it (BLMOVE in worker_agent.py) and removes it on any terminal state.
# A worker that is SIGKILLed — OOM, node death — removes nothing, and its
# heartbeat key expires. This task notices that and fails the stranded jobs
# loudly, which is the difference between "failed: worker died" and a browser
# waiting on a bar that never moves until the stream TTL runs out.
#
# Deliberately fail, not requeue: a workflow that OOM-killed one worker would
# OOM-kill every worker it is requeued onto, in sequence, at $0.80/hour each.
# ---------------------------------------------------------------------------


async def fail_orphaned_job(conn: redis.Redis, raw: str) -> None:
    try:
        job_id = json.loads(raw)["job_id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return

    await conn.hset(state_key(job_id), mapping={"status": "failed"})
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL)

    await conn.xadd(
        stream_key(job_id),
        {"data": json.dumps({"type": "failed", "data": {"error": "the worker running this job died without reporting back"}})},
    )
    await conn.expire(stream_key(job_id), EVENT_STREAM_TTL)


async def reap_orphaned_jobs() -> None:
    while True:
        try:
            conn = client()

            async for key in conn.scan_iter(match=f"{PROCESSING_KEY_PREFIX}*"):
                worker_id = key[len(PROCESSING_KEY_PREFIX):]

                if await conn.exists(f"{WORKER_KEY_PREFIX}{worker_id}"):
                    continue

                while (raw := await conn.rpop(key)) is not None:
                    await fail_orphaned_job(conn, raw)

        except Exception:  # noqa: BLE001 - a Redis blip; readiness reports it, next tick retries
            pass

        await asyncio.sleep(REAPER_INTERVAL)


# ---------------------------------------------------------------------------
# Submitting work
# ---------------------------------------------------------------------------


@app.post("/api/generate")
async def generate(request: Request):
    """
    Queue a workflow. Returns immediately with a job id — the actual work may
    not start for minutes if the GPU pool is scaled to zero and a node has to be
    provisioned first. That is expected; see docs/06-enterprise-architecture.md.

    Takes the raw Request rather than a `payload: dict` parameter so the body
    size can be capped while it streams in — by the time FastAPI hands over a
    parsed dict, an oversized body has already been buffered whole.
    """
    declared = request.headers.get("content-length", "")

    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(413, f"workflow too large (limit {MAX_BODY_BYTES} bytes)")

    body = bytearray()

    async for chunk in request.stream():
        body.extend(chunk)

        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, f"workflow too large (limit {MAX_BODY_BYTES} bytes)")

    try:
        payload = json.loads(bytes(body))
    except json.JSONDecodeError:
        raise HTTPException(400, "body must be JSON") from None

    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a ComfyUI workflow object, or {\"workflow\": {...}}")

    workflow = payload.get("workflow", payload)

    if not isinstance(workflow, dict) or not workflow:
        raise HTTPException(400, "body must be a ComfyUI workflow object, or {\"workflow\": {...}}")

    conn = client()

    # Backpressure. Without this a stuck worker pool turns into an unbounded
    # Redis list, and the first symptom is Redis OOM rather than a slow queue.
    depth = await conn.llen(QUEUE_KEY)

    if depth >= MAX_QUEUE_DEPTH:
        raise HTTPException(503, f"queue is full ({depth} jobs). Try again shortly.")

    job_id = str(uuid.uuid4())

    # Who spent the GPU. oauth-proxy sets X-Forwarded-User for the
    # authenticated user (its pass-user-headers default); recording it answers
    # "whose job is this?" without building per-user anything. With
    # AUTH_MODE=none there is no proxy and the header is client-supplied —
    # informational at best, so never treat it as authorization.
    user = request.headers.get("x-forwarded-user", "")

    state = {"status": "queued", "queue_depth_at_submit": depth}

    if user:
        state["user"] = user

    await conn.hset(state_key(job_id), mapping=state)
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL)

    # Seed the stream so a browser that opens the WebSocket before any worker
    # picks the job up sees "queued" instead of an empty blocking read.
    await conn.xadd(stream_key(job_id), {"data": json.dumps({"type": "queued", "data": {"position": depth}})})
    await conn.expire(stream_key(job_id), EVENT_STREAM_TTL)

    await conn.lpush(QUEUE_KEY, json.dumps({"job_id": job_id, "workflow": workflow}))

    return {"job_id": job_id, "status": "queued", "queue_depth": depth}


@app.get("/api/jobs/{job_id}")
async def job_state(job_id: str):
    state = await client().hgetall(state_key(job_id))

    if not state:
        raise HTTPException(404, "unknown or expired job")

    return state


@app.post("/api/jobs/{job_id}/cancel")
async def cancel(job_id: str):
    """
    Cooperative cancel. Sets a flag the worker checks between events; a job that
    has not been picked up yet never starts. This does not interrupt a sampler
    mid-step — ComfyUI's own /interrupt would, but the workers are not
    reachable from here by design.
    """
    conn = client()

    # 404 unknown ids rather than HSET-ing them into existence: a fresh hash
    # created here would have no TTL, so cancelling made-up ids would grow
    # Redis one permanent key per request.
    if not await conn.exists(state_key(job_id)):
        raise HTTPException(404, "unknown or expired job")

    await conn.hset(state_key(job_id), "cancel_requested", "1")

    # Re-arm the TTL: if the key expired between the check and the write, the
    # HSET recreated it with no TTL at all.
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL)

    return {"job_id": job_id, "cancel_requested": True}


# ---------------------------------------------------------------------------
# Streaming progress
# ---------------------------------------------------------------------------

TERMINAL_TYPES = {"completed", "failed", "cancelled"}


@app.websocket("/ws/{job_id}")
async def progress(websocket: WebSocket, job_id: str):
    await websocket.accept()

    conn = client()
    key = stream_key(job_id)
    last_id = "0-0"
    redis_errors = 0

    try:
        while True:
            # BLOCK is in milliseconds. The timeout exists so we can send a ping
            # and notice a client that has gone away; without it a browser tab
            # closed mid-generation leaves this coroutine parked forever.
            try:
                entries = await conn.xread({key: last_id}, count=100, block=15_000)

            except redis.RedisError:
                # A Redis blip is not a job failure — the worker may be running
                # the job perfectly well. Do NOT fall through to the generic
                # handler below, which would tell the browser "failed". Retry;
                # last_id is preserved, so nothing is lost. If Redis stays gone,
                # close the socket and let the client reconnect and replay.
                redis_errors += 1

                if redis_errors > 20:
                    return

                await asyncio.sleep(1)
                continue

            redis_errors = 0

            if not entries:
                await websocket.send_json({"type": "ping"})
                continue

            for _stream, records in entries:
                for entry_id, fields in records:
                    last_id = entry_id

                    try:
                        event = json.loads(fields["data"])
                    except (KeyError, json.JSONDecodeError):
                        continue

                    await websocket.send_json(rewrite_image_urls(event))

                    if event.get("type") in TERMINAL_TYPES:
                        return

    except WebSocketDisconnect:
        return

    except Exception as exc:  # noqa: BLE001 - the socket is the only place to report
        try:
            await websocket.send_json({"type": "failed", "data": {"error": str(exc)}})
        except Exception:  # noqa: BLE001
            pass

    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


def rewrite_image_urls(event: dict) -> dict:
    """
    ComfyUI reports outputs as {filename, subfolder, type} relative to its own
    output directory. The browser cannot reach the worker, so turn each one into
    a URL this gateway serves off the shared volume.
    """
    images = event.get("data", {}).get("output", {}).get("images")

    if not images:
        return event

    for image in images:
        if not isinstance(image, dict) or "filename" not in image:
            continue

        subfolder = image.get("subfolder") or ""
        image["url"] = f"/outputs/{subfolder}/{image['filename']}".replace("//", "/")

    return event


# ---------------------------------------------------------------------------
# Serving results
# ---------------------------------------------------------------------------


@app.get("/outputs/{path:path}")
async def output_file(path: str):
    """
    Serve a generated image from the shared volume.

    The resolve()-and-compare below is the only thing standing between this
    endpoint and an arbitrary file read: a path like ../../etc/passwd resolves
    outside OUTPUT_ROOT and is rejected. Do not "simplify" it into a join.
    """
    candidate = (OUTPUT_ROOT / path).resolve()

    if not candidate.is_relative_to(OUTPUT_ROOT):
        raise HTTPException(403, "path escapes the output directory")

    if not candidate.is_file():
        raise HTTPException(404, "no such output")

    return FileResponse(candidate)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    """Liveness: the process is up. Deliberately does not touch Redis — a Redis
    blip should not cause every gateway pod to be restarted."""
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    """Readiness: we can actually serve, which means Redis answers."""
    try:
        await client().ping()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    return {"ok": True}


async def gather_stats() -> dict:
    conn = client()

    # Count heartbeat keys, not a set: keys expire on their own, so a worker
    # that was SIGKILLed stops being counted instead of inflating this forever.
    workers = 0

    async for _ in conn.scan_iter(match=f"{WORKER_KEY_PREFIX}*"):
        workers += 1

    return {
        "queue_depth": await conn.llen(QUEUE_KEY),
        "workers_registered": workers,
    }


@app.get("/api/stats")
async def stats():
    return await gather_stats()


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    """
    Prometheus text format, hand-rolled — two gauges do not justify a client
    library dependency. OpenShift's user-workload monitoring scrapes this via
    the ServiceMonitor enterprise/setup.sh applies, which is what makes
    "queue deeper than N for 30 minutes" an alert instead of a support ticket.
    """
    data = await gather_stats()

    return (
        "# HELP comfy_queue_depth Jobs waiting in the Redis queue.\n"
        "# TYPE comfy_queue_depth gauge\n"
        f"comfy_queue_depth {data['queue_depth']}\n"
        "# HELP comfy_workers_registered Workers with a live heartbeat.\n"
        "# TYPE comfy_workers_registered gauge\n"
        f"comfy_workers_registered {data['workers_registered']}\n"
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    page = STATIC_ROOT / "index.html"

    if not page.is_file():
        return HTMLResponse("<h1>ComfyUI Gateway</h1><p>API is up. See /docs.</p>")

    return HTMLResponse(page.read_text())
