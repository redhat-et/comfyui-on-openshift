"""SIGKILL: a worker that dies with no warning must not strand its job — and
whether that death is retried depends on WHEN it happened.

SIGTERM (check-20-failure-paths.py) is the polite case. This is the impolite
one — OOM kill, node reclaim — where the agent gets no chance to clean up.
docs/10-roadmap.md's "Decisions already made" narrows retry deliberately: a
VRAM OOM already arrives as execution_error and stays a terminal `failed`
carrying ComfyUI's own message (retrying it burns a second GPU-hour on a
workflow that cannot fit), and a host-RAM OOM that kills the pod is
indistinguishable at the queue layer from a node reclaim — so blanket "retry
on worker death" would retry the poison pill by construction. The only death
that is safe to retry is one where ComfyUI never saw the workflow at all.

Two scenarios, both against a worker's per-worker processing list and TTL'd
heartbeat (point 5 in worker_agent.py):

  A. SIGKILL BEFORE the workflow ever reached ComfyUI (the agent has claimed
     the job and is still connecting the ComfyUI WebSocket it opens before
     submitting anything). The gateway's reaper must requeue this job exactly
     once, as a non-terminal `retry` event a tailing browser does not stop at,
     and a second agent must pick it up and complete it.

     The premise is asserted rather than assumed: the stub records every
     workflow handed to it, and this check proves ComfyUI had not been handed
     this one at the moment the worker died. An earlier version of this
     scenario killed the agent while it was blocked inside submit_prompt()
     instead, which does NOT satisfy the premise — ComfyUI receives a workflow
     when the POST is written, not when it answers, so that kill point was on
     the far side of the line this whole mechanism draws, and asserting a
     retry there asserted the replay the mechanism exists to prevent. See
     check-35-retry-doors.py, which now holds that window as a NON-retryable
     case.

  B. SIGKILL AFTER execution began (the original scenario here: the agent is
     mid-generation, ComfyUI already accepted the prompt). This must stay
     exactly what it always was — one terminal `failed`, naming the dead
     worker, never requeued — because a workflow that OOM-killed one worker
     would OOM-kill the next one it was handed too.

Both scenarios rely on a `phase` breadcrumb the job's own state hash must
carry once a worker has picked a job up: "dispatched" before it has heard back
from ComfyUI, "executing" once ComfyUI has accepted the prompt. That
breadcrumb is what the reaper needs in order to tell A from B — the stranded
queue entry itself is a static copy of what hub.py pushed, never updated by
the worker, so the phase has to live on the state hash instead of on the
queue entry the reaper is scanning. Nothing here writes it yet, so every
assertion that touches it fails, as does the retry itself: HEAD's reaper
(fail_orphaned_job) fails ANY stranded job unconditionally.

run.sh shrinks HEARTBEAT_TTL and REAPER_INTERVAL so this resolves in seconds
rather than the production-default minutes, and exports WORKER_AGENT so this
check can start a second agent of its own — proving scenario A needs one,
since the agent this check is handed (argv[1]) is the one that dies in
scenario A, and nothing between checks in run.sh restarts one mid-check.
"""
import os, signal, sys, time, uuid

from harness import (
    COMFY, GW, QUEUE_KEY, REDIS_PASSWORD, REDIS_URL, check, comfy_saw, connect_redis,
    drain, failures, start_agent, state_key, stop_agent,
)
from queue_watch import QueueWriteWatcher

get, post = GW.get, GW.post

# Line-buffer stdout explicitly, rather than trust the interpreter to pick
# line buffering on its own. It only does that for a TTY; run.sh's stdout is
# a pipe (through `make test`, and again through whatever the caller does
# with it), so by default CPython fully buffers stdout in blocks and every
# PASS/FAIL line sits in that buffer until enough output accumulates or the
# process exits normally. drain() below can hang under a wrong
# implementation until run.sh's CHECK_TIMEOUT kills this process with a
# plain SIGTERM -- no handler is installed for it, so the interpreter is torn
# down without running its normal atexit flush, and every line still sitting
# in that buffer is lost with it. Line buffering writes each PASS/FAIL (and
# each print()) through to the pipe as soon as its newline lands, so the
# diagnostic survives a kill that gives the process no chance to clean up
# after itself.
sys.stdout.reconfigure(line_buffering=True)

r = connect_redis()

agent_pid = int(sys.argv[1])
agent2 = None

try:
    print("\n== A: SIGKILL before ComfyUI was ever handed the workflow -> retried once, a restarted agent completes it")

    # Park the agent in the window a pre-execution death actually lives in:
    # after it has claimed the job and written its breadcrumb, and before it
    # has sent ComfyUI anything at all. worker_agent.py connects the ComfyUI
    # WebSocket before it submits (point 1 in that file), so stalling the
    # stub's accept holds the agent there with the workflow still in its own
    # memory -- for 15s, which is a window rather than a race, and well inside
    # the agent's own 30s connect timeout.
    COMFY.post("/__stall_next_ws__", {"seconds": 15})

    probe = f"probe-{uuid.uuid4().hex[:8]}"
    job = post("/api/generate", {"workflow": {probe: {"class_type": "KSampler"}}})
    job_id = job["job_id"]

    # Armed now, after the submit above's own (legitimate) insert, so it
    # counts only what happens to comfy:queue from here on: the reaper's one
    # required requeue, and nothing else. Reading LLEN once the job has
    # finished cannot prove "exactly once" or even "at all" -- the only agent
    # able to pop a requeued entry off this queue is the same one under test,
    # so by the time a terminal event has been drained any entry that ever
    # existed is already gone (queue_watch.py). Watching the write itself
    # instead makes "exactly once" a count of commands actually issued, not
    # an inference from a length that reads 0 whichever way this went.
    queue_watcher = QueueWriteWatcher(REDIS_URL, REDIS_PASSWORD, QUEUE_KEY).start()

    deadline = time.time() + 10
    picked_up = False
    phase_at_pickup = None
    while time.time() < deadline:
        st = get(f"/api/jobs/{job_id}")
        if st.get("status") == "running":
            picked_up = True
            phase_at_pickup = st.get("phase")
            break
        time.sleep(0.1)

    check("the worker picked up the job before it was killed", picked_up, phase_at_pickup)
    check("the phase breadcrumb is recorded and visible on job state, and reads "
          "'dispatched' -- picked up, but ComfyUI has not yet been heard from",
          phase_at_pickup == "dispatched", phase_at_pickup)
    check("and ComfyUI really has not been handed this workflow -- the premise "
          "of the retry below, checked rather than assumed",
          not comfy_saw(probe), probe)

    os.kill(agent_pid, signal.SIGKILL)

    # A fresh agent has to exist for the retried job to land on, or nothing on
    # this laptop is polling comfy:queue once the original is dead. Findable
    # by its fixed HOSTNAME (worker_ids.py) rather than parsed off log output
    # -- harness.start_agent's ready="heartbeat" default.
    agent2 = start_agent("q2-retry-agent", r=r)

    kinds, terminal = drain(job_id, timeout=40)

    check("a non-terminal 'retry' event was published (breadcrumb (c))",
          "retry" in kinds, kinds)
    check("the job still reached a terminal state", terminal is not None, terminal)
    check("and that state was 'completed' -- the restarted agent ran it, "
          "not a second, silent 'failed'",
          terminal and terminal["type"] == "completed",
          terminal["type"] if terminal else None)

    state = r.hgetall(state_key(job_id))
    check("the job's own state agrees it completed",
          state.get("status") == "completed", state)
    check("attempt_count shows it was retried exactly once, not more",
          state.get("attempt_count") == "1", state.get("attempt_count"))

    check("the second attempt is what handed the workflow to ComfyUI -- so the "
          "retry ran it once in total, rather than replaying a run",
          comfy_saw(probe), probe)

    queue_writes, queue_write_cmds = queue_watcher.stop()
    check("the reaper wrote this job back onto comfy:queue exactly once -- "
          "not zero (which the 'retry' event and attempt_count above would "
          "already have caught) and not twice (a double-push that an "
          "after-the-fact LLEN read could never distinguish from a single "
          "one, since the only agent able to pop either entry is the same "
          "one already proven to have completed the job)",
          queue_writes == 1, queue_write_cmds)

    print("\n== B: SIGKILL after execution began -> stays a single terminal failure, never requeued")

    # agent2 is idle now that A finished with it; reuse it as the worker that
    # dies mid-generation, so this check needs no third process.
    job = post("/api/generate", {"workflow": {"__slow__": {"class_type": "KSampler"}}})
    job_id = job["job_id"]

    # Same reasoning as scenario A's watcher: armed after this job's own
    # initial insert, so what it counts from here is only a wrongly-issued
    # requeue -- which must be zero, since this death happens after execution
    # began and must stay a single terminal failure.
    queue_watcher = QueueWriteWatcher(REDIS_URL, REDIS_PASSWORD, QUEUE_KEY).start()

    time.sleep(3)          # let agent2 pick it up and get into the job, well
                            # past ComfyUI's acceptance of the prompt

    phase_mid_job = get(f"/api/jobs/{job_id}").get("phase")
    check("the phase breadcrumb shows execution had begun ('executing') "
          "by the time this worker was killed -- the reason it must NOT "
          "be retried",
          phase_mid_job == "executing", phase_mid_job)

    os.kill(agent2.pid, signal.SIGKILL)

    kinds, terminal = drain(job_id, timeout=60)

    check("the job still reached a terminal state", terminal is not None, terminal)
    check("and that state was 'failed', not a silent drop, and not a 'retry' "
          "en route to a second attempt",
          terminal and terminal["type"] == "failed" and "retry" not in kinds,
          (terminal["type"] if terminal else None, kinds))
    err = (terminal or {}).get("data", {}).get("error", "")
    check("the failure names the dead worker as the cause", "worker" in err, err[:120])

    state = get(f"/api/jobs/{job_id}")
    check("job state agrees", state.get("status") == "failed", state)
    check("attempt_count stayed at 0 -- this death was never retried",
          state.get("attempt_count") in (None, "0"), state.get("attempt_count"))

    queue_writes, queue_write_cmds = queue_watcher.stop()
    check("comfy:queue never received a write for this job -- the death was "
          "not put back, proven as the absence of the LPUSH/RPUSH/LINSERT a "
          "requeue would have to issue rather than an LLEN read that would "
          "show 0 either way (queue_watch.py)",
          queue_writes == 0, queue_write_cmds)

    print("\n== the dead worker (agent2) drops out of the registered count")
    deadline = time.time() + 30
    gone = False
    while time.time() < deadline:
        if get("/api/stats")["workers_registered"] == 0:
            gone = True
            break
        time.sleep(1)
    check("workers_registered returned to 0 via heartbeat expiry", gone,
          get("/api/stats"))

finally:
    stop_agent(agent2)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
