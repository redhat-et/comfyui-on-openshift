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
Q2): a worker SIGKILLed before ComfyUI was ever handed the workflow (still
parked in the ComfyUI WebSocket connect it does before submitting anything)
has its job requeued exactly once, as a non-terminal `retry` event a tailing
browser does not stop at, and a second agent completes it. A worker SIGKILLed
after execution began gets the original behavior unchanged — one terminal
`failed` naming the dead worker, never requeued, and it drops out of
`/api/stats` on its own — because a workflow that OOM-killed one worker would
OOM-kill whichever worker it was requeued onto next. The stub records every
workflow it is handed, so "ComfyUI had not seen it" is asserted rather than
assumed on both sides of that line.

**And the two doors into a replay stay shut** (`check-35-retry-doors.py`).
The breadcrumb decides whether a death is retried, so it has to be true at
every instant, and the retry has to be about work somebody still wants.
ComfyUI receives a workflow when the POST is *written*, not when it answers:
the check waits until the stub reports it has the workflow — while the agent
is still blocked on a stalled `/prompt` — and asserts the breadcrumb already
reads `executing`, then kills the agent there and asserts one terminal
`failed` and no requeue. Separately, a job the user cancelled must not be
requeued when its worker dies (the reaper's door), and must never be handed to
ComfyUI at all when a healthy worker pops it (the worker's door, and what
`cancel()` promises: a job that has not been picked up yet never starts). The
second of those is measured by asking the stub whether the workflow ever
arrived, which is the only way to see a GPU that was spent.

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

**A save node's `filename_prefix` is rewritten into the workspace, and a
traversal through it is refused.** Every workflow every other check submits is
a bare `KSampler` with no `filename_prefix` input, so `scope_workflow_outputs()`
(`worker_agent.py`) finds nothing to rewrite anywhere else in this suite —
without this, every agent log line in a full run reads "0 save node(s)
rewritten", and half of Q3 is never actually exercised. `check-60` gives one
workflow a `SaveImage` node and asks `fake_comfy.py` what `filename_prefix` it
actually received (its output manifest is the same either way, so that alone
cannot prove the rewrite happened): a plain prefix must arrive already moved
inside the submitter's workspace, and one carrying `..` must never arrive at
all — the job fails first, naming the prefix, with no GPU spent on a workflow
that was always going to be refused.

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

**GPU seconds land on the right line, including when nobody reported them.**
`check-90-showback.py` (docs/10-roadmap.md, Q4) does not check that showback
recorded *something* — it submits a deliberately slow job, measures the job's
real wall-clock duration itself, and requires the recorded figure to track it,
so a hardcoded constant, a per-job count and a definition that measures only
ComfyUI's own execution all fail alongside zero. It then checks the three
places the accounting can silently go missing. A second user's job must leave
the first user's total **bit-for-bit** unchanged, which is what tells one
counter shared by two submitters apart from two counters. A submission with no
`X-Forwarded-User` at all must land in the explicit `anonymous_gpu_seconds`
bucket rather than in a blank `users[""]` key. And a job whose worker was
SIGKILLed mid-execution must still be accounted for — that death is terminated
by `hub.py`'s `fail_orphaned_job()`, which never calls `worker_agent.py`'s
`finish()`, so an implementation hooked only into `finish()` drops precisely
the jobs where a card was held and nothing came back. The check reproduces
that death the way `check-30` does, confirming with the stub that ComfyUI
really had the workflow first, so "GPU time was actually spent" is asserted
rather than assumed. Finally it drives nine distinct submitter identities —
including a path-traversal string and a 300-character one — through the system
and asserts `comfy:showback:*` holds *fewer keys than that*: the identity is a
client-supplied header, and one Redis key per submitter is unbounded growth
against a `noeviction` Redis. What this cannot see is the accumulator's
expiry, which is measured in months, or its identity cap, which is a thousand;
`scripts/lint.sh` pins both.

**An over-quota submission never reaches the queue, and everything else
still does.** `check-95-quota-breaker.py` (docs/10-roadmap.md, Q5) starts its
own gateway on :8101 with `QUOTA_GPU_SECONDS` pinned to a value it chose —
the same reason `check-30` starts its own agent with a shrunk
`HEARTBEAT_TTL` — and seeds four synthetic submitters straight into the real
showback Hash: one far over the ceiling, one under it, one with no field at
all, and one whose field holds a non-numeric value. The refusal is proven as
**zero writes to `comfy:queue`**, counted by a `QueueWriteWatcher` armed
before the request, not by an `LLEN` afterwards — an implementation that
rejects the response *after* enqueueing is worse than none, and a queue read
after the fact cannot see the difference once the live agent has popped the
job. The other three must each be queued exactly once and run to completion,
which is the fail-open requirement stated as three separate fixtures: a
missing field, an unreadable one, and a genuinely under-quota user are three
different code paths, and a strict `float()` passes two of them. Finally
`/readyz` must still read exactly `{"ok": true}` with the over-quota identity
sent on the readyz request itself. That last one passes trivially today and is
meant to: it is the regression guard against wiring the breaker into the
readiness probe, which would turn one person's ceiling into a gateway-wide
outage. What it cannot see is the *shape* of that separation — a health
endpoint that reads the quota is green whenever its reader is under the cap —
so `scripts/lint.sh` walks `hub.py`'s call graph for it.

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
it drains; `check-30-sigkill.py` and `check-35-retry-doors.py` SIGKILL it and
assert the gateway's reaper notices. `run.sh` checks the agent is alive before each check and starts a fresh
one if the previous check left it dead, so the check after yours is unaffected —
and no check ever runs beside a second registered worker, which assertions about
`workers_registered` could not see through.

**What a check may assume:** Redis, the stub ComfyUI, the gateway and one live
agent are all up, and every earlier check passed. **What it may not assume:**
that it is last, or that the queue is empty — an earlier check may have left
finished jobs and their streams behind.
