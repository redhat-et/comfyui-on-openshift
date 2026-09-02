"""A job the agent gives up on is INTERRUPTED at ComfyUI — and if ComfyUI
will not stop, the agent exits rather than take work it cannot run.

The mechanism. ComfyUI executes one prompt at a time and binds 127.0.0.1, so
this agent is the only thing that ever submits to it and the only thing
that can ever stop it. When a job hit JOB_TIMEOUT the agent raised, main()
reported the job failed, and the next BLMOVE took the next job — while
ComfyUI was still executing the one that timed out. The next job's prompt
went into ComfyUI's queue_pending behind it, no event ever arrived for it,
and it timed out too; and the one after that; every job on this worker,
until the wedged node finished or the pod was deleted by hand. The liveness
probe asks ComfyUI's HTTP server whether it is up, and it was. One custom
node stuck in a C call bricked the pod with everything green. The same held
for the mid-job RuntimeError (a closed socket with /history not knowing the
prompt) and for any other exception out of the receive loop.

Two scenarios, one per half of the fix, and both run against an agent this
check starts itself — it needs a JOB_TIMEOUT of seconds and an interrupt
budget it chose, and run.sh's agent has neither:

  A. THE INTERRUPT. A prompt that never finishes (`__never__`: one progress
     event, then silence, honouring /interrupt the way the real sampler
     does between steps). The job must fail at JOB_TIMEOUT naming the
     deadline, ComfyUI must have received exactly one /interrupt for it,
     ComfyUI's queue must be empty afterwards, and the NEXT job must
     complete normally on the SAME worker. The last of those is the
     assertion that fails on HEAD: with no interrupt the never-prompt holds
     ComfyUI's single execution slot, the next prompt waits behind it, and
     the next job times out exactly as the first did.

  B. THE EXIT. A prompt that ignores /interrupt (`__unkillable__`). The
     agent must still fail the job, then — having waited its bounded
     INTERRUPT_DRAIN_TIMEOUT for ComfyUI's queue to empty and seen it not
     — exit with a non-zero status and deregister, instead of taking the
     next job. In the pod, start.sh waits on both children and exits when
     either does, so the kubelet restarts the container: a ComfyUI that
     cannot be interrupted is replaced by one that can, and the queue is
     served by a worker that can run it. On HEAD the agent sits there
     polling, alive, taking jobs it cannot run.

The stub was made serial for this (fake_comfy.py, execution_lock): with the
old concurrent stub the next job simply ran beside the wedged one, and no
assertion about the next job could fail. Every stub prompt this check
starts is released in the `finally` below, because a never-ending prompt
left behind would hold ComfyUI's slot for every later check in the suite.
"""
import json, os, signal, subprocess, sys, time, urllib.request, uuid
import redis

from worker_ids import heartbeat_keys

sys.stdout.reconfigure(line_buffering=True)

GW = "http://127.0.0.1:8100"
COMFY = "http://127.0.0.1:8999"
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None
WORKER_AGENT = os.environ["WORKER_AGENT"]
RECV_TIMEOUT = int(os.environ.get("RECV_TIMEOUT", "60"))

# The agent this check starts. JOB_TIMEOUT short enough that a wedged job
# resolves in seconds; the drain budget shorter still, so scenario B's exit
# is observed inside this check rather than inside CHECK_TIMEOUT.
JOB_TIMEOUT = 6
DRAIN_TIMEOUT = 8
TERMINAL_TYPES = {"completed", "failed", "cancelled"}

failures = []
r = redis.from_url(REDIS_URL, password=REDIS_PASSWORD, decode_responses=True)


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def post(path, body=None, base=GW):
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(path, base=GW):
    return json.loads(urllib.request.urlopen(base + path, timeout=10).read())


def interrupts():
    return get("/__interrupts__", base=COMFY)["count"]


def comfy_queue():
    q = get("/queue", base=COMFY)
    return q.get("queue_running", []), q.get("queue_pending", [])


def handoffs(probe):
    return get("/__received__", base=COMFY)["nodes"].count(probe)


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    state = subprocess.run(["ps", "-p", str(pid), "-o", "state="],
                           capture_output=True, text=True).stdout.strip()
    return not state.startswith("Z")


def wait_gone(pid, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


def drain(job_id, timeout=60):
    """Tail the gateway WebSocket to the first terminal event, with an
    absolute deadline the gateway's pings cannot extend (see check-30)."""
    import websocket
    ws = websocket.WebSocket()
    ws.connect(f"ws://127.0.0.1:8100/ws/{job_id}", timeout=10)
    deadline = time.time() + timeout
    seen, terminal = [], None
    while time.time() < deadline:
        ws.settimeout(max(1.0, deadline - time.time()))
        try:
            m = json.loads(ws.recv())
        except Exception:  # noqa: BLE001
            break
        if m.get("type") == "ping":
            continue
        seen.append(m["type"])
        if m["type"] in TERMINAL_TYPES:
            terminal = m
            break
    ws.close()
    return seen, terminal


def start_agent(hostname, env_extra, timeout=30):
    """A worker agent of this check's own, under a fixed HOSTNAME so its
    keys are findable (worker_ids.py), logging to agent-<hostname>.log so
    run.sh's failure-path `cat agent*.log` shows it."""
    env = dict(os.environ)
    env["HOSTNAME"] = hostname
    env.update(env_extra)
    log = open(f"agent-{hostname}.log", "w")
    proc = subprocess.Popen([sys.executable, WORKER_AGENT], env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if heartbeat_keys(r, hostname) or proc.poll() is not None:
            break
        time.sleep(0.2)
    return proc


def stop_agent(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:  # noqa: BLE001
        proc.kill()


def release_stub():
    try:
        post("/__release__", base=COMFY)
    except Exception:  # noqa: BLE001
        pass


suite_agent = int(sys.argv[1])
agent = None

try:
    print("\n== the suite's own agent stands down, so this check's agent is the only worker")

    # SIGTERM while idle: it exits on its own and deletes its heartbeat key.
    # run.sh notices it is gone and starts a fresh one for the next check.
    os.kill(suite_agent, signal.SIGTERM)
    check("the suite's agent exited on SIGTERM", wait_gone(suite_agent), suite_agent)
    deadline = time.time() + 15
    while time.time() < deadline and r.keys("comfy:worker:*"):
        time.sleep(0.1)
    check("no worker is registered before this check starts its own",
          not r.keys("comfy:worker:*"), r.keys("comfy:worker:*"))

    hostname = f"timeout-pod-{uuid.uuid4().hex[:6]}"
    agent = start_agent(hostname, {"JOB_TIMEOUT": str(JOB_TIMEOUT),
                                   "INTERRUPT_DRAIN_TIMEOUT": str(DRAIN_TIMEOUT)})
    check("this check's agent is up and heartbeating",
          agent.poll() is None and bool(heartbeat_keys(r, hostname)), hostname)

    print("\n== A: a job that hits JOB_TIMEOUT is interrupted, and the next job runs")

    interrupts_before = interrupts()
    # The marker key selects the stub's behaviour; the probe key is what the
    # stub's arrival log is asked about (check-36 does the same).
    probe = f"never-{uuid.uuid4().hex[:8]}"
    job_a = post("/api/generate", {"workflow": {
        "__never__": {"class_type": "KSampler"}, probe: {"class_type": "KSampler"}}})["job_id"]
    started = time.time()
    kinds, terminal = drain(job_a, timeout=JOB_TIMEOUT + RECV_TIMEOUT + DRAIN_TIMEOUT + 15)
    elapsed = time.time() - started

    check("ComfyUI was handed the never-finishing workflow", handoffs(probe) == 1, handoffs(probe))
    check("the job reached a terminal state", terminal is not None, kinds)
    check("and it failed", terminal and terminal["type"] == "failed", terminal and terminal["type"])
    err = ((terminal or {}).get("data") or {}).get("error", "")
    check("naming the deadline", f"{JOB_TIMEOUT}s" in err and "exceeded" in err, err[:160])
    check("inside JOB_TIMEOUT plus one receive timeout -- the deadline is checked "
          "between receives, not only when an event arrives",
          elapsed < JOB_TIMEOUT + RECV_TIMEOUT + 5, f"{elapsed:.1f}s")
    check("ComfyUI received exactly one /interrupt for it -- the agent does not "
          "walk away from a prompt it stopped listening to",
          interrupts() - interrupts_before == 1, interrupts() - interrupts_before)

    running, pending = comfy_queue()
    check("ComfyUI's queue is empty before the agent takes anything else -- "
          "nothing running, nothing pending behind it",
          not running and not pending, (running, pending))

    follow = f"after-{uuid.uuid4().hex[:8]}"
    job_b = post("/api/generate", {"workflow": {follow: {"class_type": "KSampler"}}})["job_id"]
    kinds2, terminal2 = drain(job_b, timeout=30)
    check("the NEXT job completes normally -- on HEAD it sat in ComfyUI's queue "
          "behind the prompt nobody interrupted and timed out exactly as the first did",
          terminal2 and terminal2["type"] == "completed", (kinds2, terminal2 and terminal2["type"]))
    check("on the same worker", r.hget(f"comfy:job:{job_b}:state", "worker") == hostname,
          r.hget(f"comfy:job:{job_b}:state", "worker"))
    check("which is still alive and registered",
          agent.poll() is None and bool(heartbeat_keys(r, hostname)), heartbeat_keys(r, hostname))

    print("\n== B: a prompt that ignores /interrupt -- the agent fails the job and EXITS")

    interrupts_before = interrupts()
    probe = f"wedged-{uuid.uuid4().hex[:8]}"
    job_c = post("/api/generate", {"workflow": {
        "__unkillable__": {"class_type": "KSampler"}, probe: {"class_type": "KSampler"}}})["job_id"]
    started = time.time()
    kinds, terminal = drain(job_c, timeout=JOB_TIMEOUT + RECV_TIMEOUT + DRAIN_TIMEOUT + 15)

    check("the job still reaches a terminal state", terminal is not None, kinds)
    check("and it failed", terminal and terminal["type"] == "failed", terminal and terminal["type"])
    check("ComfyUI was asked to interrupt it", interrupts() - interrupts_before >= 1,
          interrupts() - interrupts_before)

    running, _pending = comfy_queue()
    check("the fixture held: ComfyUI is STILL running the prompt after the "
          "interrupt -- this is the wedged case, not scenario A again",
          bool(running), running)

    exited = False
    exit_deadline = time.time() + DRAIN_TIMEOUT + 20
    while time.time() < exit_deadline:
        if agent.poll() is not None:
            exited = True
            break
        time.sleep(0.2)
    check("the agent exited on its own rather than take the next job -- a "
          "worker whose ComfyUI cannot be interrupted is a worker that "
          "cannot run anything, and only a restart replaces its ComfyUI",
          exited, agent.poll())
    check("with a non-zero status, so start.sh's wait -n ends the container "
          "and the kubelet restarts it",
          exited and agent.returncode not in (0, None), agent.returncode)
    check("and it deregistered on the way out -- no heartbeat left for the "
          "reaper to read as a live worker",
          not heartbeat_keys(r, hostname), heartbeat_keys(r, hostname))
    check("the job's own state agrees it failed, once",
          r.hget(f"comfy:job:{job_c}:state", "status") == "failed",
          r.hgetall(f"comfy:job:{job_c}:state"))

finally:
    release_stub()
    stop_agent(agent)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
