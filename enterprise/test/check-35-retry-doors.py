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
import json, os, signal, subprocess, sys, time, urllib.request, uuid
import redis

from worker_ids import heartbeat_keys

GW = "http://127.0.0.1:8100"
COMFY = "http://127.0.0.1:8999"
QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
WORKER_AGENT = os.environ["WORKER_AGENT"]
failures = []

r = redis.from_url(
    os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0"),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def post(url, body=None):
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(url):
    return json.loads(urllib.request.urlopen(url, timeout=10).read())


def state_key(job_id):
    return f"comfy:job:{job_id}:state"


def stream_key(job_id):
    return f"comfy:job:{job_id}:events"


def payload_key(job_id):
    return f"comfy:job:{job_id}:payload"


def probe_workflow(*markers):
    """A workflow carrying a node key nothing else in the suite uses, so
    "did ComfyUI ever see THIS workflow" has a yes/no answer."""
    probe = f"probe-{uuid.uuid4().hex[:8]}"
    workflow = {marker: {"class_type": "KSampler"} for marker in markers}
    workflow[probe] = {"class_type": "KSampler"}

    return probe, workflow


def comfy_saw(probe):
    return probe in get(f"{COMFY}/__received__")["nodes"]


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


def wait_for(predicate, timeout=45, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)

    return False


def start_extra_agent(hostname, timeout=30):
    """A worker agent of this check's own, identified by a fixed HOSTNAME so
    its heartbeat key is findable — by a prefix match on the identity, since
    the key itself is named from the agent's INCARNATION and carries a nonce
    this process cannot know (worker_agent.py, note 9; worker_ids.py). Named
    to match run.sh's agent*.log glob so a failure dumps its log too."""
    env = dict(os.environ)
    env["HOSTNAME"] = hostname
    log = open(f"agent-{hostname}.log", "w")
    proc = subprocess.Popen([sys.executable, WORKER_AGENT], env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if heartbeat_keys(r, hostname):
            return proc
        if proc.poll() is not None:
            return proc  # died on startup; later assertions will show it
        time.sleep(0.2)

    return proc


def stop_agent(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


agent_pid = int(sys.argv[1])
agent2 = None

try:
    print("\n== A: ComfyUI has the workflow -> the breadcrumb already says so, and this death is terminal")

    # __slow_prompt__ holds POST /prompt open for SLOW_PROMPT_DELAY_S AFTER
    # the stub has recorded the workflow, which is the whole window: ComfyUI
    # has it, the agent has not been told so yet.
    probe, workflow = probe_workflow("__slow_prompt__")
    job_id = post(f"{GW}/api/generate", {"workflow": workflow})["job_id"]

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
                       or "retry" in event_types(job_id))
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
    job_b1 = post(f"{GW}/api/generate", {"workflow": workflow})["job_id"]

    raw = queued_raw(job_b1)
    check("the submitted job is on comfy:queue, unclaimed", raw is not None, r.llen(QUEUE_KEY))

    post(f"{GW}/api/jobs/{job_b1}/cancel")
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
    job_b2 = post(f"{GW}/api/generate", {"workflow": workflow})["job_id"]
    post(f"{GW}/api/jobs/{job_b2}/cancel")

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
