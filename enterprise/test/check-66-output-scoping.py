"""
Under AUTH_MODE=oauth, a submitter can read their own outputs and nobody
else's -- and the showback report names other people only to operators.

Q3 (check-60-user-workspaces.py) put every submitter's outputs in their own
directory and said, deliberately, that reads were not scoped: under
AUTH_MODE=none the identity is a header the caller wrote themselves. That
argument is exactly right for AUTH_MODE=none and exactly wrong for
AUTH_MODE=oauth, where oauth-proxy sets X-Forwarded-User from a real login
and strips whatever the client sent -- and where, before this check, any
logged-in user could read any other user's images. The workspace name is a
pure function of the username (slug + 12 hex of sha256), /api/showback
listed every submitter, so the directory names were one line of arithmetic
away for anyone with an account.

This check runs a dedicated gateway with AUTH_MODE=oauth (the shared one on
:8100 runs with no AUTH_MODE, i.e. none) and asserts:

  (a) a file inside alice's workspace is served to alice (200) and refused to
      bob and to a request with no identity (403). alice's workspace is
      DISCOVERED the way check-60 discovers it -- off the URL the worker
      reports for a real job of hers -- not recomputed here, so this also
      proves the gateway's copy of workspace_name() (the SHARED WORKSPACE
      block, mirrored from worker_agent.py) agrees with the worker's: a
      gateway that computed a different name would refuse alice her own
      file. bob's own workspace is checked the same way, so the scoping is
      per caller and not "alice only".
  (b) the scope is decided on the RESOLVED path, not on the first URL
      segment: /outputs/<alice>/../<bob>/file names alice's workspace in its
      first segment and bob's on disk, and is refused.
  (c) the shared AUTH_MODE=none gateway still serves alice's file to bob.
      Pinned on purpose: that is the documented behaviour of that mode, and a
      scoping rule that silently applied there would be authorization
      derived from an unauthenticated header.
  (d) /api/showback under oauth returns only the caller's own row to an
      ordinary caller, plus the pool totals; a caller named in
      SHOWBACK_OPERATORS gets every row. Under none the report is unscoped
      exactly as before.

The workspaces are written to directly rather than through more jobs -- the
system under test is who may READ a file, not how one gets written, and
check-60 already owns that half.
"""
import json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request, uuid
import redis
import websocket

sys.stdout.reconfigure(line_buffering=True)

GW = "http://127.0.0.1:8100"
OGW_PORT = 8103
OGW = f"http://127.0.0.1:{OGW_PORT}"
OUTPUT_ROOT = pathlib.Path(os.environ["OUTPUT_ROOT"]).resolve()
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None

OPERATOR = "ops@example.com"
ALICE = f"scope-alice-{uuid.uuid4().hex[:6]}@example.com"
BOB = f"scope-bob-{uuid.uuid4().hex[:6]}@example.com"

failures = []
r = redis.from_url(REDIS_URL, password=REDIS_PASSWORD, decode_responses=True)


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def request(method, path, base=GW, user=None, body=None):
    """(status, body-bytes-or-parsed). user=None sends no identity at all."""
    headers = {"Content-Type": "application/json"}
    if user is not None:
        headers["X-Forwarded-User"] = user
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        raw, status = resp.read(), resp.getcode()
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return status, raw


def seed_output():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "out_0001.png").write_bytes(b"fake png bytes")


def drain(job_id, timeout=20):
    ws = websocket.WebSocket()
    ws.connect(f"ws://127.0.0.1:8100/ws/{job_id}", timeout=10)
    ws.settimeout(timeout)
    terminal = None
    while True:
        try:
            m = json.loads(ws.recv())
        except Exception:  # noqa: BLE001
            break
        if m.get("type") == "ping":
            continue
        if m["type"] in ("completed", "failed", "cancelled"):
            terminal = m
            break
    ws.close()
    return terminal


def workspace_of(user):
    """The directory the WORKER put this user's output in, read off a real
    job's URL -- check-60's discovery, and the reason (a) doubles as a
    mirror test of the gateway's workspace_name()."""
    seed_output()
    status, resp = request("POST", "/api/generate", user=user,
                           body={"workflow": {"3": {"class_type": "KSampler", "inputs": {}}}})
    terminal = drain(resp["job_id"]) if status == 200 else None
    images = (terminal or {}).get("data", {}).get("images", [])
    url = images[0]["url"] if images else ""
    parts = url[len("/outputs/"):].split("/") if url.startswith("/outputs/") else []
    return parts[0] if len(parts) >= 2 else None


print(f"--- starting a dedicated gateway on :{OGW_PORT} with AUTH_MODE=oauth "
      f"SHOWBACK_OPERATORS={OPERATOR}")

env = dict(os.environ)
env["AUTH_MODE"] = "oauth"
env["SHOWBACK_OPERATORS"] = f" {OPERATOR} , auditor@example.com"
ogw_log = open("oauth-gateway.log", "w")
ogw = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1",
     "--port", str(OGW_PORT), "--log-level", "warning"],
    env=env, stdout=ogw_log, stderr=subprocess.STDOUT)

up = False
deadline = time.time() + 30
while time.time() < deadline and ogw.poll() is None:
    try:
        if urllib.request.urlopen(OGW + "/healthz", timeout=2).getcode() == 200:
            up = True
            break
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)

check("the dedicated AUTH_MODE=oauth gateway came up", up,
      "" if up else open("oauth-gateway.log").read()[-2000:])
if not up:
    print("1 FAILED: ['oauth gateway did not start']")
    sys.exit(1)

try:
    print("\n== (a) a workspace is readable by its owner and by nobody else")

    ws_alice = workspace_of(ALICE)
    ws_bob = workspace_of(BOB)
    check("fixture: alice's and bob's workspaces were discovered off real jobs and differ",
          bool(ws_alice) and bool(ws_bob) and ws_alice != ws_bob, (ws_alice, ws_bob))

    secret_name = f"secret-{uuid.uuid4().hex[:8]}.png"
    secret_bytes = f"alice's private render {uuid.uuid4().hex}".encode()
    (OUTPUT_ROOT / (ws_alice or "_missing")).mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / (ws_alice or "_missing") / secret_name).write_bytes(secret_bytes)
    bob_name = f"bobs-{uuid.uuid4().hex[:8]}.png"
    (OUTPUT_ROOT / (ws_bob or "_missing")).mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / (ws_bob or "_missing") / bob_name).write_bytes(b"bob's render")

    alice_url = f"/outputs/{ws_alice}/{secret_name}"
    bob_url = f"/outputs/{ws_bob}/{bob_name}"

    status, body = request("GET", alice_url, base=OGW, user=ALICE)
    check("alice reads her own output (200, the bytes she wrote) -- which also "
          "proves the gateway's mirrored workspace_name() names the directory "
          "the worker actually used",
          status == 200 and body == secret_bytes, (status, body[:40] if isinstance(body, bytes) else body))

    status, body = request("GET", alice_url, base=OGW, user=BOB)
    check("bob is refused alice's output with 403 under AUTH_MODE=oauth",
          status == 403, (status, body if not isinstance(body, bytes) else body[:40]))

    status, body = request("GET", alice_url, base=OGW, user=None)
    check("a request carrying no identity at all is refused alice's output",
          status == 403, status)

    status, body = request("GET", bob_url, base=OGW, user=BOB)
    check("and bob reads his own -- the scope is per caller, not 'alice only'",
          status == 200 and body == b"bob's render", status)

    status, _ = request("GET", f"/outputs/{ws_alice}/no-such-{uuid.uuid4().hex[:6]}.png",
                        base=OGW, user=ALICE)
    check("a missing file inside the caller's own workspace is still a 404",
          status == 404, status)

    print("\n== (b) the scope is decided on the resolved path, not the first URL segment")

    status, _ = request("GET", f"/outputs/{ws_alice}/../{ws_bob}/{bob_name}", base=OGW, user=ALICE)
    check("/outputs/<alice>/../<bob>/file is refused to alice even though its "
          "first segment is her workspace",
          status in (403, 404), status)

    print("\n== (c) the AUTH_MODE=none gateway is unchanged: reads are not scoped there")

    status, body = request("GET", alice_url, base=GW, user=BOB)
    check("under AUTH_MODE=none bob still reads alice's file -- the documented "
          "behaviour of that mode, where the identity is client-supplied and a "
          "scope on it would be a control that only pretends to exist",
          status == 200 and body == secret_bytes, status)

    print("\n== (d) /api/showback names other submitters only to operators")

    period = time.strftime("%Y-%m", time.gmtime(time.time()))
    r.hset(f"comfy:showback:{period}", mapping={f"u:{ALICE}": 12.5, f"u:{BOB}": 7.25})

    status, report = request("GET", "/api/showback", base=OGW, user=ALICE)
    users = report.get("users", {}) if isinstance(report, dict) else {}
    check("an ordinary caller gets her own row", status == 200 and users.get(ALICE) == 12.5,
          (status, users))
    check("and nobody else's", BOB not in users and set(users) <= {ALICE}, list(users)[:5])
    check("the pool totals are still there for her",
          isinstance(report, dict) and all(k in report for k in
                                           ("anonymous_gpu_seconds", "excluded_gpu_seconds",
                                            "other_gpu_seconds", "users_total_gpu_seconds")),
          report if isinstance(report, dict) else status)

    status, report = request("GET", "/api/showback", base=OGW, user=OPERATOR)
    users = report.get("users", {}) if isinstance(report, dict) else {}
    check("a caller listed in SHOWBACK_OPERATORS gets every submitter's row",
          status == 200 and users.get(ALICE) == 12.5 and users.get(BOB) == 7.25, (status, users))

    status, report = request("GET", "/api/showback", base=OGW, user=None)
    users = report.get("users", {}) if isinstance(report, dict) else {}
    check("a caller with no identity gets no rows at all",
          status == 200 and users == {}, (status, users))

    status, report = request("GET", "/api/showback", base=GW, user=BOB)
    users = report.get("users", {}) if isinstance(report, dict) else {}
    check("under AUTH_MODE=none the report is unscoped, as documented",
          status == 200 and ALICE in users and BOB in users, (status, list(users)[:5]))

finally:
    ogw.terminate()
    try:
        ogw.wait(timeout=10)
    except Exception:  # noqa: BLE001
        ogw.kill()

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
