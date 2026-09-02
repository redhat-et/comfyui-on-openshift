"""Failure paths: rejected workflow, cancel, and SIGTERM drain."""
import os, signal, sys, time

from harness import GW, QUEUE_KEY, REDIS_PASSWORD, REDIS_URL, check, connect_redis, drain, failures, state_key
from queue_watch import QueueWriteWatcher

post = GW.post

r = connect_redis()


print("\n== a workflow ComfyUI rejects becomes a 'failed' event, with the reason")

# Armed BEFORE the submit, deliberately. Arming after it looks tidier -- the
# job's own fair-queueing insert is a legitimate write to this key, so a
# watcher started later counts only writes that should never happen -- but it
# cannot work: the agent can pick the job up, be rejected by ComfyUI, and
# requeue it before MONITOR is attached, so the write this exists to catch is
# already past. The wave-2a re-gate proved exactly that, against a mutation
# that really did requeue the rejection: the assertion still passed.
#
# So count instead of watching for absence. Exactly one write may touch this
# key for this job -- the submit's own insert -- and a requeue is a second.
# check-30 scenario A uses the same construction for the same reason.
watcher = QueueWriteWatcher(REDIS_URL, REDIS_PASSWORD, QUEUE_KEY).start()

job = post("/api/generate", {"workflow": {"__fail__": {"class_type": "KSampler"}}})
job_id = job["job_id"]

kinds, terminal = drain(job_id, timeout=30)
check("terminated as failed", terminal and terminal["type"] == "failed", terminal)
err = (terminal or {}).get("data", {}).get("error", "")
check("the reason from ComfyUI is surfaced, not swallowed",
      "ckpt_name" in err, err[:160])

# docs/10-roadmap.md, Q2: retry is scoped to a worker dying before ComfyUI
# ever saw the workflow. This job never involved a dead worker at all -- the
# very-much-alive agent submitted it, ComfyUI answered synchronously with a
# rejection, and the agent reported that back itself. A phase-only reading of
# "died early" must not be confused with this: the agent set phase to
# 'dispatched' here too (it has not heard back from ComfyUI yet, same as a
# job about to be retried), and it must still end up NOT retried, because
# nothing here went through the reaper's worker-death path at all.
check("a rejected workflow is not retried -- no 'retry' event was published",
      "retry" not in kinds, kinds)

requeues, requeue_cmds = watcher.stop()
check("comfy:queue received exactly one write for this job -- the submit's "
      "own insert, and no second one putting the rejection back. Counted "
      "from before the submit, because a watcher armed after it misses a "
      "requeue that beats MONITOR to the key",
      requeues == 1, (requeues, requeue_cmds))
state = r.hgetall(state_key(job_id))
check("attempt_count stayed at 0 -- a rejection is a terminal failure, not "
      "an attempt that gets retried",
      state.get("attempt_count") in (None, "0"), state.get("attempt_count"))

print("\n== cancel")
job = post("/api/generate", {"workflow": {"__slow__": {"class_type": "KSampler"}}})
time.sleep(2)
post(f"/api/jobs/{job['job_id']}/cancel")
kinds, terminal = drain(job["job_id"], timeout=60)
check("terminated as cancelled", terminal and terminal["type"] == "cancelled",
      terminal["type"] if terminal else None)

print("\n== SIGTERM drains the job in flight instead of dropping it")
agent_pid = int(sys.argv[1])
job = post("/api/generate", {"workflow": {"__slow__": {"class_type": "KSampler"}}})
time.sleep(3)          # let the agent pick it up and get into the job
os.kill(agent_pid, signal.SIGTERM)
kinds, terminal = drain(job["job_id"], timeout=90)
check("the in-flight job still reached a terminal state after SIGTERM",
      terminal is not None, terminal)
check("and that state was 'completed', not a silent drop",
      terminal and terminal["type"] == "completed",
      terminal["type"] if terminal else None)

deadline = time.time() + 30
exited = False
while time.time() < deadline:
    try:
        os.kill(agent_pid, 0)
        time.sleep(0.5)
    except ProcessLookupError:
        exited = True
        break
check("the agent then exited on its own", exited)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
