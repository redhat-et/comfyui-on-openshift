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
import calendar
import contextlib
import hashlib
import json
import os
import pathlib
import re
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

# Refused at import rather than tolerated: EXPIRE with a non-positive TTL
# deletes the key on the spot, so an EVENT_STREAM_TTL of 0 is a gateway that
# erases every job the instant it creates it and reports nothing wrong.
if EVENT_STREAM_TTL <= 0:
    raise ValueError(f"EVENT_STREAM_TTL must be a positive number of seconds, got {EVENT_STREAM_TTL}")

# The queue carries an ordering record and the workflow itself sits beside it
# at payload_key(job_id) — see BEGIN SHARED ENVELOPE. This is the backstop on
# that key, not the reclaim path: the reclaim path is the explicit delete every
# terminal outcome does (the worker's finally, the reaper's fail_orphaned_job),
# and the backstop exists because this Redis is `noeviction`, where a key
# nothing deletes is a key forever. A full day rather than EVENT_STREAM_TTL
# because the thing it must not do is expire out from under a job that is
# still QUEUED, and a 500-deep queue on a pool of three cards is hours of work,
# not an hour.
PAYLOAD_TTL = int(os.environ.get("PAYLOAD_TTL", str(24 * 3600)))

# Real API-format workflows are tens of KB. The cap exists because uvicorn
# imposes no body limit of its own, the whole body is buffered in memory and
# then stored in Redis, and with AUTH_MODE=none this endpoint is public.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(2 * 1024 * 1024)))

# Workers announce themselves with a heartbeat key (TTL-based, so a SIGKILLed
# worker disappears from the count on its own) and park the job they are
# running in a per-worker processing list. The reaper below fails jobs whose
# worker's heartbeat is gone — without it, a worker OOM-kill or node death
# leaves the browser on a progress bar that never moves.
#
# What follows either prefix is a worker INCARNATION id, not a pod name:
# worker_agent.py's BEGIN WORKER IDENTITY explains why the distinction is
# load-bearing here specifically, since the reaper's entire liveness test is
# pairing one of these keys with the other by that suffix. Nothing on this
# side parses the suffix; it only has to stay opaque and stay matched.
WORKER_KEY_PREFIX = "comfy:worker:"
PROCESSING_KEY_PREFIX = "comfy:processing:"
REAPER_INTERVAL = int(os.environ.get("REAPER_INTERVAL", "30"))

# Same posture: a zero interval is a tight loop of keyspace SCANs against a
# single-threaded Redis, which is an outage with a config-file cause.
if REAPER_INTERVAL <= 0:
    raise ValueError(f"REAPER_INTERVAL must be a positive number of seconds, got {REAPER_INTERVAL}")

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

# The ceiling on this process's Redis connection pool. Every open /ws/<job>
# holds one connection for as long as it tails, so this is also the ceiling
# on concurrent viewers per replica: past it a tail's read raises, retries
# briefly and closes, instead of the pool growing until Redis's own
# maxclients starts refusing the workers. Generous for a pool of a few GPUs,
# and finite.
REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", "200"))


def log(message: str) -> None:
    """
    One operator-visible line on the pod log, matching worker_agent.py's
    `[agent]` prefix so `oc logs` reads the same on both sides.

    Deliberately rare. Everything ordinary about this process is already in
    uvicorn's access log, so a line printed here is one an operator is meant
    to notice: the quota breaker announcing that it could not read its own
    accounting and is therefore NOT enforcing (BEGIN QUOTA BREAKER below), and
    the reaper announcing that it could not reap a stranded entry (BEGIN REAP
    DURABILITY), which is a job with no terminal event and a person's problem.
    A control that stops existing silently is the failure mode this exists for.
    """
    print(f"[gateway] {message}", flush=True)


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
# — except that `workflow`, alone among those fields, is stored BESIDE the
# queue rather than on it, at payload_key(job_id), and the entry on the list
# carries the other six. The split is not about the envelope; it is about what
# a submit costs. Q1's fair-queueing insert has to look at every entry already
# queued to decide where the new one goes, Redis is single-threaded, and the
# workflow is the only field whose size is the client's to choose — tens of KB
# typically and MAX_BODY_BYTES at worst. With it in the list, a submit against
# a full queue walked megabytes and stalled every other client, workers parked
# in BLMOVE included, for as long as that took. With it out, the walk is over
# a few hundred bytes an entry whatever the workflow weighs. See BEGIN FAIR
# QUEUEING in hub.py for the insert itself.
#
# CONSUMERS MUST TAKE BOTH SHAPES, and this is a third direction of the same
# tolerance the rest of this block is about. An entry that carries its own
# `workflow` is complete and is used as it stands — that is the pre-F2 two-key
# shape, it is what a not-yet-upgraded gateway writes through a rolling
# deploy, and it is what anything hand-pushing onto the queue writes. An entry
# without one is a pointer: the workflow is read from payload_key(job_id) and
# rejoined. Absence is the discriminator rather than a flag, because a flag is
# a field an older peer does not write.
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

# Which incarnation currently OWNS this job, on that same state hash. The
# fence that makes a requeue safe against a worker that is still alive
# (worker_agent.py, point 10).
#
# The phase breadcrumb answers "how far did it get"; this answers the question
# underneath it, which the reaper cannot otherwise ask: is the process that
# got it that far still entitled to finish it? A heartbeat that lapses without
# a death is not a hypothetical — the reaper's liveness test is the existence
# of one key, so any pause longer than HEARTBEAT_TTL reads as a death, and a
# worker declared dead in the middle of its own prologue is at a retryable
# phase by construction. Without this field the requeue and the original
# attempt both run to completion: ComfyUI is handed one workflow twice, two
# terminal events land on one stream (the browser closed at the first), and
# two accruals bill one job_id.
#
# A worker writes its INCARNATION here when it claims a job, and re-reads it
# before the two acts that are irreversible from anyone else's point of view —
# the submit, and the terminal write. The reaper stamps REAPED_OWNER over it
# before it decides anything, so "somebody took this off me" is a value the
# original attempt can see rather than something it has to infer.
#
# Shared vocabulary, in this block, for the same reason the phases are: the
# gateway writes the sentinel and the worker compares against it, they ship in
# two different images, and a rolling deploy always has one vintage of each
# running at once. Two files disagreeing about this string would silently make
# every fence a no-op with nothing failing anywhere to say so.
#
# Absence is deliberately NOT a fence. A job whose state hash was written by a
# pre-F4 worker, or whose hash expired and was recreated by an HSET, has no
# owner at all — and a missing field must not be able to suppress a real
# terminal event, which is the one failure mode worse than the replay this
# closes. Unowned means proceed; only a DIFFERENT owner means abandon.
OWNER_FIELD = "owner"

# What the reaper stamps on a job it has taken off a worker. Not a valid
# incarnation — INCARNATION_SEP cannot appear in a pod name, so no worker can
# ever be called this — so it can never accidentally match a live claim.
REAPED_OWNER = "#reaped"


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


# Where the workflow half of an entry lives when the entry does not carry it.
# Namespaced under the job like its state hash and its event stream, so one
# job is still one `comfy:job:<id>:*` prefix to an operator holding an id.
PAYLOAD_KEY_PREFIX = "comfy:job:"
PAYLOAD_KEY_SUFFIX = ":payload"


def payload_key(job_id: str) -> str:
    """The key holding the workflow for an entry that does not carry one."""
    return f"{PAYLOAD_KEY_PREFIX}{job_id}{PAYLOAD_KEY_SUFFIX}"


def queue_record(envelope: dict) -> dict:
    """
    The producer side of the split: what actually goes on the list.

    Every field except the workflow, so the entry on the queue is still a
    readable envelope rather than a bare id — an operator, and
    check-40-envelope.py, reads the version, the lane, the attempt breadcrumb,
    the submitter and the submit time straight off an LRANGE exactly as
    before. Those six are scalars by contract (see above: nothing unbounded
    goes in here), which is what makes the entry small enough for the insert
    to walk cheaply.
    """
    return {key: value for key, value in envelope.items() if key != "workflow"}


def needs_payload(record) -> bool:
    """The consumer side: is this entry a pointer rather than a whole
    envelope? Anything carrying a workflow is complete, whatever else it
    says."""
    return isinstance(record, dict) and "workflow" not in record


def with_workflow(record: dict, stored: str) -> dict:
    """
    Rejoin a pointer entry with the workflow stored beside the queue.

    The result is an ordinary envelope of the shape at the top of this block,
    for parse_envelope() to read as usual — the tolerance rules do not get a
    second implementation for pointer entries. Raises on a payload that is not
    JSON, which the caller treats as it treats any malformed entry.
    """
    return dict(record, workflow=json.loads(stored))

# END SHARED ENVELOPE
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BEGIN SHARED SHOWBACK — the GPU-second accumulator (docs/10-roadmap.md, Q4)
#
# WHAT A GPU SECOND IS HERE, written down in the code because a number nobody
# can reproduce is worse than no number at all:
#
#     one GPU second is one second for which a worker held the card on this
#     job — wall-clock time between the instant a worker wrote `running` on
#     the job's state hash (run_job() in worker_agent.py) and the instant that
#     job reached a terminal state.
#
# It is HELD time, not utilised time, and it is deliberately an OVER-COUNT of
# the sampler. Everything below is inside the number:
#
#   - loading the checkpoint (~7 GB off EFS for an SDXL-class model, and the
#     first job on a cold node pays for the page cache being empty),
#   - the workspace mkdir, the ComfyUI WebSocket connect and the /prompt round
#     trip, all of which happen after `running` is written,
#   - any stretch the agent spends parked on a ComfyUI that has gone quiet,
#     up to RECV_TIMEOUT at a time and JOB_TIMEOUT in total,
#   - a job that failed or was cancelled part-way: it still held the card for
#     as long as it held it.
#
# That is on purpose, and it is the only definition this system can actually
# MEASURE. The alternative — "time spent inside ComfyUI's own execution" —
# would under-count precisely the expensive part, because the pool runs one
# job per pod on a dedicated card: nobody else could have used the GPU while
# this job was loading a checkpoint, so somebody has to be shown as having
# spent it. Queue time is NOT in the number: nothing was held before a worker
# picked the job up.
#
# The reaper path is the one case where the measurement is not honest, and it
# is bucketed separately rather than fudged — see SHOWBACK_TO_EXCLUDED below.
#
# WHY THIS BLOCK IS DUPLICATED. Two terminal paths write GPU time and they
# live in different files: worker_agent.py's finish(), and hub.py's reaper
# (fail_orphaned_job()/cancel_orphaned_job(), which never call finish() at
# all). They must agree on the key, the period, the field names, the cap and
# the expiry, and there is nowhere to import a shared definition from —
# enterprise/setup.sh builds the two images from two different build contexts.
# So this follows the rule the queue envelope already follows: MIRRORED
# VERBATIM between enterprise/gateway/hub.py and
# enterprise/worker/worker_agent.py, change both or neither, with
# scripts/lint.sh diffing the two copies line for line. What is NOT in here is
# the Redis call itself: hub.py is redis.asyncio and worker_agent.py is
# synchronous redis, so each file runs the script below its own way.
#
# THE KEY SPACE IS BOUNDED THREE TIMES OVER, and each bound is load-bearing
# against `maxmemory-policy noeviction` at `--maxmemory 512mb`
# (00-redis.yaml): under `noeviction` a key nothing deletes is a key forever,
# and the identity every field here is named from is an X-Forwarded-User
# header that is entirely client-supplied whenever AUTH_MODE=none. An
# accumulator that took one Redis key per submitter — or, worse, per job —
# would let an unauthenticated caller fill Redis by varying one header, which
# presents as queued work vanishing at random.
#
#   1. ONE KEY PER PERIOD, not per user. The whole report for a calendar
#      month is a single Hash at comfy:showback:<YYYY-MM>, one field per
#      identity, HINCRBYFLOAT per job. A year of traffic is at most twelve
#      keys however many people submitted, and point 2 keeps only the last
#      few of those.
#   2. THE KEY EXPIRES. Every accrual re-arms an expiry with EXPIRE ... NX —
#      NX so a bucket's lifetime is measured from its first write and a busy
#      month cannot push its own deletion forward forever, and on every write
#      rather than only the first because HINCRBYFLOAT recreates a key that
#      expired mid-flight and a recreated key has no TTL at all. That is the
#      same rule, for the same reason, as hub.py's arm_state_ttl().
#   3. THE FIELD COUNT IS CAPPED. A new identity is only given its own field
#      while the bucket holds fewer than SHOWBACK_MAX_USERS fields; past that
#      it accrues to one shared overflow field. The report says so rather
#      than pretending, so a report that hit the cap is visibly truncated
#      instead of quietly wrong.
#
# Worst case is therefore SHOWBACK_MAX_USERS × (MAX_ENVELOPE_FIELD_CHARS + a
# float) per period, times SHOWBACK_RETENTION_PERIODS periods live at once:
# ~1000 × ~280 bytes × 3 ≈ 0.8 MB against a 512 MB instance, and it does not
# grow with throughput.
# ---------------------------------------------------------------------------

# One Hash per period. The prefix is this item's namespace the way
# comfy:worker:* and comfy:processing:* are the reaper's; enterprise/test/
# check-90-showback.py scans it directly to assert the bound above.
SHOWBACK_KEY_PREFIX = "comfy:showback:"

# The period is a UTC calendar month. UTC and not local time because the two
# gateway replicas, the workers and whoever reads the report are not
# guaranteed to agree on a timezone, and a month boundary that moves per
# reader is a report that does not add up.
SHOWBACK_PERIOD_FORMAT = "%Y-%m"

# How long a bucket lives, counted from its first write. Three periods keeps
# the current month plus the two before it readable — enough to answer "what
# did last month cost?" after last month ended — and then Redis deletes it
# without anybody remembering to. 31 days is the period length used for the
# TTL arithmetic: it must be the LONGEST month rather than the average, or a
# bucket opened on the 1st of a 31-day month would expire before the same
# calendar span had elapsed.
SHOWBACK_RETENTION_PERIODS = max(1, int(os.environ.get("SHOWBACK_RETENTION_PERIODS", "3")))
SHOWBACK_PERIOD_SECONDS = 31 * 24 * 3600

# The ceiling on distinct fields in one period's Hash. Generous for any real
# organisation — this is a pool of a few GPUs — and small enough that the
# whole key stays under a megabyte in a Redis that must never fill up.
SHOWBACK_MAX_USERS = max(1, int(os.environ.get("SHOWBACK_MAX_USERS", "1000")))

# Field names inside that Hash. Every submitter's field is PREFIXED, which is
# what makes the three reserved buckets un-collidable: a submitter who calls
# themselves "anonymous" gets the field "u:anonymous" and cannot land on the
# anonymous bucket by choosing a name. The identity itself is already clamped
# to MAX_ENVELOPE_FIELD_CHARS on the way in (see the shared envelope block),
# so a field name is bounded too.
SHOWBACK_USER_PREFIX = "u:"

# Submitted with no X-Forwarded-User at all — the ordinary AUTH_MODE=none,
# no-proxy shape. Its own named bucket rather than an empty-string user,
# because "explicit" means an operator reading the report sees the number
# without having to know to look for a blank key.
SHOWBACK_ANONYMOUS_FIELD = "anonymous"

# GPU time that was really spent and is deliberately NOT billed to a
# submitter. See SHOWBACK_TO_EXCLUDED.
SHOWBACK_EXCLUDED_FIELD = "excluded"

# Where accruals go once the field cap above is reached. Real seconds, real
# users, no longer told apart.
SHOWBACK_OTHER_FIELD = "other"

# On the job's own state hash. hub.py's generate() writes the submitter here
# at submit and writes NOTHING when there was no header — so "absent" is what
# anonymous looks like, and the accrual below reads it rather than inventing a
# second copy of the identity. Named once because three readers now depend on
# the spelling.
SHOWBACK_USER_FIELD = "user"

# On the job's own state hash. started_at is written by the worker when the
# job starts running and REMOVED by the accrual below, in the same atomic
# script that reads it: it is both the clock's start and the claim on it, so
# a job can be billed at most once however many terminal paths race for it —
# which they do, whenever a worker's heartbeat lapses while the worker is in
# fact alive and finishing. gpu_seconds is what it was billed, left on the
# job so a line in the report can be traced back to the jobs behind it.
STARTED_AT_FIELD = "started_at"
GPU_SECONDS_FIELD = "gpu_seconds"

# Where an accrual is sent, and the whole of the reaper-path decision.
#
#   SHOWBACK_TO_SUBMITTER  the job reached a terminal state through the
#                          worker that was running it (worker_agent.py's
#                          finish(), whatever the status: completed, failed
#                          or cancelled). Both ends of the interval were
#                          written by that worker, so the number is a
#                          measurement and it is billed to the submitter —
#                          or to the anonymous bucket if there was no
#                          identity.
#
#   SHOWBACK_TO_EXCLUDED   the job was terminated by hub.py's reaper because
#                          its worker's heartbeat lapsed (a SIGKILL, an OOM
#                          kill, a node reclaim). The GPU time is real and
#                          often the most expensive in the system, so it is
#                          NOT dropped — but the gateway cannot know WHEN the
#                          worker died, only when it noticed, so the interval
#                          it can compute is inflated by up to
#                          HEARTBEAT_TTL + REAPER_INTERVAL (~3.5 minutes on
#                          the defaults). Billing a user for a number that is
#                          mostly detection lag, for a job that produced
#                          nothing, is the kind of figure an operator cannot
#                          defend in the meeting the report exists for. It
#                          goes to one visible line instead, where it doubles
#                          as a cluster-health signal: excluded_gpu_seconds
#                          climbing is workers dying while holding cards.
#
# A requeued job is not accrued at all: the reaper only requeues deaths that
# happened BEFORE ComfyUI was handed the workflow (RETRYABLE_PHASES), which
# by construction spent no GPU time, and the attempt that eventually runs
# writes its own started_at over the old one.
SHOWBACK_TO_SUBMITTER = "submitter"
SHOWBACK_TO_EXCLUDED = "excluded"

# One accrual, atomically: claim the job's clock, compute the interval, place
# it in the right field of the right period's Hash, and re-arm the Hash's
# expiry. Atomic because the claim and the increment must not be separable —
# see STARTED_AT_FIELD above — and because "is this identity new, and is the
# Hash full?" is a read the cap's decision is taken from.
#
# Returns {field, seconds} for the caller to log, or {"", "0"} when there was
# nothing to bill: no clock on the job (it never ran, or it has already been
# billed by whichever terminal path got there first).
SHOWBACK_ACCRUE_LUA = """
local state = KEYS[1]
local bucket = KEYS[2]
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local max_users = tonumber(ARGV[3])
local user_prefix = ARGV[4]
local anonymous_field = ARGV[5]
local excluded_field = ARGV[6]
local other_field = ARGV[7]
local destination = ARGV[8]
local started_field = ARGV[9]
local seconds_field = ARGV[10]
local user_field = ARGV[11]
local to_submitter = ARGV[12]

-- The claim. HGET and HDEL in one script is what makes billing idempotent:
-- a worker finishing a job whose heartbeat lapsed a moment earlier and the
-- reaper failing that same job are two terminal paths racing, and exactly
-- one of them can see a clock here.
local started = redis.call('HGET', state, started_field)

if not started then
  return {'', '0'}
end

redis.call('HDEL', state, started_field)

local began = tonumber(started)

if not began then
  return {'', '0'}
end

local seconds = now - began

-- Clocks are not monotonic across two pods. A negative interval is not
-- evidence of anything except skew, and must never be able to REDUCE a
-- running total.
if seconds < 0 then
  seconds = 0
end

local field = excluded_field

if destination == to_submitter then
  local user = redis.call('HGET', state, user_field)

  if user and user ~= '' then
    field = user_prefix .. user
  else
    field = anonymous_field
  end
end

-- The cap, checked only for submitter fields: the three reserved buckets are
-- fixed in number and must always be reachable, or the overflow itself would
-- be what stopped being recorded once the Hash filled up.
if field ~= excluded_field and field ~= anonymous_field and field ~= other_field
   and redis.call('HEXISTS', bucket, field) == 0
   and redis.call('HLEN', bucket) >= max_users then
  field = other_field
end

redis.call('HINCRBYFLOAT', bucket, field, seconds)

-- NX, and on every write rather than only the first. See point 2 of the
-- block comment above: this is what keeps a `noeviction` Redis from
-- accumulating a Hash per month forever.
redis.call('EXPIRE', bucket, ttl, 'NX')

-- What this job was billed, left on the job itself so a report line can be
-- traced back to the jobs behind it.
redis.call('HSET', state, seconds_field, string.format('%.3f', seconds))

return {field, string.format('%.3f', seconds)}
"""


def showback_period(now: float) -> str:
    """The bucket a moment belongs to. UTC, see SHOWBACK_PERIOD_FORMAT."""
    return time.strftime(SHOWBACK_PERIOD_FORMAT, time.gmtime(now))


def showback_key(period: str) -> str:
    """One Hash holds one period's whole report."""
    return f"{SHOWBACK_KEY_PREFIX}{period}"


def showback_ttl_seconds() -> int:
    """How long a bucket lives from its first write. See point 2 above."""
    return SHOWBACK_RETENTION_PERIODS * SHOWBACK_PERIOD_SECONDS


def showback_accrue_call(state: str, destination: str,
                         now: float | None = None) -> tuple[list, list]:
    """
    The keys and args for one accrual, in one place because there are three
    call sites in two files — the worker's finish() and both of the reaper's
    terminal paths — and an argument order remembered in three places is an
    argument order that is wrong in one of them.
    """
    now = time.time() if now is None else now

    return (
        [state, showback_key(showback_period(now))],
        [now, showback_ttl_seconds(), SHOWBACK_MAX_USERS,
         SHOWBACK_USER_PREFIX, SHOWBACK_ANONYMOUS_FIELD,
         SHOWBACK_EXCLUDED_FIELD, SHOWBACK_OTHER_FIELD, destination,
         STARTED_AT_FIELD, GPU_SECONDS_FIELD, SHOWBACK_USER_FIELD,
         SHOWBACK_TO_SUBMITTER],
    )

# END SHARED SHOWBACK
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BEGIN QUOTA BREAKER (docs/10-roadmap.md, Q5)
#
# A ceiling on the GPU seconds one submitter may spend inside one showback
# period, enforced in generate() before a job is placed on the queue. The
# roadmap settled what this is and is not, and every line below is one of
# those sentences:
#
#   1. IT IS A LOCAL QUOTA READ OUT OF Q4's ACCOUNTING, and there is no
#      second accounting path. The number compared here is the same field
#      SHOWBACK_ACCRUE_LUA writes and /api/showback reports — one Hash per
#      period, one field per identity — so a user cannot be refused for
#      seconds the report does not show, and an operator asked "why was I
#      refused?" answers it from a URL. The alternative the roadmap rejected
#      was an AWS budget lookup, which would put cloud credentials on the one
#      pod docs/09 calls the entire public attack surface, to enforce a figure
#      that lags real spend by hours.
#
#   2. IT FAILS OPEN, AND SAYS SO. The field missing, the value not a number,
#      the env var not a number, or the read to fetch the field raising:
#      every one of those proceeds with the submission, and
#      quota_gpu_seconds_used() logs which one it was. The asymmetry is the
#      roadmap's — "a breaker that trips on an unreachable dependency halts a
#      cluster you are already paying for, while the risk it guards against
#      is slow". But a control that stops enforcing silently is a control
#      that has quietly stopped existing, so every fail-open that is not
#      simply "this submitter has spent nothing yet" prints a line naming
#      what could not be read (see log() above). The budget alarm remains the
#      backstop.
#
#      "Redis unreachable" reaches this fail-open on its own terms: the quota
#      read is the first Redis call generate() makes, so an outage that takes
#      the whole instance down logs a fail-open here and then raises,
#      unhandled, on the enqueue script one call later — the submitter sees a
#      500, not a quiet pass-through, and nothing was written. That script is
#      NOT softened to match, because backpressure lives inside it
#      (FAIR_ENQUEUE_LUA) precisely so that "proceed when you cannot even
#      read the depth" is an answer this endpoint cannot give. The logged
#      fail-open here is real and reachable — a read that fails on its own
#      (a single flaky call, a differently-sharded key in a clustered Redis)
#      — just not the "Redis is entirely down" case the phrase most readily
#      suggests.
#
#   3. IT IS NOT WIRED INTO readyz(), NOT EVEN TRANSITIVELY. That endpoint is
#      the gateway's readiness probe: a quota check inside it would take the
#      whole gateway out of the Service the moment one submitter went over,
#      dropping every WebSocket that is reporting an in-flight job, on a pool
#      that is still running work. The outage would be caused by the control
#      meant to prevent one. scripts/lint.sh holds that separation as a shape
#      rule ("the quota breaker is not reachable from readyz()"), because it
#      is a one-line edit to reintroduce and nothing in a test run of a
#      healthy gateway would notice.
#
#   4. IT IS OFF BY DEFAULT — QUOTA_GPU_SECONDS unset, or <= 0, means every
#      call below short-circuits before it touches Redis. A quota nobody
#      chose is a support ticket on somebody else's cluster.
#
# What it does NOT do: interrupt work already queued or running. This is
# admission control on new submissions only, which is also why the refusal
# says so — a user who is refused still has their in-flight jobs.
# ---------------------------------------------------------------------------

# The per-submitter ceiling, in GPU seconds per showback period, from the
# environment (.env.example, wired into the Deployment by enterprise/setup.sh).
# Parsed tolerantly on purpose, unlike MAX_QUEUE_DEPTH's int(): this is the
# breaker, and the posture in point 2 above applies to its own configuration
# too. A garbled value disables it loudly rather than crash-looping the
# gateway, which is the failure that would take the cluster out.
QUOTA_GPU_SECONDS_RAW = os.environ.get("QUOTA_GPU_SECONDS", "0")

try:
    QUOTA_GPU_SECONDS = float(QUOTA_GPU_SECONDS_RAW)
except (TypeError, ValueError):
    log(f"QUOTA_GPU_SECONDS={QUOTA_GPU_SECONDS_RAW!r} is not a number — the "
        f"GPU-second quota breaker is OFF and no submission will be refused "
        f"for quota (docs/10-roadmap.md, Q5)")
    QUOTA_GPU_SECONDS = 0.0

# How the refusal reads a period's end, for humans: the count restarts when
# the period does, and the period is a UTC calendar month (see
# SHOWBACK_PERIOD_FORMAT).
QUOTA_RESET_FORMAT = "%Y-%m-%d %H:%M UTC"


def quota_enabled() -> bool:
    """Off unless somebody chose a positive ceiling. See point 4 above."""
    return QUOTA_GPU_SECONDS > 0


def quota_field(user: str) -> str:
    """
    The field in the period's Hash that holds THIS submitter's seconds.

    It has to name the same field the accrual writes, or the breaker would
    enforce against a number nobody accrues into: SHOWBACK_ACCRUE_LUA reads
    the submitter off the job's state hash — which generate() writes from this
    same X-Forwarded-User header — and prefixes it, or sends it to the
    anonymous bucket when there was no header at all.

    Anonymous submissions are therefore counted against the shared anonymous
    bucket rather than exempted. Under AUTH_MODE=none that makes the quota a
    single pool for every caller without a header, which is the honest reading
    of "the header is client-supplied": exempting the no-header case would
    turn the breaker off for exactly the deployment shape where anyone with
    the URL can spend the GPU budget. It is not a security control either way
    — varying a header buys a fresh quota — it is a cost guardrail, and the
    identity it uses is the identity the report uses.
    """
    return f"{SHOWBACK_USER_PREFIX}{user}" if user else SHOWBACK_ANONYMOUS_FIELD


def quota_period_reset(now: float) -> float:
    """
    When the count restarts: 00:00 UTC on the first of the next calendar
    month, because the bucket read below is named from showback_period(now)
    and the next period is a different, empty Hash.

    Not a rolling window and not the Hash's TTL (which spans several periods
    — see showback_ttl_seconds()). A refusal that quoted the TTL would tell
    the user to come back months after their quota had in fact reset.
    """
    moment = time.gmtime(now)
    year = moment.tm_year + (1 if moment.tm_mon == 12 else 0)
    month = 1 if moment.tm_mon == 12 else moment.tm_mon + 1

    return float(calendar.timegm((year, month, 1, 0, 0, 0, 0, 1, 0)))


def quota_refusal_text(used: float, now: float) -> str:
    """
    What the caller is told. Three things, because "rejected" with no reason
    is a support ticket: what happened, what the numbers were, and when it
    resets — plus where to read the usage it was decided from.
    """
    return (f"GPU-second quota exhausted: {used:.1f} of {QUOTA_GPU_SECONDS:.1f} "
            f"GPU seconds used in period {showback_period(now)}. This quota is "
            f"per user, so other submitters are unaffected, and jobs you have "
            f"already queued keep running. It resets at "
            f"{time.strftime(QUOTA_RESET_FORMAT, time.gmtime(quota_period_reset(now)))}, "
            f"when the next period begins. GET /api/showback for the usage "
            f"this was decided from.")


async def quota_gpu_seconds_used(conn: redis.Redis, user: str,
                                 now: float) -> float | None:
    """
    This submitter's accrued GPU seconds in the current period, or None when
    the answer is not knowable — Redis unreachable, or a value that is not a
    number. None means FAIL OPEN, and every None here is logged.

    A field that is simply ABSENT is 0.0 rather than None: a submitter who has
    not spent anything this period is not an unreadable submitter, and logging
    every first-ever submission as a fail-open would bury the lines that
    matter under the ordinary case.

    Tolerant parsing rather than a bare float() for the same reason
    showback_report() is tolerant on the read side ("a value that is not a
    number is skipped") — the breaker must not be stricter about Q4's data
    than Q4 is.
    """
    key = showback_key(showback_period(now))
    field = quota_field(user)

    try:
        raw = await conn.hget(key, field)
    except Exception as exc:  # noqa: BLE001
        log(f"QUOTA FAILING OPEN: could not read {key} field {field!r} "
            f"({exc!r}). This submission is NOT being checked against the "
            f"{QUOTA_GPU_SECONDS:.1f}s quota.")
        return None

    if raw is None:
        return 0.0

    try:
        return float(raw)
    except (TypeError, ValueError):
        log(f"QUOTA FAILING OPEN: {key} field {field!r} holds {raw!r}, which "
            f"is not a number. This submission is NOT being checked against "
            f"the {QUOTA_GPU_SECONDS:.1f}s quota.")
        return None


async def quota_refusal(conn: redis.Redis, user: str) -> str | None:
    """
    The message to refuse this submitter with, or None to let the submission
    through — which includes every case where the quota could not be decided.

    ONE CALLER, generate(), and deliberately: the readiness probe must never
    reach this. See point 3 above and the shape rule in scripts/lint.sh.
    """
    if not quota_enabled():
        return None

    now = time.time()
    used = await quota_gpu_seconds_used(conn, user, now)

    if used is None or used < QUOTA_GPU_SECONDS:
        return None

    return quota_refusal_text(used, now)


def quota_headers(now: float | None = None) -> dict:
    """
    Retry-After on the refusal, in seconds, so a client that retries on a 429
    does not do it every second until the month turns over. The same instant
    the message names, in the form the HTTP spec defines.
    """
    now = time.time() if now is None else now

    return {"Retry-After": str(max(1, int(quota_period_reset(now) - now)))}

# END QUOTA BREAKER
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
#
# THE COST OF THAT RECOMPUTE is the thing to understand before editing this,
# because it is paid by every client rather than by the submitter. Redis runs
# one command at a time, so whatever an EVAL does, every other connection
# waits — including every worker parked in BLMOVE, which is to say the pool
# stops being handed work for the duration. Two things therefore have to stay
# true of the script below, and the first version of it had neither:
#
#   1. What it reads per entry is SMALL and fixed. It walks the whole queue,
#      which is bounded by MAX_QUEUE_DEPTH; what it must never also scale with
#      is the size of a workflow, which the client chooses and which reaches
#      MAX_BODY_BYTES. That is why the queue carries the ordering record and
#      the workflow lives at payload_key(job_id) (see the shared envelope
#      block above), and it is worth a number: with 26 KB workflows in the
#      list, one submit against a 499-deep queue measured ~86 ms of exclusive
#      Redis time; over an ordering record it is under a millisecond, and it
#      no longer moves at all when the workflows get bigger.
#
#   2. It NEVER unmakes the queue to remake it. Redis does not roll back a
#      script's partial effects, so a script that DELs the list and RPUSHes it
#      back has a window whose failure mode is losing every queued job at once
#      — the same "work vanishing at random" that `maxmemory-policy
#      noeviction` exists to prevent, arriving by a door the policy does not
#      cover. LINSERT places the new entry against a pivot already in the list
#      and touches nothing else, so there is no state in which the queue is
#      empty, short, or half-written, and no error path that could leave one.
#      Anything that reintroduces a read-modify-rewrite of the whole list —
#      including a "tidy" refactor — reintroduces that window.
# ---------------------------------------------------------------------------

FAIR_ENQUEUE_LUA = """
local key = KEYS[1]
local payload = KEYS[2]
local new_entry = ARGV[1]
local new_lane = ARGV[2] or ""
local workflow = ARGV[3]
local payload_ttl = tonumber(ARGV[4])
local max_depth = tonumber(ARGV[5]) or 0

-- Backpressure, INSIDE the script rather than an LLEN in the caller: a depth
-- read in one round trip and an insert in the next is a window every submit
-- that arrived in between fits through, so N clients retrying against a full
-- queue all read "one short of full" and all get in. Here the read and the
-- insert are one atomic unit, and the refusal happens before the payload is
-- written so a refused submit leaves nothing behind. 0 disables the ceiling.
if max_depth > 0 then
  local depth = redis.call('LLEN', key)
  if depth >= max_depth then
    return {-1, depth}
  end
end

-- The workflow lands beside the queue BEFORE the entry pointing at it lands on
-- the queue, so there is no ordering in which a worker pops a pointer whose
-- payload has not been written. KEEPTTL, then EXPIRE NX: a requeue rewrites
-- the workflow (its attempt count moved) without buying the job another full
-- lifetime, which is the same rule hub.py's arm_state_ttl() follows and for
-- the same reason. A plain SET would clear the TTL and the EXPIRE would then
-- re-arm it in full.
redis.call('SET', payload, workflow, 'KEEPTTL')
if payload_ttl and payload_ttl > 0 then
  redis.call('EXPIRE', payload, payload_ttl, 'NX')
end

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

-- Service position to physical index. `insert_at - 1` jobs are to be served
-- before this one and the list runs newest-first, so the new entry belongs at
-- physical index n - insert_at + 1, counting from the head.
local at = n - insert_at + 1

if at <= 0 then
  -- Served last: the back of the line is the physical head, the plain LPUSH
  -- this queue did before Q1 existed. Also the whole of the empty-queue case.
  redis.call('LPUSH', key, new_entry)
elseif at >= n then
  -- Served first. Unreachable: front[1] has nothing of its own lane ahead of
  -- it, so its round is 0, so no existing job's round can be strictly greater
  -- than a new job's and insert_at is never 1 for a non-empty queue. Written
  -- out anyway rather than left to fall into the branch below, so the index
  -- arithmetic does not silently depend on that argument staying true.
  redis.call('RPUSH', key, new_entry)
else
  -- The ordinary case, and the reason nothing here rewrites the list: LINSERT
  -- splices one element in against a pivot and leaves every other element
  -- alone. raw[at + 1] is the entry currently at physical index `at`, which is
  -- the one the new entry must push back by one place.
  local placed = redis.call('LINSERT', key, 'BEFORE', raw[at + 1], new_entry)
  if placed < 0 then
    -- LINSERT reports -1 for a pivot it cannot find, which cannot happen here
    -- -- the list cannot change under an EVAL and entries are unique by
    -- job_id. If it somehow does, the job joins the back of the queue. Losing
    -- a place in line is a bad outcome; losing the job is not an outcome.
    redis.call('LPUSH', key, new_entry)
  end
end

-- {jobs queued before this one (any lane), jobs that will be served before
-- this one under fair-queueing order} — or {-1, depth} when the queue was
-- full and nothing was written (QUEUE_FULL below).
return {n, insert_at - 1}
"""

# What the script's first return value is when it refused the insert. The
# second value is then the depth it saw, for the 503's text.
QUEUE_FULL = -1


def fair_enqueue_call(envelope: dict) -> tuple[list, list]:
    """
    The keys and args for one fair-queueing insert.

    In one place because there are two call sites — a first submission and the
    reaper's requeue — and an argument order remembered in two places is an
    argument order that is wrong in one of them. It is also what a benchmark
    or a future third caller should use rather than re-deriving the split.
    """
    return (
        [QUEUE_KEY, payload_key(envelope["job_id"])],
        [json.dumps(queue_record(envelope)),
         envelope["queue_key"],
         json.dumps(envelope["workflow"]),
         PAYLOAD_TTL,
         MAX_QUEUE_DEPTH],
    )


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
    # one. Exactly one reaper can hold a given stranded entry — the SET NX
    # claim in reap_processing_list(), which took that job over from RPOP when
    # the reap stopped destroying what it was reaping (BEGIN REAP DURABILITY) —
    # which is the whole argument for failing a job at most once. It is NOT the
    # argument for requeueing one at most once:
    # a requeued job can be stranded again, on another worker, and be seen by
    # the other replica's reaper. That bound comes from the atomic HINCRBY in
    # reap_stranded_job() below, not from this.
    reaper = asyncio.create_task(reap_orphaned_jobs())

    yield

    # Cancel, then AWAIT the cancellation: a task cancelled and dropped is one
    # uvicorn's shutdown logs as "Task was destroyed but it is pending", and
    # its Redis connection stays checked out of a pool that is about to be
    # closed underneath it.
    reaper.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await reaper

    if _redis is not None:
        await _redis.aclose()


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
            max_connections=REDIS_MAX_CONNECTIONS,
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
#   - Unless the job was cancelled, which is checked before the requeue and not
#     after. A cancelled job is at a retryable phase precisely BECAUSE the
#     cancel stopped it early, so "retryable" would otherwise mean "hand the
#     workflow the user withdrew to a second worker". It ends `cancelled`.
#
# Why the counter is HINCRBY and not read-then-write. "only one reaper can be
# holding a given entry, so two reapers fail different jobs" is the argument
# that makes fail-once safe — it was RPOP's atomicity and is now the claim in
# reap_processing_list(), for the reason BEGIN REAP DURABILITY gives — and it
# does NOT carry over to requeue-once. A requeued job goes back on
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

# The cancel flag, on the job's own state hash beside the phase breadcrumb.
# cancel() below sets it and worker_agent.py reads it; the reaper reads it too,
# because a stranded job the user has already withdrawn must not be requeued
# and handed to a second worker. Named once rather than spelled three times:
# the flag is only load-bearing while every reader agrees on the string.
CANCEL_REQUESTED_FIELD = "cancel_requested"

# What the operator can do that the gateway cannot. The gateway sees a lapsed
# heartbeat and nothing else — but the pod is not ambiguous, because F1 sized
# the worker Guaranteed and within what the node can hand one pod, so a
# host-RAM OOM terminates it OOMKilled instead of disappearing into node
# pressure. Every failure text below carries this, because the person reading
# it is the person who can answer the question this code cannot.
DESCRIBE_HINT = ("`oc describe pod` on that worker names the reason it went — "
                 "OOMKilled is a host-RAM OOM, and a node reclaim says so too")

# ---------------------------------------------------------------------------
# BEGIN REAP DURABILITY — how an entry leaves a processing list, which is a
# different question from what reaping a job does.
#
# reap_processing_list() below READS the tail of the list and removes it only
# once reap_stranded_job() has returned. It used to `RPOP`, which is to say the
# entry came off the list first and the reap then ran against a value held
# nowhere but in this process's memory. Anything that raised inside that body —
# the terminal XADD against a key of the wrong type, a Redis that went away
# between two of its commands, a bug — destroyed the only record that the job
# had ever been queued. The `except Exception: pass` around the tick said "next
# tick retries"; there was nothing left for a next tick to retry, the job
# reached no terminal state at all, and the browser sat on a bar that never
# moved until the stream TTL ran out. That is precisely the work-vanishing the
# `noeviction` invariant exists to prevent (docs/09, section 3), arriving by a
# door `noeviction` does not cover, and one injected transient error reproduces
# it.
#
# Reading without removing gives up something RPOP provided for free, and it is
# not a small something: RPOP is atomic, so exactly one reaper — of the two
# gateway replicas 01-gateway.yaml runs — could ever be holding a given entry,
# and that is what bounds FAILING a stranded job to once. Two reapers reading
# the same tail have no such bound: both would fail the same job, and on a
# retryable one both would reach the HINCRBY claim, where one requeues and the
# other terminates the job it just requeued. So the exclusion stops being a
# side effect of the read and becomes the thing it always was in substance: a
# claim, `SET NX` on a key named from the entry's own bytes, which exactly one
# caller can win. It is held for the DURATION of the reap rather than for the
# instant of a pop, which is strictly the stronger property; the HINCRBY claim
# above is unchanged and still carries requeue-once on its own.
#
# What is traded, stated plainly: this is at-least-once where RPOP was
# at-most-once. A gateway that dies in the window between a reap completing and
# its LREM leaves the entry to be reaped a second time. That costs a duplicate
# terminal event on a stream a browser stopped reading at the first one — where
# the old shape's equivalent window cost the job entirely.
#
# And a bound, because "leave it until it works" is a loop that never ends on
# an entry that can never work: the tail would be retried every tick for the
# life of the gateway, with everything behind it stuck. Failures are counted
# per ENTRY, and after REAP_MAX_ATTEMPTS the entry is set aside on a capped,
# expiring list with a line on the log — not deleted, because "bounded" must
# not quietly mean the same vanishing act by a slower route.
# ---------------------------------------------------------------------------

# Named from the entry's bytes, because an entry has no id of its own and the
# job's id names the wrong thing: a job stranded twice is two entries on two
# processing lists, and the second must not inherit the first one's attempt
# count. Collisions are harmless — two byte-identical entries are two copies of
# one stranding, and serialising them is the correct handling anyway.
def reap_entry_id(raw: str) -> str:
    """A stable, short name for one queue entry, for the two marks below."""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


# The exclusive claim on one entry. Its TTL is a visibility timeout and nothing
# else: the reap itself is milliseconds, so this only ever has to cover a
# reaper that DIED mid-reap, and it is released explicitly on both ordinary
# exits (see reap_processing_list and defer_stranded_entry) so a retry never
# waits for it. Long enough to outlive a tick, and never shorter than a minute,
# because a REAPER_INTERVAL tuned down for a test must not turn this into a
# window two reapers fit inside.
REAP_CLAIM_PREFIX = "comfy:reap:claim:"
REAP_CLAIM_TTL = max(2 * REAPER_INTERVAL, 60)

# How many times one entry may fail to reap before it is set aside, and where
# that count lives. Five: enough that a Redis unavailable across several ticks
# is ridden out rather than given up on, few enough that a genuinely poisonous
# entry stops holding up the list behind it within a minute of production
# ticks. The counter expires on its own so a gateway that dies between the last
# failure and the give-up cannot leave one key per entry behind forever in an
# instance that is deliberately `noeviction`.
REAP_FAILURES_PREFIX = "comfy:reap:failures:"
REAP_MAX_ATTEMPTS = int(os.environ.get("REAP_MAX_ATTEMPTS", "5"))
REAP_FAILURES_TTL = max(REAP_CLAIM_TTL * (REAP_MAX_ATTEMPTS + 2), 3600)

# Where an entry goes when it has been given up on. Capped and expiring for the
# same reason everything else here is: this is a `noeviction` Redis, so a list
# that only ever grows is the outage. PAYLOAD_TTL rather than the shorter
# EVENT_STREAM_TTL because the reader is a person who was paged, not a browser
# — a day is the window in which "a job of mine disappeared last night" is
# still an answerable question.
REAP_UNDELIVERABLE_KEY = "comfy:reap:undeliverable"
REAP_UNDELIVERABLE_MAX = 100
REAP_UNDELIVERABLE_TTL = PAYLOAD_TTL

# END REAP DURABILITY
# ---------------------------------------------------------------------------


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


_showback_script = None


def showback_script():
    """The accrual script, registered lazily against the shared connection —
    the same cheap, cached pattern as fair_enqueue_script() above."""
    global _showback_script

    if _showback_script is None:
        _showback_script = client().register_script(SHOWBACK_ACCRUE_LUA)

    return _showback_script


async def record_gpu_seconds(conn: redis.Redis, job_id: str, destination: str) -> None:
    """
    Bill a job's held-GPU time from the REAPER's side — see BEGIN SHARED
    SHOWBACK for what a GPU second means and why this side's accruals go to
    the excluded bucket rather than to the submitter.

    This is the half an implementation that only instruments the worker
    silently drops, and it is the expensive half: a worker that was SIGKILLed
    mid-generation held a card for everything up to that moment, and
    fail_orphaned_job()/cancel_orphaned_job() below are the only code that
    ever learns those jobs ended. worker_agent.py's finish() is never called
    for them at all.

    Never allowed to fail a reap. A stranded job that is not marked terminal
    is a browser on a progress bar that never moves; a missing line in a
    monthly total is a missing line in a monthly total.
    """
    keys, args = showback_accrue_call(state_key(job_id), destination)

    try:
        await showback_script()(keys=keys, args=args)
    except Exception:  # noqa: BLE001 - the reap must complete regardless
        pass


async def fail_orphaned_job(conn: redis.Redis, job_id: str, error: str) -> None:
    """Terminal. This is the last write on the job, so the TTL is re-armed
    outright rather than NX'd: there is nothing left to compound it."""
    # Before the status, for the same reason as in worker_agent.py's finish():
    # the job's clock is on this same hash and a reader must never be able to
    # see the terminal status without the accounting that goes with it.
    await record_gpu_seconds(conn, job_id, SHOWBACK_TO_EXCLUDED)

    await conn.hset(state_key(job_id), mapping={"status": "failed"})
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL)

    # Nothing will run this workflow now, so the copy beside the queue goes
    # with the job. PAYLOAD_TTL would collect it eventually; this is what keeps
    # the steady state proportional to the queue rather than to a day's
    # throughput, in a Redis that is deliberately `noeviction`.
    await conn.delete(payload_key(job_id))

    await conn.xadd(
        stream_key(job_id),
        {"data": json.dumps({"type": "failed", "data": {"error": error}})},
    )
    await conn.expire(stream_key(job_id), EVENT_STREAM_TTL)


async def cancel_orphaned_job(conn: redis.Redis, job_id: str) -> None:
    """
    Terminal, for a stranded job whose owner had already cancelled it.

    The alternative is not "fail it" but "requeue it", which is the door this
    closes: a job cancelled while queued or dispatched is at a retryable phase,
    so without this its worker's death puts it back on the queue with its
    status reset to 'queued', a second worker picks it up, and a workflow the
    user withdrew is run on a GPU. `cancelled` rather than `failed` because
    that is what actually happened to it and what the user asked for — and it
    is equally terminal, so a tailing browser stops here either way.
    """
    # Same as fail_orphaned_job(): the worker died holding the card, and how
    # long it held it before dying is not something this side can measure
    # honestly. See BEGIN SHARED SHOWBACK, SHOWBACK_TO_EXCLUDED.
    await record_gpu_seconds(conn, job_id, SHOWBACK_TO_EXCLUDED)

    await conn.hset(state_key(job_id), mapping={"status": "cancelled"})
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL)

    # Nothing will run this workflow now, exactly as in fail_orphaned_job().
    await conn.delete(payload_key(job_id))

    await conn.xadd(
        stream_key(job_id),
        {"data": json.dumps({"type": "cancelled", "data": {
            "reason": f"{DEAD_WORKER} — and this job had already been cancelled, "
                      f"so it was not requeued",
        }})},
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
    keys, args = fair_enqueue_call(envelope)
    queued_before, position = await fair_enqueue_script()(keys=keys, args=args)

    if queued_before == QUEUE_FULL:
        # The queue filled between reap_stranded_job()'s early check and
        # here. Hand the retry back — nothing was written — and fail the job
        # the way that check would have.
        await conn.hincrby(state_key(job_id), ATTEMPT_COUNT_FIELD, -1)
        await fail_orphaned_job(
            conn, job_id,
            f"{DEAD_WORKER} before ComfyUI saw the workflow, and the queue was "
            f"full ({position} jobs) so it could not be requeued. Resubmit. {DESCRIBE_HINT}.")
        return

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

    # FENCE FIRST, before anything is read and before anything is decided.
    #
    # This entry is on a processing list whose worker's heartbeat has lapsed,
    # and a lapse is not a death: the only liveness test here is whether one
    # key exists, so a worker paused longer than HEARTBEAT_TTL — an unbounded
    # mkdir on the shared volume, a Redis blip across the whole TTL — is
    # indistinguishable from an OOM kill. Whatever this function goes on to do
    # with the job, the worker that was running it must be able to find out
    # that it happened, or a requeue hands one workflow to two GPUs and a fail
    # is overwritten by a terminal event from a worker that no longer owns the
    # job. worker_agent.py's still_ours() is the other half; see OWNER_FIELD.
    #
    # Before the read rather than after it, because the ordering that matters
    # is with respect to the WORKER, not to this function: a worker that reads
    # its ownership after this line abandons, which is always safe (the job is
    # about to reach a terminal state or be requeued either way), while one
    # that reads it before this line submits and is then racing exactly as it
    # did before the fence existed. Erring towards fencing early costs nothing
    # — the entry is already off the list.
    #
    # arm_state_ttl, because HSET recreates a state hash that expired
    # mid-flight and a recreated key has no TTL at all: an immortal one-field
    # hash per reaped job, in a Redis that is deliberately noeviction.
    await conn.hset(state_key(job_id), OWNER_FIELD, REAPED_OWNER)
    await arm_state_ttl(conn, job_id)

    # Both facts in one round trip, and both read before anything is decided:
    # how far the job got, and whether its owner still wants it.
    phase, cancel_requested = await conn.hmget(
        state_key(job_id), [PHASE_FIELD, CANCEL_REQUESTED_FIELD])

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

    # Retryable by phase is not the same as worth retrying. A job cancelled
    # while it was queued or dispatched is at a retryable phase by
    # construction — the cancel is what stopped it before ComfyUI saw it — so
    # this is the one place a requeue would hand a withdrawn workflow to a
    # second worker and spend a GPU on it. Checked before the payload fetch and
    # before the counter moves: a cancelled job neither needs its workflow back
    # nor spends a retry it will never use.
    #
    # This does not make the worker's own check redundant, and is not made
    # redundant by it: this one closes the death path, and run_job()'s closes
    # the ordinary one, where a job cancelled while queued is popped by a
    # perfectly healthy worker.
    if cancel_requested == "1":
        await cancel_orphaned_job(conn, job_id)
        return

    # A pointer entry names its workflow rather than carrying it, so fetch it
    # back before parsing. Nothing about the retry decision changes: this is
    # only where the bytes live. An entry that carries its own workflow (an
    # older gateway's, or a hand-pushed one) skips this entirely.
    stored = None

    if needs_payload(payload):
        stored = await conn.get(payload_key(job_id))

        if stored is None:
            await fail_orphaned_job(
                conn, job_id,
                f"{DEAD_WORKER}, and the workflow it was running is no longer "
                f"in Redis to requeue. {DESCRIBE_HINT}.")
            return

    try:
        entry = parse_envelope(payload if stored is None else with_workflow(payload, stored))
    except (json.JSONDecodeError, KeyError, TypeError):
        await fail_orphaned_job(
            conn, job_id,
            f"{DEAD_WORKER}, and its queue entry carries no workflow to requeue. {DESCRIBE_HINT}.")
        return

    # Backpressure applies to requeued work exactly as it does to new work:
    # this is the same one physical list, bounded by the same ceiling, and a
    # pool that is dying faster than it drains must not be the one path that
    # gets to grow the queue past it. The bound itself is inside
    # FAIR_ENQUEUE_LUA; this early read is so that a job refused on a full
    # queue has not spent its retry, and requeue_orphaned_job() hands the
    # retry back in the rare case the queue fills in between.
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


async def defer_stranded_entry(conn: redis.Redis, key: str, raw: str,
                               entry_id: str) -> None:
    """
    A reap raised, and the entry it raised on is still on its processing list.

    Two outcomes. Below the cap the entry simply stays where it is and the
    claim is released, so the next tick tries again — the claim's TTL exists
    for a reaper that died, and making an ordinary retry wait for it would cost
    a minute per attempt for nothing.

    At the cap it is set aside. Not deleted: "bounded" must not turn into the
    vanishing this whole mechanism exists to stop, so it is moved onto one
    capped, expiring list an operator holding a job id can read, and a line is
    printed — at that point the job really does have no terminal event, and
    only a person can decide what to do about it.

    Every write here is best-effort, and the failure of any of them leaves the
    entry exactly where it is: on the processing list, for the next tick. The
    one thing this function must never do is lose it.
    """
    failures_key = f"{REAP_FAILURES_PREFIX}{entry_id}"
    claim_key = f"{REAP_CLAIM_PREFIX}{entry_id}"

    try:
        failed = await conn.incr(failures_key)
        await conn.expire(failures_key, REAP_FAILURES_TTL)

        if failed < REAP_MAX_ATTEMPTS:
            log(f"could not reap a stranded entry on {key} (attempt {failed} "
                f"of {REAP_MAX_ATTEMPTS}) — it is still parked there and the "
                f"next tick will try it again")
            await conn.delete(claim_key)
            return

        # The entry this failed on is the tail: the reaper reads only the tail,
        # and the other replica's reaper cannot be holding this one. Confirmed
        # rather than assumed, because a worker whose heartbeat merely LAPSED
        # is not dead and does remove its own entry when it finishes — and if
        # the tail has moved on, the right thing to do is nothing at all and
        # let the next tick look again at whatever is there now.
        if await conn.lindex(key, -1) != raw:
            await conn.delete(claim_key)
            return

        # RIGHT is that tail. One LMOVE rather than LREM-then-LPUSH so the
        # entry is on exactly one list at every instant, this process dying
        # between two commands included.
        await conn.lmove(key, REAP_UNDELIVERABLE_KEY, "RIGHT", "LEFT")
        await conn.ltrim(REAP_UNDELIVERABLE_KEY, 0, REAP_UNDELIVERABLE_MAX - 1)
        await conn.expire(REAP_UNDELIVERABLE_KEY, REAP_UNDELIVERABLE_TTL)
        await conn.delete(failures_key, claim_key)

        log(f"a stranded entry on {key} could not be reaped in "
            f"{REAP_MAX_ATTEMPTS} attempts and has been set aside on "
            f"{REAP_UNDELIVERABLE_KEY} — the job it names has no terminal "
            f"event and nothing will retry it now")
    except Exception:  # noqa: BLE001 - the entry is still on the processing list
        pass


async def reap_processing_list(conn: redis.Redis, key: str) -> None:
    """
    Every stranded entry on one dead worker's processing list, oldest first,
    each removed only once its reap has actually finished.

    See BEGIN REAP DURABILITY above for why the entry is read rather than
    popped, and for what replaces the exclusion RPOP used to provide.
    """
    while True:
        # The tail — the same entry, in the same order, that RPOP took.
        raw = await conn.lindex(key, -1)

        if raw is None:
            return

        entry_id = reap_entry_id(raw)
        claim_key = f"{REAP_CLAIM_PREFIX}{entry_id}"

        # Losing the claim is not an error and not something to work around:
        # the other replica's reaper has this entry, and there is nothing
        # behind it to get on with, because the tail is the only entry this
        # loop ever reads.
        if not await conn.set(claim_key, "1", nx=True, ex=REAP_CLAIM_TTL):
            return

        try:
            await reap_stranded_job(conn, raw)
        except Exception:  # noqa: BLE001 - handled by leaving the entry where it is
            await defer_stranded_entry(conn, key, raw, entry_id)

            # Stop here rather than moving down the list. A reap that just
            # raised is most often a Redis that is unwell, and the rest of
            # this list would raise the same way and spend its attempts doing
            # it. Nothing is lost by waiting: every entry is still parked.
            return

        # Only now, with every write the reap makes landed — the terminal
        # event included — is losing this entry the same as losing nothing.
        await conn.lrem(key, 1, raw)
        await conn.delete(claim_key, f"{REAP_FAILURES_PREFIX}{entry_id}")


async def reap_orphaned_jobs() -> None:
    while True:
        try:
            conn = client()

            async for key in conn.scan_iter(match=f"{PROCESSING_KEY_PREFIX}*"):
                # The suffix is an INCARNATION — one running worker process —
                # and this one line is the whole liveness test, so it is only
                # as true as that. Named from a pod instead, a container
                # restarted inside its pod (restartPolicy: Always, which is how
                # an OOM-killed worker comes back) heartbeats under the id its
                # predecessor died holding, this `continue` fires forever, and
                # the predecessor's stranded job never reaches a terminal state
                # at all. worker_agent.py's BEGIN WORKER IDENTITY is the other
                # half of this; enterprise/test/check-32-worker-restart.py is
                # what fails if either half is undone.
                incarnation = key[len(PROCESSING_KEY_PREFIX):]

                if await conn.exists(f"{WORKER_KEY_PREFIX}{incarnation}"):
                    continue

                await reap_processing_list(conn, key)

        except Exception:  # noqa: BLE001 - see below
            # A Redis blip, and "the next tick retries" is now something this
            # loop has actually arranged rather than a hope: no entry is
            # removed from a processing list before its own reap has returned,
            # so whatever this swallowed, every entry the tick did not finish
            # is still parked where the next one will find it. What it cannot
            # fix is a Redis that stays unreachable — readiness reports that.
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
    # The body is parsed as JSON whatever the client says it is, so say what
    # it must be: a form post, or a cross-site text/plain submission a browser
    # will send without a preflight, must not be able to queue a job.
    # index.html sends exactly this. Parameters (charset) are allowed.
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    if media_type != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")

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

    # Q5's quota breaker, and the only place it is consulted. 429 rather than
    # the 503 above, because this is one caller's ceiling and not the pool's:
    # a second submitter is unaffected, and a client that treats 503 as "the
    # service is down" would be told the wrong thing.
    #
    # HERE, before the job_id exists and before anything is written, because
    # the only refusal worth having is one that leaves nothing behind. A check
    # placed after fair_enqueue_call() would answer the browser with a
    # rejection while a worker spent a GPU on the job anyway — worse than no
    # breaker, since the user is told nothing ran. enterprise/test/
    # check-95-quota-breaker.py asserts exactly that, by counting the writes
    # to comfy:queue rather than by reading the response.
    #
    # quota_refusal() returns None whenever the answer is not knowable, so
    # every failure of this read is a submission that proceeds. See
    # BEGIN QUOTA BREAKER.
    refusal = await quota_refusal(conn, user)

    if refusal:
        raise HTTPException(429, refusal, headers=quota_headers())

    job_id = str(uuid.uuid4())

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
    #
    # NAMING: this is a POSITION (how many jobs ahead of this one), not a
    # DEPTH (how many jobs total). Before this comment the response field and
    # the state hash field below were both spelled "queue_depth", identically
    # to gather_stats()'s real backlog length in /api/stats and /metrics —
    # same name, two different quantities, and the two only ever agree when
    # exactly one submitter is queued. A reader of comfy:job:{id}:state
    # directly (no docs in front of them) had no way to tell which one they
    # were looking at. Renamed here rather than documented in place, because
    # unlike a doc comment the field name travels with the value to every
    # place it is read.
    keys, args = fair_enqueue_call(envelope)
    queued_before, position = await fair_enqueue_script()(keys=keys, args=args)

    # Backpressure. Without it a stuck worker pool turns into an unbounded
    # Redis list, and the first symptom is Redis OOM rather than a slow queue.
    # Decided INSIDE the script, atomically with the insert — an LLEN here
    # followed by the enqueue was a window N simultaneous submits all fit
    # through — and before anything was written, so a refused submit leaves
    # no payload, no state and no stream behind.
    if queued_before == QUEUE_FULL:
        raise HTTPException(503, f"queue is full ({position} jobs). Try again shortly.")

    # phase is seeded here rather than left for the worker to create, so the
    # breadcrumb is never absent on a job that exists. The reaper reads a
    # missing phase as "unknown, do not retry", and the window between a
    # worker's BLMOVE and its first HSET would otherwise land a genuinely
    # pre-execution death in that bucket.
    state = {"status": "queued", "queue_position_at_submit": position,
             PHASE_FIELD: PHASE_QUEUED}

    # envelope["user"], not the raw header: envelope_text() already clamped it
    # to MAX_ENVELOPE_FIELD_CHARS in build_envelope() above, and this value is
    # what SHOWBACK_ACCRUE_LUA later reads back (user_field) to build a Hash
    # FIELD NAME (quota_field()'s f"{SHOWBACK_USER_PREFIX}{user}"). Writing the
    # raw header here put an unbounded, client-supplied string on that path —
    # the showback key-space bound above ("THE FIELD COUNT IS CAPPED") caps
    # the number of fields, not their size, so an uncapped field name is a
    # second, uncounted way to grow that Hash against `noeviction` Redis.
    if envelope["user"]:
        state["user"] = envelope["user"]

    await conn.hset(state_key(job_id), mapping=state)
    await conn.expire(state_key(job_id), EVENT_STREAM_TTL)

    # Seed the stream so a browser that opens the WebSocket before any worker
    # picks the job up sees "queued" instead of an empty blocking read.
    await conn.xadd(stream_key(job_id), {"data": json.dumps({"type": "queued", "data": {"position": position}})})
    await conn.expire(stream_key(job_id), EVENT_STREAM_TTL)

    return {"job_id": job_id, "status": "queued", "queue_position": position}


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

    await conn.hset(state_key(job_id), CANCEL_REQUESTED_FIELD, "1")

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

# Application close codes (the 4000-4999 range the RFC leaves to us), so a
# browser can tell these apart from a gateway that went away (1006).
WS_CLOSE_UNKNOWN_JOB = 4404   # no such job, or it expired
WS_CLOSE_LIFETIME = 4408      # held open for EVENT_STREAM_TTL; reconnect if you still care

# How long one XREAD may block before the loop sends a ping. The ping is
# what notices a client that has gone away; without it a browser tab closed
# mid-generation leaves the coroutine parked forever.
WS_PING_MS = 15_000


def xread_block_ms(deadline: float) -> int | None:
    """How long the next XREAD may block: the ping interval, or the lifetime
    left if that is shorter — None once the lifetime has run out."""
    remaining = deadline - time.monotonic()

    if remaining <= 0:
        return None

    return min(WS_PING_MS, max(1, int(remaining * 1000)))


@app.websocket("/ws/{job_id}")
async def progress(websocket: WebSocket, job_id: str):
    await websocket.accept()

    conn = client()
    key = stream_key(job_id)
    last_id = "0-0"
    redis_errors = 0

    # Two bounds this socket used to lack, both about the same thing: one
    # Redis connection is held for as long as this coroutine runs, on a pool
    # that is now finite (REDIS_MAX_CONNECTIONS).
    #
    # A job id that names nothing is closed, not tailed: /ws/<anything> was
    # accepted and parked forever on an id that never had a job behind it.
    # And a socket on a real job lives at most EVENT_STREAM_TTL — the stream
    # it tails expires that long after its last write, so past that it is
    # blocked on a key that no longer exists. A client that still cares
    # reconnects and replays, exactly as it would after any other close.
    try:
        if not await conn.exists(state_key(job_id)):
            await websocket.close(code=WS_CLOSE_UNKNOWN_JOB, reason="unknown or expired job")
            return

        deadline = time.monotonic() + EVENT_STREAM_TTL

        while True:
            block = xread_block_ms(deadline)

            if block is None:
                await websocket.close(code=WS_CLOSE_LIFETIME,
                                      reason="stream lifetime reached; reconnect to resume")
                return

            try:
                entries = await conn.xread({key: last_id}, count=100, block=block)

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


def is_bare_filename(name: str) -> bool:
    """
    True iff `name` is a single path component: no separator, no NUL, and not
    "." or "..". This is the confinement rule for the REPORTED FILENAME,
    mirroring what workspace_path() already enforces for the reported
    workspace — a run of unsafe characters cannot be un-collapsed into a
    traversal the way a raw "/" or ".." can.

    ComfyUI's own manifest entries never put a separator in `filename`;
    `subfolder` is the only field a save node uses to nest. So a `filename`
    that fails this is not ComfyUI's own shape and is refused outright rather
    than sanitized into something else — see FIX 4a, docs/10-roadmap.md.
    """
    return name not in ("", ".", "..") and "/" not in name and "\\" not in name and "\0" not in name


def output_url(subfolder, filename) -> str | None:
    """The /outputs URL for one reported image, or None if it cannot be named
    safely: the filename and every subfolder component must pass the
    worker's is_bare_filename() (BEGIN SHARED WORKSPACE)."""
    if not isinstance(filename, str) or not isinstance(subfolder, str):
        return None

    components = subfolder.split("/") if subfolder else []

    if not all(is_bare_filename(part) for part in [filename, *components]):
        return None

    return "/".join(["/outputs", *components, filename])


def rewrite_image_urls(event: dict) -> dict:
    """
    ComfyUI reports outputs as {filename, subfolder, type} relative to its own
    output directory. The browser cannot reach the worker, so turn each one into
    a URL this gateway serves off the shared volume.

    With the worker's confinement rule, not without it. The worker applies
    is_bare_filename() and output_subfolder() to the /history manifest it puts
    on the terminal event — but a live `executed` event is forwarded from
    ComfyUI verbatim, and this used to build /outputs/{subfolder}/{filename}
    from it as it stood. A custom node reporting {subfolder: "../../etc"} was
    handed to the browser as a URL under /outputs/../../etc; output_file()
    refuses to serve that, but the refusal was the only thing standing there.
    An entry that is not made of bare components keeps its event and loses
    its URL, which is what the worker does with the same shape.
    """
    images = event.get("data", {}).get("output", {}).get("images")

    if not images:
        return event

    for image in images:
        if not isinstance(image, dict) or "filename" not in image:
            continue

        url = output_url(image.get("subfolder") or "", image["filename"])

        if url is None:
            image.pop("url", None)
        else:
            image["url"] = url

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


async def estimated_wait_seconds(conn: redis.Redis) -> float | None:
    """
    Q6 (docs/10-roadmap.md) — how long has the job about to be served already
    been waiting, derived from the `submitted_at` F2 already reserved on the
    envelope. No second time field, no service-time model.

    Which entry: LMOVE pops `src="RIGHT"` (worker_agent.py's BLMOVE), so the
    tail — index -1 — is always the entry served next, whatever fair queueing
    (BEGIN FAIR QUEUEING above) did to the rest of the list. Its age is a
    fact read off one entry, not a depth-based guess — the same "age of the
    oldest thing in line" a queueing system reports when it has no per-item
    service-time estimate to build a forecast from (the SQS
    ApproximateAgeOfOldestMessage shape).

    That shape holds cleanly for a job on its FIRST attempt: `submitted_at` is
    when this caller actually started waiting, so the age is exactly the
    queue-side latency they have been waiting behind. It does not hold as
    cleanly once requeue_orphaned_job() has touched the entry —
    `submitted_at` is carried over unchanged across a requeue (see that
    function's comment: Q6 should measure from the original submission, not
    restart every time the cluster loses a pod), so a retried job's age also
    counts the time it spent dispatched to the worker that died and the
    reaper's own detection lag (up to HEARTBEAT_TTL + REAPER_INTERVAL). If
    that entry lands at the tail, the gauge reads a real elapsed time, but one
    that overstates how long a caller submitting right now would actually
    wait — it is no longer purely queue-side latency once part of it was
    spent elsewhere.

    An empty queue reads as LINDEX returning None: 0.0, not "unknown" — there
    is nothing to be waiting on. A malformed or pre-F2 entry (no
    `submitted_at` at all) reads as None — "unknown" — rather than a
    fabricated 0, the same "absence is a distinct value from zero" rule
    check-80's `gauge_value()` relies on.

    Scale-to-zero (`workers_registered == 0`) is the case the roadmap item
    calls out by name, and it is deliberately NOT special-cased into
    "unknown" here: unlike a depth × average-service-time forecast — which has
    no service-time sample to build from when nothing has ever finished, and
    would be a genuinely fabricated number at zero workers — this is a
    directly measured elapsed time that stays true, and keeps growing, with
    no worker running at all. Making it absent at zero workers would blind the
    one signal I4's Prometheus scaler needs in order to scale the pool up FROM
    zero in the first place (see docs/10-roadmap.md's deferred_to_cluster note
    for I4's trigger contract) — the exact case an "honest unknown" would be
    most tempting, and least affordable, to report.
    """
    raw = await conn.lindex(QUEUE_KEY, -1)

    if raw is None:
        return 0.0

    try:
        submitted_at = json.loads(raw).get("submitted_at")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None

    if isinstance(submitted_at, bool) or not isinstance(submitted_at, (int, float)):
        return None

    return max(0.0, time.time() - submitted_at)


async def gather_stats() -> dict:
    conn = client()

    # Count heartbeat keys, not a set: keys expire on their own, so a worker
    # that was SIGKILLed stops being counted instead of inflating this forever.
    #
    # One key is one live INCARNATION, so a pod that has just restarted is
    # counted twice until its predecessor's key lapses (at most HEARTBEAT_TTL).
    # Deliberately not deduplicated by pod name: expiry already bounds it, a
    # replaced POD has always over-counted the same way for the same window,
    # and the alternative is this gauge growing an opinion about the shape of
    # an id the reaper is careful to treat as opaque. The consequence is an
    # estimated wait that is briefly optimistic after a crash-restart.
    workers = 0

    async for _ in conn.scan_iter(match=f"{WORKER_KEY_PREFIX}*"):
        workers += 1

    return {
        "queue_depth": await conn.llen(QUEUE_KEY),
        "workers_registered": workers,
        "estimated_wait_seconds": await estimated_wait_seconds(conn),
    }


@app.get("/api/stats")
async def stats():
    return await gather_stats()


# ---------------------------------------------------------------------------
# Showback — who spent the card (docs/10-roadmap.md, Q4)
#
# The report side of the accumulator defined in BEGIN SHARED SHOWBACK, which
# is where the definition of a GPU second, what it over-counts, the reaper
# decision and the three key-space bounds all live. This end only reads.
#
# Deliberately NOT a Prometheus gauge beside comfy_queue_depth. A per-user
# series is one label value per submitter, the submitter is an
# X-Forwarded-User header that is client-supplied under AUTH_MODE=none, and
# unbounded label cardinality is how a monitoring stack is taken down from
# outside. The Redis-side cap bounds this report; nothing bounds a metric's
# label set once it has been scraped. `/metrics` therefore keeps its three
# pool-level gauges and this stays a JSON read.
# ---------------------------------------------------------------------------

# What a caller may name as a period. The read below is an HGETALL of one
# exact key rather than a pattern match, so this is not what stands between a
# caller and somebody else's keys — but a 400 is a more useful answer than an
# empty report for a period that could never have existed.
SHOWBACK_PERIOD_PATTERN = re.compile(r"^\d{4}-\d{2}$")


async def showback_report(period: str) -> dict:
    """
    One period's whole report, read out of the single Hash that holds it.

    Tolerant of anything unexpected in that Hash rather than strict: a field
    this version does not recognise, or a value that is not a number, is
    skipped. A monthly report is read by a person who wants a number today,
    and a newer gateway's extra field is not a reason to give them a 500.
    """
    conn = client()

    totals = await conn.hgetall(showback_key(period))

    users = {}
    buckets = {SHOWBACK_ANONYMOUS_FIELD: 0.0,
               SHOWBACK_EXCLUDED_FIELD: 0.0,
               SHOWBACK_OTHER_FIELD: 0.0}

    for field, value in (totals or {}).items():
        try:
            # Rounded because HINCRBYFLOAT accumulates binary float error over
            # thousands of jobs and a report showing 41.30000000000001 seconds
            # invites the reader to distrust the whole column. Milliseconds is
            # already far finer than anything this measures.
            seconds = round(float(value), 3)
        except (TypeError, ValueError):
            continue

        if field.startswith(SHOWBACK_USER_PREFIX):
            users[field[len(SHOWBACK_USER_PREFIX):]] = seconds
        elif field in buckets:
            buckets[field] = seconds

    # Which periods are still in Redis at all. Cheap — the key space is
    # bounded to SHOWBACK_RETENTION_PERIODS by construction — and it is the
    # thing an operator actually needs to know before a teardown, since the
    # answer after `make down` is "none": see this endpoint's docstring.
    periods = []

    async for key in conn.scan_iter(match=f"{SHOWBACK_KEY_PREFIX}*"):
        periods.append(key[len(SHOWBACK_KEY_PREFIX):])

    return {
        "period": period,
        "users": users,
        "anonymous_gpu_seconds": buckets[SHOWBACK_ANONYMOUS_FIELD],
        "excluded_gpu_seconds": buckets[SHOWBACK_EXCLUDED_FIELD],
        "other_gpu_seconds": buckets[SHOWBACK_OTHER_FIELD],
        # True once the identity cap has sent at least one submitter's seconds
        # into the shared overflow field. Reported rather than hidden: a
        # truncated report that does not say so is a wrong report.
        "truncated": buckets[SHOWBACK_OTHER_FIELD] > 0,
        "periods_available": sorted(periods),
    }


@app.get("/api/showback")
async def showback(period: str | None = None):
    """
    Who spent the card this period, in GPU seconds.

        {"period": "2026-08",
         "users": {"alice@example.com": 4102.5, ...},
         "anonymous_gpu_seconds": 0.0,
         "excluded_gpu_seconds": 0.0,
         "other_gpu_seconds": 0.0,
         "truncated": false,
         "periods_available": ["2026-06", "2026-07", "2026-08"]}

    `period` is a UTC calendar month, defaulting to the current one. A GPU
    second is one second for which a worker held the card on that job — see
    BEGIN SHARED SHOWBACK for the full definition, including what it
    deliberately over-counts. `excluded_gpu_seconds` is time that was really
    spent but is not billed to anybody, and `other_gpu_seconds` is time from
    submitters past the identity cap; the two exist so that there is no
    fourth, silent possibility.

    THIS DOES NOT SURVIVE `make down`. The accumulator is in Redis, Redis's
    PVC is gp3 (`enterprise/manifests/00-redis.yaml`), and gp3 dies with the
    cluster — so on the nightly-teardown habit docs/09 recommends as the
    default, "last month's report" is gone every morning. Capture it before
    the teardown rather than discovering this on the 1st:

        oc exec deploy/comfy-gateway -c gateway -- \\
            curl -s localhost:8000/api/showback > showback-$(date -u +%Y-%m).json

    `make park` is safe — the cluster stays and so does the volume. See
    docs/09-engineering-handoff.md section 5.

    Reads are not caller-scoped, deliberately and on the same terms as Q3's
    output workspaces: under AUTH_MODE=oauth the whole gateway is behind the
    proxy, and under AUTH_MODE=none the identity this would scope by is a
    header the caller sets on themselves, so a guarantee that evaporates in
    one of the two modes is worse than a documented absence. What this exposes
    beyond /api/jobs/<id> is the LIST of submitters, which is why it is a
    report an operator reads rather than something linked from the UI.
    """
    period = period or showback_period(time.time())

    if not SHOWBACK_PERIOD_PATTERN.match(period):
        raise HTTPException(400, "period must be a UTC calendar month, e.g. 2026-08")

    return await showback_report(period)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    """
    Prometheus text format, hand-rolled — three gauges do not justify a client
    library dependency. OpenShift's user-workload monitoring scrapes this via
    the ServiceMonitor enterprise/setup.sh applies, which is what makes
    "queue deeper than N for 30 minutes" an alert instead of a support ticket.
    """
    data = await gather_stats()

    text = (
        "# HELP comfy_queue_depth Jobs waiting in the Redis queue.\n"
        "# TYPE comfy_queue_depth gauge\n"
        f"comfy_queue_depth {data['queue_depth']}\n"
        "# HELP comfy_workers_registered Workers with a live heartbeat.\n"
        "# TYPE comfy_workers_registered gauge\n"
        f"comfy_workers_registered {data['workers_registered']}\n"
    )

    # Absent, not zero, when estimated_wait_seconds() could not read a real
    # submitted_at off the next entry to serve (see its docstring) — an
    # omitted sample is Prometheus's own way of saying "no data" instead of
    # a fabricated number a dashboard would plot as if it meant something.
    wait = data["estimated_wait_seconds"]

    if wait is not None:
        text += (
            "# HELP comfy_estimated_wait_seconds Age of the queue entry "
            "served next, i.e. how long a job submitted right now would "
            "wait behind it.\n"
            "# TYPE comfy_estimated_wait_seconds gauge\n"
            f"comfy_estimated_wait_seconds {wait:.3f}\n"
        )

    return text


@app.get("/", response_class=HTMLResponse)
async def index():
    page = STATIC_ROOT / "index.html"

    if not page.is_file():
        return HTMLResponse("<h1>ComfyUI Gateway</h1><p>API is up. See /docs.</p>")

    return HTMLResponse(page.read_text())
