"""Assertions against the running gateway + worker."""
import json, sys, time, urllib.request
import websocket

GW = "http://127.0.0.1:8100"
failures = []

def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)

def post(path, body):
    req = urllib.request.Request(GW + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def get(path):
    return json.loads(urllib.request.urlopen(GW + path, timeout=10).read())

print("\n== readiness")
check("readyz reports Redis reachable", get("/readyz")["ok"])
check("stats endpoint works", "queue_depth" in get("/api/stats"))
check("a worker registered itself", get("/api/stats")["workers_registered"] >= 1,
      get("/api/stats"))

print("\n== submit then stream (the normal path)")
job = post("/api/generate", {"workflow": {"3": {"class_type": "KSampler", "inputs": {}}}})
check("generate returns a job id", bool(job.get("job_id")), job)

# Deliberately connect LATE, after the job has almost certainly finished, to
# prove the Stream replays rather than dropping what happened before we arrived.
time.sleep(3)

ws = websocket.WebSocket()
ws.connect(f"ws://127.0.0.1:8100/ws/{job['job_id']}", timeout=10)
ws.settimeout(20)

events, terminal = [], None
while True:
    try:
        msg = json.loads(ws.recv())
    except Exception as exc:
        print("  recv stopped:", exc)
        break
    if msg.get("type") == "ping":
        continue
    events.append(msg)
    if msg["type"] in ("completed", "failed", "cancelled"):
        terminal = msg
        break
ws.close()

kinds = [e["type"] for e in events]
print("  events replayed:", kinds)

check("late subscriber still got the 'queued' event", "queued" in kinds, kinds)
check("got 'started'", "started" in kinds)
check("got progress events", "progress" in kinds)
check("terminated as completed", terminal and terminal["type"] == "completed", terminal)

progress = [e for e in events if e["type"] == "progress"]
check("progress was filtered by prompt_id (no foreign events)",
      all(e["data"].get("prompt_id") != "other-prompt" for e in progress),
      [e["data"].get("prompt_id") for e in progress])
check("foreign terminal event did NOT end the job early",
      len(progress) >= 3, f"{len(progress)} progress events")

images = (terminal or {}).get("data", {}).get("images", [])
check("completion carried the output manifest", len(images) == 1, images)
check("image url was rewritten for the gateway",
      images and images[0]["url"] == "/outputs/out_0001.png", images)

print("\n== serving the image off the shared volume")
body = urllib.request.urlopen(GW + images[0]["url"], timeout=10).read()
check("image is served", b"fake png" in body)

try:
    urllib.request.urlopen(GW + "/outputs/../../etc/passwd", timeout=10)
    check("path traversal is blocked", False, "it was NOT blocked")
except urllib.error.HTTPError as exc:
    check("path traversal is blocked", exc.code in (403, 404), exc.code)

print("\n== job state")
state = get(f"/api/jobs/{job['job_id']}")
check("job state is completed", state.get("status") == "completed", state)

print("\n== reconnect replays the whole stream")
ws2 = websocket.WebSocket()
ws2.connect(f"ws://127.0.0.1:8100/ws/{job['job_id']}", timeout=10)
ws2.settimeout(15)
replay = []
while True:
    try:
        m = json.loads(ws2.recv())
    except Exception:
        break
    if m.get("type") == "ping":
        continue
    replay.append(m["type"])
    if m["type"] in ("completed", "failed", "cancelled"):
        break
ws2.close()
check("second connection replayed identically", replay == kinds, f"{replay} vs {kinds}")

print("\n== bad input")
try:
    post("/api/generate", {"workflow": "not-an-object"})
    check("non-object workflow rejected", False)
except urllib.error.HTTPError as exc:
    check("non-object workflow rejected", exc.code == 400, exc.code)

try:
    get("/api/jobs/does-not-exist")
    check("unknown job is a 404", False)
except urllib.error.HTTPError as exc:
    check("unknown job is a 404", exc.code == 404, exc.code)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
