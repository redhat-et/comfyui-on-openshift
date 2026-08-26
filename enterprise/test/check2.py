"""Failure paths: rejected workflow, cancel, and SIGTERM drain."""
import json, os, signal, subprocess, sys, time, urllib.error, urllib.request
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

def drain(job_id, timeout=90):
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


print("\n== a workflow ComfyUI rejects becomes a 'failed' event, with the reason")
job = post("/api/generate", {"workflow": {"__fail__": {"class_type": "KSampler"}}})
kinds, terminal = drain(job["job_id"], timeout=30)
check("terminated as failed", terminal and terminal["type"] == "failed", terminal)
err = (terminal or {}).get("data", {}).get("error", "")
check("the reason from ComfyUI is surfaced, not swallowed",
      "ckpt_name" in err, err[:160])

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
