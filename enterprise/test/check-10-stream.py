"""Assertions against the running gateway + worker."""
import json, os, socket, struct, sys, time, urllib.request, uuid
import redis
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

# The filter is ONE line in worker_agent.py's recv loop and it covers every
# event type, terminal ones included. That second half needs saying out loud,
# because the stub's foreign terminal event (`executing`, node=None,
# prompt_id="other-prompt") arrives AFTER all three of this job's own progress
# events: an agent that filters progress but not terminals ends the job on
# somebody else's completion, reports success on work still running, and still
# shows three progress events here. "len(progress) >= 3" was the assertion
# that stood here, and it passes on exactly that agent.
#
# The stream is the whole record of the job rather than a snapshot taken
# afterwards -- Redis Streams keep every event from `queued` onwards, which is
# what the late-subscriber assertions above rely on -- so these are exact
# counts over a complete history, not an absence checked after the fact.
own_prompt = next((e["data"].get("prompt_id") for e in events
                   if e["type"] == "accepted" and isinstance(e.get("data"), dict)), None)

check("fixture: the job's own prompt_id is on the stream and is not the "
      "stub's foreign one, so 'foreign' below means something",
      bool(own_prompt) and own_prompt != "other-prompt", own_prompt)

foreign = [e for e in events
           if isinstance(e.get("data"), dict)
           and e["data"].get("prompt_id") not in (None, own_prompt)]
check("not one event belonging to another prompt reached this job's stream — "
      "terminal events included, which is the half a progress-only filter "
      "still gets wrong",
      len(foreign) == 0, foreign)

own_terminal = [e for e in events
                if isinstance(e.get("data"), dict)
                and e["data"].get("prompt_id") == own_prompt
                and (e["type"] == "execution_success"
                     or (e["type"] == "executing" and e["data"].get("node") is None))]
check("the job ended on exactly one terminal event, and it was its OWN: the "
      "foreign terminal event the stub sends first ended nothing",
      len(own_terminal) == 1, [e["type"] for e in events])

check("all three of this job's progress events survived the filter",
      len(progress) == 3, f"{len(progress)} progress events")

images = (terminal or {}).get("data", {}).get("images", [])
check("completion carried the output manifest", len(images) == 1, images)

# Was: check("image url was rewritten for the gateway",
#            images and images[0]["url"] == "/outputs/out_0001.png", images)
# A flat name every job on this gateway shares is exactly what let two
# different users' outputs collide onto the same URL in the first place
# (docs/10-roadmap.md, Q3 -- see check-60-user-workspaces.py). The literal
# pin cannot survive Q3 unchanged: a per-submitter workspace directory means
# this job -- submitted with no X-Forwarded-User header at all -- no longer
# resolves to a bare filename directly under /outputs/. What replaces it is
# strictly stronger as a claim about isolation, not weaker as a string match:
# it requires a real, non-flat workspace directory to exist, where the old
# pin only required one specific flat string.
url = images[0]["url"] if images else ""
workspace_depth = (
    len(url[len("/outputs/"):].split("/")) - 1 if url.startswith("/outputs/") else -1
)
check("image url is scoped under a per-submitter output workspace "
      "directory, not the single flat name every job on this gateway "
      "shared before (docs/10-roadmap.md, Q3)",
      workspace_depth >= 1, images)

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

print("\n== attribution and metrics")
req = urllib.request.Request(
    GW + "/api/generate",
    data=json.dumps({"workflow": {"3": {"class_type": "KSampler", "inputs": {}}}}).encode(),
    headers={"Content-Type": "application/json", "X-Forwarded-User": "alice"})
job2 = json.loads(urllib.request.urlopen(req, timeout=10).read())
state2 = get(f"/api/jobs/{job2['job_id']}")
check("the authenticated user is stamped onto the job",
      state2.get("user") == "alice", state2)

metrics = urllib.request.urlopen(GW + "/metrics", timeout=10).read().decode()
check("prometheus metrics are served",
      "comfy_queue_depth" in metrics and "comfy_workers_registered" in metrics,
      metrics.splitlines()[:2])

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


def raw_post(path, data, headers):
    """Like post(), but returns (status, body) for error responses too."""
    req = urllib.request.Request(GW + path, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


WORKFLOW_BYTES = json.dumps({"workflow": {"3": {"class_type": "KSampler", "inputs": {}}}}).encode()

status, body = raw_post("/api/generate", b"{this is not json", {"Content-Type": "application/json"})
check("a body that is not JSON is refused with 400, not a 500", status == 400, (status, body[:80]))

status, body = raw_post("/api/generate", WORKFLOW_BYTES, {"Content-Type": "text/plain"})
check("a POST whose Content-Type is not application/json is refused with 415 -- "
      "the body is parsed as JSON whatever the header says, so a form post or a "
      "cross-site text/plain submission must not be able to queue a job",
      status == 415, (status, body[:80]))

status, body = raw_post("/api/generate", WORKFLOW_BYTES,
                        {"Content-Type": "application/json; charset=utf-8"})
check("a Content-Type carrying a charset parameter is still application/json",
      status == 200, (status, body[:80]))

# The declared-length path: the handler refuses on Content-Length before it
# reads a byte of the body, so the response is on the wire while this socket
# has sent none of it -- which is why this is a raw socket rather than urllib,
# whose sendall of two megabytes would hit the closed connection first.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(2 * 1024 * 1024)))
sock = socket.create_connection(("127.0.0.1", 8100), timeout=10)
sock.sendall((f"POST /api/generate HTTP/1.1\r\nHost: 127.0.0.1\r\n"
              f"Content-Type: application/json\r\n"
              f"Content-Length: {MAX_BODY_BYTES + 1}\r\n\r\n").encode())
sock.sendall(b"{")
status_line = b""
try:
    status_line = sock.recv(64).split(b"\r\n", 1)[0]
except OSError as exc:
    status_line = repr(exc).encode()
sock.close()
check("a body declared larger than MAX_BODY_BYTES is refused with 413 before it is read",
      b" 413 " in status_line, status_line)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
