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
import json
import os
import pathlib
import uuid

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None

QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
EVENT_STREAM_TTL = int(os.environ.get("EVENT_STREAM_TTL", "3600"))
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "500"))

# The same EFS volume the workers write into, mounted read-only here. This is
# why the enterprise configuration requires STORAGE_MODE=rwx: two pods on two
# different nodes need the same filesystem, and a gp3 block volume cannot do
# that.
OUTPUT_ROOT = pathlib.Path(os.environ.get("OUTPUT_ROOT", "/output")).resolve()
STATIC_ROOT = pathlib.Path(__file__).parent / "static"

app = FastAPI(title="ComfyUI Gateway")

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
# Submitting work
# ---------------------------------------------------------------------------


@app.post("/api/generate")
async def generate(payload: dict):
    """
    Queue a workflow. Returns immediately with a job id — the actual work may
    not start for minutes if the GPU pool is scaled to zero and a node has to be
    provisioned first. That is expected; see docs/06-enterprise-architecture.md.
    """
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

    await conn.hset(
        state_key(job_id),
        mapping={"status": "queued", "queue_depth_at_submit": depth},
    )
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
    await client().hset(state_key(job_id), "cancel_requested", "1")

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

    try:
        while True:
            # BLOCK is in milliseconds. The timeout exists so we can send a ping
            # and notice a client that has gone away; without it a browser tab
            # closed mid-generation leaves this coroutine parked forever.
            entries = await conn.xread({key: last_id}, count=100, block=15_000)

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


@app.get("/api/stats")
async def stats():
    conn = client()

    return {
        "queue_depth": await conn.llen(QUEUE_KEY),
        "workers_registered": await conn.scard("comfy:workers"),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    page = STATIC_ROOT / "index.html"

    if not page.is_file():
        return HTMLResponse("<h1>ComfyUI Gateway</h1><p>API is up. See /docs.</p>")

    return HTMLResponse(page.read_text())
