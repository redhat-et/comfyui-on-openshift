"""A retry is not a promotion: a requeued job rejoins its own lane's round.

Q2 gives a job whose worker died before ComfyUI ever saw the workflow exactly
one more attempt, and Q1 decides where a job sits in the one physical queue.
This is the seam between them, and it is one line inside
`requeue_orphaned_job()` (hub.py): the requeue goes back through the SAME
fair-queueing insert a first submission uses, rather than straight onto the
end of the list that is served next.

Why that line is load-bearing, and not a tidy-up waiting to happen. The queue
is popped `BLMOVE ... src=RIGHT`, so the physical TAIL is served next: a
requeue written as a plain `RPUSH` puts the retried job at the front of the
whole line, ahead of every other submitter, and does it again on every death.
A submitter whose workers keep dying — a poisonous node, an image that OOMs on
boot, a bad card — then takes slots from lanes that did nothing wrong, which
is precisely the starvation Q1 exists to prevent, arriving through a door Q1's
own check (check-50-fair-queue.py) never looks at because nothing there ever
dies. The queue-jump is also invisible to every other check in this suite: it
still requeues exactly once, the job still completes, `comfy:queue` still
receives exactly one write, and the retry event still says what it always
said. Only the POSITION changes, and until this file nothing read it.

The shape of the fixture, and why it is this shape:

  Six real submissions build a queue with two lanes interleaved by round, so
  the list has a middle as well as two ends: [A1 B1 A2 B2 A3 B3] in service
  order (userA and userB alternating, exactly as Q1 places them). Then one
  stranded job in a THIRD lane, which has nothing else queued, is left for the
  gateway's reaper to requeue.

  A third lane is the point. Its round is 0, so a correct fair insert puts the
  retried job at the back of round 0 — third in service order, behind A1 and
  B1 and ahead of A2 — which is neither end of the list. "Not at the front" on
  its own would also be satisfied by a requeue that dumped the job at the very
  back (a punishment rather than a promotion, and just as wrong: the user's
  job was never at fault), and "somewhere in the middle" is not a claim a
  one-ended assertion can make. The whole service order is asserted instead.

  Nothing is killed. What is under test is where the reaper puts a job, not
  any particular way of dying, so the stranded entry is fabricated directly on
  a processing list whose worker never existed — the same technique, and for
  the same reason, as check-37-reap-durability.py. A real SIGKILL would work
  and would also make this check depend on how fast a worker dies.

  The live agent is frozen for the whole measurement, because a queue is only
  observable while nothing is draining it (see check-50-fair-queue.py's
  docstring for why SIGSTOP alone is not enough, and what the sacrificial
  submission is for). A frozen process cannot refresh its own heartbeat
  either, so this check holds that heartbeat armed while it works — otherwise
  the gateway's reaper would eventually read the freeze as a death and start
  reaping the very lists being measured.

Every count comes from an observer armed before the event: the writes to
`comfy:queue` are counted by a `QueueWriteWatcher` started before the stranded
entry exists (an `LLEN` afterwards cannot tell one requeue from two, or from
none, once the agent resumes), and the queue's service order is read off the
raw list both before and after the requeue.
"""
import json, os, signal, sys, time, urllib.error, urllib.request, uuid
import redis

from queue_watch import QueueWriteWatcher

# See check-30-sigkill.py: run.sh's stdout is a pipe, and a check killed by
# CHECK_TIMEOUT loses every buffered PASS/FAIL line with it.
sys.stdout.reconfigure(line_buffering=True)

GW = "http://127.0.0.1:8100"
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None
QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")

# Read the way hub.py reads them, so run.sh's shrunk values move this check's
# budgets with them instead of pinning it to numbers nothing else uses.
REAPER_INTERVAL = int(os.environ.get("REAPER_INTERVAL", "30"))
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "60"))

WORKFLOW = {"3": {"class_type": "KSampler", "inputs": {}}}

# The two lanes that build the queue, and the third the retried job belongs
# to. hub.py's generate() uses the X-Forwarded-User header verbatim as the
# fair-queueing lane (`queue_key=user`), so these strings ARE the lanes.
LANE_A = "retryplace-userA"
LANE_B = "retryplace-userB"
LANE_C = "retryplace-userC"

failures = []

r = redis.from_url(REDIS_URL, password=REDIS_PASSWORD, decode_responses=True)


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def post(path, body, user=None):
    headers = {"Content-Type": "application/json"}
    if user is not None:
        headers["X-Forwarded-User"] = user
    req = urllib.request.Request(GW + path, data=json.dumps(body).encode(), headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(path):
    return json.loads(urllib.request.urlopen(GW + path, timeout=10).read())


def state_key(job_id):
    return f"comfy:job:{job_id}:state"


def service_order():
    """The queue's job ids in the order they will be SERVED.

    comfy:queue is newest-first and a worker pops the tail (BLMOVE src=RIGHT),
    so service order is the raw list reversed. Ids are shortened to keep a
    failing check's detail readable.
    """
    ids = []

    for raw in reversed(r.lrange(QUEUE_KEY, 0, -1)):
        try:
            ids.append(json.loads(raw).get("job_id"))
        except json.JSONDecodeError:
            ids.append(None)

    return ids


def rearm_worker_heartbeats():
    """Keep the frozen agent's heartbeat alive for the duration.

    The agent refreshes it from a thread (worker_agent.py, note 10), and
    SIGSTOP stops that thread with everything else. A heartbeat that lapses IS
    a death as far as the gateway is concerned, so without this the reaper
    would start reaping the frozen agent's own processing list part-way
    through the measurement below and the queue this check is reading would
    change underneath it for a reason that has nothing to do with what it is
    asserting. Only the expiry is touched; nothing is created.
    """
    for key in r.scan_iter(match="comfy:worker:*"):
        r.expire(key, HEARTBEAT_TTL)


def until(predicate, timeout):
    """Poll until the predicate holds; report whether it ever did. The caller
    asserts on state it reads itself afterwards."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        if predicate():
            return True

        rearm_worker_heartbeats()
        time.sleep(0.2)

    return False


def terminal_status(job_id, timeout=60):
    deadline = time.time() + timeout
    last = None

    while time.time() < deadline:
        try:
            last = get(f"/api/jobs/{job_id}").get("status")
            if last in ("completed", "failed", "cancelled"):
                return last
        except urllib.error.HTTPError:
            pass
        time.sleep(0.2)

    return last


agent_pid = int(sys.argv[1])

# The stranded job, invented rather than submitted: it stands for a job that
# was popped by a worker that then died, so it must NOT be on the queue when
# the measurement starts. Its incarnation names no heartbeat key, which is
# exactly what makes the reaper treat that worker as dead; the `#` separator
# is the one a real incarnation carries (worker_ids.py).
retry_job = f"retryplace-{uuid.uuid4().hex[:12]}"
dead_incarnation = f"retryplace-worker-{uuid.uuid4().hex[:8]}#{uuid.uuid4().hex[:8]}"
dead_processing_key = f"comfy:processing:{dead_incarnation}"

stranded_entry = json.dumps({
    "schema_version": 1,
    "job_id": retry_job,
    "workflow": WORKFLOW,
    "queue_key": LANE_C,
    "attempt": {"count": 0, "phase": "dispatched"},
    "user": LANE_C,
    "submitted_at": time.time(),
})

print("\n== build a two-lane backlog, then let the reaper requeue a third lane's job")

r.delete(dead_processing_key, state_key(retry_job), f"comfy:job:{retry_job}:events")

a_ids, b_ids, before, after, requeued = [], [], [], [], False
watcher = None

os.kill(agent_pid, signal.SIGSTOP)
try:
    rearm_worker_heartbeats()

    # Absorbs whatever BLMOVE the frozen agent was already blocked in —
    # check-50-fair-queue.py's docstring explains why this is necessary and
    # why SIGSTOP alone is not. Everything pushed after it stays put.
    post("/api/generate", {"workflow": WORKFLOW})

    a_ids = [post("/api/generate", {"workflow": WORKFLOW}, user=LANE_A)["job_id"]
             for _ in range(3)]
    b_ids = [post("/api/generate", {"workflow": WORKFLOW}, user=LANE_B)["job_id"]
             for _ in range(3)]

    before = service_order()

    # phase `dispatched` — the worker had claimed the job and had not yet
    # handed it to ComfyUI — is what makes this death retryable at all
    # (RETRYABLE_PHASES, hub.py). Nothing else about the job is unusual: no
    # cancel flag, no attempt count, so the reaper's only decision left is
    # WHERE the job goes back.
    r.hset(state_key(retry_job), mapping={
        "status": "running",
        "phase": "dispatched",
        "user": LANE_C,
        "worker": "retryplace-worker",
    })
    # Armed HERE: after the backlog is built and before the entry the reaper
    # will act on exists, so what it counts is the requeue and nothing else.
    # An LLEN afterwards could not do this job — the agent resumes at the end
    # of this block and pops whatever was put back, so the list length reads
    # the same whether the job was requeued once, twice, or never
    # (queue_watch.py's docstring is the long version).
    watcher = QueueWriteWatcher(REDIS_URL, REDIS_PASSWORD, QUEUE_KEY).start()

    r.lpush(dead_processing_key, stranded_entry)

    check("fixture: the backlog is two lanes interleaved by round, so the "
          "queue has a middle and not just two ends",
          before == [a_ids[0], b_ids[0], a_ids[1], b_ids[1], a_ids[2], b_ids[2]],
          {"service order": [j[:8] if j else j for j in before]})
    check("fixture: the stranded job is parked on a dead worker's processing "
          "list and is not on the queue",
          r.llen(dead_processing_key) == 1 and retry_job not in before,
          {"llen": r.llen(dead_processing_key)})
    check("fixture: its worker's heartbeat is gone, which is the only thing "
          "the reaper reads as death",
          not r.exists(f"comfy:worker:{dead_incarnation}"))
    check("fixture: nothing has reaped it yet",
          r.hget(state_key(retry_job), "owner") is None,
          r.hget(state_key(retry_job), "owner"))

    # The reaper gets four ticks plus a margin. It requeues by writing to
    # comfy:queue, so the job id appearing on the list is the event; the
    # entry leaving the processing list is bookkeeping that follows it.
    requeued = until(lambda: retry_job in service_order(),
                     REAPER_INTERVAL * 4 + 10)

    after = service_order()
finally:
    os.kill(agent_pid, signal.SIGCONT)

writes, write_commands = watcher.stop() if watcher else (0, [])

check("the reaper put the stranded job back on the queue", requeued,
      {"service order": [j[:8] if j else j for j in after]})

check("comfy:queue received exactly one write between the stranded entry "
      "appearing and the queue being read — the requeue, once",
      writes == 1, (writes, write_commands))

# The two halves of the invariant, asserted separately: a requeue that jumped
# the line fails the first, and one that was banished to the back of the queue
# — equally wrong, and equally consistent with "not a promotion" — passes the
# first and fails the second.
check("a retry is not a promotion: the requeued job is NOT the next one "
      "served, ahead of every submitter that was already waiting",
      after[:1] != [retry_job],
      {"served next": (after[0][:12] if after else None)})

expected = [a_ids[0], b_ids[0], retry_job, a_ids[1], b_ids[1], a_ids[2], b_ids[2]]
check("the requeued job rejoined its own lane's round — third in service "
      "order, behind the two round-0 jobs already queued and ahead of round "
      "1 — exactly where a first submission from that lane would have gone",
      after == expected,
      {"got": [j[:8] if j else j for j in after],
       "expected": [j[:8] if j else j for j in expected]})

print("\n== and the queue drains, retried job included")

retry_status = terminal_status(retry_job)
check("the requeued job runs to completion on the next worker to reach it",
      retry_status == "completed", retry_status)

drained = [jid for jid in a_ids + b_ids if terminal_status(jid) != "completed"]
check("every job used to build the backlog finished too, so this check "
      "leaves nothing queued for the next one",
      not drained, drained)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
