"""
Q4 -- showback (docs/10-roadmap.md).

The gateway already stamps the authenticated submitter on every job (F2's
`user` field, oauth-proxy's X-Forwarded-User under AUTH_MODE=oauth). Showback
turns that plus elapsed GPU time into "who spent the card" -- a report an
operator reads to answer, in dollars, who is actually using the pool.

Nothing implements this yet. `hub.py` has no `/api/showback` route and writes
no per-user accumulator anywhere; `gather_stats()` returns only queue_depth,
workers_registered and estimated_wait_seconds, and worker_agent.py's `finish()`
records a job's terminal status and nothing about how long it ran. Every
Q4-specific assertion below is expected to fail on HEAD.

THE INTERFACE THIS CHECK PINS, the same way check-40-envelope.py pins the
envelope shape and check-80-estimated-wait.py pins `comfy_estimated_wait_seconds`
-- whoever implements Q4 either matches this or updates this file, with both
shown in review:

    GET /api/showback ->
      {"users": {"<submitter>": <cumulative GPU seconds, float>, ...},
       "anonymous_gpu_seconds": <float>,
       "excluded_gpu_seconds": <float>}

  - `users` is keyed by the same string F2's `user` field already carries
    (X-Forwarded-User, or hub.py's `queue_key` -- one identity concept, see
    hub.py's own comment on generate()). A submitter who never authenticated
    (no header at all) must NOT get a "" entry in this dict -- see (3) below.
  - `anonymous_gpu_seconds` is the EXPLICIT bucket for exactly that case --
    "an anonymous submission is accounted somewhere explicit rather than
    dropped" (docs/10-roadmap.md, Q4). Not folded into `users[""]`, because a
    bare 0/absent key an operator has to know to look for is not "explicit".
  - `excluded_gpu_seconds` is the explicit bucket for GPU time this report
    could not attribute to a specific completed-or-failed run -- see (6)
    below. Whether a given implementation ever routes anything there or
    always attributes everything is its own choice; what it may not do is
    have a third, silent, unaccounted-for possibility.

WHAT "GPU SECONDS" MEANS, decided rather than left open (the roadmap's own
warning: "GPU seconds is ambiguous"). The only thing this system can actually
measure is wall-clock time between a job's state reading "running" (`run_job()`
in worker_agent.py, written before the workspace is even created) and its
terminal event -- which INCLUDES checkpoint load and any time the agent spent
parked waiting on ComfyUI, not just sampler time. (1) below does not merely
check that some positive number is recorded; it checks that the number tracks
REAL WALL-CLOCK TIME for a job of known, deliberately-slow duration, which is
the only definition consistent with "wall time between running and finishing"
-- a hardcoded constant, a step-count multiplier, or "time actually spent
inside ComfyUI's own execution" (which would UNDER-count exactly the parked
time the roadmap calls out) cannot pass it.

Six things are asserted, matched to the roadmap item's own sentences. They
run in the order below, which is NOT quite the order the roadmap prose lists
them in: (6), the reaper-death scenario, is deliberately LAST, because it
SIGKILLs the only live worker agent this check is handed and never replaces
it -- unlike check-30-sigkill.py, nothing here needs a second attempt to
succeed, only the accounting to have registered something. Running it before
(5) would starve (5)'s burst of jobs of any worker to serve them.

  (0) GET /api/showback exists at all and returns the shape above.
  (1) GPU seconds are attributed to the submitting user, and the number
      reflects real wall-clock time (not a constant, not zero).
  (2) Two users' totals do not bleed into each other -- proven by an
      arm-before/after snapshot: submitter A's total, read right after A's
      own job finished, must be BIT-FOR-BIT unchanged after a second user's
      job also finishes -- not merely "greater than 0", which two users
      sharing one counter would also satisfy.
  (3) An anonymous submission (no X-Forwarded-User header at all -- the
      ordinary AUTH_MODE=none shape) is accounted under the explicit
      `anonymous_gpu_seconds` bucket, and does NOT leak into `users[""]`.
  (4) The accounting survives a job that fails through the ORDINARY path --
      worker_agent.py's own finish(conn, job_id, "failed", ...), reached when
      ComfyUI itself reports execution_error (a VRAM OOM). This is the
      easy 90% case: whatever hooks finish() gets this one for free unless it
      only hooks the "completed" branch specifically.
  (5) THE KEY-SPACE TRAP: the Redis this runs against is `maxmemory-policy
      noeviction` at a fixed cap (docs/09-engineering-handoff.md section 3),
      and an accumulator that hands out one NEW top-level Redis key per
      distinct submitter (or, worse, per job) grows that key space without
      bound against a header AUTH_MODE=none makes entirely client-supplied --
      an attacker who varies X-Forwarded-User on every request can OOM this
      Redis for free, which is exactly the "work vanishing at random" failure
      mode section 3 exists to prevent, arriving through a door `noeviction`
      does not cover. This drives several distinct identities (plus the ones
      already used above) through the system and asserts the number of Redis
      keys actually holding this accumulator -- KEYS matching
      `comfy:showback:*`, the namespace this item owns, the same way
      `comfy:worker:*` and `comfy:processing:*` are hub.py's -- stays bounded:
      present (not zero -- that would just be "nothing implemented" again),
      but strictly fewer than the number of distinct identities that fed it.
      One shared Hash (HINCRBYFLOAT per field) satisfies this trivially; one
      key per submitter does not.
  (6) The accounting survives a job that fails through the REAPER's path --
      hub.py's fail_orphaned_job(), reached when a worker is SIGKILLed
      mid-execution (docs/10-roadmap.md, Q2's scenario B, check-30-sigkill.py).
      This is the trap: fail_orphaned_job() writes the job's terminal status
      directly onto the state hash and is never routed through
      worker_agent.py's finish() at all -- so an implementation that only
      instruments finish() silently drops exactly the most expensive jobs in
      the system, the ones where a worker died holding a card. The premise
      (ComfyUI really had the workflow, i.e. real GPU time was actually
      spent, and this went through fail-once rather than the retry door) is
      checked the same way check-30-sigkill.py checks it, not assumed. This
      runs last and does not restart the agent it kills -- see above.
"""
import json, os, signal, sys, time, urllib.error, urllib.request, uuid
import redis

sys.stdout.reconfigure(line_buffering=True)

GW = "http://127.0.0.1:8100"
COMFY = "http://127.0.0.1:8999"
SHOWBACK_PATH = "/api/showback"
SHOWBACK_KEY_PATTERN = "comfy:showback:*"
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


def post(path, body, headers=None, base=GW):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=hdrs)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(path, base=GW):
    return json.loads(urllib.request.urlopen(base + path, timeout=10).read())


def state_key(job_id):
    return f"comfy:job:{job_id}:state"


def comfy_saw(probe):
    """Has the stub ComfyUI actually been handed a workflow carrying this
    node -- i.e. was real (simulated) GPU time actually spent? Mirrors
    check-30-sigkill.py's helper of the same name."""
    return probe in get("/__received__", base=COMFY)["nodes"]


def submit(user, workflow):
    """Returns (job_id, submit_wallclock_time). user=None omits
    X-Forwarded-User entirely -- the ordinary AUTH_MODE=none, no-proxy shape
    that (3) below exercises."""
    headers = {}
    if user is not None:
        headers["X-Forwarded-User"] = user
    t0 = time.time()
    resp = post("/api/generate", {"workflow": workflow}, headers)
    return resp["job_id"], t0


def poll_state(job_id, timeout=40):
    """Poll /api/jobs/<id> until it reaches a terminal status. Returns
    (state_dict_or_None, wallclock_time_terminal_was_first_observed)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = get(f"/api/jobs/{job_id}")
            if last.get("status") in ("completed", "failed", "cancelled"):
                return last, time.time()
        except urllib.error.HTTPError:
            pass
        time.sleep(0.2)
    return last, time.time()


def showback():
    """The report as it stands right now. A missing/broken endpoint reads as
    all-zero rather than raising, so callers below can take before/after
    snapshots unconditionally -- the (0) assertion is what actually catches
    the endpoint being absent; every later assertion is about the VALUES."""
    try:
        data = get(SHOWBACK_PATH)
        if not isinstance(data, dict):
            return {}
    except Exception:  # noqa: BLE001
        return {}
    return data


def users_of(data):
    u = data.get("users")
    return u if isinstance(u, dict) else {}


def user_total(data, user):
    try:
        return float(users_of(data).get(user, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def anon_total(data):
    try:
        return float(data.get("anonymous_gpu_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def excluded_total(data):
    try:
        return float(data.get("excluded_gpu_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def slow_workflow(probe):
    """~6s of simulated execution (3 progress events, 2s apart -- see
    fake_comfy.py's run()), long enough that a wall-clock measurement and a
    hardcoded constant are trivially distinguishable."""
    return {probe: {"class_type": "KSampler"}, "__slow__": {"class_type": "KSampler"}}


def fast_workflow(probe):
    return {probe: {"class_type": "KSampler", "inputs": {}}}


ALICE = "alice@example.com"
BOB = "bob@example.com"
DAVE = "dave@example.com"
CAROL = "carol@example.com"

distinct_identities = set()

agent_pid = int(sys.argv[1])


print("\n== (0) GET /api/showback exists and returns the report shape")

endpoint_ok, endpoint_detail = True, None
try:
    raw = urllib.request.urlopen(GW + SHOWBACK_PATH, timeout=10).read()
    data0 = json.loads(raw)
    endpoint_ok = isinstance(data0, dict) and isinstance(data0.get("users"), dict) \
        and "anonymous_gpu_seconds" in data0 and "excluded_gpu_seconds" in data0
    endpoint_detail = data0
except Exception as exc:  # noqa: BLE001
    endpoint_ok, endpoint_detail = False, repr(exc)

check("GET /api/showback exists and returns {users, anonymous_gpu_seconds, "
      "excluded_gpu_seconds}", endpoint_ok, endpoint_detail)


print("\n== (1) GPU seconds are attributed to the submitting user, tracking real wall-clock time")

before_alice = showback()
baseline_alice = user_total(before_alice, ALICE)
distinct_identities.add(ALICE)

probe_a = f"probe-a-{uuid.uuid4().hex[:8]}"
job_a, t_submit_a = submit(ALICE, slow_workflow(probe_a))
state_a, t_done_a = poll_state(job_a)

check("alice's job actually completed (precondition for the rest of this "
      "assertion)", bool(state_a) and state_a.get("status") == "completed", state_a)

elapsed_a = t_done_a - t_submit_a
after_a_solo = showback()
delta_alice = user_total(after_a_solo, ALICE) - baseline_alice

# Generous band (0.3x-1.6x real elapsed) -- wide enough to absorb this
# script's own polling granularity and process scheduling noise, narrow
# enough that neither 0 (nothing recorded) nor a small constant (e.g. "1
# job" or "3 progress events") can land inside it for a ~6s job.
check(f"alice's recorded GPU seconds ({delta_alice:.2f}) track this job's "
      f"real wall-clock duration (~{elapsed_a:.2f}s), not zero and not a "
      f"constant -- the only definition consistent with 'wall time between "
      f"running and finishing'",
      elapsed_a * 0.3 <= delta_alice <= elapsed_a * 1.6,
      {"delta_alice": delta_alice, "elapsed_a": elapsed_a})


print("\n== (2) two users' totals do not bleed into each other")

before_bob = showback()
baseline_bob = user_total(before_bob, BOB)
distinct_identities.add(BOB)

probe_b = f"probe-b-{uuid.uuid4().hex[:8]}"
job_b, t_submit_b = submit(BOB, slow_workflow(probe_b))
state_b, t_done_b = poll_state(job_b)

check("bob's job actually completed (precondition for the rest of this "
      "assertion)", bool(state_b) and state_b.get("status") == "completed", state_b)

after_both = showback()

check("alice's total is BIT-FOR-BIT unchanged by a second user's job "
      "finishing -- not merely 'still greater than 0', which two users "
      "sharing one counter would also satisfy",
      user_total(after_both, ALICE) == user_total(after_a_solo, ALICE),
      {"after_alice_only": user_total(after_a_solo, ALICE),
       "after_bob_too": user_total(after_both, ALICE)})

elapsed_b = t_done_b - t_submit_b
delta_bob = user_total(after_both, BOB) - baseline_bob
check(f"bob's own recorded GPU seconds ({delta_bob:.2f}) track his job's "
      f"real wall-clock duration (~{elapsed_b:.2f}s)",
      elapsed_b * 0.3 <= delta_bob <= elapsed_b * 1.6,
      {"delta_bob": delta_bob, "elapsed_b": elapsed_b})


print("\n== (3) an anonymous submission is accounted explicitly, not dropped and not aliased onto users['']")

before_anon = showback()
baseline_anon = anon_total(before_anon)
baseline_users_empty = user_total(before_anon, "")

probe_anon = f"probe-anon-{uuid.uuid4().hex[:8]}"
job_anon, t_submit_anon = submit(None, slow_workflow(probe_anon))  # no header at all
state_anon, t_done_anon = poll_state(job_anon)

check("the anonymous job actually completed (precondition)",
      bool(state_anon) and state_anon.get("status") == "completed", state_anon)

elapsed_anon = t_done_anon - t_submit_anon
after_anon = showback()
delta_anon = anon_total(after_anon) - baseline_anon

check(f"anonymous_gpu_seconds grew by roughly this job's real duration "
      f"(~{elapsed_anon:.2f}s), i.e. the submission was accounted for "
      f"somewhere explicit rather than dropped",
      elapsed_anon * 0.3 <= delta_anon <= elapsed_anon * 1.6,
      {"delta_anon": delta_anon, "elapsed_anon": elapsed_anon})

check("the anonymous job did NOT leak into users[''] -- 'explicit' means "
      "its own named bucket, not a blank key an operator has to know to "
      "look for",
      user_total(after_anon, "") == baseline_users_empty,
      {"before": baseline_users_empty, "after": user_total(after_anon, "")})


print("\n== (4) accounting survives a job that fails through the ORDINARY path (worker's own finish())")

before_dave = showback()
baseline_dave = user_total(before_dave, DAVE)
distinct_identities.add(DAVE)

job_dave, t_submit_dave = submit(DAVE, {"__vram_oom__": {"class_type": "KSampler"}})
state_dave, t_done_dave = poll_state(job_dave)

check("dave's job actually reached 'failed' (a VRAM OOM, reported by "
      "ComfyUI itself -- worker_agent.py's ordinary finish(..., 'failed', ...) "
      "path, ComfyUI stays up)",
      bool(state_dave) and state_dave.get("status") == "failed", state_dave)

after_dave = showback()
delta_dave = user_total(after_dave, DAVE) - baseline_dave
check("dave's GPU time is still recorded even though the job failed -- "
      "showback must not silently require a 'completed' status",
      delta_dave > 0, delta_dave)


print("\n== (5) the accumulator's key space is bounded, not one key per submitter (or per job)")

BURST_USERS = [
    f"burst-{uuid.uuid4().hex[:8]}@example.com" for _ in range(4)
] + ["../../../../tmp/evil", "a" * 300]

for burst_user in BURST_USERS:
    distinct_identities.add(burst_user)
    probe_burst = f"probe-burst-{uuid.uuid4().hex[:8]}"
    job_burst, _ = submit(burst_user, fast_workflow(probe_burst))
    state_burst, _ = poll_state(job_burst, timeout=20)
    check(f"burst identity {burst_user[:24]!r}... completed without crashing "
          "the gateway (a hostile/oversized X-Forwarded-User must not break "
          "showback accounting, whatever it does with the value)",
          bool(state_burst) and state_burst.get("status") in ("completed", "failed"),
          state_burst)

key_count = len(list(r.scan_iter(match=SHOWBACK_KEY_PATTERN)))
n_identities = len(distinct_identities)

check(f"comfy:showback:* holds at least one key (the accumulator exists) "
      f"but strictly fewer keys ({key_count}) than the {n_identities} "
      f"distinct submitter identities that fed it this run -- one shared "
      f"structure (e.g. a Hash, HINCRBYFLOAT per field) satisfies this; a "
      f"new top-level key per submitter (or per job, which would be worse) "
      f"does not, and is exactly what turns a client-supplied header into "
      f"unbounded growth against a `noeviction` Redis at a fixed memory cap "
      f"(docs/09-engineering-handoff.md section 3)",
      1 <= key_count < n_identities,
      {"key_count": key_count, "n_identities": n_identities,
       "keys": list(r.scan_iter(match=SHOWBACK_KEY_PATTERN))})


print("\n== (6) accounting survives a job that fails through the REAPER's path (fail_orphaned_job(), never calls finish()) -- runs LAST, kills the only live agent")

before_carol_users = showback()
baseline_carol = user_total(before_carol_users, CAROL)
baseline_excluded = excluded_total(before_carol_users)
distinct_identities.add(CAROL)

probe_carol = f"probe-carol-{uuid.uuid4().hex[:8]}"
job_carol, t_submit_carol = submit(CAROL, slow_workflow(probe_carol))

# Let the agent actually get ComfyUI the workflow and run for a known,
# measurable stretch before it dies -- mirrors check-30-sigkill.py's
# scenario B exactly, including why: the phase breadcrumb must read
# 'executing' (ComfyUI already has it) before the kill, which is the
# premise that makes this a "GPU time was actually spent and then lost"
# case rather than a pre-execution death Q2 already retries for free.
time.sleep(3)
KNOWN_RUN_SECONDS = time.time() - t_submit_carol

phase_mid_job = get(f"/api/jobs/{job_carol}").get("phase")
check("the phase breadcrumb shows execution had begun ('executing') before "
      "the kill below -- the premise that real GPU time was spent",
      phase_mid_job == "executing", phase_mid_job)

os.kill(agent_pid, signal.SIGKILL)

state_carol, t_done_carol = poll_state(job_carol, timeout=40)

check("the job reached 'failed' via the reaper (heartbeat lapsed, no second "
      "agent in this check to retry a mid-execution death)",
      bool(state_carol) and state_carol.get("status") == "failed", state_carol)
check("this really went through fail-once, not the retry door -- "
      "attempt_count stayed at 0",
      (state_carol or {}).get("attempt_count") in (None, "0"),
      (state_carol or {}).get("attempt_count"))
check("and ComfyUI really had been handed this workflow before the kill -- "
      "the GPU time this assertion is about was actually spent, not "
      "hypothetical",
      comfy_saw(probe_carol), probe_carol)

after_carol = showback()
recorded_carol = (
    (user_total(after_carol, CAROL) - baseline_carol)
    + (excluded_total(after_carol) - baseline_excluded)
)
check(f"the ~{KNOWN_RUN_SECONDS:.1f}s this job actually ran before its "
      f"worker was SIGKILLed is reflected SOMEWHERE explicit -- attributed "
      f"to carol, or in excluded_gpu_seconds -- rather than silently lost "
      f"because fail_orphaned_job() (hub.py) never calls worker_agent.py's "
      f"finish() at all",
      recorded_carol >= KNOWN_RUN_SECONDS * 0.3,
      {"recorded_carol": recorded_carol, "known_run_seconds": KNOWN_RUN_SECONDS,
       "delta_users_carol": user_total(after_carol, CAROL) - baseline_carol,
       "delta_excluded": excluded_total(after_carol) - baseline_excluded})


print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
