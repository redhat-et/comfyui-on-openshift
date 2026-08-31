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
import time
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

# How many times the reaper may put a job back on the queue (docs/10-roadmap.md,
# Q2) — and only ever a job that died before ComfyUI saw the workflow, see
# RETRYABLE_PHASES. One, not three: the deaths this retries (a node reclaim or
# an evicted pod in the seconds between BLMOVE and ComfyUI's acceptance) are
# uncorrelated with the workflow, so a second attempt that dies the same way
# again is evidence about the cluster rather than about the job, and a user
# staring at a bar that never moves is worse than a failure they can read. It
# is an env var so cluster day can raise it on a pool that is provably
# thrashing, not because anything here wants it raised.
MAX_JOB_RETRIES = int(os.environ.get("MAX_JOB_RETRIES", "1"))

# The same EFS volume the workers write into, mounted read-only here. This is
# why the enterprise configuration requires STORAGE_MODE=rwx: two pods on two
# different nodes need the same filesystem, and a gp3 block volume cannot do
# that.
OUTPUT_ROOT = pathlib.Path(os.environ.get("OUTPUT_ROOT", "/output")).resolve()
STATIC_ROOT = pathlib.Path(__file__).parent / "static"


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


# ---------------------------------------------------------------------------
# BEGIN FAIR QUEUEING (docs/10-roadmap.md, Q1)
#
# The problem: comfy:queue is one Redis list, and one submitter's batch of 200
# jobs is served start to finish before anything behind it, so a second
# submitter's single job waits out the whole batch. docs/06's roadmap
# deliberately rejected a priority lane for this — a priority claim has to
# come from the caller, and the same X-Forwarded-User header this endpoint
# already reads is client-supplied and unauthenticated whenever AUTH_MODE=none,
# so "I'm interactive" would just be a header everyone sets on themselves.
#
# Round-robin needs no such claim: every submitter identity hub.py already
# records (queue_key, mirroring the same value as the reserved `user` field —
# see build_envelope() below) is a lane, each lane's Nth queued job is served
# in round N-1, and rounds are served lowest-first. Impersonating a real
# submitter only shares that submitter's slots, not a new one, and a caller
# who sends no identity at all (the common AUTH_MODE=none case, no proxy, no
# header) shares the single lane "" with every other anonymous caller — which
# makes every job its own round in arrival order, i.e. today's plain FIFO.
# That is fairness degrading to FIFO exactly where there is nothing to be
# fair about, not a special case in the code.
#
# What does NOT change: the physical queue is still the one Redis list
# comfy:queue, still bounded by MAX_QUEUE_DEPTH, still popped by the same
# BLMOVE src=RIGHT in worker_agent.py's main loop into the same per-worker
# processing list the reaper depends on. Fairness is entirely a question of
# WHERE in that one list a new job is inserted, computed by the Lua script
# below and run atomically (one EVAL) so a job push can never interleave with
# another push or with the worker's blocking pop mid-reorder. Because it is
# still one list of the same shape, KEDA's `listName: comfy:queue` trigger in
# enterprise/manifests/03-autoscale.yaml needs no change — see the comment
# there for what cluster day must still confirm.
#
# Physical layout is unchanged from before Q1: index 0 is the most recently
# queued job, the tail (index -1) is served next. A plain LPUSH always landed
# a new job at index 0 — "join the back of the line" — which is a special
# case of the general rule below (every lane empty, at the same identical
# round 0, breaks ties by arrival, so it appends). Round numbers are
# recomputed from the queue's own current contents on every insert rather than
# kept in a persistent per-lane counter: a persistent counter would keep
# counting a lane's already-*served* jobs too, so a submitter whose earlier
# jobs already ran would wrongly look deep into round N for everything after.
# ---------------------------------------------------------------------------

FAIR_ENQUEUE_LUA = """
local key = KEYS[1]
local new_entry = ARGV[1]
local new_lane = ARGV[2] or ""

local raw = redis.call('LRANGE', key, 0, -1)
local n = #raw

-- front[1] is served soonest (the physical tail), front[n] is served last
-- (the physical head) -- i.e. front is `raw` reversed into service order.
local front = {}
for i = n, 1, -1 do
  front[#front + 1] = raw[i]
end

-- Round of each existing job: the count of that job's own lane already seen
-- earlier in service order. Non-decreasing front-to-back by construction,
-- since every prior insert placed things the same way.
local round_of, lane_count = {}, {}
for i = 1, n do
  local lane = ""
  local ok, obj = pcall(cjson.decode, front[i])
  if ok and type(obj) == 'table' and type(obj.queue_key) == 'string' then
    lane = obj.queue_key
  end
  local c = lane_count[lane] or 0
  round_of[i] = c
  lane_count[lane] = c + 1
end

local new_round = lane_count[new_lane] or 0

-- The new job goes immediately ahead of the first existing job whose round is
-- strictly later than its own -- i.e. at the back of its own round's group,
-- ahead of every later round. No later round exists: it joins the very back.
local insert_at = n + 1
for i = 1, n do
  if round_of[i] > new_round then
    insert_at = i
    break
  end
end

local result = {}
for i = 1, insert_at - 1 do result[#result + 1] = front[i] end
result[#result + 1] = new_entry
for i = insert_at, n do result[#result + 1] = front[i] end

redis.call('DEL', key)

-- Write back in physical order (head first, i.e. `result` reversed), in
-- batches so a very deep queue never approaches Lua's per-call argument
-- limit.
local batch = {}
for i = #result, 1, -1 do
  batch[#batch + 1] = result[i]
  if #batch == 200 then
    redis.call('RPUSH', key, unpack(batch))
    batch = {}
  end
end
if #batch > 0 then
  redis.call('RPUSH', key, unpack(batch))
end

-- {jobs queued before this one (any lane), jobs that will be served before
-- this one under fair-queueing order}
return {n, insert_at - 1}
"""

_fair_enqueue_script = None


def fair_enqueue_script():
    """Lazily registered against the shared connection. register_script()
    only computes a SHA1 client-side — no I/O — so this is cheap to call on
    every request; it is cached anyway so there is exactly one Script object
    per process."""
    global _fair_enqueue_script

    if _fair_enqueue_script is None:
        _fair_enqueue_script = client().register_script(FAIR_ENQUEUE_LUA)

    return _fair_enqueue_script

# END FAIR QUEUEING
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # One reaper per gateway replica is fine, but for two reasons rather than
    # one. RPOP is atomic, so two reapers racing over the same processing list
    # each take different entries — which is the whole argument for failing a
    # job at most once. It is NOT the argument for requeueing one at most once:
    # a requeued job can be stranded again, on another worker, and be seen by
    # the other replica's reaper. That bound comes from the atomic HINCRBY in
    # reap_stranded_job() below, not from this.
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
# heartbeat key expires. This task notices that and acts on the stranded jobs
# loudly, which is the difference between "failed: worker died" and a browser
# waiting on a bar that never moves until the stream TTL runs out.
#
# WHAT it does depends on how far the job had got, and on nothing else
# (docs/10-roadmap.md, Q2). The rule is one line: retry only a job whose worker
# died before ComfyUI was ever handed the workflow.
#
#   - Retryable is a PHASE, never an exception. A ComfyUI 400, an
#     execution_error (which is how a VRAM OOM arrives), and a job deadline are
#     all reported by an agent that is alive and well; none of them reaches this
#     code at all, and none of them is retried by anything, anywhere. They are
#     terminal because the workflow itself is what failed, and a second GPU-hour
#     spent on it fails identically.
#   - A death at PHASE_EXECUTING stays exactly what it always was: one terminal
#     `failed` naming the dead worker. A host-RAM OOM kills the pod and is
#     indistinguishable *at the queue layer* from a node reclaim, so requeueing
#     it would walk a poison workflow across the whole pool at GPU prices. That
#     ambiguity is total only here: since F1 sized the pod Guaranteed and inside
#     what the node can actually give it, a host-RAM OOM now terminates the pod
#     OOMKilled and `oc describe pod` names the reason — which is why the
#     failure text below points the operator at it rather than shrugging.
#   - A death at PHASE_QUEUED or PHASE_DISPATCHED is requeued, at most
#     MAX_JOB_RETRIES times. Nothing ran, so there is nothing to replay.
#
# Why the counter is HINCRBY and not read-then-write. "RPOP is atomic, so two
# reapers each fail different entries" is the argument that makes fail-once
# safe, and it does NOT carry over to requeue-once. A requeued job goes back on
# comfy:queue, is picked up by another worker, and can be stranded a second
# time — a different entry, on a different processing list, seen on a different
# pass, quite possibly by the OTHER gateway replica (01-gateway.yaml runs two,
# each with its own reaper). "Is this the first attempt?" is then a question
# about shared state rather than about who won a pop, and the obvious
# implementation — HGET the count, compare, HSET count+1 — is a lost update
# between the two replicas: both read 0, both requeue, and one job becomes two
# jobs on one GPU pool. HINCRBY returns the value AFTER incrementing, so the
# decision is taken from the return of the atomic operation itself and exactly
# one caller can ever see 1. This cannot be exhibited by enterprise/test/run.sh,
# which starts one gateway; it is correct by construction instead, and
# scripts/lint.sh pins the shape so a later read-modify-write cannot creep back.
# ---------------------------------------------------------------------------

# The one event the reaper publishes that is NOT terminal. progress() below
# stops reading at the first event whose type is in TERMINAL_TYPES, so putting
# "retry" in that set would end every tailing browser's stream on the retry
# itself and send the second attempt's progress to nobody — the precise bug a
# retry is supposed to hide from the user. scripts/lint.sh pins both halves.
RETRY_TYPE = "retry"

DEAD_WORKER = "the worker running this job died without reporting back"

# What the operator can do that the gateway cannot. The gateway sees a lapsed
# heartbeat and nothing else — but the pod is not ambiguous, because F1 sized
# the worker Guaranteed and within what the node can hand one pod, so a
# host-RAM OOM terminates it OOMKilled instead of disappearing into node
# pressure. Every failure text below carries this, because the person reading
# it is the person who can answer the question this code cannot.
DESCRIBE_HINT = ("`oc describe pod` on that worker names the reason it went — "
                 "OOMKilled is a host-RAM OOM, and a node reclaim says so too")


async def arm_state_ttl(conn: redis.Redis, job_id: str) -> None:
    """
    Give the state hash an expiry if it has none, and never extend one it has.

    HSET and HINCRBY recreate a key that expired mid-flight, and a recreated
    key has no TTL at all — which is what "immortal job" actually looks like
    here: one hash per job, left in Redis forever, in an instance configured
    `noeviction` precisely so nothing quietly deletes things. EXPIRE ... NX
    (Redis 7; 00-redis.yaml pins redis-7) sets one only when there is none, so
    a requeue can never push a job's death further out than its own submit did.
    The retry cap already bounds this; this is the belt to that brace, and it
    is what keeps "retry does not re-arm the TTL" a property of the code rather
    than of the cap's current value.
    """
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL, nx=True)


async def fail_orphaned_job(conn: redis.Redis, job_id: str, error: str) -> None:
    """Terminal. This is the last write on the job, so the TTL is re-armed
    outright rather than NX'd: there is nothing left to compound it."""
    await conn.hset(state_key(job_id), mapping={"status": "failed"})
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL)

    await conn.xadd(
        stream_key(job_id),
        {"data": json.dumps({"type": "failed", "data": {"error": error}})},
    )
    await conn.expire(stream_key(job_id), EVENT_STREAM_TTL)


async def requeue_orphaned_job(conn: redis.Redis, entry: dict, attempt: int) -> None:
    """Put a pre-execution death back on the queue, once."""
    job_id = entry["job_id"]

    # submitted_at is carried over unchanged, not reset: the user submitted
    # once, and Q6's estimated wait should measure from when they did rather
    # than restarting its clock every time the cluster loses a pod.
    envelope = build_envelope(
        job_id,
        entry["workflow"],
        queue_key=entry["queue_key"],
        user=entry["user"],
        attempt={"count": attempt, "phase": PHASE_QUEUED},
        submitted_at=entry["submitted_at"],
    )

    # Through the same fair-queueing insert as a first submission, not an
    # LPUSH to the front. A retry is not a promotion: jumping the queue would
    # let a submitter whose worker keeps dying take slots from lanes that did
    # nothing wrong, which is the starvation Q1 exists to prevent, arriving by
    # a new door. It rejoins at the back of its own lane's round.
    _queued_before, position = await fair_enqueue_script()(
        keys=[QUEUE_KEY], args=[json.dumps(envelope), envelope["queue_key"]]
    )

    await conn.hset(
        state_key(job_id),
        mapping={"status": "queued", PHASE_FIELD: PHASE_QUEUED},
    )
    await arm_state_ttl(conn, job_id)

    await conn.xadd(
        stream_key(job_id),
        {"data": json.dumps({"type": RETRY_TYPE, "data": {
            "attempt": attempt,
            "max_attempts": MAX_JOB_RETRIES,
            "position": position,
            "reason": f"{DEAD_WORKER}, before ComfyUI was handed the workflow — requeued",
        }})},
    )
    # NX, for the same reason as arm_state_ttl: the stream must outlive the
    # retry, but a retry must not be able to buy a job another full
    # EVENT_STREAM_TTL of life. (The second attempt's own events re-arm it the
    # way any running job's do — bounded by JOB_TIMEOUT, not by this path.)
    await conn.expire(stream_key(job_id), EVENT_STREAM_TTL, nx=True)


async def reap_stranded_job(conn: redis.Redis, raw: str) -> None:
    """One entry off a dead worker's processing list: retry it, or fail it."""
    try:
        payload = json.loads(raw)
        job_id = payload["job_id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        # Deliberately narrower than parse_envelope(): a stranded entry with no
        # job_id names no job, so there is nothing to report a failure on and
        # nowhere to report it. Anything else is failed below rather than
        # dropped.
        return

    phase = await conn.hget(state_key(job_id), PHASE_FIELD)

    if phase not in RETRYABLE_PHASES:
        # Includes the case where the state hash is gone entirely (phase is
        # None). Not knowing how far a job got is a reason not to retry it, not
        # a reason to assume the safe half.
        got_there = (f"it had already handed the workflow to ComfyUI (phase "
                     f"{phase!r}), so it was not retried — replaying a workflow "
                     f"that killed one worker would kill the next one too"
                     if phase == PHASE_EXECUTING else
                     f"how far it got is no longer recorded (phase {phase!r}), "
                     f"so it was not retried")

        await fail_orphaned_job(conn, job_id, f"{DEAD_WORKER}: {got_there}. {DESCRIBE_HINT}.")
        return

    try:
        entry = parse_envelope(payload)
    except (KeyError, TypeError):
        await fail_orphaned_job(
            conn, job_id,
            f"{DEAD_WORKER}, and its queue entry carries no workflow to requeue. {DESCRIBE_HINT}.")
        return

    # Backpressure applies to requeued work exactly as it does to new work:
    # this is the same one physical list, bounded by the same ceiling, and a
    # pool that is dying faster than it drains must not be the one path that
    # gets to grow the queue past it. Checked before the counter moves, so a
    # job refused here has not spent its retry.
    depth = await conn.llen(QUEUE_KEY)

    if depth >= MAX_QUEUE_DEPTH:
        await fail_orphaned_job(
            conn, job_id,
            f"{DEAD_WORKER} before ComfyUI saw the workflow, and the queue was "
            f"full ({depth} jobs) so it could not be requeued. Resubmit. {DESCRIBE_HINT}.")
        return

    # The atomic claim. See the block comment above for why this is HINCRBY and
    # why the decision is taken from its return value rather than from a
    # subsequent read: with two gateway replicas, exactly one caller can ever
    # see 1, and that caller is the one that requeues.
    attempt = await conn.hincrby(state_key(job_id), ATTEMPT_COUNT_FIELD, 1)
    await arm_state_ttl(conn, job_id)

    if attempt > MAX_JOB_RETRIES:
        # Hand the claim back, so attempt_count keeps meaning "times this job
        # was actually requeued" for whoever reads it later. Bookkeeping only —
        # the retry decision was already made, above, from the atomic return,
        # and is never re-derived from this value.
        await conn.hincrby(state_key(job_id), ATTEMPT_COUNT_FIELD, -1)
        await fail_orphaned_job(
            conn, job_id,
            f"{DEAD_WORKER} before ComfyUI saw the workflow, and it had already "
            f"been retried {MAX_JOB_RETRIES} time(s). Workers dying this early "
            f"repeatedly is a cluster problem, not a workflow problem. {DESCRIBE_HINT}.")
        return

    await requeue_orphaned_job(conn, entry, attempt)


async def reap_orphaned_jobs() -> None:
    while True:
        try:
            conn = client()

            async for key in conn.scan_iter(match=f"{PROCESSING_KEY_PREFIX}*"):
                worker_id = key[len(PROCESSING_KEY_PREFIX):]

                if await conn.exists(f"{WORKER_KEY_PREFIX}{worker_id}"):
                    continue

                while (raw := await conn.rpop(key)) is not None:
                    await reap_stranded_job(conn, raw)

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

    # Who spent the GPU, and — since Q1 — whose fair-queueing lane this job
    # belongs to. oauth-proxy sets X-Forwarded-User for the authenticated user
    # (its pass-user-headers default); recording it answers "whose job is
    # this?" without building per-user anything. With AUTH_MODE=none there is
    # no proxy and the header is client-supplied — informational at best, so
    # never treat it as authorization. Fairness does not need it to be one:
    # the queue_key lane below only decides serving ORDER among jobs that are
    # all going to run regardless, and claiming someone else's identity only
    # shares their slots. See BEGIN FAIR QUEUEING above.
    user = request.headers.get("x-forwarded-user", "")

    # The envelope, not a hand-rolled dict: every reserved field is written
    # here with its default, so the worker never has to ask whether a key is
    # present. queue_key is the fair-queueing lane (Q1) — the same value as
    # the reserved `user` field, not a second identity concept.
    envelope = build_envelope(job_id, workflow, user=user, queue_key=user)

    # Atomically place the job in the single physical queue at its
    # fair-queueing position, rather than always at the back. `position` is
    # how many jobs will be served before this one under that ordering, which
    # is what index.html shows the caller as "N ahead" — it can differ from a
    # raw list-length snapshot once more than one lane is active, which is the
    # whole point. The overall backlog size (unaffected by fairness — it is
    # still one list) stays available from gather_stats()'s queue_depth.
    _queued_before, position = await fair_enqueue_script()(
        keys=[QUEUE_KEY], args=[json.dumps(envelope), envelope["queue_key"]]
    )

    # phase is seeded here rather than left for the worker to create, so the
    # breadcrumb is never absent on a job that exists. The reaper reads a
    # missing phase as "unknown, do not retry", and the window between a
    # worker's BLMOVE and its first HSET would otherwise land a genuinely
    # pre-execution death in that bucket.
    state = {"status": "queued", "queue_depth_at_submit": position,
             PHASE_FIELD: PHASE_QUEUED}

    if user:
        state["user"] = user

    await conn.hset(state_key(job_id), mapping=state)
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL)

    # Seed the stream so a browser that opens the WebSocket before any worker
    # picks the job up sees "queued" instead of an empty blocking read.
    await conn.xadd(stream_key(job_id), {"data": json.dumps({"type": "queued", "data": {"position": position}})})
    await conn.expire(stream_key(job_id), EVENT_STREAM_TTL)

    return {"job_id": job_id, "status": "queued", "queue_depth": position}


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

# The event types that END a stream. Exactly three, and RETRY_TYPE is
# deliberately not among them — a retried job's stream continues into its
# second attempt, and a browser that stopped reading at the retry would sit on
# a dead socket while the work it is waiting for ran to completion behind it.
# scripts/lint.sh pins this line for that reason.
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
