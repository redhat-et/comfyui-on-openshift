"""
Q5 -- GPU-second quota breaker (docs/10-roadmap.md).

Decision already made in the roadmap and repeated in the item itself: this is
a LOCAL quota computed from Q4's showback accounting. No AWS SDK, no IAM
identity on the gateway -- hub.py is the entire public attack surface and its
image deliberately carries no boto3. And it FAILS OPEN: "a breaker that trips
on an unreachable dependency halts a cluster you are already paying for,
while the risk it guards against is slow."

Nothing implements this yet: `generate()` (hub.py) has no quota check at
all -- it goes straight from the MAX_QUEUE_DEPTH backpressure check to
building the envelope and enqueueing, for every submitter regardless of past
usage. Every Q5-specific assertion below is expected to fail on HEAD.

THE INTERFACE THIS CHECK PINS, the same way check-90-showback.py pins
`/api/showback`'s shape and check-80-estimated-wait.py pins the gauge name --
whoever implements Q5 either matches this or updates this file, with both
shown in review:

  - A new env var, `QUOTA_GPU_SECONDS`, read the same way MAX_QUEUE_DEPTH
    already is: the per-submitter cap of GPU seconds accrued in the CURRENT
    showback period (the same UTC-month Hash Q4 already writes,
    `comfy:showback:<period>`, field `u:<user>` -- BEGIN SHARED SHOWBACK in
    hub.py). This check runs its own gateway process with this env var set to
    a known value, rather than trusting whatever default a real deployment
    picks, for the same reason check-30-sigkill.py runs its own worker agent
    with a shrunk HEARTBEAT_TTL: a deterministic threshold this file chose,
    not a production default nobody here controls.
  - Enforced in `generate()`, before the job is placed on the queue -- read
    the submitter's current-period total, compare, and refuse before
    `fair_enqueue_call()` ever runs. A submission that is refused must
    therefore produce ZERO writes to comfy:queue, not merely a non-200
    response: an HTTP layer that rejects the response but has already queued
    the job is worse than doing nothing, since the browser sees a refusal
    while a worker silently spends a GPU on it anyway.
  - Refusal is `HTTPException(429, "...quota..." )` -- the same clean-
    rejection shape `generate()` already uses for MAX_QUEUE_DEPTH two lines
    above (`HTTPException(503, ...)`), not an unhandled exception. 429 rather
    than 503, because this is a per-caller limit, not "the whole queue is
    full" -- a second submitter must not see it.
  - FAILS OPEN: if the current period's Hash has no field for this submitter,
    or the field is present but does not parse as a number, `generate()`
    proceeds exactly as if the submitter were under quota. This is the same
    tolerant-parsing posture `showback_report()` already takes on the read
    side ("a value that is not a number is skipped") -- Q5 must not be
    stricter than Q4 already is about its own data.
  - MUST NOT touch `/readyz`. That endpoint drives the gateway's readiness
    probe; wiring a quota check into it would pull an otherwise-healthy
    gateway out of service -- and with it every WebSocket reporting an
    in-flight job -- because one submitter went over a GPU-second cap that
    has nothing to do with whether the gateway can serve.

Five things are asserted, matched to the roadmap item's own sentences:

  (1) A submitter already over quota is refused -- proven as ZERO writes to
      comfy:queue (queue_watch.py), the same "arm before, count the actual
      command" proof check-30-sigkill.py uses for its own queue assertions,
      not an after-the-fact LLEN read (queue_watch.py's own docstring: by the
      time this check could look, the one live agent may already have popped
      anything a wrong implementation queued).
  (2) That refusal is a clean HTTP rejection carrying the word "quota" --
      not a 200 (nothing implemented), not a 500 (an unhandled exception).
  (3) A submitter under quota is unaffected -- accepted, queued exactly once,
      and runs to completion, distinguished from (1) and (4)/(5) by using a
      submitter with real, present, sub-threshold data.
  (4) A submitter with NO quota data at all for this period (never
      submitted before) is allowed -- the "missing" half of fail-open,
      distinct from (3): this submitter has no Hash field whatsoever.
  (5) A submitter whose quota field exists but is UNREADABLE -- a
      non-numeric value, simulating corrupt or malformed accounting data --
      is also allowed. Distinct from (4): here the field is present, just
      not parseable, which is the case a strict `float()` with no try/except
      would get backwards (raise, not fail open).

Then, CRITICALLY: with (1)'s over-quota submitter still over quota, /readyz
must still read healthy. Tripping the breaker must never show up there.
"""
import json, os, subprocess, sys, time, urllib.error, urllib.request, uuid

from harness import QUEUE_KEY, REDIS_PASSWORD, REDIS_URL, check, connect_redis, failures
from queue_watch import QueueWriteWatcher

sys.stdout.reconfigure(line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A dedicated gateway process, not the shared one on 8100 -- so this check can
# pin QUOTA_GPU_SECONDS to a value it controls instead of trusting whatever
# default (if any) a real deployment picks. Same reasoning as
# check-30-sigkill.py's start_extra_agent(): a fresh process with one env var
# changed, pointed at the same Redis and the same stub ComfyUI everything
# else in this suite already uses.
QGW_PORT = 8101
QGW = f"http://127.0.0.1:{QGW_PORT}"
QUOTA_GPU_SECONDS = 100.0

SHOWBACK_KEY_PREFIX = "comfy:showback:"
SHOWBACK_USER_PREFIX = "u:"

r = connect_redis()


def showback_period(now):
    """UTC calendar month, matching hub.py's SHOWBACK_PERIOD_FORMAT."""
    return time.strftime("%Y-%m", time.gmtime(now))


def showback_key(period):
    return f"{SHOWBACK_KEY_PREFIX}{period}"


def user_field(user):
    return f"{SHOWBACK_USER_PREFIX}{user}"


def seed_showback(user, value):
    """Directly write this submitter's current-period accrual, bypassing
    real job execution entirely -- Q4's own correctness (real wall-clock
    accrual, no bleed between users, the bounded key space) is
    check-90-showback.py's job, already proven. Q5 only has to READ this
    number and act on it; this check's fixtures control exactly what that
    number is."""
    period = showback_period(time.time())
    r.hset(showback_key(period), user_field(user), value)


def request(method, path, headers=None, body=None, base=QGW):
    """Like check-90's post()/get(), but returns (status, parsed_body) for
    BOTH success and HTTP error responses instead of raising on a non-2xx --
    a quota refusal is an expected outcome here, not a script bug."""
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        raw, status = resp.read(), resp.getcode()
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    return status, parsed


def submit(user, headers=None):
    hdrs = {"X-Forwarded-User": user}
    hdrs.update(headers or {})
    probe = f"probe-{uuid.uuid4().hex[:8]}"
    return request("POST", "/api/generate",
                    headers=hdrs,
                    body={"workflow": {probe: {"class_type": "KSampler", "inputs": {}}}})


def poll_completed(job_id, timeout=20):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, last = request("GET", f"/api/jobs/{job_id}")
            if status == 200 and isinstance(last, dict) and last.get("status") in (
                    "completed", "failed", "cancelled"):
                return last
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return last


def watch():
    return QueueWriteWatcher(REDIS_URL, REDIS_PASSWORD, QUEUE_KEY).start()


HEAVY_USER = f"quota-heavy-{uuid.uuid4().hex[:8]}@example.com"
LIGHT_USER = f"quota-light-{uuid.uuid4().hex[:8]}@example.com"
NODATA_USER = f"quota-nodata-{uuid.uuid4().hex[:8]}@example.com"
CORRUPT_USER = f"quota-corrupt-{uuid.uuid4().hex[:8]}@example.com"

seed_showback(HEAVY_USER, QUOTA_GPU_SECONDS * 5)     # 500 -- well over
seed_showback(LIGHT_USER, QUOTA_GPU_SECONDS * 0.1)   # 10  -- well under
# NODATA_USER: deliberately never written -- no field exists for it at all.
seed_showback(CORRUPT_USER, "not-a-number")          # present, unparseable

print(f"--- starting a dedicated gateway on :{QGW_PORT} with "
      f"QUOTA_GPU_SECONDS={QUOTA_GPU_SECONDS}")

env = dict(os.environ)
env["QUOTA_GPU_SECONDS"] = str(QUOTA_GPU_SECONDS)
qgw_log = open("quota-gateway.log", "w")
qgw_proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1",
     "--port", str(QGW_PORT), "--log-level", "warning"],
    env=env, stdout=qgw_log, stderr=subprocess.STDOUT,
)

qgw_up = False
deadline = time.time() + 30
while time.time() < deadline:
    try:
        if urllib.request.urlopen(QGW + "/healthz", timeout=2).getcode() == 200:
            qgw_up = True
            break
    except Exception:  # noqa: BLE001
        pass
    if qgw_proc.poll() is not None:
        break
    time.sleep(0.5)

check("the dedicated quota-enabled gateway process came up",
      qgw_up, open("quota-gateway.log").read()[-2000:] if not qgw_up else "")

if not qgw_up:
    print(f"1 FAILED: {['dedicated quota gateway did not start']}")
    sys.exit(1)

try:
    print("\n== (1) a submitter already over quota is refused -- zero writes to comfy:queue")

    watcher = watch()
    status_heavy, body_heavy = submit(HEAVY_USER)
    writes_heavy, cmds_heavy = watcher.stop()

    check("an over-quota submission never reaches comfy:queue at all -- "
          "proven as zero writes actually issued (queue_watch.py), not by "
          "trusting the HTTP status alone, since a refusal that queues the "
          "job anyway is worse than no refusal",
          writes_heavy == 0,
          {"writes": writes_heavy, "commands": cmds_heavy,
           "status": status_heavy, "body": body_heavy})

    print("\n== (2) the refusal is a clean rejection naming the quota, not a 200 and not a 500")

    check("the over-quota submission is refused with 429, matching the "
          "clean HTTPException(429, ...) shape the existing "
          "HTTPException(503, ...) MAX_QUEUE_DEPTH backpressure already "
          "uses two lines above in generate() -- not accepted (200) and not "
          "an unhandled exception (500)",
          status_heavy == 429, {"status": status_heavy, "body": body_heavy})

    reason = json.dumps(body_heavy) if isinstance(body_heavy, (dict, list)) else str(body_heavy)
    check("the rejection names the reason as the quota, not a generic error",
          "quota" in reason.lower(), reason[:200])

    print("\n== (3) a submitter under quota (real, present, sub-threshold data) is unaffected")

    watcher = watch()
    status_light, body_light = submit(LIGHT_USER)
    writes_light, cmds_light = watcher.stop()

    check("an under-quota submission is queued exactly once",
          writes_light == 1,
          {"writes": writes_light, "commands": cmds_light, "status": status_light})
    check("and accepted with 200 and a job id",
          status_light == 200 and isinstance(body_light, dict) and "job_id" in body_light,
          {"status": status_light, "body": body_light})

    state_light = None
    if isinstance(body_light, dict) and "job_id" in body_light:
        state_light = poll_completed(body_light["job_id"])
    check("and the job actually runs to completion -- the breaker being "
          "present must not merely accept the submission and then still "
          "block it somewhere downstream",
          bool(state_light) and state_light.get("status") == "completed",
          state_light)

    print("\n== (4) a submitter with NO quota data at all this period is allowed (fail open: missing)")

    watcher = watch()
    status_nodata, body_nodata = submit(NODATA_USER)
    writes_nodata, cmds_nodata = watcher.stop()

    check("a submitter this period has never accrued anything for is still "
          "queued exactly once -- missing data must not be read as "
          "'unlimited usage, refuse everything'",
          writes_nodata == 1,
          {"writes": writes_nodata, "commands": cmds_nodata, "status": status_nodata})
    check("and accepted with 200 and a job id",
          status_nodata == 200 and isinstance(body_nodata, dict) and "job_id" in body_nodata,
          {"status": status_nodata, "body": body_nodata})

    state_nodata = None
    if isinstance(body_nodata, dict) and "job_id" in body_nodata:
        state_nodata = poll_completed(body_nodata["job_id"])
    check("and it runs to completion", bool(state_nodata) and state_nodata.get("status") == "completed",
          state_nodata)

    print("\n== (5) a submitter whose quota field exists but is UNREADABLE is allowed (fail open: unreadable)")

    watcher = watch()
    status_corrupt, body_corrupt = submit(CORRUPT_USER)
    writes_corrupt, cmds_corrupt = watcher.stop()

    check("a present-but-non-numeric quota field is queued exactly once -- "
          "a strict float() with no fallback would raise here instead, "
          "which is the failure mode this fixture exists to catch",
          writes_corrupt == 1,
          {"writes": writes_corrupt, "commands": cmds_corrupt, "status": status_corrupt})
    check("and accepted with 200 and a job id",
          status_corrupt == 200 and isinstance(body_corrupt, dict) and "job_id" in body_corrupt,
          {"status": status_corrupt, "body": body_corrupt})

    state_corrupt = None
    if isinstance(body_corrupt, dict) and "job_id" in body_corrupt:
        state_corrupt = poll_completed(body_corrupt["job_id"])
    check("and it runs to completion", bool(state_corrupt) and state_corrupt.get("status") == "completed",
          state_corrupt)

    print("\n== CRITICAL: /readyz stays healthy for a user who is over quota")

    status_ready, body_ready = request(
        "GET", "/readyz", headers={"X-Forwarded-User": HEAVY_USER})

    check("/readyz reads exactly {'ok': True} with HEAVY_USER still over "
          "quota -- this drives the gateway's readiness probe, so wiring "
          "the breaker into it would pull an otherwise-healthy gateway out "
          "of service (and kill every WebSocket reporting an in-flight job) "
          "over a per-user GPU-second cap that has nothing to do with "
          "whether the gateway itself can serve",
          status_ready == 200 and body_ready == {"ok": True},
          {"status": status_ready, "body": body_ready})

finally:
    qgw_proc.terminate()
    try:
        qgw_proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        qgw_proc.kill()

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
