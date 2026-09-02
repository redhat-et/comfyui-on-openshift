"""
Worker agent: the sidecar-in-the-same-container that connects a locked-down
ComfyUI process to the Redis queue.

Runs alongside ComfyUI in the GPU pod. ComfyUI binds 127.0.0.1 only, so this
agent is the sole path in or out — no user can reach the GPU pod directly, and
the pod needs no Service, no Route, and no ingress rules.

Ten things here that the obvious version of this script gets wrong, each of
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
     it disappear. Both keys are named from the INCARNATION and not from the
     pod — see point 9, which is the whole of what "this worker" has to mean
     for that pairing to be a liveness test at all.

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
     replays a poison workflow. ComfyUI is handed the workflow when the POST is
     WRITTEN, not when it answers, so PHASE_EXECUTING is written before
     submit_prompt() is called rather than after it returns — the round trip is
     the window, and on a loaded or wedged ComfyUI it is not a short one. The
     queue entry cannot carry this — it is a static copy of what hub.py pushed
     and nothing rewrites it.

  7. Treat the submitter's name as hostile input, because it becomes a PATH.
     Each job writes into its own output workspace under OUTPUT_ROOT, named
     from an X-Forwarded-User that is client-supplied whenever AUTH_MODE=none.
     Sanitize into a name that cannot contain a separator, THEN join, THEN
     resolve, THEN verify the result is still inside OUTPUT_ROOT — a resolve
     before the join proves nothing about the joined path. And create those
     directories with an EXPLICIT mode: OpenShift's arbitrary, unstable UID
     means a directory this pod creates is unwritable by the next one unless
     it is group-writable and setgid. See BEGIN OUTPUT WORKSPACES below.

  8. Treat the REPORTED FILENAME as hostile too, not just the submitter's
     name and the workspace it names — it becomes half of a served URL.
     output_subfolder() confines the subfolder ComfyUI reports; before FIX 4a
     (docs/10-roadmap.md) it handed the reported `filename` back unexamined,
     so collect_outputs() concatenated a raw, unconfined string onto an
     otherwise-safe subfolder — confinement in one half of a path is not
     confinement of the path. A `filename` that is not a single bare path
     component (is_bare_filename() below) is refused outright, the same way
     a hostile `filename_prefix` is refused rather than merely sanitized.

  9. Name the heartbeat and the processing list after this INCARNATION, not
     after the pod. Point 5's pair is a claim of the form "the process holding
     THIS list is still alive", and the gateway's reaper tests it by pairing
     the two keys by name — so whatever the name identifies is what "alive"
     ends up meaning. HOSTNAME identifies the POD, and `restartPolicy: Always`
     restarts a container inside its pod: an OOM-killed worker comes back with
     the identity it died with, its first heartbeat answers the reaper's
     question on the dead incarnation's behalf, and the reaper skips that
     incarnation's processing list — not for a while, but for as long as the
     pod keeps restarting. A boot nonce makes the identity name the process,
     which is what the pair was always asserting about. See BEGIN WORKER
     IDENTITY below.

 10. Heartbeat from a thread, and check you still OWN the job before you act
     on it. Both halves answer one failure, and it needs nothing to die:
     point 5's heartbeat used to be refreshed only from inside the two loops,
     and run_job()'s prologue blocks outside both of them — ensure_workspace()
     is an mkdir on a shared RWX volume and is UNBOUNDED, ws.connect() and
     submit_prompt() are 30 seconds each. A heartbeat that merely LAPSED in
     there is indistinguishable from a death to a reaper whose only liveness
     test is whether the key exists, and the job is at PHASE_DISPATCHED by
     construction the whole time — so the reaper requeued a live worker's job
     and the same workflow was handed to ComfyUI twice, which is the precise
     replay the narrow retry exists to prevent. The keepalive (start_heartbeat)
     closes the window: the heartbeat is now a property of the process being
     alive rather than of it being somewhere particular in its own code.

     That shrinks the race; it cannot close it, because a worker slow enough —
     an EFS mkdir that takes minutes, a Redis partition longer than the TTL —
     can still be declared dead while it is alive. So the job carries an OWNER
     (OWNER_FIELD): this incarnation writes its name on the job when it claims
     it, the reaper stamps REAPED_OWNER over it before it does anything else
     to a stranded entry, and this agent re-reads it at the two moments its
     next action would become other people's business — before handing the
     workflow to ComfyUI, and before writing a terminal outcome. Reaped means
     abandon: no submit, no terminal event, no accrual. See still_ours(), and
     claim_executing(): the first of those two reads is folded into the
     write it authorises, because a reap can land between a read and a
     separate write, and did.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import signal
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
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

# TEST-ONLY. Never set this in a manifest. A pause, in seconds, immediately
# before the point-10 claim that turns "dispatched" into "executing" — the
# last instant at which a reap can still be free. The window between reading
# the owner and writing the phase is microseconds in production, and
# enterprise/test/check-36-live-worker-fencing.py scenario C needs the
# gateway's reaper to act INSIDE it, deterministically, to prove that a
# requeue landing there is noticed rather than raced. There is no stub-side
# hook that can widen this window: ComfyUI is not involved until the
# submit, which is after it. Zero, the default, is no pause and no code
# path — the sleep is not called.
TEST_DELAY_BEFORE_CLAIM_S = float(os.environ.get("TEST_DELAY_BEFORE_CLAIM_S", "0"))

# The shared output volume, as this container sees it. It must be the same
# directory start.sh hands ComfyUI as --output-directory — start.sh reads this
# same variable for exactly that reason — because everything below computes
# where an output SHOULD be from paths ComfyUI reports relative to it.
OUTPUT_ROOT = pathlib.Path(os.environ.get("OUTPUT_ROOT", "/output")).resolve()

# ---------------------------------------------------------------------------
# BEGIN WORKER IDENTITY — point 9
#
# Two identities, because the two questions asked of "which worker is this?"
# have different right answers, and the failure that made this a pair was the
# single id being used for both.
#
#   WORKER_ID          what a HUMAN is told. The pod name, so the string in a
#                      failure message ("worker comfy-worker-8s4qd died") is
#                      the one an operator pastes into `oc describe pod` /
#                      `oc logs` — which is precisely what DESCRIBE_HINT in
#                      hub.py sends them off to do. It goes on the job's state
#                      hash and into the `started` event, and nowhere else.
#
#   WORKER_INCARNATION what REDIS is told. The pod name plus a nonce chosen
#                      once, here, at process start — so it names THIS RUNNING
#                      PROCESS rather than the pod that happens to be hosting
#                      it. The heartbeat key and the processing list are both
#                      named from it, and from nothing else.
#
# Why the second one has to exist. The heartbeat/processing pair (point 5) is
# a claim of the form "the process holding THIS list is still alive", and the
# gateway's reaper tests it by pairing the two keys BY NAME: it scans
# comfy:processing:*, takes the suffix, and skips the list if
# comfy:worker:<suffix> exists. So whatever that suffix identifies is what the
# reaper's word "alive" ends up meaning.
#
# HOSTNAME identifies the POD. A container restart — `restartPolicy: Always`,
# which is how an OOM-killed worker comes back, and the common path rather
# than an exotic one — replaces the container inside the same pod and keeps
# it. Named from HOSTNAME alone, the new incarnation's very first heartbeat
# therefore answers the reaper's liveness question on behalf of the DEAD
# incarnation, whose stranded job the reaper then skips for as long as the pod
# keeps restarting: no terminal event (a progress bar that never moves), no
# GPU seconds in either bucket, and a processing entry with no TTL left in a
# `noeviction` Redis. enterprise/test/check-32-worker-restart.py reproduces
# exactly that.
#
# The nonce closes it by construction rather than by procedure: two
# incarnations cannot collide because they do not share a key, so there is no
# startup step to get right, to skip on a Redis blip, or to remove later. The
# cost is one line of readability in a place nobody reads — the KEY names —
# and none at all in the messages, which still carry WORKER_ID.
#
# The separator is not a character a Kubernetes pod name can contain (RFC 1123
# labels are lowercase alphanumerics and '-'), so a display id can always be
# recovered as everything before the first one, and a glob on an identity can
# never straddle two of them. enterprise/test/worker_ids.py relies on that.
# ---------------------------------------------------------------------------
INCARNATION_SEP = "#"

WORKER_ID = os.environ.get("HOSTNAME") or f"worker-{uuid.uuid4().hex[:8]}"
WORKER_INCARNATION = f"{WORKER_ID}{INCARNATION_SEP}{uuid.uuid4().hex[:8]}"

CLIENT_ID = str(uuid.uuid4())

# The heartbeat is how the gateway distinguishes a busy worker from a dead one.
# It is refreshed by a keepalive thread that runs for as long as this process
# does (see start_heartbeat, point 10), so the TTL does not have to be longer
# than the longest thing the job path can block on — which was the bug: it
# used to be refreshed only from the two loops, and the run_job() prologue
# blocks in three places outside both of them.
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "180"))

# How often the keepalive re-arms it. A third of the TTL, so two consecutive
# failed refreshes still leave a third attempt inside the window — heartbeat()
# swallows a Redis blip precisely so a blip is not a death, and that is only
# true if there is another try before the key expires. Floored at a second so
# a very small TTL in a test harness cannot turn this into a busy loop.
HEARTBEAT_REFRESH = max(1.0, HEARTBEAT_TTL / 3.0)

WORKER_KEY = f"comfy:worker:{WORKER_INCARNATION}"

# The job currently being executed lives in this list (moved there from the
# queue by BLMOVE, removed on any terminal state). If this process dies without
# removing it, the gateway's reaper fails the job loudly instead of letting it
# vanish. Key shape is shared with hub.py — change both or neither — and it is
# named from the INCARNATION, not from WORKER_ID: the reaper pairs this key
# with WORKER_KEY above by name, so the two must name the same thing, and that
# thing must be this process. See BEGIN WORKER IDENTITY.
PROCESSING_KEY = f"comfy:processing:{WORKER_INCARNATION}"
# END WORKER IDENTITY


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
# BEGIN OUTPUT WORKSPACES — one directory per submitter (docs/10-roadmap.md, Q3)
#
# Every generation used to land in one flat /output shared by everyone, so two
# users' jobs could and did report the same URL. Each job now writes inside
# OUTPUT_ROOT/<workspace>/, where <workspace> is derived from the envelope's
# `user` field — the X-Forwarded-User the gateway recorded, which F2 reserved
# for exactly this.
#
# WHAT THIS IS AND IS NOT. It is organisation and confinement: whose output is
# whose, and no job can write or name a path outside OUTPUT_ROOT. It is NOT an
# access control boundary — reads are deliberately not caller-scoped, see
# docs/06-enterprise-architecture.md. Under AUTH_MODE=none X-Forwarded-User is
# client-supplied, and hub.py says in three places that it must never be
# treated as authorization; scoping reads on it would be authorization derived
# from an unauthenticated header, i.e. a control that silently evaporates in
# one of the two supported modes. A URL is exactly as guessable as it was
# before this item.
#
# That is also why this code lives HERE rather than in hub.py. The gateway is
# the public attack surface and it never needed to grow a filesystem-naming
# rule: the agent is the only process that both knows who submitted the job
# and touches the volume, and it is already the only path in or out of a GPU
# pod. No envelope field is added either — the workspace is a pure function of
# `user`, so there is nothing for a gateway and a worker of different vintages
# to disagree about on the wire.
#
# THE SANITIZATION RULE: allowlist-slug + hash suffix, and it never rejects a
# username.
#
#   workspace("alice.smith@example.com") -> "alice-smith-example-com-9f2a...."
#   workspace("../../etc/passwd")        -> "etc-passwd-1c07...."
#   workspace("")  / no header           -> "_anonymous"
#
# Every run of characters outside [A-Za-z0-9] collapses to "-", so a separator
# ("/"), a traversal segment (".."), a leading "/" and a NUL cannot survive
# into the name at all — the dangerous shapes are not escaped or rejected,
# they are unrepresentable. The 12 hex characters of sha256(user) then restore
# what the allowlist threw away: two different usernames that slug identically
# ("a/b" and "a-b", or two long names sharing a 40-character prefix) still get
# two different directories. That is why this is not a plain allowlist —
# a plain one silently MERGES two people's outputs, which is the same bug as
# the flat directory, only harder to see.
#
# And it is why it is not a plain hash either. An operator looking at EFS, or
# at a support ticket, has to be able to tell whose directory this is; a tree
# of bare digests is unusable, and the roadmap's own warning is that mangling
# real usernames is as bad as admitting bad ones. An oauth-proxy username is
# an email — "@" and "." are the ordinary case, not the hostile one, and they
# survive here as readable "-" separators with the identity kept whole by the
# digest.
#
# Rejecting was the third option and is the wrong one for the username: a
# rejected submit is a user who cannot use the cluster at all because of how
# their IdP spells their name, and the shape that would have to be rejected
# (anything with a "/" or a "..") is not reliably distinguishable from a
# legitimate exotic username. Rejection IS used, deliberately, one layer down
# — for a filename_prefix that already contains traversal, see
# scoped_prefix() — because that one is a workflow the caller wrote, it has an
# unambiguous safe form, and the caller can fix it.
#
# ORDER OF OPERATIONS, which is the whole security argument: sanitize the
# username into a name that cannot contain a separator, THEN join it onto
# OUTPUT_ROOT, THEN resolve, THEN verify the result is still inside
# OUTPUT_ROOT (workspace_path below). Resolving before joining proves nothing
# about the joined path, and a join whose containment is never re-checked
# trusts the sanitizer completely — the resolve()-then-verify is what catches
# a symlink planted in the output volume, which no amount of string filtering
# can see.
#
# DIRECTORY MODE, which is the half a laptop cannot prove. OpenShift runs each
# pod as an arbitrary high UID with GID 0, and the UID is not stable across
# pods. A directory one worker creates at runtime is therefore read-only to
# the next worker unless it is group-writable, and its children belong to the
# creator's group unless the setgid bit forces GID 0 down the tree. Hence an
# EXPLICIT chmod to 2775 rather than trusting mkdir's mode argument (masked by
# umask, which is 022 in this image and would produce 0755 — group-readable,
# not group-writable) and rather than trusting the local filesystem's
# defaults. See docs/09-engineering-handoff.md §3.
# ---------------------------------------------------------------------------

# Where a job with no authenticated submitter goes. Deliberately not "" (which
# would put anonymous output loose in the shared root again) and deliberately
# not derivable from any username: a real workspace is always
# "<slug>-<12 hex>", and the leading underscore is a character the slug rule
# cannot emit, so "no user" can never alias onto whoever submits next.
ANON_WORKSPACE = "_anonymous"

# Everything outside the allowlist collapses to a single "-".
WORKSPACE_UNSAFE = re.compile(r"[^A-Za-z0-9]+")

# Bounds on the readable half. 40 + 1 + 12 is far below NAME_MAX (255) even
# before hub.py's own 256-character clamp on the header.
MAX_WORKSPACE_SLUG_CHARS = 40
WORKSPACE_DIGEST_CHARS = 12

# setgid + group-writable. Both halves are load-bearing under an arbitrary UID:
# g+w is what lets the NEXT pod write here at all, and setgid is what makes the
# files and subdirectories ComfyUI creates inside inherit GID 0 instead of the
# creating pod's own group.
WORKSPACE_DIR_MODE = 0o2775

# What ComfyUI's own SaveImage defaults to when a workflow leaves it empty.
DEFAULT_FILENAME_PREFIX = "ComfyUI"

# The manifest `type` of a file written under --output-directory. The other
# value ComfyUI emits is "temp" (PreviewImage and friends, written under
# --temp-directory), which never reaches the shared volume at all.
DURABLE_OUTPUT_TYPE = "output"

# Where hub.py serves the shared volume from. The URL half of every manifest
# entry is built under this; the path half is confined by output_subfolder().
OUTPUTS_URL_PREFIX = "/outputs/"

# The one input ComfyUI treats as a path relative to --output-directory. Save
# nodes spell it this way (SaveImage, SaveAnimatedPNG/WEBP and the video nodes
# that copy them), which is what makes rewriting it the whole per-job scoping
# mechanism: ComfyUI is a long-lived process started with ONE fixed
# --output-directory, so there is no per-job flag to set instead.
FILENAME_PREFIX_INPUT = "filename_prefix"


def workspace_name(user: str) -> str:
    """The submitter's directory name. Total, never raises, never rejects."""
    if not user:
        return ANON_WORKSPACE

    slug = WORKSPACE_UNSAFE.sub("-", user).strip("-").lower()[:MAX_WORKSPACE_SLUG_CHARS]
    digest = hashlib.sha256(user.encode("utf-8", "replace")).hexdigest()[:WORKSPACE_DIGEST_CHARS]

    # .strip("-") again: the truncation above can leave a trailing separator.
    # "user" when nothing readable survived — the digest still separates two
    # such names from each other.
    return f"{slug.strip('-') or 'user'}-{digest}"


def workspace_path(workspace: str) -> pathlib.Path:
    """
    Join, resolve, then verify — in that order, and never any other.

    The sanitizer above is the reason this can only ever be a single name, and
    this is the reason it does not have to be trusted: a resolved path that is
    not under OUTPUT_ROOT is refused whatever produced it, including a symlink
    already sitting in the output volume that no string rule could see.
    """
    candidate = (OUTPUT_ROOT / workspace).resolve()

    if not candidate.is_relative_to(OUTPUT_ROOT):
        raise ValueError(f"workspace {workspace!r} resolves outside {OUTPUT_ROOT}")

    return candidate


def set_shared_mode(path: pathlib.Path) -> None:
    """
    Force WORKSPACE_DIR_MODE, explicitly, on a directory we may not own.

    Not a no-op even right after mkdir: mkdir's mode is masked by umask. Not
    fatal if it fails either — a directory created by an EARLIER pod is owned
    by a UID this pod does not have, so chmod is EPERM there, and that is the
    normal steady state rather than an error. It is logged, because a
    workspace that is not group-writable is precisely what makes the next
    pod's write fail with a permission error that reads like a storage fault.

    The read-back is what keeps the cluster's steady state quiet: pod 2 finds
    the mode pod 1 already set, and never issues the chmod that would EPERM.
    It does not converge everywhere — a developer laptop, where this process
    is not a member of the directory's group, has the kernel silently drop
    S_ISGID and this re-chmods once per job, harmlessly. The bit that is not
    observable on a laptop is exactly the bit cluster day is for; see
    docs/09-engineering-handoff.md §3.
    """
    try:
        if stat.S_IMODE(path.stat().st_mode) != WORKSPACE_DIR_MODE:
            os.chmod(path, WORKSPACE_DIR_MODE)

    except OSError as exc:
        log(f"warning: could not set mode {oct(WORKSPACE_DIR_MODE)} on {path}: {exc} "
            f"— another pod's UID may not be able to write here")


def ensure_workspace(path: pathlib.Path) -> None:
    """
    Create every level below OUTPUT_ROOT, each with the explicit mode.

    Only ever for a path that is already resolved and inside OUTPUT_ROOT, and
    that is asserted rather than assumed: this walks `path` one component at
    a time and chmods each one, so a ".." anywhere in it walks UP — the
    caller that once let one through (output_subfolder(), before the
    per-component rule below) had this function chmod OUTPUT_ROOT itself and
    then create a sibling of every workspace at the root. relative_to()
    raises for a path outside OUTPUT_ROOT; the component check is for a path
    that stays inside it only because the ".." is cancelled by what follows.
    """
    relative = path.relative_to(OUTPUT_ROOT)

    if not all(is_bare_filename(part) for part in relative.parts):
        raise ValueError(f"refusing to create {path}: not a resolved path under {OUTPUT_ROOT}")

    current = OUTPUT_ROOT

    for part in relative.parts:
        current = current / part

        try:
            current.mkdir()

        except FileExistsError:
            pass  # another worker pod got here first; that is the normal case

        set_shared_mode(current)


def scoped_prefix(workspace: str, prefix) -> str:
    """
    Move one save node's filename_prefix inside this submitter's workspace.

    ComfyUI treats filename_prefix as a path relative to its output directory
    and will happily create subdirectories from it, so it is a caller-supplied
    path component in the same sense the username is — except that here
    rejection is the right answer. A prefix carrying ".." or an absolute path
    has no legitimate reading: the caller is asking to write outside the place
    the system just decided their output goes, and the job fails with a
    message naming the prefix so they can fix it.

    Idempotent: a prefix already inside this workspace is left alone, so a
    requeued job (Q2) or a workflow copied out of a previous run is not nested
    one level deeper each time. A prefix naming SOMEBODY ELSE'S workspace is
    not special-cased — it is simply prefixed like any other, landing inside
    the submitter's own workspace under a confusing name and outside nobody's.
    """
    text = prefix if isinstance(prefix, str) else ""
    text = text.strip() or DEFAULT_FILENAME_PREFIX

    if "\0" in text or "\\" in text or text.startswith("/"):
        raise ValueError(
            f"{FILENAME_PREFIX_INPUT} {prefix!r} is an absolute or escaped path; "
            f"it must be a plain relative name inside your output workspace")

    parts = [part for part in text.split("/") if part not in ("", ".")]

    if any(part == ".." for part in parts):
        raise ValueError(
            f"{FILENAME_PREFIX_INPUT} {prefix!r} contains a '..' path segment; "
            f"it must be a plain relative name inside your output workspace")

    if not parts:
        parts = [DEFAULT_FILENAME_PREFIX]

    if parts[0] == workspace:
        return "/".join(parts)

    return "/".join([workspace] + parts)


def scope_workflow_outputs(workflow: dict, workspace: str) -> int:
    """
    Rewrite every save node's filename_prefix in place, before submitting.

    This is what makes the common case cost nothing: ComfyUI writes straight
    into the workspace, so there is no copy afterwards. It is best-effort by
    construction — a custom node that hardcodes its own output path, or spells
    the input differently, is not covered — which is why the collection side
    below enforces the same confinement again on what actually came out.
    """
    scoped = 0

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue

        inputs = node.get("inputs")

        if not isinstance(inputs, dict) or FILENAME_PREFIX_INPUT not in inputs:
            continue

        prefix = inputs[FILENAME_PREFIX_INPUT]

        # A list here is a LINK — [node_id, output_index], the prefix coming
        # from another node's output (a string primitive, a text node) —
        # and scoped_prefix() cannot follow it at submit time, so it puts
        # the default in its place. That is the confined answer, and it is
        # also a silent change to what the user's workflow said; say so,
        # naming the node, so a support ticket about "my prefix was ignored"
        # has a line to find.
        if isinstance(prefix, list):
            log(f"warning: node {node_id}: {FILENAME_PREFIX_INPUT} is linked from "
                f"another node ({prefix!r}); replaced by {DEFAULT_FILENAME_PREFIX!r} "
                f"inside workspace {workspace}")

        inputs[FILENAME_PREFIX_INPUT] = scoped_prefix(workspace, prefix)
        scoped += 1

    return scoped


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


def is_confined_subfolder(subfolder: str) -> bool:
    """
    True iff every component of a reported `subfolder` is a bare path
    component by the rule above — so no "..", no ".", no empty run, no
    backslash, no NUL. Empty (no subfolder at all) is the common case and is
    fine.

    Per component, and BEFORE anything is resolved, because resolving the
    whole path and asking "still inside OUTPUT_ROOT?" is the wrong question:
    "../<OUTPUT_ROOT's own name>" resolves to OUTPUT_ROOT and passes it, and
    the destination the file is then moved to is built by joining the raw
    string under the workspace — where the ".." walks back out of it.
    ComfyUI's own save nodes normalise the subfolder they report, so one that
    fails this is not ComfyUI's shape and is refused, not repaired.
    """
    return all(is_bare_filename(part) for part in subfolder.split("/")) if subfolder else True


# The shape of a workspace directory name, for telling "a file some node left
# in the shared root" apart from "a file inside somebody else's workspace".
# Mirrors what workspace_name() emits: a lowercase slug, "-", twelve hex
# digits — or the anonymous workspace. A shared-root subfolder a custom node
# hardcodes ("video/", "upscaled/") does not look like this.
WORKSPACE_SHAPE = re.compile(r"[a-z0-9][a-z0-9-]*-[0-9a-f]{%d}" % WORKSPACE_DIGEST_CHARS)


def looks_like_workspace(name: str) -> bool:
    return name == ANON_WORKSPACE or WORKSPACE_SHAPE.fullmatch(name) is not None


def output_subfolder(workspace: str, subfolder: str, filename: str) -> tuple[str, str]:
    """
    Where this output actually lives, relative to OUTPUT_ROOT, and the bare
    filename to serve it under — moving the file into the workspace if a node
    put it somewhere else.

    Both halves of what a URL gets built from are confined HERE, on the same
    footing — not just subfolder. Before FIX 4a (docs/10-roadmap.md) this
    function validated and rewrote only subfolder and handed filename back
    unexamined; a caller building a URL by concatenating the two (as
    collect_outputs() below does) then reproduced whatever traversal was in
    the raw filename verbatim, subfolder confinement notwithstanding. A
    filename that is not already a bare name (is_bare_filename() above) is
    refused — returned as "" — rather than served from wherever it resolves
    to; the caller is required to treat "" as "do not build a URL for this
    output at all" (collect_outputs() does).

    The prefix rewrite above covers save nodes that honour filename_prefix.
    This covers the rest, and it is the half that makes "every output of this
    job is inside its submitter's workspace" a property rather than a hope:
    the agent is the only path out of the pod, so it is the only place that
    can enforce it on outputs it did not get to name.
    """
    if not filename:
        return subfolder, ""

    if not is_bare_filename(filename):
        log(f"warning: output filename {filename!r} is not a bare filename — not served")
        return workspace, ""

    if not is_confined_subfolder(subfolder):
        log(f"warning: output subfolder {subfolder!r} is not a plain relative path — not served")
        return workspace, ""

    try:
        ws_root = workspace_path(workspace)
        reported = (OUTPUT_ROOT / subfolder / filename).resolve()

    except (ValueError, OSError) as exc:
        log(f"warning: cannot place {subfolder}/{filename} in workspace {workspace}: {exc}")
        return workspace, ""

    if not reported.is_relative_to(OUTPUT_ROOT):
        # ComfyUI does not do this; a custom node with a hardcoded path could.
        # Never name it in a URL — hub.py would refuse to serve it anyway, and
        # this way the refusal is not the only thing standing there.
        log(f"warning: output {subfolder}/{filename} resolves outside {OUTPUT_ROOT} — not served")
        return workspace, ""

    if reported.is_relative_to(ws_root):
        return subfolder, filename  # already scoped, by the prefix rewrite

    # Inside OUTPUT_ROOT, outside this workspace — the move below exists for
    # exactly that, so a custom node that hardcodes its own subfolder still
    # has its output served from the right place. But "not inside mine" is
    # satisfied by "inside somebody else's", and the move is an os.replace:
    # a manifest naming a file in another submitter's workspace (any node
    # that lets the workflow choose its subfolder will do, and workspace
    # names derive from usernames /api/showback lists) took that user's
    # finished file, put it under this submitter's directory and handed
    # this submitter a URL for it — a cross-user read that is also a
    # deletion. No legitimate job writes there: scoped_prefix() puts a
    # prefix naming another workspace INSIDE the submitter's own. Refused,
    # and the file is not touched.
    inside = reported.relative_to(OUTPUT_ROOT).parts

    if len(inside) > 1 and looks_like_workspace(inside[0]):
        log(f"warning: output {subfolder}/{filename} is inside workspace {inside[0]!r}, "
            f"not {workspace!r} — not moved, not served")
        return workspace, ""

    if not reported.exists():
        # A node reported a file it did not write, or something else removed
        # it. There is nothing to move and nothing to serve either way, so
        # name the place it belonged rather than the shared root it did not.
        log(f"note: output {subfolder}/{filename} is not on disk — "
            f"reporting it under workspace {workspace}")
        return f"{workspace}/{subfolder}".rstrip("/"), filename

    destination = ws_root / subfolder / filename

    try:
        ensure_workspace(destination.parent)
        os.replace(reported, destination)

    except OSError as exc:
        log(f"warning: could not move {reported} into {ws_root}: {exc}")

        # Still inside OUTPUT_ROOT, just not namespaced. Serving it from where
        # it really is beats reporting a URL for a file that is not there.
        if reported.exists():
            return subfolder, filename

    return f"{workspace}/{subfolder}".rstrip("/"), filename

# END OUTPUT WORKSPACES
# ---------------------------------------------------------------------------


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


# Set to end the keepalive below. Also what makes the thread stop BEFORE main()
# deletes WORKER_KEY on the way out: a refresh racing that delete would put the
# key back with a full TTL and leave a worker that has exited looking alive to
# the reaper for HEARTBEAT_TTL — which is the same false-liveness bug as point
# 9's, arriving from the other side.
_heartbeat_stop = threading.Event()


def start_heartbeat(conn: redis.Redis) -> threading.Thread:
    """
    Keep this worker's heartbeat armed for as long as the PROCESS is alive
    (point 10), rather than for as long as it happens to be inside one of the
    two loops that used to refresh it.

    The loops cannot do this job. run_job()'s prologue blocks in three places
    outside both of them — ensure_workspace() is an mkdir on a shared RWX
    volume and has no timeout at all, ws.connect() and submit_prompt() have 30
    seconds each — and the gateway's reaper reads a lapsed heartbeat as a
    death, at a phase that is retryable by construction. The result was a live
    worker's job requeued underneath it and one workflow handed to ComfyUI
    twice (enterprise/test/check-36-live-worker-fencing.py, scenario A).

    A thread rather than a deadline check inside the blocking calls, because
    the unbounded one is a filesystem call with nowhere to put a check. It is
    a daemon so it can never hold up an exit, and it shares the agent's
    connection: redis-py hands each command its own connection out of the
    pool, so this cannot interleave with the BLMOVE the main loop parks in.

    This does not make the fence redundant. A refresh that cannot run — the
    process stopped by the kernel, Redis unreachable for longer than the TTL —
    still looks exactly like a death from the gateway's side. It narrows the
    window; still_ours() is what makes being wrong about it safe.
    """
    def beat() -> None:
        while not _heartbeat_stop.wait(HEARTBEAT_REFRESH):
            heartbeat(conn)

    thread = threading.Thread(target=beat, name="heartbeat", daemon=True)
    thread.start()

    return thread


def still_ours(conn: redis.Redis, job_id: str) -> bool:
    """
    Is this job still mine to act on? See point 10, and OWNER_FIELD.

    Read immediately before writing a terminal outcome — the second of the
    two acts a reaped worker must not perform. The first, handing the
    workflow to ComfyUI, is guarded by claim_executing() below, which asks
    the same question inside the write it protects rather than before it.
    Everything between the two is this process talking to its own ComfyUI
    about work that has already started, which a second opinion from Redis
    cannot undo.

    Unowned is OURS. A hash written before this field existed, or recreated by
    an HSET after expiring, carries no owner — and a missing field that
    suppressed a terminal event would strand exactly the jobs this whole
    mechanism exists to stop stranding. claim_executing() applies the same
    rule, in Lua, for the same reason.
    """
    return conn.hget(state_key(job_id), OWNER_FIELD) in (None, "", WORKER_INCARNATION)


# The claim: "executing" is written only if this incarnation still owns the
# job, in one script, so nothing can land between the check and the write.
#
# This used to be still_ours() followed by an HSET — a read, then separately
# a write that the read had supposedly authorised. Between them the reaper
# could requeue the job (it stamps REAPED_OWNER first, then writes
# phase=queued and pushes the entry back); the HSET then wrote "executing"
# over the retry's "queued" and the submit that followed handed ComfyUI the
# workflow beside the retry's own — the exact replay the fence exists to
# prevent, arriving one line below it. The window was microseconds, which is
# why it survived every check that parks a worker in front of the fence
# (check-36 A/B) and why check-36 C needs TEST_DELAY_BEFORE_CLAIM_S to
# reach it.
#
# Absence is not a fence, here as in still_ours(): a hash with no owner
# field is claimed. The EXPIRE is inside the script for the reason
# arm_state_ttl() gives in hub.py — an HSET recreates an expired hash with
# no TTL — and because the caller used to issue it as a second round trip.
CLAIM_EXECUTING_LUA = """
local owner = redis.call('HGET', KEYS[1], ARGV[1])

if owner and owner ~= '' and owner ~= ARGV[2] then
  return 0
end

redis.call('HSET', KEYS[1], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))

return 1
"""

_claim_script = None


def claim_executing(conn: redis.Redis, job_id: str) -> bool:
    """
    Write PHASE_EXECUTING iff OWNER_FIELD is still this incarnation (or
    absent). True means the claim held and ComfyUI may be handed the
    workflow; False means somebody reaped this job in the meantime, and the
    caller abandons exactly as it would have on a failed still_ours().
    """
    global _claim_script

    if _claim_script is None:
        _claim_script = conn.register_script(CLAIM_EXECUTING_LUA)

    held = _claim_script(
        keys=[state_key(job_id)],
        args=[OWNER_FIELD, WORKER_INCARNATION, PHASE_FIELD, PHASE_EXECUTING, EVENT_STREAM_TTL],
    )

    return int(held) == 1


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

    # Whose output this is (docs/10-roadmap.md, Q3). Derived from the
    # envelope's `user` — the only identity that reaches this pod — and from
    # nothing about this worker or this attempt, so a requeued job lands in
    # the same place on whichever worker picks it up next.
    workspace = workspace_name(job["user"])

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
            # Point 10: and this incarnation is the one holding it. Written in
            # the same HSET as the phase for the same reason the phase is
            # written in the same HSET as the status — the two are read
            # together, by this worker before it submits and before it reports,
            # and a job that looked claimed with no owner would be read as
            # unowned, which is the fence's fail-open direction.
            OWNER_FIELD: WORKER_INCARNATION,
            # The GPU clock starts here (docs/10-roadmap.md, Q4), in the same
            # HSET as `running` and for a stricter version of the same reason:
            # this worker holds the card from this instant, and a clock
            # started any later would silently stop charging for the
            # checkpoint load — the most expensive part of a cold job. It is
            # also the CLAIM on this job's billing, removed by whichever
            # terminal path bills it first, so a worker finishing a job the
            # reaper has already given up on cannot bill it twice. See
            # BEGIN SHARED SHOWBACK.
            STARTED_AT_FIELD: repr(time.time()),
        },
    )
    conn.expire(state_key(job_id), EVENT_STREAM_TTL)
    emit(conn, job_id, {"type": "started", "data": {"worker": WORKER_ID}})

    # Before anything is submitted, and before the WebSocket is even opened:
    # make the workspace exist with the right mode, and point every save node
    # at it. A workflow whose filename_prefix already tries to escape raises
    # here and fails the job with a message naming the prefix — the caller
    # never reaches ComfyUI, and no directory is created for a request that
    # was going to be refused.
    scoped = scope_workflow_outputs(workflow, workspace)
    ensure_workspace(workspace_path(workspace))
    log(f"job {job_id} -> workspace {workspace} ({scoped} save node(s) rewritten)")

    # Connect first, submit second. See point 1 in the module docstring.
    ws = websocket.WebSocket()
    ws.connect(f"ws://{COMFY_ADDR}/ws?clientId={CLIENT_ID}", timeout=30)
    ws.settimeout(RECV_TIMEOUT)

    try:
        # The last moment a cancel can still be free. /api/jobs/<id>/cancel is
        # cooperative and promises that "a job that has not been picked up yet
        # never starts" — which is only true if somebody checks between the pop
        # and the POST. Everything above this line is reversible; the next line
        # is not. Without this, a job cancelled while it sat in the queue is
        # submitted anyway and the cancel becomes an /interrupt mid-sampler,
        # i.e. GPU time spent on work the user withdrew before it began.
        if cancelled(conn, job_id):
            finish(conn, job_id, "cancelled", {})
            return

        # And the last moment a reap can still be free (point 10). Everything
        # above this line happened inside this process; the next line spends a
        # GPU. If the gateway decided this worker was dead while it was in the
        # prologue above — an unbounded mkdir on the shared volume, a slow
        # connect, a Redis pause longer than HEARTBEAT_TTL — the job has
        # already been requeued and another worker either has it or is about to.
        # Submitting anyway is the replay: one workflow on two cards, two
        # terminal events on a stream the browser closed at the first, and two
        # accruals against one job_id.
        #
        # Point 6, the other half, in the same write: ComfyUI is about to be
        # handed the workflow, and "executing" is written BEFORE the POST
        # rather than after it returns, because ComfyUI has the prompt from
        # the moment the request is written — the round trip is a window in
        # which a death would otherwise be read as "nothing ran" and replayed
        # onto a second GPU, which is the one thing the narrow retry exists to
        # prevent. The cost of being early is a job that dies inside the POST
        # and is not retried; the cost of being late is a poison workflow
        # walking the pool, so this errs early on purpose. Written BEFORE the
        # `accepted` event for the same reason: this is the flag that decides
        # a death's fate, and the event is only a thing to look at. From here
        # on, every death this job suffers is terminal.
        #
        # One compare-and-set rather than a read and then a write, because a
        # reap can land between the two (see CLAIM_EXECUTING_LUA). Abandon
        # rather than fail when it does: the retry is running and will report
        # for both of us, so a `failed` here would close a browser that is
        # watching work which is going to succeed. Nothing is emitted, nothing
        # is billed, and main()'s LREM already declines to delete a payload
        # the reaper may have re-queued behind it.
        if TEST_DELAY_BEFORE_CLAIM_S:
            time.sleep(TEST_DELAY_BEFORE_CLAIM_S)  # see TEST_DELAY_BEFORE_CLAIM_S

        if not claim_executing(conn, job_id):
            log(f"job {job_id}: reaped while this worker was still alive — "
                f"another attempt owns it now, abandoning before submit "
                f"(incarnation {WORKER_INCARNATION})")
            return

        prompt_id = submit_prompt(workflow)

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

                # A server-side close surfaces as an empty frame rather than an
                # exception, and "" is a str, so it slips past the binary-frame
                # guard below, fails to parse, and hits `continue`. That one
                # iteration is cheap; what is not is what follows it. Nothing
                # on the wire is coming after an ordinary close, so every
                # later ws.recv() just blocks for RECV_TIMEOUT and lands in
                # the WebSocketTimeoutException branch below -- which is
                # already correct in isolation (it re-checks /history and
                # keeps waiting), so this loop is paced, not spinning. The
                # cost is JOB_TIMEOUT of that pacing to notice a connection
                # that is never coming back, while holding a GPU. Treat the
                # empty frame as what it is instead of waiting that out.
                if raw == "":
                    raise websocket.WebSocketConnectionClosedException(
                        "ComfyUI closed the connection")

            except (websocket.WebSocketConnectionClosedException,
                    ConnectionResetError, OSError) as exc:
                # ComfyUI's socket is gone mid-job. Do not wait out JOB_TIMEOUT
                # for it: ask /history once, because the prompt may have landed
                # in the instant before the process went, and otherwise fail now
                # with a reason that names what happened.
                #
                # In a pod this is usually moot -- start.sh waits on both
                # children, so a dead ComfyUI ends the pod and the gateway's
                # reaper takes the job. The case this covers is a socket that
                # closes while ComfyUI is still alive, where the deadline was
                # otherwise 1800 seconds of a card doing nothing.
                if prompt_finished(prompt_id):
                    finish(conn, job_id, "completed", collect_outputs(prompt_id, workspace))
                    return

                raise RuntimeError(
                    f"ComfyUI closed the connection mid-job and /history does "
                    f"not know prompt {prompt_id} ({exc})") from exc

            except websocket.WebSocketTimeoutException:
                # No news for RECV_TIMEOUT. Confirm ComfyUI is still alive and
                # that our prompt has not quietly completed while we were not
                # looking, then keep waiting.
                if prompt_finished(prompt_id):
                    finish(conn, job_id, "completed", collect_outputs(prompt_id, workspace))
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
                finish(conn, job_id, "completed", collect_outputs(prompt_id, workspace))
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


def collect_outputs(prompt_id: str, workspace: str) -> dict:
    """
    Pull the output manifest from /history. The WebSocket 'executed' events also
    carry it, but history is authoritative and survives a reconnect.

    Each entry's subfolder AND filename are resolved through output_subfolder()
    first (docs/10-roadmap.md, Q3 / FIX 4a), so what the browser is handed is
    where the file is inside this submitter's workspace — not where a save
    node happened to put it, and not whatever string ComfyUI (or a hostile
    custom node) put in the manifest's `filename` field. Before FIX 4a this
    loop confined subfolder but then concatenated the RAW filename into the
    URL verbatim, so a manifest entry like {subfolder: "", filename:
    "../../OUTSIDE/secret.txt"} produced a URL that resolved outside
    OUTPUT_ROOT even though output_subfolder() itself never named a path
    outside it — a confinement gap in the URL, not in the filesystem
    (hub.py's /outputs endpoint independently refuses to serve the result).
    output_subfolder() returning "" for filename now means "refused, do not
    build a URL for this output at all", and the assert below is the same
    invariant checked again, independently of how the URL was built — a
    manifest entry that fails it is a bug in this function, not in the
    filename.
    """
    try:
        entry = comfy_get(f"/history/{prompt_id}").get(prompt_id, {})
    except Exception as exc:  # noqa: BLE001
        return {"warning": f"could not read history: {exc}"}

    images = []

    for node_output in (entry.get("outputs") or {}).values():
        for image in node_output.get("images", []):
            # Only what was written to the shared volume. ComfyUI's manifest
            # carries every file a node reported, and a PreviewImage node
            # reports its files exactly like SaveImage does except for
            # `type`: "temp" instead of "output", written under
            # --temp-directory (/tmp in the pod, start.sh) rather than under
            # --output-directory. A URL built for one of those is a 404 by
            # construction, and not a harmless one: it resolves into the
            # shared volume at a path a later save on another worker could
            # legitimately fill with somebody else's image.
            if image.get("type") != DURABLE_OUTPUT_TYPE:
                continue

            raw_filename = image.get("filename")
            subfolder, filename = output_subfolder(workspace, image.get("subfolder") or "", raw_filename)

            if not filename:
                continue  # refused as unsafe by output_subfolder() — never named in a URL

            # Joined, then percent-encoded. A filename is caller-chosen text
            # (it starts as a filename_prefix), so a space, a "#" or a "%" in
            # it is ordinary — and each breaks a URL differently: a space is
            # refused on the request line, "#" starts a fragment and drops
            # everything after it, "%" is a malformed escape. quote() with
            # "/" kept as the separator, which is safe because both halves
            # are already bare components (no "/" inside a component to
            # confuse with a separator). The gateway decodes the path before
            # it resolves it, so the file is found under its real name.
            relative = "/".join(part for part in (subfolder, filename) if part)
            url = OUTPUTS_URL_PREFIX + urllib.parse.quote(relative, safe="/")

            # Defense in depth: output_subfolder() should already guarantee
            # this, but the URL is what actually reaches the browser, so it
            # is what gets checked, independently of the function that built
            # it — decoded first, because the decoded path is the one the
            # gateway will resolve. A violation here means the confinement
            # contract itself broke, so it is loud rather than swallowed into
            # a "warning" log line the way a hostile INPUT is above.
            resolved = (OUTPUT_ROOT / urllib.parse.unquote(url[len(OUTPUTS_URL_PREFIX):])).resolve()
            if not resolved.is_relative_to(OUTPUT_ROOT):
                raise RuntimeError(
                    f"confinement invariant broken: {url!r} (from subfolder={subfolder!r}, "
                    f"filename={filename!r}, raw_filename={raw_filename!r}) resolves to "
                    f"{resolved}, outside {OUTPUT_ROOT}")

            images.append(
                {
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": image.get("type"),
                    "url": url,
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


_showback_script = None


def showback_script(conn: redis.Redis):
    """
    The accrual script, registered once per process. register_script() only
    computes a SHA1 client-side — no I/O — so this is cheap, and it is cached
    anyway so there is exactly one Script object per agent.
    """
    global _showback_script

    if _showback_script is None:
        _showback_script = conn.register_script(SHOWBACK_ACCRUE_LUA)

    return _showback_script


def record_gpu_seconds(conn: redis.Redis, job_id: str, destination: str) -> None:
    """
    Bill this job's held-GPU time — see BEGIN SHARED SHOWBACK for what a GPU
    second means here and where the number goes.

    This is the worker's own terminal path, so both ends of the interval were
    written by this process and the time is billed to the submitter.

    It may never fail a job. The accounting is a report; the generation is the
    product, and a Redis blip that costs one line of a monthly total must not
    also cost the user a job that actually completed. The failure is logged
    rather than swallowed silently, because a total that is quietly short is
    exactly the kind of wrong a monthly report does not survive.
    """
    keys, args = showback_accrue_call(state_key(job_id), destination)

    try:
        field, seconds = showback_script(conn)(keys=keys, args=args)
    except Exception as exc:  # noqa: BLE001 - never terminal for the job
        log(f"job {job_id}: could not record GPU seconds ({exc})")
        return

    # An empty field is "there was no clock on this job": it never started, or
    # some other terminal path already claimed it. Both are normal.
    if field:
        log(f"job {job_id} -> {seconds}s of GPU time recorded against {field}")


def finish(conn: redis.Redis, job_id: str, status: str, payload: dict) -> None:
    # The other half of point 10's fence, and the reason it is here rather than
    # at each of finish()'s seven call sites: this is the single door every
    # terminal outcome leaves by, so one check covers completed, failed and
    # cancelled alike — including the failures main() reports for a run_job()
    # that raised.
    #
    # A job this worker no longer owns has already been reported by whoever
    # took it: the reaper wrote a terminal event, or the retry it queued will.
    # Writing a second outcome over that is what the browser saw as a
    # completion arriving after its stream had closed. Not billed either — the
    # accrual below claims the clock, and the attempt that actually holds the
    # card is entitled to it.
    if not still_ours(conn, job_id):
        log(f"job {job_id}: discarding '{status}' — this job was reaped and "
            f"belongs to another attempt now")
        return

    # Before the terminal status, not after it: the job's clock lives on the
    # same state hash, and anything that reads `status` and then reads the
    # report must not be able to see the first without the second. Every
    # status goes through here — completed, failed and cancelled alike — which
    # is the point: a job that failed after twenty minutes held the card for
    # twenty minutes.
    record_gpu_seconds(conn, job_id, SHOWBACK_TO_SUBMITTER)

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

    # From here the heartbeat is somebody's full-time job. Started before the
    # poll loop rather than inside it, because the window it exists to cover is
    # inside run_job() and neither loop is running then (point 10).
    keepalive = start_heartbeat(conn)

    # Both identities, once, at the one moment an operator reading a restart
    # loop needs to tell two incarnations of one pod apart: same WORKER_ID,
    # different key suffix. See BEGIN WORKER IDENTITY.
    log(f"worker {WORKER_ID} (incarnation {WORKER_INCARNATION}) "
        f"ready, polling {QUEUE_KEY}")

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

            job_id = None

            try:
                try:
                    record = json.loads(raw)

                    # A pointer entry names its workflow instead of carrying
                    # it; fetch it back and rejoin before parsing. An entry
                    # that carries its own workflow — a pre-F2 payload, an
                    # older gateway's, a hand-pushed one — skips this and is
                    # parsed exactly as it always was.
                    if needs_payload(record):
                        stored = conn.get(payload_key(record["job_id"]))

                        if stored is None:
                            # The one failure this split adds, and it is
                            # reported rather than dropped: the job exists, its
                            # owner is waiting on the stream, and silence here
                            # would be a bar that never moves.
                            log(f"job {record['job_id']}: workflow missing from Redis")
                            finish(conn, record["job_id"], "failed",
                                   {"error": "the workflow for this job is no longer in "
                                             "Redis — it expired or was removed before a "
                                             "worker could run it. Resubmit."})
                            continue

                        record = with_workflow(record, stored)

                    # Tolerant by contract: a pre-F2 {"job_id", "workflow"}
                    # entry parses as version 1 with every reserved field
                    # defaulted, and a key from a newer gateway is carried and
                    # not read. Only a missing job_id or workflow is malformed.
                    job = parse_envelope(record)
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
                removed = 0

                try:
                    removed = conn.lrem(PROCESSING_KEY, 1, raw)
                except Exception:  # noqa: BLE001
                    pass

                # And drop the workflow stored beside the queue with it, which
                # is what keeps live payloads proportional to the queue rather
                # than to a day of throughput (PAYLOAD_TTL is only the
                # backstop). Conditional on the LREM having actually removed
                # something: a zero means this worker's heartbeat lapsed and
                # the reaper took the entry first, in which case it may already
                # have requeued the job and rewritten this exact key, and
                # deleting it here would strand the retry.
                if removed and job_id is not None:
                    try:
                        conn.delete(payload_key(job_id))
                    except Exception:  # noqa: BLE001
                        pass

    finally:
        # Stop the keepalive BEFORE deregistering, and wait for it: a refresh
        # that lands after the delete re-creates the key with a full TTL, and
        # a worker that has exited then reads as alive to the reaper for
        # HEARTBEAT_TTL — with its processing list skipped for exactly that
        # long. The join is bounded because this is an exit path; a keepalive
        # wedged in a Redis call is a daemon thread and dies with the process.
        _heartbeat_stop.set()
        keepalive.join(timeout=HEARTBEAT_REFRESH + 5)

        try:
            conn.delete(WORKER_KEY)
        except Exception:  # noqa: BLE001
            pass

    log("exiting cleanly")

    return 0


if __name__ == "__main__":
    sys.exit(main())
