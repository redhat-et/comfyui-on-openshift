"""
F2 — the versioned queue payload envelope (docs/10-roadmap.md).

Today enterprise/gateway/hub.py's generate() pushes exactly
    {"job_id": ..., "workflow": ...}
onto comfy:queue (the lpush in generate()), and
enterprise/worker/worker_agent.py's main() parses exactly that shape back out
(payload["job_id"], payload["workflow"] in the BLMOVE loop). Four later
roadmap items (Q1 a fair-queueing key, Q2 an attempt count + phase, Q4
attribution, Q6 a submit timestamp) each need to add a field to that payload.
F2 defines the envelope once instead of four items each renegotiating a
contract two files must agree on.

The envelope this pins down:

    {
      "schema_version": <int>,
      "job_id": "<uuid>",
      "workflow": {...},
      "queue_key": "<str>",                    # Q1 fair-queueing key
      "attempt": {"count": <int>, "phase": "<str>"},  # Q2 attempt + phase
      "user": "<str>",                         # Q4 attribution
      "submitted_at": <epoch seconds>          # Q6 submit timestamp
    }

with every reserved field defaulted by the producer (hub.py) when the caller
didn't supply one: queue_key/user default to "", attempt defaults to a fresh
{"count": 0, "phase": "queued"}, submitted_at defaults to the push time.

The assertion that matters most: a worker must still run a job whose payload
is the OLD, version-less shape ({"job_id", "workflow"} only) — a rolling
deploy leaves old-shape entries on the queue behind a new worker, and a new
gateway's entries behind an old worker, and neither may strand work. That
has to be provable, not incidental, so the contract also requires the worker
to record which schema_version it actually parsed onto the job's own state
hash (state_key(job_id) -> "schema_version"), defaulting to "1" (the implicit
floor version) when the payload carried none at all. That state field is
exactly what makes "old shape still worked" observable from outside the
worker process, and it's also what ops would want mid-rollout: a job that
says schema_version=1 arrived from a not-yet-upgraded gateway or a leftover
queue entry.

A field neither the gateway nor the worker currently knows about (from a
newer peer during a rolling deploy) must not be fatal either — it is simply
not acted on.

None of this exists yet: hub.py's lpush and worker_agent.py's payload[...]
parsing are both exactly the two-key shape today, so every assertion below
fails on HEAD.
"""
import json, os, signal, sys, time, uuid

from harness import GW, QUEUE_KEY, check, connect_redis, failures, poll_status as _poll_status, state_key

get, post = GW.get, GW.post

r = connect_redis()

agent_pid = int(sys.argv[1])


def poll_status(job_id):
    return _poll_status(GW, job_id, timeout=30, interval=0.5)
WORKFLOW = {"3": {"class_type": "KSampler", "inputs": {}}}


print("\n== a queued job carries a schema version and the four reserved fields, defaulted")

# Freeze the live agent so the entry generate() pushes is still sitting on
# comfy:queue, unmodified, when we inspect it directly — otherwise the same
# worker that is already blocked on BLMOVE would dequeue it out from under us.
#
# SIGSTOP alone does not achieve that, which is worth spelling out because it
# looks like it should. BLMOVE blocks SERVER-side: while the agent's connection
# sits in Redis's blocked-clients list, Redis performs the move itself the
# instant anything pushes, and hands the result to that connection whether or
# not the process behind it is scheduled. Stopping the process freezes the
# consumer, not the consumption — the first entry pushed after a SIGSTOP is
# gone from comfy:queue before this check can read it.
#
# So push a sacrificial job first. It absorbs the BLMOVE the agent is already
# blocked in; the stopped process cannot issue another one, so from then on
# nothing is blocked on comfy:queue and the next entry stays put. If the agent
# happened NOT to be blocked when it was stopped (it was between passes), the
# sacrificial job simply stays on the queue too and the search below skips it —
# either way the entry under inspection is there. Waiting out the agent's
# 5-second BLMOVE timeout instead would also free the queue, but a process
# frozen that long misses its own client-side socket deadline and dies on
# resume, taking the rest of this check with it.
#
# The sacrificial job is a real submission and runs to completion once the
# agent is resumed, which is why it is not cleaned up.
os.kill(agent_pid, signal.SIGSTOP)
job_id = None
try:
    post("/api/generate", {"workflow": WORKFLOW})   # sacrificial; see above

    job = post("/api/generate", {"workflow": WORKFLOW})
    job_id = job["job_id"]

    raw_entries = r.lrange(QUEUE_KEY, 0, -1)
    entry = None
    for raw in raw_entries:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if parsed.get("job_id") == job_id:
            entry = parsed
            break

    check("the pushed entry is present on the raw queue for inspection",
          entry is not None, raw_entries)
    entry = entry or {}

    check("envelope carries a schema_version field", "schema_version" in entry, entry)
    check("envelope carries the fair-queueing key field (Q1: queue_key)",
          "queue_key" in entry, entry)
    check("envelope carries the attempt/phase field (Q2: attempt)",
          "attempt" in entry, entry)
    check("envelope carries the attribution field (Q4: user)", "user" in entry, entry)
    check("envelope carries the submit-timestamp field (Q6: submitted_at)",
          "submitted_at" in entry, entry)
    check("envelope carries the VRAM-tier field (N2: tier)", "tier" in entry, entry)

    check("queue_key defaults to empty when the caller declared no lane",
          entry.get("queue_key") == "", entry.get("queue_key"))
    check("attempt defaults to a fresh, not-yet-tried breadcrumb",
          entry.get("attempt") == {"count": 0, "phase": "queued"}, entry.get("attempt"))
    check("user defaults to empty when no X-Forwarded-User was sent",
          entry.get("user") == "", entry.get("user"))
    check("tier defaults to empty on an untiered gateway -- the same lane an "
          "old envelope's silence about the field already meant",
          entry.get("tier") == "", entry.get("tier"))

    submitted_at = entry.get("submitted_at")
    check("submitted_at defaults to roughly the push time",
          isinstance(submitted_at, (int, float)) and abs(time.time() - submitted_at) < 30,
          submitted_at)
finally:
    os.kill(agent_pid, signal.SIGCONT)

status = poll_status(job_id) if job_id else None
check("the inspected job still ran to completion once the agent resumed",
      status == "completed", status)


print("\n== a worker parsing an OLD-shape payload (no version, two keys only) still runs it")

legacy_id = str(uuid.uuid4())
# Exactly today's hub.py shape, pushed the same way hub.py pushes it (lpush),
# simulating a leftover queue entry from before a rolling deploy, or a
# not-yet-upgraded gateway replica sitting beside an upgraded worker.
r.lpush(QUEUE_KEY, json.dumps({"job_id": legacy_id, "workflow": WORKFLOW}))

status = poll_status(legacy_id)
check("an old-shape payload with no version field still completes",
      status == "completed", status)

legacy_state = r.hgetall(state_key(legacy_id))
check("tolerant parsing is explicit: the worker recorded schema_version=1 "
      "(the floor/implicit version) for a payload that declared none",
      legacy_state.get("schema_version") == "1", legacy_state)


print("\n== a field neither side recognizes yet is ignored, not fatal")

future_id = str(uuid.uuid4())
future_payload = {
    "schema_version": 99,
    "job_id": future_id,
    "workflow": WORKFLOW,
    "queue_key": "",
    "attempt": {"count": 0, "phase": "queued"},
    "user": "",
    "submitted_at": time.time(),
    # Not part of the envelope contract at all — stands in for a field a
    # later roadmap item adds that this worker predates.
    "not_yet_a_real_field": {"from": "a future gateway"},
}
r.lpush(QUEUE_KEY, json.dumps(future_payload))

status = poll_status(future_id)
check("a payload carrying an unrecognized field still completes, not fails",
      status == "completed", status)

future_state = r.hgetall(state_key(future_id))
check("the declared schema_version still came through despite the unknown field",
      future_state.get("schema_version") == "99", future_state)


print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
