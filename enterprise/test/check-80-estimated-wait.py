"""
Q6 -- estimated wait (docs/10-roadmap.md).

Queue depth is a proxy; what a user feels is time to first pixel. The
envelope already carries `submitted_at` (F2, reserved for this item) on every
queue entry, so the gateway can compute how long queued work has actually
been waiting and export it -- instead of a number that means something
different at every workflow size and GPU speed.

Nothing here implements Q6: `gather_stats()` (hub.py) returns only
`queue_depth` and `workers_registered` today, and `metrics()` emits exactly
those two gauges. Every assertion below is expected to fail on HEAD.

The metric name asserted here is `comfy_estimated_wait_seconds` -- chosen to
match the existing `comfy_queue_depth` / `comfy_workers_registered`
convention (a `comfy_` prefix, snake_case, a unit suffix for a duration). It
is this check, not hub.py, that fixes that name: whoever implements Q6 either
matches it or updates this file, the same contract check-50-fair-queue.py and
check-40-envelope.py already hold for their items.

Five things are asserted -- four matched to the four sentences in the roadmap
item, and one that none of them pins down:

  1. The gauge exists on /metrics, alongside the two that already do.
  2. It is zero or absent with an empty queue.
  3. It grows while jobs sit unserved.
  4. It is derived from real timestamps, not a constant.
  5. It reads the entry at the end of the list that is actually served next.

(2) alone is weak on purpose -- the roadmap item calls it out explicitly: "a
gauge hardcoded to 0 would satisfy a careless test". A gauge hardcoded to 0
also passes (2). What it cannot do is (3): grow while wall-clock time passes
over a queue that is not otherwise changing. And a gauge that is some
function of queue depth alone (constant while depth is constant) cannot do
(3) either, which is why growth is measured with the queue depth held fixed
(the agent frozen, no new submits) across the interval.

(4) goes further and ties the number to the *submitted_at* field specifically,
not just to "time since this check pushed something": a job is pushed onto
the raw queue with `submitted_at` hand-set to several minutes in the past --
simulating an entry that has genuinely been sitting there a while -- and the
gauge is asserted to reflect roughly that manufactured age despite having
just landed on the list. A wait figure computed from anything other than the
real timestamps on real queue entries (a constant, a depth multiplier, a
lookup unrelated to `submitted_at`) has no way to produce that number.

(5) exists because (1)-(4) all run against a queue exactly ONE entry deep,
where index -1 and index 0 are the same entry: every one of them passes
identically whichever end the gauge reads, and a one-character edit to
`estimated_wait_seconds()` moves it from one to the other. They are not the
same claim. The list is newest-first and a worker pops the tail, so the
tail's age is the wait a caller is actually queued behind, while the head is
an entry nobody is waiting on yet -- a gauge reading the head reports ~0 on
an hour-old backlog, which is the number an operator would size the pool on.
So a second job is submitted, through the real endpoint, behind the stale
one; the two ends of the list are then minutes apart in age, and the gauge is
required to match the one at the served-next end. Both ages are read back off
the queue itself rather than restated from the constant this file chose.
"""
import json, os, re, signal, sys, time, urllib.error, urllib.request, uuid
import redis

GW = "http://127.0.0.1:8100"
QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
GAUGE_NAME = "comfy_estimated_wait_seconds"
failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def post(path, body, headers=None):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(GW + path, data=json.dumps(body).encode(), headers=hdrs)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(path):
    return json.loads(urllib.request.urlopen(GW + path, timeout=10).read())


# /metrics and /api/stats serve one shared snapshot per STATS_CACHE_SECONDS
# (hub.py's cached_stats(): the endpoints are polled unauthenticated, and each
# call used to be a keyspace SCAN). So a read taken right after the queue
# changed can legitimately be up to that old, and every read here first waits
# the window out -- the assertions below are about the gauge's VALUE, and a
# stale snapshot is not a wrong value.
STATS_CACHE_SECONDS = float(os.environ.get("STATS_CACHE_SECONDS", "5"))
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "60"))


def read_metrics():
    # Waiting the window out lengthens the freeze below past HEARTBEAT_TTL,
    # and a lapsed heartbeat IS a death to the reaper, which would then
    # requeue the frozen agent's own job and move the queue under these
    # assertions. So the frozen agent's heartbeat is re-armed while waiting
    # -- check-55-retry-placement.py's technique; only the expiry is touched.
    deadline = time.time() + STATS_CACHE_SECONDS + 0.5

    while time.time() < deadline:
        for key in r.scan_iter(match="comfy:worker:*"):
            r.expire(key, HEARTBEAT_TTL)
        time.sleep(0.2)

    return urllib.request.urlopen(GW + "/metrics", timeout=10).read().decode()


def gauge_value(text, name):
    """The numeric value of a Prometheus gauge sample line, or None if the
    metric is not in the text at all -- absence and "reads 0" are different
    things, which is exactly what assertion (2) distinguishes."""
    m = re.search(rf"^{re.escape(name)}\s+([0-9.eE+-]+)\s*$", text, re.M)
    return float(m.group(1)) if m else None


def gauge_or_zero(text, name):
    """For arithmetic across two reads (growth, magnitude) where a metric
    that is currently absent is fairly compared as 0 -- an absent gauge grew
    by exactly as little as a gauge hardcoded to 0 did."""
    value = gauge_value(text, name)
    return 0.0 if value is None else value


def exposed_as_gauge(text, name):
    """HELP + TYPE gauge + a sample line -- the same three-line shape
    comfy_queue_depth and comfy_workers_registered already use in
    metrics() -- not merely the name appearing somewhere in the text."""
    return (
        f"# HELP {name}" in text
        and f"# TYPE {name} gauge" in text
        and gauge_value(text, name) is not None
    )


def poll_status(job_id, terminal=("completed", "failed", "cancelled"), timeout=30):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = get(f"/api/jobs/{job_id}").get("status")
            if last in terminal:
                return last
        except urllib.error.HTTPError:
            pass
        time.sleep(0.5)
    return last


r = redis.from_url(
    os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0"),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)

agent_pid = int(sys.argv[1])
WORKFLOW = {"3": {"class_type": "KSampler", "inputs": {}}}


print("\n== baseline: let the queue actually drain before measuring the empty case")

# An earlier check may have left completed jobs' streams behind, but not
# queue *entries* -- those are popped the moment a live agent serves them, and
# one has been polling throughout the suite. Still, don't assume it: wait for
# LLEN to actually hit 0 rather than asserting straight off a snapshot that
# might catch a check-70 job still in flight.
deadline = time.time() + 30
depth = r.llen(QUEUE_KEY)
while depth != 0 and time.time() < deadline:
    time.sleep(0.5)
    depth = r.llen(QUEUE_KEY)

check("the queue actually reached empty before the empty-queue assertion below",
      depth == 0, depth)


print("\n== 1 & 2: the gauge exists, and reads zero or absent with an empty queue")

baseline_text = read_metrics()

check(f"gateway exposes {GAUGE_NAME} on /metrics alongside comfy_queue_depth "
      "and comfy_workers_registered",
      exposed_as_gauge(baseline_text, GAUGE_NAME), baseline_text)

baseline_value = gauge_value(baseline_text, GAUGE_NAME)
check(f"{GAUGE_NAME} is zero or absent with an empty queue",
      baseline_value is None or abs(baseline_value) < 1e-6, baseline_value)


print("\n== 3 & 4: freeze the agent, let jobs sit unserved, and watch the gauge")

# Freezing the process does not freeze BLMOVE -- it blocks server-side in
# Redis, which hands a push straight to the blocked connection whether or not
# the process behind it is scheduled (see check-40-envelope.py /
# check-50-fair-queue.py for the full explanation). A sacrificial job first
# absorbs whatever BLMOVE the agent was already parked in, so everything
# pushed after it is guaranteed to still be sitting on comfy:queue below.
os.kill(agent_pid, signal.SIGSTOP)

stale_id = fresh_id = None
try:
    post("/api/generate", {"workflow": WORKFLOW})  # sacrificial; see above

    # A job that LOOKS like it has been waiting several minutes, even though
    # it is landing on the list right now -- built and pushed exactly like
    # F2's envelope, but with submitted_at hand-set into the past. This is
    # the assertion a constant, or a depth-based estimate, cannot pass: there
    # is nothing about "one extra queued job" that implies ~3 minutes, only
    # the timestamp on the entry itself does.
    STALE_AGE = 180.0
    stale_id = str(uuid.uuid4())
    stale_envelope = {
        "schema_version": 1,
        "job_id": stale_id,
        "workflow": WORKFLOW,
        "queue_key": "",
        "attempt": {"count": 0, "phase": "queued"},
        "user": "",
        "submitted_at": time.time() - STALE_AGE,
    }
    r.lpush(QUEUE_KEY, json.dumps(stale_envelope))

    # Not a raw depth check: the sacrificial job is GONE from comfy:queue by
    # design the instant it is pushed (it is what the frozen agent's own
    # already-blocked BLMOVE absorbs -- see the comment above), so a raw
    # LLEN here is 1, not 2, on a correctly-working sacrificial push. What
    # has to still be there is the stale entry itself.
    raw_entries = r.lrange(QUEUE_KEY, 0, -1)
    stale_present = any(json.loads(raw).get("job_id") == stale_id for raw in raw_entries)
    check("the stale job is sitting on comfy:queue, unserved, while the "
          "agent is frozen",
          stale_present, raw_entries)

    text_1 = read_metrics()
    value_1 = gauge_or_zero(text_1, GAUGE_NAME)

    check(f"{GAUGE_NAME} reflects the manufactured job's real submitted_at "
          f"(~{STALE_AGE:.0f}s old) rather than a constant or a plain job "
          "count -- one extra queued job does not by itself imply minutes",
          value_1 >= STALE_AGE * 0.4, value_1)

    # Nothing about the queue changes here -- no submit, no dequeue, the
    # agent stays frozen. If the gauge moves at all, it can only be because
    # it is a function of wall-clock time elapsed since submitted_at, which
    # is exactly what "grows when jobs sit unserved" means and exactly what
    # a hardcoded value (0 or otherwise) cannot do.
    GROWTH_WINDOW = 4.0
    time.sleep(GROWTH_WINDOW)

    text_2 = read_metrics()
    value_2 = gauge_or_zero(text_2, GAUGE_NAME)

    growth = value_2 - value_1
    check(f"{GAUGE_NAME} grows by roughly the real elapsed time "
          f"({GROWTH_WINDOW:.0f}s) while the same jobs sit unserved and "
          "nothing else about the queue changed",
          growth >= GROWTH_WINDOW * 0.4, (value_1, value_2, growth))

    # 5: WHICH END OF THE LIST. Everything above passes just as happily on a
    # gauge that reads the wrong end, because everything above runs against a
    # queue ONE entry deep -- and on a one-entry list index -1 and index 0 are
    # the same entry, so `LINDEX comfy:queue -1` and `LINDEX comfy:queue 0`
    # are the same number and nothing in this suite could tell them apart.
    #
    # They are not the same claim. The list runs newest-first: a worker's
    # BLMOVE pops src=RIGHT, so the TAIL is the entry about to be served and
    # its age is the queue-side latency this gauge is defined to report
    # (hub.py's estimated_wait_seconds), while the HEAD is whatever was
    # queued most recently -- an entry nobody is waiting behind yet. Reading
    # the head reports ~0 on a queue that has been backed up for an hour,
    # which is the number an operator would scale the pool on and the signal
    # I4's Prometheus scaler is meant to read.
    #
    # So: submit a real job through the real endpoint while the agent is
    # still frozen. It is submitted with no X-Forwarded-User, so it shares
    # lane "" with the stale entry already queued, lands one round behind it,
    # and is spliced in at the physical head -- the two ends of the list are
    # now ~STALE_AGE apart in age, and the gauge has to say which one it
    # means.
    fresh_id = post("/api/generate", {"workflow": WORKFLOW})["job_id"]

    raw_entries = r.lrange(QUEUE_KEY, 0, -1)
    entries = []
    for raw in raw_entries:
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            entries.append({})

    # The gauge is read BEFORE the ages are computed: read_metrics() waits
    # the cache window out, and the ages must be taken from the same moment
    # as the snapshot they are compared with. The list cannot change in
    # between -- the agent is frozen.
    text_3 = read_metrics()

    now = time.time()
    head, tail = (entries[0], entries[-1]) if entries else ({}, {})
    head_age = now - (head.get("submitted_at") or now)
    tail_age = now - (tail.get("submitted_at") or now)

    # The fixture, asserted rather than assumed and one clause at a time: if
    # the two ends are not the entries this expects, the reading below is
    # about something other than what it says it is.
    check("fixture: the entry at the served-next end (index -1) is the stale "
          "job, so the gauge has an old entry to report",
          tail.get("job_id") == stale_id,
          {"tail": tail.get("job_id"), "stale": stale_id})
    check("fixture: the entry at the just-queued end (index 0) is the job "
          "submitted a moment ago, so the wrong end has a young entry to "
          "report",
          head.get("job_id") == fresh_id,
          {"head": head.get("job_id"), "fresh": fresh_id})
    check("fixture: the two ends are far enough apart in age that reading "
          "either one is unambiguous",
          tail_age - head_age >= STALE_AGE * 0.5,
          {"head_age": round(head_age, 1), "tail_age": round(tail_age, 1)})

    value_3 = gauge_or_zero(text_3, GAUGE_NAME)

    # Both ages are read off the list itself rather than restated from
    # STALE_AGE, so this compares the gauge against the queue's own contents.
    check(f"{GAUGE_NAME} reports the age of the entry at the SERVED-NEXT end "
          f"of the queue (index -1, ~{tail_age:.0f}s) and not the one just "
          f"queued at the other end (index 0, ~{head_age:.0f}s)",
          abs(value_3 - tail_age) < 5.0,
          {"gauge": round(value_3, 1), "tail_age": round(tail_age, 1),
           "head_age": round(head_age, 1)})

finally:
    os.kill(agent_pid, signal.SIGCONT)

if stale_id:
    status = poll_status(stale_id)
    check("the manufactured stale job still ran to completion once the "
          "agent resumed", status == "completed", status)
else:
    check("the manufactured stale job still ran to completion once the "
          "agent resumed", False, "job was never queued")

if fresh_id:
    status = poll_status(fresh_id)
    check("and so did the job submitted behind it, so this check leaves "
          "nothing queued for the next one", status == "completed", status)
else:
    check("and so did the job submitted behind it, so this check leaves "
          "nothing queued for the next one", False, "job was never queued")


print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
