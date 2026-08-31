"""
What one /api/generate costs Redis, measured (docs/10-roadmap.md, Q1).

Not a check, and deliberately not named like one: `run.sh` discovers
`check*.py`, and this is a measurement rather than an assertion. It answers a
question the e2e suite structurally cannot, because that suite runs a queue
three jobs deep with a two-node workflow, where every version of the insert is
instant.

The question. Fair queueing means placing a new job relative to the jobs
already queued, which means reading them, and hub.py's FAIR_ENQUEUE_LUA does
that inside one EVAL. Redis executes one command at a time, so the whole of
that read is time no other client gets — including every worker parked in
BLMOVE, which is to say the pool stops being handed work for the duration.
That makes the per-submit cost a cluster-wide property, not a submitter's own
problem, and it is worth being able to re-measure rather than reason about.

The workload is a 100-node graph with the usual links and prompt text, which
lands the envelope at ~26 KB — a realistic production workflow, not a
worst case; MAX_BODY_BYTES allows 2 MB. Depths are 0, 100 and 499 against
MAX_QUEUE_DEPTH's 500. The queue is built one real submit at a time and every
insert is timed, so the figure for depth D is the cost of the (D+1)th submit
against a queue D earlier submits actually built.

Recorded, on an M-series laptop with redis-server 8 and three active lanes.
The lower row is this script; the upper row is the same workload against the
version of FAIR_ENQUEUE_LUA this one replaced, which kept the whole envelope
in the list:

                          depth 0     depth 100    depth 499
    whole envelope
    in the list             3.2 ms      22.6 ms      117.7 ms
    ordering record
    in the list             0.7 ms       0.8 ms        1.6 ms

Run it again with NODES=400 (a ~103 KB workflow) and the two rows become
7.2 / 218 / 1235 ms and 1.3 / 1.3 / 2.2 ms. That second comparison matters
more than any single number: what the insert reads per entry is now fixed, so
the cost has stopped tracking a size the *client* chooses.

Absolute numbers will differ on your machine; the shape is what to look at. If
depth 499 is tens of milliseconds rather than low single digits, something has
put the workflow back in the list — `make lint` pins that, and
`docs/09-engineering-handoff.md` section 3 has the row.

Usage (needs a redis and nothing else — it starts nothing and drains nothing,
and it uses its own key namespace so it is safe against a live instance):

    REDIS_URL=redis://127.0.0.1:6399/0 REDIS_PASSWORD=testpass123 \\
        python3 enterprise/test/bench-fair-enqueue.py
"""
import importlib.util
import json
import os
import pathlib
import statistics
import time
import uuid

import redis

# Its own list, set before hub.py is imported: hub reads QUEUE_KEY once at
# import time, and pointing it away from comfy:queue is what makes this safe
# to run against a Redis a real gateway and real workers are using.
os.environ.setdefault("QUEUE_KEY", "bench:fair-enqueue:queue")

REPO = pathlib.Path(__file__).resolve().parents[2]
DEPTHS = (0, 100, 499)
NODES = int(os.environ.get("NODES", "100"))
REPS = int(os.environ.get("REPS", "3"))


def load_hub():
    """The real hub.py, imported rather than reimplemented — the point is to
    time the script and the call shape the gateway actually uses, and a copy
    of either here would be a copy that drifts."""
    path = REPO / "enterprise" / "gateway" / "hub.py"
    spec = importlib.util.spec_from_file_location("hub_under_bench", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def workflow(nodes: int) -> dict:
    """A ComfyUI graph of `nodes` nodes with the links and prompt text a real
    one carries. 100 nodes puts the envelope at ~26 KB."""
    return {
        str(i): {
            "class_type": "KSampler" if i % 3 else "CLIPTextEncode",
            "inputs": {
                "seed": 100000 + i, "steps": 20, "cfg": 8.0,
                "sampler_name": "euler_ancestral",
                "model": [str(max(0, i - 1)), 0],
                "positive": [str(max(0, i - 2)), 0],
                "negative": [str(max(0, i - 3)), 0],
                "latent_image": [str(max(0, i - 4)), 0],
                "text": "a mountain range at golden hour",
            },
        }
        for i in range(nodes)
    }


hub = load_hub()
conn = redis.from_url(
    os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0"),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)
conn.ping()

WF = workflow(NODES)
LANES = ["userA", "userB", "userC"]

# EVALSHA after the first call, exactly as the gateway's registered script
# does. Sending the script body on every call would add bytes this is not
# trying to measure.
script = conn.register_script(hub.FAIR_ENQUEUE_LUA)

# Payload keys this bench wrote, so cleanup() removes its own and nothing
# else's.
written = set()


def submit(lane: str) -> None:
    """One /api/generate's worth of Redis work, through hub.py's own call
    shape rather than a hand-written EVAL."""
    envelope = hub.build_envelope(str(uuid.uuid4()), WF, user=lane, queue_key=lane)
    keys, args = hub.fair_enqueue_call(envelope)
    script(keys=keys, args=args)


def cleanup() -> None:
    for key in conn.scan_iter(match="bench:fair-enqueue:*"):
        conn.delete(key)
    for key in written:
        # Only this bench's own payloads. Every job it invented is a fresh
        # uuid4 no gateway ever handed out, but deleting by what was written
        # rather than by pattern is what keeps that true of the code too.
        conn.delete(key)


envelope_bytes = len(json.dumps(
    hub.build_envelope(str(uuid.uuid4()), WF, user="userA", queue_key="userA")).encode())

print(f"\nenvelope {envelope_bytes / 1024:.1f} KB ({NODES}-node workflow), "
      f"{len(LANES)} lanes, {REPS} repetitions")
print(f"queue    {hub.QUEUE_KEY}\n")

samples = {depth: [] for depth in DEPTHS}

try:
    for _ in range(REPS):
        conn.delete(hub.QUEUE_KEY)

        for depth in range(max(DEPTHS) + 1):
            lane = LANES[depth % len(LANES)]
            started = time.perf_counter()
            submit(lane)
            elapsed = (time.perf_counter() - started) * 1000

            if depth in samples:
                samples[depth].append(elapsed)

        for raw in conn.lrange(hub.QUEUE_KEY, 0, -1):
            written.add(hub.payload_key(json.loads(raw)["job_id"]))

    print(f"{'depth':>7}  {'median ms':>10}  {'min':>8}  {'max':>8}")

    for depth in DEPTHS:
        taken = samples[depth]
        print(f"{depth:>7}  {statistics.median(taken):>10.2f}  "
              f"{min(taken):>8.2f}  {max(taken):>8.2f}")

    print("\nEvery millisecond above is exclusive: Redis is single-threaded, so\n"
          "it is also a millisecond no worker parked in BLMOVE was handed work.\n")
finally:
    cleanup()
