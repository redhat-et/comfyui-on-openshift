"""A worker that RESTARTS must not be able to hide its own previous
incarnation's stranded job.

check-30-sigkill.py kills a worker and proves the gateway's reaper acts on
what it left behind. It proves that with a worker that never comes back, and
with a second agent under a DIFFERENT name -- which is the case the reaper
was built for and the only one that was ever exercised.

This is the case underneath it. `worker_agent.py` used to take its identity
from `HOSTNAME` alone, and a container that is restarted INSIDE THE SAME POD
keeps its HOSTNAME: kubelet's `restartPolicy: Always` replaces the container,
not the pod, so an OOM-killed worker comes back with the identity it died
with. That identity names two things -- the heartbeat key and the processing
list -- and the gateway's reaper pairs them:

    if await conn.exists(f"{WORKER_KEY_PREFIX}{worker_id}"): continue

The whole liveness test is that one line. With the identity reused, the NEW
incarnation's heartbeat answers it on the DEAD incarnation's behalf, and the
`continue` skips the dead one's processing list -- not for a while, but for
as long as the pod keeps restarting. Everything the reaper exists to do stops
happening for that job: it never reaches a terminal state, so the browser's
progress bar never moves; its GPU seconds reach neither the submitter's line
nor the excluded one; and its processing entry sits in a `noeviction` Redis
with no TTL forever. README's failure table and docs/09-engineering-handoff.md
section 3 both promise this exact path is handled.

THE FIXTURE, and why each half of it is asserted rather than assumed. Three
things have to be true at the instant of the restart or this check proves
something else:

  1. The job is at phase `executing` -- ComfyUI really has the workflow. That
     makes this the never-retried kind of death (a workflow that OOM-killed
     one worker would OOM-kill the next), so the terminal state asserted
     below can only have come from the reaper, and the GPU seconds are real.
  2. The dead incarnation's stranded entry is still parked and UNREAPED when
     the replacement registers. If the reap had already happened, the restart
     would be irrelevant and every assertion below would pass on a broken
     reaper.
  3. The replacement registers inside HEARTBEAT_TTL of the kill, under the
     same identity. That is what makes this identity REUSE rather than an
     ordinary lapse-then-reap, and it is the one condition the two controls
     in the original sweep isolated: a replacement under a distinct hostname
     reaps correctly, and so does the same hostname restarted after the TTL
     had lapsed. check-30-sigkill.py's scenario B is the first of those
     controls and already runs, immediately before this file.

This check kills the agent it is handed (argv[1]) up front rather than
working beside it: it needs to know WHICH worker picks its job up, and
`run.sh` re-establishes a live agent before the next check either way.
"""
import json, os, signal, subprocess, sys, time, urllib.request, uuid
import redis
import websocket

from worker_ids import heartbeat_keys, processing_keys

sys.stdout.reconfigure(line_buffering=True)

GW = "http://127.0.0.1:8100"
COMFY = "http://127.0.0.1:8999"
QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
WORKER_AGENT = os.environ["WORKER_AGENT"]
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "180"))

# One pod name, used by both incarnations below -- this is the reuse.
HOSTNAME = f"restart-pod-{uuid.uuid4().hex[:6]}"
USER = f"restart-{uuid.uuid4().hex[:8]}@example.com"

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


def post(path, body=None, base=GW, headers=None):
    data = json.dumps(body).encode() if body is not None else b"{}"
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=data, headers=hdrs)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(path, base=GW):
    return json.loads(urllib.request.urlopen(base + path, timeout=10).read())


def comfy_saw(probe):
    """Has the stub ComfyUI been handed a workflow carrying this node? Same
    question, and the same answer source, as check-30-sigkill.py's."""
    return probe in get("/__received__", base=COMFY)["nodes"]


def state_key(job_id):
    return f"comfy:job:{job_id}:state"


def showback():
    try:
        return get("/api/showback")
    except Exception:  # noqa: BLE001
        return {}


def user_total(data, who):
    try:
        return float((data.get("users") or {}).get(who, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def excluded_total(data):
    try:
        return float(data.get("excluded_gpu_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def wait_for(predicate, timeout=30, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False


def drain(job_id, timeout=60):
    """Tail the WebSocket to its terminal event, with a real wall-clock
    ceiling -- see check-30-sigkill.py's copy for why the deadline is
    recomputed before every recv() rather than handed to settimeout() once
    (hub.py pings every 15s, and a ping resets a per-call timeout)."""
    ws = websocket.WebSocket()
    ws.connect(f"ws://127.0.0.1:8100/ws/{job_id}", timeout=10)
    deadline = time.time() + timeout
    seen, terminal = [], None
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            print(f"  drain({job_id}): no terminal event within {timeout}s "
                  f"-- gave up; events seen so far: {seen}")
            break
        ws.settimeout(remaining)
        try:
            m = json.loads(ws.recv())
        except Exception:  # noqa: BLE001
            break
        if m.get("type") == "ping":
            continue
        seen.append(m["type"])
        if m["type"] in ("completed", "failed", "cancelled"):
            terminal = m
            break
    ws.close()
    return seen, terminal


def start_agent(tag, timeout=40):
    """An agent under this check's fixed HOSTNAME. Readiness is taken from
    the agent's own log line rather than from the existence of a heartbeat
    key: the whole point of this check is a second incarnation under an
    identity whose heartbeat key is ALREADY there, so `EXISTS` cannot tell
    'the replacement is up' from 'its predecessor's key has not expired yet'.

    Named to match run.sh's own agent*.log glob, so a failing run dumps it."""
    env = dict(os.environ)
    env["HOSTNAME"] = HOSTNAME
    log_path = f"agent-{HOSTNAME}-{tag}.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, WORKER_AGENT], env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(log_path) as fh:
                if "ready, polling" in fh.read():
                    return proc
        except OSError:
            pass
        if proc.poll() is not None:
            return proc  # died on startup; later assertions will show it
        time.sleep(0.1)
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


handed_pid = int(sys.argv[1])
first = second = None

try:
    print("\n== the suite's own agent stands down, so this check owns the queue")

    # SIGTERM rather than SIGKILL: the agent's own drain deletes its heartbeat
    # key on the way out (worker_agent.py's finally), so the "exactly one
    # heartbeat, and it is ours" assertion below is reached in a second or two
    # instead of after a full HEARTBEAT_TTL of phantom registration.
    os.kill(handed_pid, signal.SIGTERM)

    alone = wait_for(lambda: not list(r.scan_iter(match="comfy:worker:*")), timeout=40)
    check("no worker is registered before this check starts its own -- so the "
          "job below can only be picked up by the incarnation this check "
          "starts, and 'which worker died' is not a guess",
          alone, list(r.scan_iter(match="comfy:worker:*")))

    print(f"\n== incarnation 1 of pod {HOSTNAME} picks up a job and gets it into ComfyUI")

    first = start_agent("first")

    hb = heartbeat_keys(r, HOSTNAME)
    check("incarnation 1 registered exactly one heartbeat under this pod name",
          len(hb) == 1, hb)

    probe = f"probe-{uuid.uuid4().hex[:8]}"
    job_id = post("/api/generate", {"workflow": {probe: {"class_type": "KSampler"},
                                                "__slow__": {"class_type": "KSampler"}}},
                  headers={"X-Forwarded-User": USER})["job_id"]
    t_started = time.time()

    executing = wait_for(
        lambda: get(f"/api/jobs/{job_id}").get("phase") == "executing", timeout=30)
    state = get(f"/api/jobs/{job_id}")

    check("the job is running on incarnation 1, at phase 'executing' -- so "
          "this is the never-retried kind of death and the terminal state "
          "asserted below can only come from the reaper",
          executing and state.get("phase") == "executing", state)
    check("the job's state names this pod as the worker running it -- the "
          "identity the restart is about to reuse",
          state.get("worker") == HOSTNAME, state.get("worker"))
    check("and ComfyUI really was handed the workflow, so the GPU seconds "
          "this check goes looking for were actually spent",
          comfy_saw(probe), probe)

    stranded = processing_keys(r, HOSTNAME)
    check("incarnation 1 holds the job in exactly one processing list",
          len(stranded) == 1 and r.llen(stranded[0]) == 1,
          [(k, r.llen(k)) for k in stranded])

    stranded_key = stranded[0] if stranded else f"comfy:processing:{HOSTNAME}"

    baseline = showback()
    baseline_user = user_total(baseline, USER)
    baseline_excluded = excluded_total(baseline)

    print("\n== incarnation 1 is SIGKILLed and incarnation 2 comes up under the SAME pod name")

    time.sleep(2)   # a measurable, non-zero stretch of held card to account for
    ran_for = time.time() - t_started

    os.kill(first.pid, signal.SIGKILL)
    t_killed = time.time()

    second = start_agent("second")
    restart_gap = time.time() - t_killed

    check(f"incarnation 2 was polling {restart_gap:.1f}s after the kill, "
          f"inside the {HEARTBEAT_TTL}s HEARTBEAT_TTL -- the fixture is "
          f"identity REUSE, not an ordinary lapse-then-reap",
          restart_gap < HEARTBEAT_TTL, restart_gap)
    check("incarnation 2 is alive and heartbeating under the same pod name",
          bool(heartbeat_keys(r, HOSTNAME)), heartbeat_keys(r, HOSTNAME))
    check("and incarnation 1's stranded entry was still parked and unreaped "
          "at that moment -- had the reaper already acted, the restart would "
          "be beside the point and everything below would pass on a reaper "
          "that skips it",
          r.llen(stranded_key) == 1, (stranded_key, r.llen(stranded_key)))

    print("\n== the stranded job is reaped anyway")

    kinds, terminal = drain(job_id, timeout=60)

    check("THE BLOCKER: the stranded job still reached a terminal state, "
          "even though its own pod name is heartbeating again",
          terminal is not None, kinds)
    check("and that state was 'failed' -- a death with the workflow already "
          "at ComfyUI is never replayed onto a second card",
          terminal is not None and terminal["type"] == "failed" and "retry" not in kinds,
          (terminal["type"] if terminal else None, kinds))

    final = r.hgetall(state_key(job_id))
    check("the job's own state agrees it is terminal",
          final.get("status") == "failed", final)
    check("attempt_count stayed at 0 -- reaping a restarted worker's job did "
          "not open the replay door check-35-retry-doors.py holds shut",
          final.get("attempt_count") in (None, "0"), final.get("attempt_count"))

    emptied = wait_for(lambda: r.llen(stranded_key) == 0, timeout=30)
    check("incarnation 1's processing list is empty -- no entry left with no "
          "TTL in a `noeviction` Redis for the lifetime of the pod",
          emptied, (stranded_key, r.lrange(stranded_key, 0, -1)))

    after = showback()
    recorded = ((user_total(after, USER) - baseline_user)
                + (excluded_total(after) - baseline_excluded))
    check(f"the ~{ran_for:.1f}s of card this job held before the kill landed "
          f"somewhere explicit -- the submitter's line or excluded_gpu_seconds "
          f"-- rather than in neither bucket, which is where a job the reaper "
          f"never sees ends up",
          recorded >= ran_for * 0.3,
          {"recorded": recorded, "ran_for": ran_for,
           "delta_user": user_total(after, USER) - baseline_user,
           "delta_excluded": excluded_total(after) - baseline_excluded})

finally:
    stop_agent(first)
    stop_agent(second)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
