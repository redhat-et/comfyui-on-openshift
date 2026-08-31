"""
Q1 — fair queueing across submitters (docs/10-roadmap.md).

Today the queue is one Redis list, LPUSH'd by hub.py's generate() and popped
FIFO (BLMOVE ... src="RIGHT") by worker_agent.py's main loop. One submitter's
batch is served start to finish before anything behind it, so a person who
submits 200 jobs delays every other user behind all 200 of them.

The roadmap deliberately picked round-robin fair queueing over priority
lanes for this: a priority lane has to be claimed by the caller, and
hub.py's own generate() says X-Forwarded-User is client-supplied under
AUTH_MODE=none and must never be treated as authorization — so a priority
lane would just be a header everyone sets on themselves. Round-robin across
submitters needs no trust decision: claiming to be someone else only shares
your own slot, it degrades to plain FIFO when there is one submitter, and it
still solves the stated problem.

This check proves the problem exists and stays failing until Q1 gives the
queue a fairness key to round-robin on. Nothing here implements Q1 — it only
observes hub.py's/worker_agent.py's *current* single-list FIFO behaviour, so
every assertion below is expected to fail on HEAD.

The setup: submit a whole batch as one user (A), then one job as a second
user (B), and prove B is not stuck behind A's last job. That requires the
queue to be built up as a real backlog before anything starts draining it —
otherwise the live agent would interleave picking up A's jobs with our own
submissions and the "batch, then one more" shape we're asserting about never
actually exists on the queue at once.

Freezing the agent with plain SIGSTOP does not achieve that, which is worth
spelling out because it looks like it should. BLMOVE blocks SERVER-side:
while the agent's connection sits in Redis's blocked-clients list, Redis
performs the move itself the instant anything is pushed, and hands the
result to that connection whether or not the process behind it is scheduled.
Stopping the process freezes the consumer, not the consumption — the first
entry pushed after a SIGSTOP can still vanish off comfy:queue before this
check ever sees the batch it meant to build. (check-40-envelope.py hit the
same thing and documents it from the inspection side.)

So: SIGSTOP the agent, then push one sacrificial job first. It absorbs
whatever BLMOVE the agent was already blocked in — the stopped process
cannot issue another one, so from that point on nothing is blocked on
comfy:queue and every job pushed after it stays put until we SIGCONT. If the
agent happened not to be mid-BLMOVE when it was stopped, the sacrificial job
simply stays queued too and is just the first thing served; either way the
batch behind it is intact. The sacrificial job is a real submission and runs
to completion once the agent resumes, which is why it is not cleaned up.

One section here is not of that vintage. Q1's insert has to read every job
already queued to place a new one, and Redis is single-threaded, so the size
of what it reads is a stall on every other client — a 26 KB workflow at a
depth of 499 measured ~118 ms of exclusive Redis time per submit. The
workflow therefore lives at comfy:job:<id>:payload and only the ordering
record goes on comfy:queue. The last section asserts the housekeeping half of
that split: the payload is deleted with the job rather than left to its
backstop TTL. It would pass vacuously against pre-Q1 HEAD, where no such key
exists; it is a regression guard on the mechanism, not a demonstration of the
gap Q1 closed.

Ordering is read back from each job's own event stream rather than from
polling /api/jobs — worker_agent.py's run_job() emits a "started" event the
moment it picks a job up, and Redis Streams IDs are a server-assigned
millisecond clock, so comparing them is exact and immune to how fast or slow
this script happens to poll. Polling only decides *when* it is safe to look
(once every job has reached a terminal state).
"""
import json, os, signal, sys, time, urllib.error, urllib.request
import redis

GW = "http://127.0.0.1:8100"
QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
WORKFLOW = {"3": {"class_type": "KSampler", "inputs": {}}}
N = 5  # matches docs/10-roadmap.md's "a 5-job batch from one user"

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def post(path, body, user=None):
    headers = {"Content-Type": "application/json"}
    if user:
        headers["X-Forwarded-User"] = user
    req = urllib.request.Request(GW + path, data=json.dumps(body).encode(), headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(path):
    return json.loads(urllib.request.urlopen(GW + path, timeout=10).read())


def poll_status(job_id, terminal=("completed", "failed", "cancelled"), timeout=60):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = get(f"/api/jobs/{job_id}").get("status")
            if last in terminal:
                return last
        except urllib.error.HTTPError:
            pass
        time.sleep(0.2)
    return last


def started_event_id(job_id):
    """The Redis Stream id of this job's 'started' event, or None if the
    worker never got to it. Stream ids are '<epoch-ms>-<seq>', assigned by
    Redis at XADD time, so they order across DIFFERENT streams exactly like
    wall-clock timestamps would."""
    for entry_id, fields in r.xrange(f"comfy:job:{job_id}:events"):
        try:
            data = json.loads(fields["data"])
        except (KeyError, json.JSONDecodeError):
            continue
        if data.get("type") == "started":
            ms, _, seq = entry_id.partition("-")
            return (int(ms), int(seq or 0))
    return None


r = redis.from_url(
    os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0"),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)

agent_pid = int(sys.argv[1])

print("\n== fair queueing: userB's single job must not queue behind userA's whole batch")

os.kill(agent_pid, signal.SIGSTOP)
try:
    post("/api/generate", {"workflow": WORKFLOW})  # sacrificial — see module docstring

    a_ids = [post("/api/generate", {"workflow": WORKFLOW}, user="userA")["job_id"] for _ in range(N)]
    b_id = post("/api/generate", {"workflow": WORKFLOW}, user="userB")["job_id"]
finally:
    os.kill(agent_pid, signal.SIGCONT)

print(f"    submitted {N} jobs as userA, then 1 job as userB, all while the agent was held")

for jid in a_ids + [b_id]:
    status = poll_status(jid)
    check(f"job {jid[:8]} reached a terminal state", status == "completed", status)

a_last_started = started_event_id(a_ids[-1])
b_started = started_event_id(b_id)

check("both userA's last job and userB's job actually started",
      a_last_started is not None and b_started is not None,
      {"userA[last]": a_last_started, "userB": b_started})

check(f"userB's job is served before userA's {N}th (last) job",
      b_started is not None and a_last_started is not None and b_started < a_last_started,
      f"userB started at {b_started}, userA[{N}] started at {a_last_started}")


print("\n== and the workflows stored beside the queue are reclaimed, not left to a TTL")

# Placing a job fairly means looking at every job already queued, so what the
# insert has to walk must not be the client's workflow — hub.py keeps the
# ordering record on comfy:queue and the workflow at comfy:job:<id>:payload.
# That key has a TTL, but the TTL is a backstop: the reclaim path is the
# explicit delete on every terminal outcome. The distinction is invisible
# until Redis, which is deliberately `maxmemory-policy noeviction`, fills up
# with a day's worth of finished jobs' workflows — so it is asserted here
# rather than assumed.
#
# Bounded wait rather than a bare read: poll_status returns on the status the
# worker writes, and the delete is the next thing it does.
deadline = time.time() + 10
leaked = list(a_ids + [b_id])
while time.time() < deadline and leaked:
    leaked = [jid for jid in leaked if r.exists(f"comfy:job:{jid}:payload")]
    if leaked:
        time.sleep(0.2)

check("every finished job's stored workflow was deleted with the job",
      not leaked, leaked)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
