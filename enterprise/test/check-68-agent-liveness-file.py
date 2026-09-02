"""The agent keeps a liveness file fresh while it is alive, so a probe that
cannot see Redis can still tell a polling agent from a wedged one.

The pod's livenessProbe asks ComfyUI (/system_stats) whether it is up, and
nothing asks the AGENT anything. The two ways the agent stops consuming the
queue without dying are both invisible to that probe: a Redis connection
that is blackholed rather than refused parks BLMOVE forever (no socket
timeout was set, and redis-py < 7 defaults to none), and a heartbeat thread
that cannot reach Redis swallows every error by design. Either way ComfyUI
answers, the probe stays green, the heartbeat key expires, the gateway
stops counting the worker — and the pod sits there holding a card, Running,
for as long as nobody notices.

So the agent touches a file — AGENT_LIVENESS_FILE, default
/tmp/comfy-agent-alive — once per pass of its poll loop and once per
heartbeat refresh, from the same thread that would be wedged. An exec probe
that reads the file's age is then a test of the agent's own loop, not of
ComfyUI's HTTP server: `stat -c %Y` age under 120 s, which is twice the
production HEARTBEAT_REFRESH (HEARTBEAT_TTL / 3 = 60 s), so one missed
refresh is not a restart and two consecutive ones are.

What is asserted is the file's mtime ADVANCING — while the agent idles, and
again while it is inside a job. Existence alone would pass on an agent that
touched it once at startup and then wedged, which is the exact state the
probe exists to catch. The second half matters as much as the first: a
generation may legally run for JOB_TIMEOUT (1800 s in production), and the
manifest's probe kills the pod at 120 s of silence, so a touch that only
happens between jobs would have the kubelet restart a pod in the middle of
every long render. The job loop calls heartbeat() on every pass, and the
touch lives there, so the same call covers both. Nothing here runs the
probe command itself: that lives in the manifest, and this suite reads no
manifest.
"""
import json, os, sys, time, urllib.request

sys.stdout.reconfigure(line_buffering=True)

HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "180"))
# worker_agent.py: HEARTBEAT_REFRESH = max(1.0, HEARTBEAT_TTL / 3.0). Two
# refreshes plus a margin, so a single late one cannot fail this.
REFRESH_S = max(1.0, HEARTBEAT_TTL / 3.0)
WAIT_S = 2 * REFRESH_S + 1
LIVENESS_FILE = os.environ.get("AGENT_LIVENESS_FILE", "/tmp/comfy-agent-alive")

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def mtime():
    try:
        return os.stat(LIVENESS_FILE).st_mtime
    except OSError:
        return None


print(f"\n== the agent's liveness file ({LIVENESS_FILE}) is kept fresh while it idles")

first = mtime()
check("the liveness file exists while the agent is up", first is not None, LIVENESS_FILE)
check("and it is recent -- touched within one refresh interval of now, not a "
      "leftover from a previous run",
      first is not None and time.time() - first < REFRESH_S + 1,
      None if first is None else round(time.time() - first, 2))

time.sleep(WAIT_S)
second = mtime()
check(f"its mtime advanced over {WAIT_S:.1f}s of idling -- the agent's own loop "
      f"is touching it, not a one-off at startup",
      first is not None and second is not None and second > first,
      (first, second))

print("\n== and while a job is RUNNING, not only between jobs")

GW = "http://127.0.0.1:8100"


def post(path, body):
    req = urllib.request.Request(GW + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(path):
    return json.loads(urllib.request.urlopen(GW + path, timeout=10).read())


# A slow job: the stub emits progress every two seconds for about six, which
# is longer than one refresh interval here and far longer than the loop's
# own per-recv pacing. The mtime is read once the job is running and again
# a little later while it still is.
job_id = post("/api/generate", {"workflow": {"__slow__": {"class_type": "KSampler"}}})["job_id"]
deadline = time.time() + 20
while time.time() < deadline and get(f"/api/jobs/{job_id}").get("status") != "running":
    time.sleep(0.1)
during_first = mtime()
time.sleep(min(WAIT_S, 4.0))
still_running = get(f"/api/jobs/{job_id}").get("status") == "running"
during_second = mtime()
check("the fixture held: the job was still running when the second reading was taken",
      still_running, get(f"/api/jobs/{job_id}").get("status"))
check("the mtime advanced while the agent was inside the job's event loop -- "
      "a render longer than the probe's 120s window must not read as a wedge",
      during_first is not None and during_second is not None and during_second > during_first,
      (during_first, during_second))

deadline = time.time() + 30
while time.time() < deadline and get(f"/api/jobs/{job_id}").get("status") == "running":
    time.sleep(0.2)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
