"""N2 -- VRAM-tier routing: a job runs on the card class it declared, and on
nothing else.

The claim tiering makes is a ROUTING claim, so every scenario here is about
which list an entry landed on and which worker was ever allowed to see it --
proven the way this suite proves queue behaviour elsewhere: writes counted by
MONITOR (queue_watch.py), the stub's own record of which workflows reached
ComfyUI, and the worker identity each job's state hash names. Fake tiers,
real routing: "l4" and "l40s" here are two names in GPU_TIERS and two Redis
lists, which is exactly all they are on a cluster too -- the machine pools
behind them are the cluster-day half (docs/12-first-cluster-day.md).

The fixture: this check runs its own gateway (port 8104) with
GPU_TIERS=l4,l40s against the suite's own QUEUE_KEY namespace, so the
DEFAULT tier's queue is the same bare comfy:queue every other check uses --
that identity is the N2 key-shape invariant (docs/09 section 3), not a test
convenience -- and the shared gateway's reaper, which also scans
comfy:processing:*, derives the SAME per-tier queue from a stranded entry as
the tiered gateway does, so scenario E holds whichever replica's reaper wins
the claim, exactly as it must on a cluster running two gateway pods.

The suite's own agent polls that same bare queue, so it is stood down first
(the check-32 pattern) and this check runs its own workers: one with
WORKER_TIER unset (the default-tier pool -- deliberately the SAME worker
configuration an untiered pool runs, which is the migration story) and one
with WORKER_TIER=l40s.

Scenarios:

  A. An unknown tier -- and a non-string one -- is refused with a 400 that
     names the valid tiers, and ZERO writes reach any queue: a loud failure
     at submit, where the caller can fix the typo, never a job parked on a
     list nothing polls.

  B. A job that declares tier=l40s lands on comfy:queue:l40s and ONLY there.
     With no l40s worker up it stays queued -- the default-tier worker,
     polling the bare list two keys away, never sees it and ComfyUI is never
     handed it -- and the per-tier gauges say so (/api/stats tiers,
     /metrics comfy_tier_queue_depth{tier="l40s"}) while the pre-N2 metric
     names read exactly as before. Starting an l40s worker completes it.

  C. A job that declares NO tier is an l4 job: it lands on the bare queue,
     the response and state hash both say tier=l4, and the default-tier
     worker runs it.

  D. The other direction of B, and the headline economics: with the
     default-tier worker gone and the l40s worker idle beside the queue, a
     default-tier job stays queued -- an 80 GB card never drains the cheap
     lane, because its BLMOVE names a different key, not because it is
     polite. Restarting the default worker completes it.

  E. SIGKILL-reap is per-tier: an l40s worker killed before ComfyUI saw the
     workflow has its job requeued exactly once, BY THE REAPER, onto
     comfy:queue:l40s -- zero writes to the bare queue, so a retry can never
     demote a job onto a smaller card -- and a fresh l40s worker completes
     it. Showback then carries the tier: the period Hash's t:l40s field
     accrued this job's GPU seconds.
"""
import json, os, signal, subprocess, sys, time, urllib.error, urllib.request, uuid

from harness import (
    COMFY, Client, QUEUE_KEY, REDIS_PASSWORD, REDIS_URL, check, comfy_saw,
    connect_redis, drain, failures, start_agent, state_key, stop_agent,
    wait_for, wait_gone,
)
from queue_watch import QueueWriteWatcher

# See check-30-sigkill.py: run.sh's stdout is a pipe, so without this every
# PASS/FAIL line sits in a full block buffer and is lost if CHECK_TIMEOUT
# kills a hung drain.
sys.stdout.reconfigure(line_buffering=True)

r = connect_redis()

TIER_QUEUE = f"{QUEUE_KEY}:l40s"
TGW_PORT = 8104
TGW = f"http://127.0.0.1:{TGW_PORT}"

# The tiered gateway this check starts below, via the same harness Client the
# shared GW uses — the check-15/66/95 own-gateway pattern.
TG = Client(TGW)


def tgw_submit(workflow, tier=None):
    """POST to the tiered gateway; returns (status_code, parsed body) rather
    than raising — Client.post raises on any non-200, and half the point here
    is asserting on refusals."""
    body = {"workflow": workflow}
    if tier is not None:
        body["tier"] = tier
    req = urllib.request.Request(
        TGW + "/api/generate", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"detail": raw}
        return exc.code, parsed


def watchers():
    """One MONITOR watcher per tier queue, armed together: every scenario's
    routing claim is 'this list and NOT the other one', so the negative half
    is always counted too."""
    bare = QueueWriteWatcher(REDIS_URL, REDIS_PASSWORD, QUEUE_KEY).start()
    l40s = QueueWriteWatcher(REDIS_URL, REDIS_PASSWORD, TIER_QUEUE).start()
    return bare, l40s


handed_pid = int(sys.argv[1])

print("\n== the suite's own agent stands down, so this check owns both tier queues")

# SIGTERM rather than SIGKILL, as in check-32: the drain deletes the heartbeat
# on the way out, so "no worker registered" below resolves in seconds.
os.kill(handed_pid, signal.SIGTERM)
alone = wait_for(lambda: not list(r.scan_iter(match="comfy:worker:*")), timeout=40)
check("no worker is registered before this check starts its own -- so which "
      "tier's worker ran each job below is a fact, not a guess",
      alone, list(r.scan_iter(match="comfy:worker:*")))

# A clean slate on the l40s list: an earlier aborted run of this check could
# have parked an entry there, and scenario B counts its depth.
r.delete(TIER_QUEUE)

print(f"\n== a gateway with GPU_TIERS=l4,l40s comes up on :{TGW_PORT}")

env = dict(os.environ)
env["GPU_TIERS"] = "l4,l40s"
# Uncached stats, so the per-tier gauge assertions read the queue as it is
# now rather than a snapshot from before the submit they follow.
env["STATS_CACHE_SECONDS"] = "0"

tgw_log = open("tier-gateway.log", "w")
tgw_proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1",
     "--port", str(TGW_PORT), "--log-level", "warning"],
    env=env, stdout=tgw_log, stderr=subprocess.STDOUT,
)

tgw_up = wait_for(lambda: TG.get("/healthz").get("ok"), timeout=30)
check("the tiered gateway came up", tgw_up,
      "" if tgw_up else open("tier-gateway.log").read()[-2000:])

if not tgw_up:
    print(f"1 FAILED: {['tiered gateway did not start']}")
    sys.exit(1)

agent_l4 = None
agent_l40s = None
agent_l40s2 = None

try:
    # The default-tier worker: WORKER_TIER deliberately UNSET -- the same
    # configuration every pre-N2 worker and every untiered pool runs, which
    # is what makes flipping GPU_TIERS on backlog-safe.
    agent_l4 = start_agent("tier-l4-agent", r=r)

    print("\n== A: an unknown tier is refused loudly, with zero queue writes")

    bare_w, l40s_w = watchers()
    code, body = tgw_submit({"a100-probe": {"class_type": "KSampler"}}, tier="a100")
    bare_writes, bare_cmds = bare_w.stop()
    l40s_writes, l40s_cmds = l40s_w.stop()

    check("an unknown tier is a 400, not a 200 and not a 500", code == 400, (code, body))
    detail = str(body.get("detail", ""))
    check("the refusal names the valid tiers, so the 400 is actionable",
          "l4" in detail and "l40s" in detail, detail[:160])
    check("nothing was written to the default queue -- a refused submit "
          "leaves nothing behind on any list",
          bare_writes == 0, bare_cmds)
    check("and nothing to the l40s queue either", l40s_writes == 0, l40s_cmds)

    code, body = tgw_submit({"n": {"class_type": "KSampler"}}, tier=5)
    check("a non-string tier is refused the same way", code == 400, (code, body))

    print("\n== B: tier=l40s lands on comfy:queue:l40s and ONLY there; no l40s worker, no progress")

    probe_b = f"probe-b-{uuid.uuid4().hex[:8]}"
    bare_w, l40s_w = watchers()
    code, job_b = tgw_submit({probe_b: {"class_type": "KSampler"}}, tier="l40s")
    bare_writes, bare_cmds = bare_w.stop()
    l40s_writes, l40s_cmds = l40s_w.stop()

    check("the submission was accepted", code == 200, (code, job_b))
    check("the response names the tier it routed to", job_b.get("tier") == "l40s", job_b)
    check("exactly one write to comfy:queue:l40s -- the tier's own list",
          l40s_writes == 1, l40s_cmds)
    check("and ZERO writes to the bare default queue -- routing, not priority",
          bare_writes == 0, bare_cmds)

    # Long enough for the default-tier worker to have completed a whole poll
    # cycle (its BLMOVE parks 5s at a time) against the WRONG list, were it
    # ever going to.
    time.sleep(6)

    state_b = TG.get(f"/api/jobs/{job_b['job_id']}")
    check("with no l40s worker up, the job is still queued -- the default-"
          "tier worker, live and idle the whole time, never took it",
          state_b.get("status") == "queued", state_b)
    check("ComfyUI was never handed the workflow -- proven off the stub's own "
          "record, not inferred from status",
          not comfy_saw(probe_b), probe_b)
    check("the entry is sitting on the l40s list, depth 1",
          r.llen(TIER_QUEUE) == 1, r.llen(TIER_QUEUE))

    print("\n== B (gauges): the backlog is visible per tier, under NEW metric names")

    stats = TG.get("/api/stats")
    tiers = stats.get("tiers") or {}
    check("/api/stats grew a tiers map with both configured tiers",
          set(tiers) == {"l4", "l40s"}, stats)
    check("the l40s backlog is on the l40s row",
          tiers.get("l40s", {}).get("queue_depth") == 1, tiers)
    check("the parked entry's age is the l40s row's estimated wait, a real "
          "number and growing",
          (tiers.get("l40s", {}).get("estimated_wait_seconds") or 0) > 0, tiers)
    # The sum RELATION rather than an absolute: an earlier check may
    # legitimately have left an entry on the bare queue (the suite's README:
    # never assume the queue is empty), and the claim being pinned is that
    # the top-level number is the per-tier rows added up.
    check("top-level queue_depth is the pool-wide sum of the per-tier rows, "
          "as its name always promised",
          stats.get("queue_depth") == tiers.get("l4", {}).get("queue_depth", -99)
          + tiers.get("l40s", {}).get("queue_depth", -99), stats)

    metrics = urllib.request.urlopen(TGW + "/metrics", timeout=10).read().decode()
    check("per-tier depth is a NEW suffixed series labelled by tier",
          'comfy_tier_queue_depth{tier="l40s"} 1' in metrics, metrics[-500:])
    check("per-tier wait is exported for the parked tier",
          'comfy_tier_estimated_wait_seconds{tier="l40s"}' in metrics, metrics[-500:])
    check("the pre-N2 gauge names survive un-renamed and un-labelled -- "
          "dashboards built on them keep reading",
          "\ncomfy_queue_depth " in metrics
          and "\ncomfy_estimated_wait_seconds " in metrics, metrics[-500:])

    print("\n== B (drain): an l40s worker arrives and the parked job runs on it")

    agent_l40s = start_agent("tier-l40s-agent", env_extra={"WORKER_TIER": "l40s"}, r=r)

    kinds, terminal = drain(job_b["job_id"], timeout=40)
    check("the job completed once a matching-tier worker existed",
          terminal and terminal["type"] == "completed",
          (terminal or {}).get("type"))

    state_b = r.hgetall(state_key(job_b["job_id"]))
    check("the worker that ran it is the l40s one", state_b.get("worker") == "tier-l40s-agent", state_b)
    check("the state hash records the tier, for showback and for an operator "
          "reading it raw", state_b.get("tier") == "l40s", state_b)

    print("\n== C: no tier declared means the DEFAULT tier -- the smallest card")

    probe_c = f"probe-c-{uuid.uuid4().hex[:8]}"
    bare_w, l40s_w = watchers()
    code, job_c = tgw_submit({probe_c: {"class_type": "KSampler"}})
    bare_writes, bare_cmds = bare_w.stop()
    l40s_writes, l40s_cmds = l40s_w.stop()

    check("the response resolves the omitted tier to the default's real name",
          job_c.get("tier") == "l4", job_c)
    check("the entry went to the bare queue -- the default tier's list IS the "
          "pre-N2 comfy:queue, which is what makes enabling tiers "
          "backlog-safe", bare_writes == 1, bare_cmds)
    check("and not to the l40s list", l40s_writes == 0, l40s_cmds)

    kinds, terminal = drain(job_c["job_id"], timeout=40)
    state_c = r.hgetall(state_key(job_c["job_id"]))
    check("the default-tier worker ran it, with the l40s worker up and idle "
          "beside it", terminal and terminal["type"] == "completed"
          and state_c.get("worker") == "tier-l4-agent",
          (state_c.get("worker"), (terminal or {}).get("type")))
    check("the state hash carries the default tier by name",
          state_c.get("tier") == "l4", state_c)

    print("\n== D: the 80 GB card never drains the cheap lane")

    stop_agent(agent_l4)
    wait_gone(agent_l4.pid)
    agent_l4 = None

    probe_d = f"probe-d-{uuid.uuid4().hex[:8]}"
    code, job_d = tgw_submit({probe_d: {"class_type": "KSampler"}})
    check("the default-tier submission was accepted with its worker gone",
          code == 200, (code, job_d))

    # A full idle BLMOVE cycle for the l40s worker against its own (empty)
    # list -- the window in which a wrongly-shared queue would have lost this
    # assertion.
    time.sleep(6)

    state_d = TG.get(f"/api/jobs/{job_d['job_id']}")
    check("the job stays queued: the l40s worker, live and idle, polls a "
          "different key and structurally cannot take it",
          state_d.get("status") == "queued", state_d)
    check("ComfyUI never saw it", not comfy_saw(probe_d), probe_d)

    agent_l4 = start_agent("tier-l4-agent", tag="back", r=r)
    kinds, terminal = drain(job_d["job_id"], timeout=40)
    check("a returning default-tier worker completes it",
          terminal and terminal["type"] == "completed",
          (terminal or {}).get("type"))

    print("\n== E: SIGKILL-reap is per tier -- the retry rejoins the l40s list, never the bare one")

    # Park the l40s agent where a pre-execution death lives: stalled in the
    # ComfyUI WebSocket connect it does before submitting anything
    # (check-30's fixture, scenario A).
    COMFY.post("/__stall_next_ws__", {"seconds": 15})

    probe_e = f"probe-e-{uuid.uuid4().hex[:8]}"
    code, job_e = tgw_submit({probe_e: {"class_type": "KSampler"}}, tier="l40s")

    # wait_for swallows the pre-state-hash 404 the way poll_status does; the
    # full state is then read once, so the phase below is from the same
    # snapshot that proved the pickup.
    picked_up = wait_for(
        lambda: TG.get(f"/api/jobs/{job_e['job_id']}").get("status") == "running",
        timeout=10)
    picked = TG.get(f"/api/jobs/{job_e['job_id']}") if picked_up else {}
    check("the l40s worker picked the job up", picked_up, picked)
    check("and died-to-be at phase 'dispatched' -- ComfyUI not yet involved, "
          "the one death that MAY be retried",
          picked.get("phase") == "dispatched", picked.get("phase"))
    check("the premise, off the stub's record: ComfyUI has not been handed "
          "this workflow", not comfy_saw(probe_e), probe_e)

    # Armed only now, after the submit's own legitimate insert: what these
    # count from here is the reaper's requeue and nothing else.
    bare_w, l40s_w = watchers()

    os.kill(agent_l40s.pid, signal.SIGKILL)

    # The replacement is ALSO an l40s worker: the retry must be completable
    # only because a matching-tier worker exists, and the still-running l4
    # agent is the control -- if the requeue leaked onto the bare list, the
    # l4 agent would run probe_e and the worker assertion below would name it.
    agent_l40s2 = start_agent("tier-l40s-agent2", env_extra={"WORKER_TIER": "l40s"}, r=r)

    kinds, terminal = drain(job_e["job_id"], timeout=40)
    bare_writes, bare_cmds = bare_w.stop()
    l40s_writes, l40s_cmds = l40s_w.stop()

    check("a non-terminal 'retry' event was published", "retry" in kinds, kinds)
    check("the job completed on the second attempt",
          terminal and terminal["type"] == "completed",
          (terminal or {}).get("type"))

    state_e = r.hgetall(state_key(job_e["job_id"]))
    check("attempt_count shows exactly one requeue",
          state_e.get("attempt_count") == "1", state_e.get("attempt_count"))
    check("the reaper wrote the retry back onto comfy:queue:l40s exactly "
          "once -- counted as the write itself (queue_watch.py), whichever "
          "gateway replica's reaper won the claim",
          l40s_writes == 1, l40s_cmds)
    check("and NEVER onto the bare queue -- a worker death cannot demote a "
          "job onto a smaller card", bare_writes == 0, bare_cmds)
    check("the second l40s worker is what ran it",
          state_e.get("worker") == "tier-l40s-agent2", state_e)

    print("\n== E (showback): the period Hash accrued this job's seconds under t:l40s")

    period = time.strftime("%Y-%m", time.gmtime())
    tier_seconds = r.hget(f"comfy:showback:{period}", "t:l40s")
    check("the t:l40s field exists and is positive -- per-tier rates have a "
          "number to multiply",
          tier_seconds is not None and float(tier_seconds) > 0, tier_seconds)

    report = TG.get("/api/showback")
    check("/api/showback surfaces the same slice as a tiers map",
          float((report.get("tiers") or {}).get("l40s", 0)) > 0,
          report.get("tiers"))

finally:
    stop_agent(agent_l4)
    stop_agent(agent_l40s)
    stop_agent(agent_l40s2)
    if tgw_proc.poll() is None:
        tgw_proc.terminate()
        try:
            tgw_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tgw_proc.kill()

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
