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

  A. SIGKILL BEFORE the workflow ever reached ComfyUI (the agent is still
     blocked inside submit_prompt(), waiting on ComfyUI's own acceptance of
     the POST). The gateway's reaper must requeue this job exactly once, as a
     non-terminal `retry` event a tailing browser does not stop at, and a
     second agent must pick it up and complete it.

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
import json, os, signal, subprocess, sys, time, urllib.request
import redis
import websocket

GW = "http://127.0.0.1:8100"
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


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(GW + path, data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(path):
    return json.loads(urllib.request.urlopen(GW + path, timeout=10).read())


def state_key(job_id):
    return f"comfy:job:{job_id}:state"


def drain(job_id, timeout=60):
    """Tail the WebSocket to its terminal event. A `retry` event is not one of
    the terminal types below, so if the gateway emits one the loop simply
    keeps reading past it — which is exactly the "a browser tailing the
    stream does not stop at it" claim, proven structurally rather than by a
    special case."""
    ws = websocket.WebSocket()
    ws.connect(f"ws://127.0.0.1:8100/ws/{job_id}", timeout=10)
    ws.settimeout(timeout)
    seen, terminal = [], None
    while True:
        try:
            m = json.loads(ws.recv())
        except Exception:
            break
        if m.get("type") == "ping":
            continue
        seen.append(m["type"])
        if m["type"] in ("completed", "failed", "cancelled"):
            terminal = m
            break
    ws.close()
    return seen, terminal


def start_extra_agent(hostname, timeout=30):
    """A second, independent worker agent, identified by a fixed HOSTNAME so
    its heartbeat key is predictable rather than parsed off log output. Logs
    to its own file (run.sh dumps agent*.log on failure; this one is named
    for what it is) rather than the log run.sh already tracks for argv[1]."""
    env = dict(os.environ)
    env["HOSTNAME"] = hostname
    # Named to match run.sh's own "agent*.log" glob, so its failure-path
    # `cat agent*.log` picks this one up too.
    log = open(f"agent-{hostname}.log", "w")
    proc = subprocess.Popen(
        [sys.executable, WORKER_AGENT], env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if r.exists(f"comfy:worker:{hostname}"):
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
    print("\n== A: SIGKILL before ComfyUI ever saw the workflow -> retried once, a restarted agent completes it")

    job = post("/api/generate", {"workflow": {"__slow_prompt__": {"class_type": "KSampler"}}})
    job_id = job["job_id"]

    # Wait for the ORIGINAL agent to pick the job up. It is now blocked inside
    # submit_prompt(), waiting on fake_comfy's SLOW_PROMPT_DELAY_S-second stall
    # before /prompt answers -- ComfyUI has not accepted (or rejected) the
    # workflow yet by any definition.
    deadline = time.time() + 5
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

    os.kill(agent_pid, signal.SIGKILL)

    # A fresh agent has to exist for the retried job to land on, or nothing on
    # this laptop is polling comfy:queue once the original is dead.
    agent2 = start_extra_agent("q2-retry-agent")

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

    check("comfy:queue is empty once the retried job finished",
          r.llen(QUEUE_KEY) == 0, r.llen(QUEUE_KEY))

    print("\n== B: SIGKILL after execution began -> stays a single terminal failure, never requeued")

    # agent2 is idle now that A finished with it; reuse it as the worker that
    # dies mid-generation, so this check needs no third process.
    job = post("/api/generate", {"workflow": {"__slow__": {"class_type": "KSampler"}}})
    job_id = job["job_id"]
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
    check("comfy:queue is empty after -- the job was not put back",
          r.llen(QUEUE_KEY) == 0, r.llen(QUEUE_KEY))

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
