"""A reap that fails must leave the job recoverable, and must not retry forever.

The reaper is the only code that ever writes a terminal event for a job whose
worker died. Its loop used to be `while (raw := RPOP(list))`, which takes the
entry off the processing list BEFORE the reap runs -- so a reap that raised
anywhere in its body (Redis blip, a WRONGTYPE, a bug) destroyed the only
record that the job existed. The `except Exception: pass` around the tick said
"next tick retries"; there was nothing left for the next tick to retry, and
the job ended with no terminal event at all. That is the same "work vanishing
at random" the `noeviction` invariant exists to prevent, arriving by a door
noeviction does not cover.

Two scenarios, one per half of the fix, and neither one kills anything: both
fabricate a stranded entry directly, because what is under test is the
reaper's own bookkeeping rather than any particular way of dying.

  A. A TRANSIENT FAULT. The job's event stream key is replaced with a plain
     string, so the terminal XADD inside fail_orphaned_job() raises WRONGTYPE
     -- a fault injected at the exact write the reaper exists to perform, and
     one that can be lifted again. The entry must still be on the processing
     list afterwards. The fault is then cleared and the job must reach a
     terminal state on a later tick, exactly once.

     This is the assertion that fails on HEAD: on HEAD the entry is already
     gone by the time the reap raises, and no later tick can reach it, so the
     job never reaches a terminal state at all.

  B. A PERMANENT ONE. The job's state hash is replaced with a plain string, so
     the reap raises on its very first write, every time, forever. Retrying
     that entry until it works is a reaper that never gets past it. It must be
     given up on after a bounded number of attempts -- more than one, or (A)'s
     recovery would never happen either -- and set aside on
     `comfy:reap:undeliverable` rather than destroyed, so an operator holding
     a job id can still find out what became of it.

  C. A FAILED REMOVAL AFTER A SUCCESSFUL REQUEUE. The entry comes off the
     list only after the reap has finished, which for a retryable death means
     after the job is back on comfy:queue and a live worker may already be
     running it. If the LREM that follows raises, the entry stays parked, and
     a later tick reaps it AGAIN: it stamps the ownership fence over the
     second attempt's claim and reads a phase that is now `executing` -- so a
     job that is running perfectly well is failed, and the worker running it
     abandons its result because it no longer owns the job. The fault is
     injected with an ACL rule that denies LREM to the shared user for the
     instant the reaper needs it (LINDEX, the claim and the whole requeue
     path stay allowed), then lifted; the reap claim is deleted afterwards so
     the second look happens on the next tick rather than after the claim's
     60-second visibility timeout. The requeued job must run to completion
     with exactly one terminal event and its owner left untouched, and the
     entry must leave the list within a few ticks -- the removal is retried,
     not re-decided.

What is asserted, and why it is asserted this way. Every count comes from an
observer armed BEFORE the reaper could act -- the length of the processing
list, the type of the poisoned key, the contents of the undeliverable list --
rather than from a read after the fact, and each scenario injects exactly ONE
fault so the failure that fired is the failure that was armed. Scenario A
asserts WHICH door the reap failed at (status `failed` already written, event
stream still a string: it got as far as the terminal write and no further),
because an entry surviving a reap that failed somewhere earlier would prove
nothing about the write this is actually about.
"""
import hashlib, json, os, sys, time, uuid

from harness import (
    TERMINAL_TYPES, check, connect_redis, events_of as _events_of, failures,
    state_key, stream_key, terminal_events as _terminal_events, until,
)

# See check-30-sigkill.py: run.sh's stdout is a pipe, and a check killed by
# CHECK_TIMEOUT loses every buffered PASS/FAIL line with it.
sys.stdout.reconfigure(line_buffering=True)

# Both read the way hub.py reads them, so a run.sh that shrinks the tick or a
# deployment that widens the attempt cap moves this check's budgets with it
# rather than leaving them pinned to a number nothing else uses.
REAPER_INTERVAL = int(os.environ.get("REAPER_INTERVAL", "30"))
REAP_MAX_ATTEMPTS = int(os.environ.get("REAP_MAX_ATTEMPTS", "5"))

# Where a permanently unreapable entry is set aside. Named here as a literal
# on purpose: it is a Redis key an operator is told to look in, so a rename in
# hub.py that nobody meant should fail something.
DEAD_KEY = "comfy:reap:undeliverable"

r = connect_redis()


def events_of(job_id, wanted):
    return _events_of(r, job_id, wanted)


def terminal_events(job_id):
    """Every terminal event on the job's stream. One is a reaped job; two is
    a job reaped twice, which is what an entry removed only after success can
    get wrong in the other direction."""
    return _terminal_events(r, job_id)


def stranded(tag, job_id):
    """One entry on one dead worker's processing list.

    The incarnation is invented rather than taken from a real agent: it names
    no heartbeat key, which is precisely what makes the reaper treat it as
    dead. It carries the `#` separator a real incarnation has (worker_ids.py)
    so the key looks like the thing it is standing in for.
    """
    incarnation = f"reapdur-{tag}-{uuid.uuid4().hex[:8]}#{uuid.uuid4().hex[:8]}"
    key = f"comfy:processing:{incarnation}"

    entry = json.dumps({
        "schema_version": 1,
        "job_id": job_id,
        "workflow": {"1": {"class_type": "KSampler", "inputs": {}}},
        "queue_key": "",
        "attempt": {"count": 0, "phase": "queued"},
        "user": "",
        "submitted_at": time.time(),
    })

    return incarnation, key, entry


print("== A: a reap that fails at the terminal write leaves the entry recoverable")

job_a = f"reapdur-a-{uuid.uuid4().hex[:12]}"
_inc_a, proc_a, raw_a = stranded("a", job_a)

r.delete(proc_a, state_key(job_a), stream_key(job_a))

# phase=executing so the reaper's decision is "fail it", which is the path
# that ends in the terminal XADD this scenario poisons. Nothing here is
# retryable, so no requeue is in play and the only thing that can end this job
# is the reaper writing its terminal event.
r.hset(state_key(job_a), mapping={
    "status": "running",
    "phase": "executing",
    "worker": "reapdur-a",
})

# THE INJECTED FAULT, and the only one: the job's event stream is a string, so
# XADD on it raises WRONGTYPE. Everything the reaper does before that write
# succeeds normally, which is what makes the failure land on the terminal
# write rather than somewhere it would be easy to survive.
r.set(stream_key(job_a), "not a stream")

r.lpush(proc_a, raw_a)

check("fixture: the stranded entry is on the dead worker's processing list",
      r.llen(proc_a) == 1, f"llen={r.llen(proc_a)}")
check("fixture: the fault is armed -- the job's event stream is not a stream",
      r.type(stream_key(job_a)) == "string", r.type(stream_key(job_a)))
check("fixture: nothing has reaped it yet",
      r.hget(state_key(job_a), "owner") is None)

reached = until(lambda: r.hget(state_key(job_a), "owner") == "#reaped",
                REAPER_INTERVAL * 4 + 10)

check("the reaper reached this entry", reached,
      f"owner={r.hget(state_key(job_a), 'owner')!r}")

# WHICH failure fired. The reaper writes the terminal status before the
# terminal event, so `failed` on the hash with the stream still a string is
# the signature of a reap that got all the way to the XADD and raised there --
# not one that fell over earlier, which would survive this check for a reason
# that has nothing to do with the write under test.
check("the reap failed at the terminal write and nowhere earlier",
      r.hget(state_key(job_a), "status") == "failed"
      and r.type(stream_key(job_a)) == "string",
      f"status={r.hget(state_key(job_a), 'status')!r} "
      f"stream_type={r.type(stream_key(job_a))}")

check("the entry is STILL on the processing list -- a failed reap destroyed "
      "nothing", r.llen(proc_a) == 1, f"llen={r.llen(proc_a)}")

# The transient fault lifts. Nothing else changes: the same entry, the same
# reaper, the next tick.
r.delete(stream_key(job_a))

# Both halves, because the terminal event is written a few commands BEFORE the
# entry is removed and a poll that catches the gap would read the list as still
# holding it. Waiting for the pair is the only way this check can be about what
# the reaper did rather than about when it was looked at.
ended = until(lambda: len(terminal_events(job_a)) > 0 and r.llen(proc_a) == 0,
              REAPER_INTERVAL * 5 + 10)

check("the job reached a terminal state on a later tick", ended)
check("exactly one terminal event, and it names the reap",
      terminal_events(job_a) == ["failed"], terminal_events(job_a))
check("and the entry was consumed exactly once -- the processing list is "
      "empty", r.llen(proc_a) == 0, f"llen={r.llen(proc_a)}")

print("\n== B: a permanently unreapable entry is bounded, not retried forever")

job_b = f"reapdur-b-{uuid.uuid4().hex[:12]}"
_inc_b, proc_b, raw_b = stranded("b", job_b)

r.delete(proc_b, stream_key(job_b))

# THE INJECTED FAULT: the state hash is a string, so the reaper's first write
# -- the ownership fence it stamps before it decides anything -- raises
# WRONGTYPE. There is no later tick on which this one starts working.
r.set(state_key(job_b), "not a hash")

parked_before = r.lrange(DEAD_KEY, 0, -1).count(raw_b)

r.lpush(proc_b, raw_b)
pushed_at = time.time()

check("fixture: the stranded entry is on the dead worker's processing list",
      r.llen(proc_b) == 1, f"llen={r.llen(proc_b)}")
check("fixture: the fault is armed -- the job's state is not a hash",
      r.type(state_key(job_b)) == "string", r.type(state_key(job_b)))
check("fixture: this entry is not already on the undeliverable list",
      parked_before == 0, parked_before)

given_up = until(lambda: r.llen(proc_b) == 0,
                 REAPER_INTERVAL * (REAP_MAX_ATTEMPTS + 4) + 15)

check("the reaper stopped retrying an entry it can never reap", given_up,
      f"llen={r.llen(proc_b)}")

# More than one attempt, measured in ticks it survived rather than in a
# counter this check could have read off the implementation. An entry dropped
# on its first failure is scenario A with no recovery, so the bound has to be
# above one for the fix to be a fix at all.
survived = time.time() - pushed_at

check("it was retried across several ticks before being given up on",
      survived >= REAPER_INTERVAL * 2, f"{survived:.1f}s")

# The entry leaves the processing list and arrives on the undeliverable one in
# a single atomic move, so the count below cannot race -- but the cap and the
# expiry are set a command later, and reading them at the instant of the move
# would be reading the gap rather than the outcome. Bounded and unasserted: if
# they never land, the assertions below are what says so.
until(lambda: r.ttl(DEAD_KEY) > 0, 5)

check("and it was set aside on the undeliverable list, exactly once, rather "
      "than destroyed",
      r.lrange(DEAD_KEY, 0, -1).count(raw_b) == 1,
      r.lrange(DEAD_KEY, 0, -1).count(raw_b))
check("the undeliverable list is bounded -- it cannot grow without limit in a "
      "noeviction Redis",
      0 < r.llen(DEAD_KEY) <= 100 and r.ttl(DEAD_KEY) > 0,
      f"llen={r.llen(DEAD_KEY)} ttl={r.ttl(DEAD_KEY)}")

print("\n== C: a requeue whose LREM failed is not re-decided against the live second attempt")

# See check-30-sigkill.py's comfy_saw() for the stub's arrival log; here the
# job has to be one a live worker will actually run for a while, so the second
# reap lands on a job in flight. fake_comfy's __slow__ marker is ~6s.
job_c = f"reapdur-c-{uuid.uuid4().hex[:12]}"
inc_c = f"reapdur-c-{uuid.uuid4().hex[:8]}#{uuid.uuid4().hex[:8]}"
proc_c = f"comfy:processing:{inc_c}"
raw_c = json.dumps({
    "schema_version": 1,
    "job_id": job_c,
    "workflow": {"1": {"class_type": "KSampler", "inputs": {}},
                 "__slow__": {"class_type": "KSampler"}},
    "queue_key": "",
    "attempt": {"count": 0, "phase": "queued"},
    "user": "",
    "submitted_at": time.time(),
})
claim_c = "comfy:reap:claim:" + hashlib.sha256(raw_c.encode("utf-8")).hexdigest()[:32]

r.delete(proc_c, state_key(job_c), stream_key(job_c))
r.hset(state_key(job_c), mapping={"status": "queued", "phase": "queued"})
r.expire(state_key(job_c), 600)


def retry_events(job_id):
    return events_of(job_id, {"retry"})


# THE INJECTED FAULT: LREM is denied to the one user everything here connects
# as. Lifted the moment the requeue is observed, which is a few milliseconds
# after the reaper's own LREM has already raised -- and well before the live
# worker's finish() needs the command for its own list, six seconds later.
r.execute_command("ACL", "SETUSER", "default", "-lrem")
try:
    r.lpush(proc_c, raw_c)

    requeued = until(lambda: len(retry_events(job_c)) > 0, REAPER_INTERVAL * 4 + 10)
    time.sleep(0.3)
finally:
    r.execute_command("ACL", "SETUSER", "default", "+lrem")

check("fixture: the reaper requeued the stranded entry (a retry event is on the stream)",
      requeued, retry_events(job_c))
check("fixture: the fault fired -- the requeue landed but the entry is STILL on "
      "the processing list, exactly the state a raised LREM leaves behind",
      r.llen(proc_c) == 1, f"llen={r.llen(proc_c)}")

# The claim's TTL is a visibility timeout for a reaper that died mid-reap. On
# HEAD the raised LREM leaves it in place, so the next look at this entry is
# 60 seconds out; deleting it here is that timeout elapsing, on this tick.
r.delete(claim_c)

left = until(lambda: r.llen(proc_c) == 0, REAPER_INTERVAL * 4 + 5)
check("the entry left the processing list within a few ticks -- the removal "
      "was retried rather than left until the claim expired",
      left, f"llen={r.llen(proc_c)}")

ended = until(lambda: r.hget(state_key(job_c), "status") in TERMINAL_TYPES, 40)
final_c = r.hgetall(state_key(job_c))

check("the requeued job ran to completion on the live worker -- the second "
      "look at the entry did not fail a job that was executing",
      ended and final_c.get("status") == "completed", final_c)
check("exactly one terminal event, and it is the worker's own completion -- "
      "not a `failed` from a reap that re-decided an entry it had already "
      "requeued",
      terminal_events(job_c) == ["completed"], terminal_events(job_c))
check("the ownership fence was not stamped over the second attempt's claim",
      final_c.get("owner") != "#reaped", final_c.get("owner"))
check("the retry was spent exactly once", final_c.get("attempt_count") == "1",
      final_c.get("attempt_count"))

# The fixtures' own leftovers. Not the undeliverable list, which is asserted
# on above and expires on its own.
r.delete(state_key(job_a), stream_key(job_a),
         state_key(job_b), stream_key(job_b),
         state_key(job_c), stream_key(job_c), proc_c)

print()

if failures:
    print(f"FAILED: {len(failures)}")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)

print("all reaper-durability assertions passed")
