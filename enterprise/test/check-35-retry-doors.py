"""The two doors into a replay that Q2's narrow retry exists to shut.

check-30-sigkill.py proves the retry decision is taken from the `phase`
breadcrumb. This one proves the breadcrumb is written EARLY enough to be true,
and that a job the user already cancelled is never the thing being requeued.
Both are the same property from two sides: a workflow is never replayed onto a
second GPU.

A. THE BREADCRUMB MUST BE DURABLE BEFORE THE POST, NOT AFTER IT.
   ComfyUI receives a workflow when the POST is written, not when it answers.
   A breadcrumb written after submit_prompt() RETURNS therefore leaves a
   window — the whole round trip, and every retryable millisecond of a slow or
   wedged ComfyUI — in which ComfyUI holds the prompt and the record still
   says the job may be retried. A worker death in that window is a replay of a
   workflow that already ran, which is exactly the poison-pill case
   docs/10-roadmap.md's "Decisions already made" narrows retry to prevent, and
   docs/09-engineering-handoff.md section 3 pins as "the phase breadcrumb is
   written BEFORE the transition it describes".

   The stub makes this an ordering assertion rather than a timing one: it
   records every workflow handed to it the instant POST /prompt is entered,
   before the __slow_prompt__ stall. So this check waits until ComfyUI
   provably HAS the workflow, and only then reads the breadcrumb — which must
   already say "executing". A worker killed there must get one terminal
   `failed`, never a retry.

B. A CANCELLED JOB IS NEVER REQUEUED, AND NEVER SUBMITTED.
   B1 is the reaper's door: a job cancelled while queued or dispatched, whose
   worker then dies, must not be put back on the queue with its status reset
   to 'queued'. Requeueing it hands a workflow the user has already abandoned
   to the next worker.
   B2 is the worker's own door, and the endpoint's documented contract: "a job
   that has not been picked up yet never starts" (hub.py, cancel()). A worker
   that pops a cancelled job must not hand it to ComfyUI at all — measured
   directly, by asking the stub whether the workflow ever arrived.

Runs after check-30 and inherits its conventions: run.sh's shrunk
HEARTBEAT_TTL / REAPER_INTERVAL make worker-death assertions resolve in
seconds, argv[1] is a live agent's pid, and this check may leave it dead
(scenario A kills it) because run.sh starts a fresh one before the next check.
"""
import json, os, signal, sys, time, uuid

from harness import (
    GW, QUEUE_KEY, check, comfy_saw, connect_redis, failures, payload_key,
    start_agent as _start_agent, state_key, stop_agent, stream_key, wait_for,
)

r = connect_redis()


def probe_workflow(*markers):
    """A workflow carrying a node key nothing else in the suite uses, so
    "did ComfyUI ever see THIS workflow" has a yes/no answer."""
    probe = f"probe-{uuid.uuid4().hex[:8]}"
    workflow = {marker: {"class_type": "KSampler"} for marker in markers}
    workflow[probe] = {"class_type": "KSampler"}

    return probe, workflow


def event_types(job_id):
    """Every event published on the job's stream so far, read straight off
    Redis rather than through a socket: this check asserts about events that
    must NOT exist, and a WebSocket read can only ever time out waiting for
    one of those."""
    return [json.loads(fields["data"]).get("type")
            for _id, fields in r.xrange(stream_key(job_id))]


def queued_raw(job_id):
    """The job's own entry on comfy:queue, if it is on it."""
    for raw in r.lrange(QUEUE_KEY, 0, -1):
        try:
            if json.loads(raw).get("job_id") == job_id:
                return raw
        except (json.JSONDecodeError, TypeError):
            continue

    return None


def start_extra_agent(hostname, timeout=30):
    """A worker agent of this check's own, identified by a fixed HOSTNAME so
    its heartbeat key is findable (worker_ids.py, harness.start_agent's
    ready="heartbeat" default) rather than parsed off log output."""
    return _start_agent(hostname, timeout=timeout, r=r)


agent_pid = int(sys.argv[1])
agent2 = None

try:
    print("\n== A: ComfyUI has the workflow -> the breadcrumb already says so, and this death is terminal")

    # __slow_prompt__ holds POST /prompt open for SLOW_PROMPT_DELAY_S AFTER
    # the stub has recorded the workflow, which is the whole window: ComfyUI
    # has it, the agent has not been told so yet.
    probe, workflow = probe_workflow("__slow_prompt__")
    job_id = GW.post("/api/generate", {"workflow": workflow})["job_id"]

    check("the worker picked the job up",
          wait_for(lambda: r.hget(state_key(job_id), "status") == "running", timeout=15),
          r.hgetall(state_key(job_id)))

    check("ComfyUI was handed the workflow", wait_for(lambda: comfy_saw(probe), timeout=20), probe)

    # Read at the one instant that makes this an ordering claim: ComfyUI holds
    # the prompt, and its acceptance has not come back yet.
    phase_at_submit = r.hget(state_key(job_id), "phase")
    check("the phase breadcrumb already reads 'executing' at the moment ComfyUI "
          "holds the workflow -- written BEFORE the POST, not after it answers",
          phase_at_submit == "executing", phase_at_submit)

    os.kill(agent_pid, signal.SIGKILL)

    settled = wait_for(lambda: r.hget(state_key(job_id), "status") in ("failed", "completed", "cancelled")
                       or "retry" in event_types(job_id), timeout=45)
    kinds = event_types(job_id)
    state = r.hgetall(state_key(job_id))

    check("the job reached a terminal state", settled, (state, kinds))
    check("no 'retry' was published -- a death with the workflow already at "
          "ComfyUI is never replayed onto a second GPU",
          "retry" not in kinds, kinds)
    check("and the terminal state was 'failed'", state.get("status") == "failed", state)
    check("attempt_count stayed at 0 -- no retry was claimed",
          state.get("attempt_count") in (None, "0"), state.get("attempt_count"))
    check("the job was not put back on comfy:queue",
          queued_raw(job_id) is None, r.llen(QUEUE_KEY))

    print("\n== B1: a cancelled job whose worker then dies is not requeued (the reaper's door)")

    # Nothing is polling comfy:queue now (scenario A killed the agent), so the
    # entry stays put and can be moved by hand into a dead worker's processing
    # list -- which is precisely the state a SIGKILLed worker leaves behind:
    # an entry parked in comfy:processing:<worker> with no heartbeat key.
    probe_b1, workflow = probe_workflow("__slow__")
    job_b1 = GW.post("/api/generate", {"workflow": workflow})["job_id"]

    raw = queued_raw(job_b1)
    check("the submitted job is on comfy:queue, unclaimed", raw is not None, r.llen(QUEUE_KEY))

    GW.post(f"/api/jobs/{job_b1}/cancel")
    check("cancel_requested is set on the job",
          r.hget(state_key(job_b1), "cancel_requested") == "1", r.hgetall(state_key(job_b1)))

    dead_worker = f"q2-dead-{uuid.uuid4().hex[:8]}"
    processing = f"comfy:processing:{dead_worker}"

    # The worker that took it and died: entry moved off the queue into its
    # processing list, breadcrumb at 'dispatched', and no heartbeat key -- so
    # the reaper sees a stranded job at a RETRYABLE phase, which is the whole
    # point. Nothing here fakes the reaper's input; it is the same entry
    # hub.py wrote, in the place worker_agent.py's BLMOVE would have put it.
    r.lrem(QUEUE_KEY, 1, raw)
    r.lpush(processing, raw)
    r.hset(state_key(job_b1), mapping={"status": "running", "worker": dead_worker,
                                       "phase": "dispatched"})

    reaped = wait_for(lambda: r.llen(processing) == 0, timeout=30)
    settled = wait_for(lambda: r.hget(state_key(job_b1), "status") != "running", timeout=30)
    kinds = event_types(job_b1)
    state = r.hgetall(state_key(job_b1))

    check("the reaper acted on the stranded entry", reaped and settled, (state, kinds))
    check("the cancelled job was NOT put back on comfy:queue",
          queued_raw(job_b1) is None, r.lrange(QUEUE_KEY, 0, -1))
    check("no 'retry' was published for it", "retry" not in kinds, kinds)
    check("its status was not reset to 'queued'", state.get("status") != "queued", state)
    check("attempt_count stayed at 0 -- a cancelled job never spends a retry",
          state.get("attempt_count") in (None, "0"), state.get("attempt_count"))
    check("it ended 'cancelled' -- the outcome the user asked for, terminal, "
          "and the browser tailing it stops there",
          state.get("status") == "cancelled" and kinds[-1:] == ["cancelled"],
          (state.get("status"), kinds))
    check("the workflow stored beside the queue went with it",
          not r.exists(payload_key(job_b1)), payload_key(job_b1))

    print("\n== B2: a job cancelled before any worker picked it up is never handed to ComfyUI (the worker's door)")

    probe_b2, workflow = probe_workflow("__slow__")
    job_b2 = GW.post("/api/generate", {"workflow": workflow})["job_id"]
    GW.post(f"/api/jobs/{job_b2}/cancel")

    check("the job is cancelled and still queued, with no worker on it",
          r.hget(state_key(job_b2), "cancel_requested") == "1"
          and queued_raw(job_b2) is not None,
          r.hgetall(state_key(job_b2)))

    # Only now is there anything to pick it up, so the cancel provably landed
    # first -- this is "cancelled while queued", the case hub.py's cancel()
    # docstring says never starts.
    agent2 = start_extra_agent("q2-cancel-agent")

    settled = wait_for(lambda: r.hget(state_key(job_b2), "status") in ("cancelled", "failed", "completed"),
                       timeout=45)
    kinds = event_types(job_b2)
    state = r.hgetall(state_key(job_b2))

    check("the job reached a terminal state", settled, (state, kinds))
    check("ComfyUI was never handed the cancelled workflow -- no GPU was spent on it",
          not comfy_saw(probe_b2), probe_b2)
    check("no 'accepted' event -- there was no prompt to accept",
          "accepted" not in kinds, kinds)
    check("it ended 'cancelled'", state.get("status") == "cancelled", state)

finally:
    stop_agent(agent2)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
