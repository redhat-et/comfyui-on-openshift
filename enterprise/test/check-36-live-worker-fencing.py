"""A LIVE worker must not have its job requeued underneath it — and if it is,
it must not run the workflow a second time.

Every other worker-death check in this suite kills something. This one kills
nothing, and that is the whole point: the replay it reproduces needs no death
at all.

The mechanism. run_job() writes phase=dispatched, then does ensure_workspace()
(an mkdir on the RWX volume, unbounded), ws.connect() and submit_prompt() —
and before F4 there was no heartbeat refresh anywhere in that window: the
refresh only started once the post-submit receive loop was running. The
reaper's only liveness test is whether the heartbeat key exists, so a
heartbeat that merely LAPSED read as a death; the entry is at a retryable
phase by construction there; and the job was requeued while its worker was
alive and about to submit it. ComfyUI was then handed the same workflow twice,
the browser saw a second started/completed after the first terminal event had
already closed it, and showback billed one job_id twice.

Two scenarios, one per half of the fix, and neither one kills anything:

  A. THE WINDOW ITSELF. Park a live agent in ws.connect() for longer than
     HEARTBEAT_TTL and let it be. Its heartbeat must stay armed across the
     whole window — across the mkdir, the connect and the submit — so the
     reaper never sees a death that did not happen. Nothing is requeued and
     ComfyUI is handed the workflow exactly once.

     This is the assertion that fails on HEAD: on HEAD the heartbeat lapses
     inside the stall, the reaper requeues, and the same workflow reaches the
     stub twice for one submission.

  B. THE FENCE. (A) shrinks the window; it cannot close it, because a worker
     slow enough — an EFS mkdir that takes minutes, a Redis partition longer
     than the TTL — can still be declared dead while it is alive and about to
     submit. So this scenario makes that happen on purpose, still without
     killing anything: it deletes the live agent's heartbeat key out from
     under it, repeatedly, until the reaper has genuinely requeued the job.
     A real requeue, a real second attempt, and the original worker still
     running.

     The original attempt must then notice it no longer owns the job and
     abandon it rather than submitting the workflow and writing a second set
     of outputs. One handoff to ComfyUI, one terminal event, one attempt
     billed — for a job that really was requeued once.

What is asserted, and why it is asserted this way. Three observables, all of
them counts taken from an observer armed BEFORE the event rather than from a
state read afterwards:

  - Writes to comfy:queue, counted by MONITOR (queue_watch.py). An LLEN read
    after the fact cannot tell "never requeued" from "requeued and already
    popped again", because the only agent able to pop it is the one under
    test.
  - How many times the stub ComfyUI was handed THIS workflow, counted by
    giving it a node key nothing else uses and asking the stub. This is the
    replay itself — a GPU that was spent — rather than a proxy for it.
  - How many terminal events landed on the job's own stream, counted over the
    whole stream with XRANGE. The browser stops at the first one; the second
    is what nobody was there to see.

Both scenarios wait for the system to go idle before counting, because on a
broken implementation the second run happens AFTER the first terminal event
that a tailing browser stops at — counting at the terminal event would read 1
either way and pass on HEAD.

  C. THE CLAIM ITSELF. (B) proves the fence works when the requeue lands
     while the worker is parked BEFORE it. The fence used to be a bare HGET
     followed, separately, by the HSET that writes "executing" and then the
     submit — so a requeue landing between the read and the write was read
     as "still mine", the phase was written over the retry's "queued", and
     the workflow was submitted beside the retry: the exact replay (B)
     exists to prevent, one line further down. The window is microseconds,
     which is why (B) cannot reach it and why this scenario runs an agent of
     its own with TEST_DELAY_BEFORE_CLAIM_S set (worker_agent.py; test-only,
     documented there): the agent pauses inside the window, the check
     deletes its heartbeat key until the reaper has genuinely requeued the
     job, and the agent then reaches the claim with the reap already
     stamped. The claim has to be a compare-and-set that fails there —
     ComfyUI handed the workflow once, one terminal event, the retry's.
"""
import os, signal, sys, time, uuid

from harness import (
    COMFY, GW, QUEUE_KEY, REDIS_PASSWORD, REDIS_URL, alive, check,
    connect_redis, drain as _drain, failures, handoffs, start_agent,
    state_key, terminal_events as _terminal_events, wait_gone,
)
from queue_watch import QueueWriteWatcher
from worker_ids import heartbeat_keys, processing_keys

# See check-30-sigkill.py: run.sh's stdout is a pipe, and a check killed by
# CHECK_TIMEOUT loses every buffered PASS/FAIL line with it.
sys.stdout.reconfigure(line_buffering=True)

get, post = GW.get, GW.post

HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "180"))

# Longer than HEARTBEAT_TTL, and well inside worker_agent.py's own 30s connect
# timeout: the agent must be parked in ws.connect() for long enough that a
# heartbeat armed once on the way in has definitely expired. Derived from the
# TTL run.sh actually exports rather than spelled as a number, so shrinking
# that TTL cannot quietly turn this into a window nothing lapses inside.
STALL_S = HEARTBEAT_TTL + 5

r = connect_redis()


def terminal_events(job_id):
    return _terminal_events(r, job_id)


def await_pickup(job_id, timeout=20):
    """Block until a worker has claimed the job, and report the breadcrumb and
    the worker id it claimed it under."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        st = get(f"/api/jobs/{job_id}")

        if st.get("status") == "running":
            return st

        time.sleep(0.1)

    return get(f"/api/jobs/{job_id}")


def drain(job_id, timeout=90):
    """harness.drain, verbose (prints "no terminal event ... gave up" on
    timeout, as this file's original copy did)."""
    return _drain(job_id, timeout, verbose=True)


def wait_idle(worker_id, stable=2.0, cap=40):
    """Block until nothing is left to run: the queue is empty and this worker
    holds no processing entry, and both have stayed that way for `stable`
    seconds.

    This is what makes the counts below able to fail. On a broken
    implementation the SECOND run of the workflow starts after the first
    terminal event — the one a browser stops at — so counting handoffs the
    moment drain() returns reads 1 whichever way it went.
    """
    deadline = time.time() + cap
    quiet_since = None

    while time.time() < deadline:
        idle = r.llen(QUEUE_KEY) == 0 and not processing_keys(r, worker_id)

        if not idle:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since >= stable:
            return True

        time.sleep(0.2)

    return False


def watcher():
    return QueueWriteWatcher(REDIS_URL, REDIS_PASSWORD, QUEUE_KEY).start()


agent_pid = int(sys.argv[1])

print("\n== A: a live worker parked past its heartbeat TTL keeps its job")

# Park the agent in the window this is about: after the "dispatched"
# breadcrumb, after the workspace mkdir, and before ComfyUI has been sent
# anything at all. worker_agent.py connects the ComfyUI WebSocket before it
# submits (point 1), so stalling the stub's accept holds it exactly there.
#
# Timed from here, because this stall is the premise of everything below and
# it has already failed silently once. A stub that abandons the handshake part
# way through does not park the agent — it hands it a DEAD ComfyUI. The job
# then fails inside ws.connect() before a workflow is ever submitted, and
# every count below reports a fencing failure for a job that never ran. That
# is what uvicorn's websockets layer did here: it gave the whole opening
# handshake, the stub's own sleep included, ten seconds, and STALL_S is
# longer than that (fake_comfy.py, _lift_ws_handshake_timeout). The stub
# cannot see the difference — an abandoned handshake still runs its endpoint
# to completion — so the park is measured out here, by the clock.
stall_armed_at = time.time()
COMFY.post("/__stall_next_ws__", {"seconds": STALL_S})

probe_a = f"live-{uuid.uuid4().hex[:8]}"
job_a = post("/api/generate", {"workflow": {probe_a: {"class_type": "KSampler"}}})["job_id"]

# Armed after this job's own legitimate insert, so from here it counts only
# what the reaper does. An LLEN read afterwards could not tell a requeue that
# happened and was popped again from one that never happened (queue_watch.py).
queue_watcher = watcher()

st = await_pickup(job_a)
worker_a = st.get("worker")

check("the worker picked the job up and is parked in the ComfyUI connect "
      "(status running, phase 'dispatched')",
      st.get("status") == "running" and st.get("phase") == "dispatched", st)
check("and ComfyUI has not been handed this workflow yet — the window this "
      "check is about is the one BEFORE the submit",
      handoffs(probe_a) == 0, probe_a)
check("the worker is alive at this point, and nothing in this check has "
      "signalled it", alive(agent_pid), agent_pid)

# Longer than the TTL: on HEAD the heartbeat armed on the way into the poll
# loop has expired by now and the reaper has already acted.
time.sleep(HEARTBEAT_TTL + 1)

live_at_lapse = bool(heartbeat_keys(r, worker_a)) if worker_a else False
check("the worker's heartbeat is STILL armed more than HEARTBEAT_TTL into "
      "that window — the refresh covers the mkdir, the connect and the "
      "submit, not only the receive loop that comes after them",
      live_at_lapse, {"worker": worker_a, "keys": heartbeat_keys(r, worker_a or "")})
check("and the worker really is alive to have armed it — it was never "
      "signalled, so a lapse here would be a false death",
      alive(agent_pid), agent_pid)

kinds, terminal = drain(job_a)

parked_for = time.time() - stall_armed_at
check("the fixture held: nothing happened to this job until the stall it was "
      "parked in had run its full length, so the agent was waiting on a SLOW "
      "ComfyUI and not on a dropped connection",
      parked_for >= STALL_S,
      {"parked_for": round(parked_for, 2), "stall_s": STALL_S})

check("the job reached a terminal state", terminal is not None, kinds)
check("and it completed", terminal and terminal["type"] == "completed",
      terminal["type"] if terminal else None)

check("the system went idle — queue drained, nothing left in the worker's "
      "processing list — so the counts below are final",
      wait_idle(worker_a), worker_a)

queue_writes, queue_write_cmds = queue_watcher.stop()

check("comfy:queue never received a write for this job: a worker that is "
      "alive is not a worker that died, so there was nothing to requeue "
      "(counted from MONITOR, not from an LLEN that reads 0 either way)",
      queue_writes == 0, queue_write_cmds)
check("ComfyUI was handed this workflow exactly once — one submission, one "
      "run on the card, no replay",
      handoffs(probe_a) == 1, handoffs(probe_a))
check("exactly one terminal event landed on the job's stream — no second "
      "completion arriving after the browser had already closed",
      terminal_events(job_a) == ["completed"], terminal_events(job_a))

state = r.hgetall(state_key(job_a))
check("attempt_count stayed at 0 — this job was never retried",
      state.get("attempt_count") in (None, "0"), state)
check("the worker survived the whole scenario", alive(agent_pid), agent_pid)


print("\n== B: a live worker whose job IS requeued under it abandons rather "
      "than running it twice")

stall_armed_at = time.time()
COMFY.post("/__stall_next_ws__", {"seconds": STALL_S})

probe_b = f"fence-{uuid.uuid4().hex[:8]}"
job_b = post("/api/generate", {"workflow": {probe_b: {"class_type": "KSampler"}}})["job_id"]

queue_watcher = watcher()

st = await_pickup(job_b)
worker_b = st.get("worker")

check("the worker picked the job up and is parked in the ComfyUI connect",
      st.get("status") == "running" and st.get("phase") == "dispatched", st)
check("and ComfyUI has not been handed this workflow yet",
      handoffs(probe_b) == 0, probe_b)

# Force the race (a) alone cannot close: delete the LIVE agent's heartbeat key
# out from under it until the reaper has actually acted. Nothing is killed —
# the agent is parked in ws.connect() throughout — so this is exactly "slow
# enough to be declared dead", reproduced deterministically instead of waited
# for. Stops the moment the requeue is observed, so the keepalive re-arms and
# the second attempt runs against a healthy worker.
requeued = False
deadline = time.time() + STALL_S - 2

while time.time() < deadline:
    for key in heartbeat_keys(r, worker_b or ""):
        r.delete(key)

    if r.hget(state_key(job_b), "attempt_count") == "1":
        requeued = True
        break

    time.sleep(0.05)

check("the fixture reproduced: the reaper genuinely requeued this job while "
      "its worker was alive and parked before the submit (attempt_count 1)",
      requeued, r.hgetall(state_key(job_b)))
check("and the worker whose job was taken is still alive — nothing here "
      "killed it, which is the entire premise",
      alive(agent_pid), agent_pid)

kinds, terminal = drain(job_b)

# The same fixture assertion as in (A), and it carries more here: the fence
# under test is the one at the SUBMIT gate, and the reaped attempt only
# reaches that gate if it is still parked in ws.connect() when the requeue
# lands. An attempt that instead died in a dropped handshake never reaches
# still_ours() at all — it is stopped by the later fence on `finish`, and the
# counts below pass without the submit gate having been exercised once.
parked_for = time.time() - stall_armed_at
check("the fixture held: the reaped attempt was still parked in the stall "
      "when the requeue happened, so it reached the ownership fence at the "
      "submit gate rather than dying before it",
      parked_for >= STALL_S,
      {"parked_for": round(parked_for, 2), "stall_s": STALL_S})

check("the job reached a terminal state", terminal is not None, kinds)
check("and it completed — the requeued attempt ran it",
      terminal and terminal["type"] == "completed",
      terminal["type"] if terminal else None)

check("the system went idle before anything below is counted",
      wait_idle(worker_b), worker_b)

queue_writes, queue_write_cmds = queue_watcher.stop()

check("the reaper wrote this job back onto comfy:queue exactly once",
      queue_writes == 1, queue_write_cmds)
check("ComfyUI was handed this workflow exactly once in total — the original "
      "attempt noticed it had been reaped and abandoned the job instead of "
      "submitting it beside the retry",
      handoffs(probe_b) == 1, handoffs(probe_b))
check("exactly one terminal event landed on the job's stream — the reaped "
      "attempt wrote no second outcome after the retry's",
      terminal_events(job_b) == ["completed"], terminal_events(job_b))

state = r.hgetall(state_key(job_b))
check("the job's own state agrees it completed, once",
      state.get("status") == "completed", state)
check("attempt_count reads 1 — requeued once, and only once",
      state.get("attempt_count") == "1", state.get("attempt_count"))
check("the worker survived the whole scenario", alive(agent_pid), agent_pid)


print("\n== C: a reap that lands between the ownership read and the claim "
      "is noticed by the claim itself")

# Long enough for the reaper to act inside it (a deleted heartbeat plus one
# REAPER_INTERVAL tick), short enough to stay well inside the drain budget.
CLAIM_DELAY_S = HEARTBEAT_TTL


# The suite's agent stands down (SIGTERM while idle — it exits and deletes
# its own key; run.sh restarts one for the next check), and an agent with
# the pause in the window takes its place.
os.kill(agent_pid, signal.SIGTERM)
check("the suite's agent exited on SIGTERM", wait_gone(agent_pid), agent_pid)
deadline = time.time() + 15
while time.time() < deadline and r.keys("comfy:worker:*"):
    time.sleep(0.1)
check("no worker is registered before this scenario starts its own",
      not r.keys("comfy:worker:*"), r.keys("comfy:worker:*"))

hostname_c = f"claim-pod-{uuid.uuid4().hex[:6]}"
agent_c = start_agent(hostname_c, {"TEST_DELAY_BEFORE_CLAIM_S": str(CLAIM_DELAY_S)}, r=r)

try:
    check("this scenario's agent is up and heartbeating",
          agent_c.poll() is None and bool(heartbeat_keys(r, hostname_c)), hostname_c)

    probe_c = f"claim-{uuid.uuid4().hex[:8]}"
    job_c = post("/api/generate", {"workflow": {probe_c: {"class_type": "KSampler"}}})["job_id"]
    queue_watcher = watcher()

    st = await_pickup(job_c)
    check("the worker picked the job up (status running, phase 'dispatched')",
          st.get("status") == "running" and st.get("phase") == "dispatched", st)

    # It is now in the pause: past the connect, past the cancel check, past
    # the read that said "still mine", and before the write that claims.
    # Give it a moment to get there, then make the reaper act.
    time.sleep(1.0)
    check("and ComfyUI has not been handed the workflow — the agent is paused "
          "before the claim", handoffs(probe_c) == 0, handoffs(probe_c))

    requeued = False
    deadline = time.time() + CLAIM_DELAY_S - 2
    while time.time() < deadline:
        for key in heartbeat_keys(r, hostname_c):
            r.delete(key)
        if r.hget(state_key(job_c), "attempt_count") == "1":
            requeued = True
            break
        time.sleep(0.05)

    check("the fixture reproduced: the reaper requeued the job while the "
          "worker was paused between its ownership read and its claim "
          "(attempt_count 1)", requeued, r.hgetall(state_key(job_c)))
    check("and stamped the reaped owner on it, with ComfyUI still not handed "
          "the workflow", r.hget(state_key(job_c), "owner") == "#reaped"
          and handoffs(probe_c) == 0,
          (r.hget(state_key(job_c), "owner"), handoffs(probe_c)))
    check("the paused worker is alive — nothing killed it", agent_c.poll() is None)

    kinds, terminal = drain(job_c)
    check("the job reached a terminal state", terminal is not None, kinds)
    check("and it completed — the requeued attempt ran it",
          terminal and terminal["type"] == "completed", terminal["type"] if terminal else None)
    check("the system went idle before anything below is counted",
          wait_idle(hostname_c), hostname_c)

    queue_writes, queue_write_cmds = queue_watcher.stop()
    check("the reaper wrote this job back onto comfy:queue exactly once",
          queue_writes == 1, queue_write_cmds)
    check("ComfyUI was handed this workflow exactly once — the claim saw the "
          "reap that landed after the ownership read and refused, so the "
          "original attempt never submitted beside the retry",
          handoffs(probe_c) == 1, handoffs(probe_c))
    check("exactly one terminal event landed on the job's stream",
          terminal_events(job_c) == ["completed"], terminal_events(job_c))
    state = r.hgetall(state_key(job_c))
    check("the job's own state agrees: completed, once, requeued once",
          state.get("status") == "completed" and state.get("attempt_count") == "1", state)
    check("this scenario's worker survived it", agent_c.poll() is None, agent_c.poll())

finally:
    if agent_c.poll() is None:
        agent_c.terminate()
        try:
            agent_c.wait(timeout=15)
        except Exception:  # noqa: BLE001
            agent_c.kill()

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
