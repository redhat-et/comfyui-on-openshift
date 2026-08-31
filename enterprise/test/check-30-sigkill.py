"""SIGKILL: a worker that dies with no warning must not strand its job.

SIGTERM (check-20-failure-paths.py) is the polite case. This is the impolite
one — OOM kill, node reclaim — where the agent gets no chance to clean up. The
job it had moved into its processing list must be failed loudly by the
gateway's reaper once the worker's heartbeat lapses, and the worker must drop
out of the registered count on its own.

run.sh shrinks HEARTBEAT_TTL and REAPER_INTERVAL so this resolves in seconds
rather than the production-default minutes.
"""
import json, os, signal, sys, time, urllib.request
import websocket

GW = "http://127.0.0.1:8100"
failures = []

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


print("\n== SIGKILL mid-job: the reaper fails the stranded job loudly")
agent_pid = int(sys.argv[1])

job = post("/api/generate", {"workflow": {"__slow__": {"class_type": "KSampler"}}})
time.sleep(3)          # let the agent pick it up and get into the job
os.kill(agent_pid, signal.SIGKILL)

ws = websocket.WebSocket()
ws.connect(f"ws://127.0.0.1:8100/ws/{job['job_id']}", timeout=10)
ws.settimeout(60)
terminal = None
while True:
    try:
        m = json.loads(ws.recv())
    except Exception as exc:
        print("  recv stopped:", exc)
        break
    if m.get("type") == "ping":
        continue
    if m["type"] in ("completed", "failed", "cancelled"):
        terminal = m
        break
ws.close()

check("the job still reached a terminal state", terminal is not None, terminal)
check("and that state was 'failed', not a silent drop",
      terminal and terminal["type"] == "failed",
      terminal["type"] if terminal else None)
err = (terminal or {}).get("data", {}).get("error", "")
check("the failure names the dead worker as the cause", "worker" in err, err[:120])

state = get(f"/api/jobs/{job['job_id']}")
check("job state agrees", state.get("status") == "failed", state)

print("\n== the dead worker drops out of the registered count")
deadline = time.time() + 30
gone = False
while time.time() < deadline:
    if get("/api/stats")["workers_registered"] == 0:
        gone = True
        break
    time.sleep(1)
check("workers_registered returned to 0 via heartbeat expiry", gone,
      get("/api/stats"))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
