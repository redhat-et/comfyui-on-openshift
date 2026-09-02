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

print("\n== the landing page is served")
page = urllib.request.urlopen(GW + "/", timeout=10).read().decode()
check("GET / serves index.html", "ComfyUI" in page and "/api/generate" in page, page[:80])

print("\n== a WebSocket for a job that does not exist is closed, not held open")
# Before this, /ws/<anything> was accepted and parked on a 15-second XREAD loop
# forever, one Redis connection each, for an id that names no job and never
# will. Closed with an application code (4404) so a browser can tell "no such
# job" from "the gateway went away"; recv_frame() reads the close frame itself
# rather than websocket-client's "" rendering of it.
ws3 = websocket.WebSocket()
ws3.connect(f"ws://127.0.0.1:8100/ws/no-such-job-{uuid.uuid4().hex[:8]}", timeout=10)
ws3.settimeout(10)
close_code = None
try:
    frame = ws3.recv_frame()
    if frame.opcode == websocket.ABNF.OPCODE_CLOSE and len(frame.data) >= 2:
        close_code = struct.unpack("!H", frame.data[:2])[0]
    else:
        close_code = f"opcode {frame.opcode}: {frame.data[:60]!r}"
except Exception as exc:  # noqa: BLE001
    close_code = repr(exc)
ws3.close()
check("the server closes a WebSocket for an unknown job with code 4404 within "
      "seconds, instead of holding it open on a ping loop",
      close_code == 4404, close_code)

print("\n== url rewriting confines what a raw ComfyUI event reports")
# Live `executed` events reach the browser through rewrite_image_urls() with
# whatever {filename, subfolder} ComfyUI (or a custom node) put in them -- the
# worker's confinement applies to the /history manifest it builds the terminal
# event from, not to the events it forwards verbatim. The stream is written
# directly: this is about what the gateway does with an event, not about how
# a worker produces one. A filename or subfolder component that is not a bare
# single path component must lose its URL (the event itself is still
# delivered); ordinary shapes must still get one.
r = redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0"),
                   password=os.environ.get("REDIS_PASSWORD") or None, decode_responses=True)
fake_job = f"rewrite-{uuid.uuid4().hex[:12]}"
state_k, stream_k = f"comfy:job:{fake_job}:state", f"comfy:job:{fake_job}:events"
r.hset(state_k, mapping={"status": "running"})
r.expire(state_k, 120)
REPORTED = [
    {"filename": "ok.png", "subfolder": "ws-abc", "type": "output"},
    {"filename": "flat.png", "subfolder": "", "type": "output"},
    {"filename": "deep.png", "subfolder": "ws-abc/run1", "type": "output"},
    {"filename": "../../etc/passwd", "subfolder": "", "type": "output"},
    {"filename": "x.png", "subfolder": "../../etc", "type": "output"},
    {"filename": "x.png", "subfolder": "/abs", "type": "output"},
    {"filename": "a/b.png", "subfolder": "", "type": "output"},
    {"filename": "..", "subfolder": "ws-abc", "type": "output"},
    {"filename": "x.png", "subfolder": "ws-abc//run1", "type": "output"},
]
r.xadd(stream_k, {"data": json.dumps({"type": "executed", "data": {
    "node": "9", "prompt_id": "p-rewrite", "output": {"images": REPORTED}}})})
r.xadd(stream_k, {"data": json.dumps({"type": "completed", "data": {"images": []}})})
r.expire(stream_k, 120)

ws4 = websocket.WebSocket()
ws4.connect(f"ws://127.0.0.1:8100/ws/{fake_job}", timeout=10)
ws4.settimeout(10)
executed = None
while True:
    try:
        m = json.loads(ws4.recv())
    except Exception:  # noqa: BLE001
        break
    if m.get("type") == "executed":
        executed = m
    if m.get("type") in ("completed", "failed", "cancelled"):
        break
ws4.close()

got = (executed or {}).get("data", {}).get("output", {}).get("images", [])
urls = [image.get("url") for image in got]
check("the executed event itself was delivered with every image entry intact",
      len(got) == len(REPORTED), urls)
check("ordinary entries are rewritten to gateway URLs",
      urls[:3] == ["/outputs/ws-abc/ok.png", "/outputs/flat.png", "/outputs/ws-abc/run1/deep.png"],
      urls[:3])
check("a filename or subfolder that is not made of bare path components gets NO "
      "url -- traversal, an absolute subfolder, a separator inside filename, "
      "'..' as a filename and an empty component are all dropped rather than "
      "handed to the browser as /outputs/../...",
      all(url is None for url in urls[3:]), urls[3:])
r.delete(state_k, stream_k)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
