# End-to-end test

Runs the real `hub.py` and the real `worker_agent.py` against a real Redis and a
stub ComfyUI, on your laptop. No cluster, no GPU, no AWS.

```bash
./enterprise/test/run.sh
```

Needs `redis-server` on PATH and `pip install redis websocket-client fastapi
'uvicorn[standard]'`.

## What it measures, separately

`bench-fair-enqueue.py` is not a check and `run.sh` does not run it — it is a
measurement, of what one `/api/generate` costs Redis at a realistic queue depth
with a realistic workflow. The suite cannot answer that: it runs a queue three
jobs deep with a two-node workflow, where every version of the fair-queueing
insert is instant and correct. Redis is single-threaded, so that cost is time
no other client gets, workers parked in `BLMOVE` included. Point it at any
Redis; it uses its own key namespace and drains nothing:

```bash
REDIS_URL=redis://127.0.0.1:6399/0 REDIS_PASSWORD=testpass123 \
    python3 enterprise/test/bench-fair-enqueue.py
```

Its docstring carries the recorded numbers and what a bad one means.

## What it actually asserts

The point is not coverage for its own sake — it is the handful of behaviours
that are easy to get wrong, impossible to notice in a demo, and miserable to
debug in a cluster. Each of these corresponds to a bug in the original design,
documented in `docs/07-design-review.md`.

**Progress is filtered by `prompt_id`.** The stub deliberately emits events for
a second, unrelated prompt on the same socket, including a terminal
`executing: node=null`. An agent that does not filter ends the wrong job's
stream and reports success on a job still running. The test asserts no foreign
events leak through and that the foreign terminal event does not end the job
early.

**A late subscriber loses nothing.** The test waits three seconds — long enough
for the job to finish — before opening the WebSocket, and asserts it still
receives the full history from `queued` onwards. This is the case Redis pub/sub
silently drops, and it is the common case rather than an edge case.

**A reconnect replays identically.** A second WebSocket to the same job gets the
same event sequence. Browsers sleep and gateway pods roll.

**Failures surface with their reason.** A workflow ComfyUI rejects produces a
`failed` event carrying ComfyUI's own message, not a generic error. The
difference between "failed" and "failed: required input is missing: ckpt_name"
is most of the support burden. It is also not retried (docs/10-roadmap.md,
Q2): the agent that submitted it never died, so this never goes through the
reaper's worker-death path at all, whatever phase breadcrumb it happens to
share with a job that *would* be retried.

**Cancel works** on a job in flight.

**SIGTERM drains rather than drops.** The test starts a slow job, sends SIGTERM
to the agent mid-generation, and asserts the job still reaches `completed` and
the agent then exits on its own. This one matters more than it looks: the worker
pool scales to zero, so termination is routine. Without it, every scale-down
throws away whatever was rendering and leaves a browser on a progress bar that
never moves.

**SIGKILL still surfaces a terminal event — and whether it's retried depends on
when.** SIGTERM is the polite case; an OOM kill or node reclaim gives no
warning at all. The agent parks each job in a per-worker processing list and
holds a TTL'd heartbeat — see point 5 in `worker_agent.py` — and the gateway's
reaper notices a lapsed heartbeat and acts on the stranded job. What it does
depends on the `phase` breadcrumb on the job's own state (docs/10-roadmap.md,
Q2): a worker SIGKILLed before ComfyUI ever saw the workflow (still blocked
waiting on ComfyUI's acceptance) has its job requeued exactly once, as a
non-terminal `retry` event a tailing browser does not stop at, and a second
agent completes it. A worker SIGKILLed after execution began gets the original
behavior unchanged — one terminal `failed` naming the dead worker, never
requeued, and it drops out of `/api/stats` on its own — because a workflow
that OOM-killed one worker would OOM-kill whichever worker it was requeued
onto next.

**Path traversal is blocked** on the output endpoint — `/outputs/../../etc/passwd`
must not resolve.

**Outputs are per-submitter, and a submitter's name cannot escape.** Two users
submitting the identical workflow land in two different directories, which the
stub makes a real test rather than a tautology: `fake_comfy.py` reports the
same `{out_0001.png, ""}` for every job it is ever given, so the returned URLs
can only differ if the submitter's identity actually changed where the output
went (docs/10-roadmap.md, Q3). The rest of `check-60-user-workspaces.py` is
hostile-input testing, because the identity it names a directory from is a
request header: a username of `../../../../tmp/evil`, of `/etc/passwd`, empty,
and 2000 characters long must each either be refused at submit or produce only
a confined, namespaced, servable output — and, in the other direction, an
ordinary `alice.smith@example.com` must still get a real workspace, since
sanitizing so hard that real usernames break is its own failure. What this
cannot see is the directory *mode*: it runs one agent as one UID, and the bug
it would be looking for needs two pods with two different arbitrary UIDs on
EFS. `scripts/lint.sh` pins the mode instead.

**The queue payload envelope round-trips, and tolerates both vintages.** A
submitted job carries `schema_version` plus the four fields reserved for later
roadmap items, each with its default, and the test reads them off the raw Redis
list rather than trusting the gateway's response. It then pushes two payloads
the gateway would never write — the pre-F2 `{job_id, workflow}` shape, and one
carrying a field neither file defines — and asserts both run to completion with
the version the worker actually parsed recorded on the job's state hash. This is
the rolling-deploy case: the gateway and the workers are separate images, so a
queue entry written by one vintage is always read by the other at some point
during a rollout, and `docs/07-design-review.md` explains why the failure that
matters there is a discarded entry rather than a crash.

## What it does not cover

Anything requiring a real cluster: KEDA actually scaling, the machine pool
provisioning a node, oauth-proxy, EFS, the GPU itself. Those need
`enterprise/setup.sh` against a real cluster.

## Adding a check

**A check is a file. There is no second place to edit.** `run.sh` discovers
every `check*.py` in this directory, runs them in filename order, and stops at
the first one that exits non-zero — whose status becomes the suite's. A check
that is dropped in here and never mentioned anywhere else still runs, and still
fails the suite. That was not true before `docs/10-roadmap.md` item F3: the
files were copied by a glob but invoked by hardcoded name, so a new check was
copied, never run, and the suite stayed green.

The convention:

| | |
|---|---|
| **Name** | `check-NN-slug.py` — `NN` a zero-padded ordinal, `slug` what it is about. |
| **Order** | Filename order, so leave gaps: `10`, `20`, `30`. Do **not** write `check4.py`; unpadded numbers sort wrong (`check10` lands between `check` and `check2`) and the ordering here is load-bearing. |
| **argv[1]** | The pid of a worker agent that is up and polling. Ignore it if you do not need it. |
| **Environment** | Inherited from `run.sh`: Redis on 6399, the gateway on 8100, the stub ComfyUI on 8999, and the shrunk `HEARTBEAT_TTL` / `REAPER_INTERVAL` / `JOB_TIMEOUT` that let worker-death assertions resolve in seconds. |
| **Exit status** | 0 for pass, non-zero for fail. Print one `PASS`/`FAIL` line per assertion — the existing checks share a four-line `check()` helper worth copying. |
| **Runtime** | Under `CHECK_TIMEOUT` (default 240s), which is a hang kill-switch rather than a budget. Keep a check to seconds; the whole suite is meant to run in about a minute. |

**You may kill the agent.** `check-20-failure-paths.py` SIGTERMs it and asserts
it drains; `check-30-sigkill.py` SIGKILLs it and asserts the gateway's reaper
notices. `run.sh` checks the agent is alive before each check and starts a fresh
one if the previous check left it dead, so the check after yours is unaffected —
and no check ever runs beside a second registered worker, which assertions about
`workers_registered` could not see through.

**What a check may assume:** Redis, the stub ComfyUI, the gateway and one live
agent are all up, and every earlier check passed. **What it may not assume:**
that it is last, or that the queue is empty — an earlier check may have left
finished jobs and their streams behind.
