"""Out of memory: the two paths that behave completely differently.

Exceeding memory is the easiest failure to hit in real ComfyUI work, and the
README's "Out of memory, by kind" table documents two outcomes that share a
name and nothing else. This asserts the halves of that table a laptop can
reach.

  VRAM / CUDA OOM -- the common one. ComfyUI catches it inside the sampler and
  emits execution_error. The job must end as `failed` carrying ComfyUI's OWN
  message, the worker must stay healthy and take the next job, and it must NOT
  be retried: the same workflow would fail the same way on any card, so a retry
  buys nothing and spends a second GPU-hour. That last point only became
  load-bearing once Q2 added retry at all.

  HOST RAM OOM -- the kernel kills the ComfyUI process. Here that is simulated
  as far as this harness honestly can: the socket closes mid-job and /history
  never learns the prompt existed. The agent must surface a terminal failure
  rather than parking forever on a dead process. Note what actually resolves
  it -- the per-job deadline, not the closed socket. This is the only
  assertion in the suite that exercises JOB_TIMEOUT, which is why it is
  allowed to cost a minute.

WHAT THIS CANNOT REACH, and why the coverage claim stops here: a real CUDA
allocation failure, real host memory pressure, and the kubelet's
eviction-versus-OOMKilled distinction. In production the host-RAM case also
takes the POD down -- start.sh waits on both children, so a dead ComfyUI ends
the pod and the gateway's reaper fails the stranded job. There is no start.sh
in this harness; that half is check-30's SIGKILL territory and cluster day's.
"""
import json, os, time, urllib.request
import redis
import websocket

from queue_watch import QueueWriteWatcher

GW = "http://127.0.0.1:8100"
QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
failures = []

r = redis.from_url(
    os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0"),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)


def state_key(job_id):
    return f"comfy:job:{job_id}:state"

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

def drain(job_id, timeout=90):
    """Tail one job to its terminal event, with a real wall-clock deadline.

    The deadline is absolute rather than a socket timeout: the gateway sends
    keepalives, and a per-recv timeout is reset by every one of them, so a job
    that never terminates would hang until run.sh's CHECK_TIMEOUT killed this
    process with its output still in the buffer. Same reasoning as check-30.
    """
    ws = websocket.WebSocket()
    ws.connect(f"ws://127.0.0.1:8100/ws/{job_id}", timeout=10)
    deadline = time.time() + timeout
    seen, terminal = [], None
    while time.time() < deadline:
        ws.settimeout(max(1.0, deadline - time.time()))
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


# ---------------------------------------------------------------------------
print("\n== VRAM OOM: terminal, with ComfyUI's own message, and never retried")

# Armed before the submit, and counted rather than asserted absent. A watcher
# started after the submit misses a requeue that beats MONITOR to the key --
# the wave-2a re-gate proved exactly that against a live mutation.
watcher = QueueWriteWatcher(
    os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0"),
    os.environ.get("REDIS_PASSWORD") or None,
    QUEUE_KEY,
).start()

job = post("/api/generate", {"workflow": {"__vram_oom__": {"class_type": "KSampler"}}})
job_id = job["job_id"]

kinds, terminal = drain(job_id, timeout=45)
check("a VRAM OOM ends the job as failed", terminal and terminal["type"] == "failed", terminal)

err = (terminal or {}).get("data", {}).get("error", "")
check("ComfyUI's own allocation message reaches the user, not a generic error",
      "would exceed allowed memory" in err and "2.44 GiB" in err, err[:160])

check("the failure is not dressed up as a retry -- no 'retry' event",
      "retry" not in kinds, kinds)

requeues, requeue_cmds = watcher.stop()
check("comfy:queue received exactly one write for this job -- the submit's own "
      "insert, and no requeue: the same workflow would exhaust the same card "
      "again, so a retry only spends a second GPU-hour",
      requeues == 1, (requeues, requeue_cmds))

st = r.hgetall(state_key(job_id))
check("attempt_count stayed at 0", st.get("attempt_count", "0") == "0", st.get("attempt_count"))

# The card being full for one workflow does not make the worker broken.
follow = post("/api/generate", {"workflow": {"9": {"class_type": "SaveImage"}}})
kinds2, terminal2 = drain(follow["job_id"], timeout=45)
check("the worker survives and completes the next job",
      terminal2 and terminal2["type"] == "completed", terminal2 and terminal2["type"])


# ---------------------------------------------------------------------------
print("\n== host RAM OOM: ComfyUI dies mid-job, and the agent does not park on it")

job = post("/api/generate", {"workflow": {"__die__": {"class_type": "KSampler"}}})
job_id = job["job_id"]

started = time.time()
kinds, terminal = drain(job_id, timeout=90)
elapsed = time.time() - started

check("a dead ComfyUI still produces a terminal event",
      terminal is not None, terminal)
check("that terminal event is a failure, not a silent completion",
      terminal and terminal["type"] == "failed", terminal and terminal["type"])
check("the failure carries a reason rather than an empty error",
      len(((terminal or {}).get("data", {}) or {}).get("error", "")) > 0,
      ((terminal or {}).get("data", {}) or {}).get("error", "")[:160])
# A closed socket must resolve NOW, not at the job deadline. Writing this check
# is what found that it did not: a server-side close arrives as an empty frame,
# "" is a str so it slipped past the binary-frame guard, failed to parse, and
# hit `continue` -- spinning the recv loop at full speed for the whole of
# JOB_TIMEOUT while holding a GPU. 1800s by default in production.
check("a closed socket resolves immediately, not at the job deadline -- a dead "
      "ComfyUI must not hold a card for JOB_TIMEOUT",
      elapsed < 30, f"{elapsed:.1f}s")
check("and the reason names the lost connection rather than the deadline",
      terminal and "closed the connection" in ((terminal.get("data") or {}).get("error", "")),
      ((terminal or {}).get("data") or {}).get("error", "")[:160])

follow = post("/api/generate", {"workflow": {"9": {"class_type": "SaveImage"}}})
kinds2, terminal2 = drain(follow["job_id"], timeout=45)
check("the worker still takes the next job after ComfyUI came back",
      terminal2 and terminal2["type"] == "completed", terminal2 and terminal2["type"])


print("\nall assertions passed" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
