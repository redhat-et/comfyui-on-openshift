# End-to-end test

Runs the real `hub.py` and the real `worker_agent.py` against a real Redis and a
stub ComfyUI, on your laptop. No cluster, no GPU, no AWS.

```bash
./enterprise/test/run.sh
```

Needs `redis-server` on PATH and
`pip install -r enterprise/gateway/requirements.txt websocket-client`.

Install from the requirements file rather than naming the packages: the
`redis<7` ceiling in it is load-bearing — redis-py 8 defaults `socket_timeout`
to 5 seconds, which makes every blocking `BLMOVE`/`XREAD` longer than that
raise instead of waiting, and the blocking paths are most of what this suite
asserts. `CONTRIBUTING.md` says the same thing; the requirements file says why.

`make test` runs three layers: 40 shell unit assertions (`scripts/unit-tests.sh`,
the parsing edge cases and the lint fixtures), 210 pytest cases under
`enterprise/test/unit/` against the pure functions in both Python files
(`python3 -m pytest enterprise/test/unit`, under a second), and then this
suite — 369 end-to-end assertions across 21 check files, in about a minute.

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

**Every event is filtered by `prompt_id`, terminal ones included.** The stub
deliberately emits events for a second, unrelated prompt on the same socket,
including a terminal `executing: node=null`. An agent that does not filter ends
the wrong job's stream and reports success on a job still running. The check
counts, over the job's whole stream: exactly zero events belonging to any other
prompt, exactly one terminal event and that one carrying this job's own
`prompt_id`, and all three of this job's progress events. The terminal half
needs its own count because the foreign terminal event arrives *after* all
three progress events — so an agent that filters progress and not terminals
ends the job on somebody else's completion and still shows three progress
events. The assertion that stood here before ("at least three progress
events") passes on exactly that agent.

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

**A worker that restarts cannot hide its own stranded job**
(`check-32-worker-restart.py`). Everything above proves the reaper acts on a
worker that died and stayed dead, under a replacement with a different name.
This is the case underneath it. The heartbeat key and the processing list are
named from the worker's *incarnation* rather than from `HOSTNAME`, because
`restartPolicy: Always` restarts a container inside its pod and hands it back
the identity it died with — and the reaper's entire liveness test is pairing
those two keys by name, so a reused id lets the new incarnation vouch for the
dead one and the stranded job is skipped for as long as the pod keeps
restarting. The check SIGKILLs a worker mid-execution and brings a second one
up under the same `HOSTNAME` inside `HEARTBEAT_TTL`, then asserts the job
still reaches a terminal state. Three things about the fixture are asserted
rather than assumed, because each of them silently turns this into a
different, easier test: the phase breadcrumb reads `executing` (so the
terminal state can only have come from the reaper), the dead incarnation's
entry is still parked and unreaped at the instant the replacement registers
(so the reap did not simply happen first), and the replacement registered
inside `HEARTBEAT_TTL` of the kill (so this is identity reuse and not an
ordinary lapse-then-reap). `check-30-sigkill.py`'s scenario B, immediately
before it, is the control: the same death without the reuse.

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

**A LIVE worker keeps its job, and a reaped one abandons it**
(`check-36-live-worker-fencing.py`). Every other worker-death check here kills
something. This one kills nothing, because the failure it covers needs no
death: the reaper's whole liveness test is whether the heartbeat key exists,
and `run_job()`'s prologue blocks in three places that used to refresh nothing
— an unbounded `mkdir` on the shared volume, a 30-second WebSocket connect, a
30-second POST. A heartbeat that merely lapsed in there read as a death, at a
phase that is retryable by construction, so a live worker's job was requeued
underneath it and ComfyUI was handed one workflow twice. Scenario A parks a
live agent past `HEARTBEAT_TTL` in the ComfyUI connect and asserts its
heartbeat is still armed there, that `comfy:queue` received no write at all,
and that the stub was handed the workflow exactly once. Scenario B forces the
race the keepalive cannot close — it deletes the live agent's heartbeat key out
from under it until the reaper genuinely requeues the job — and asserts the
original attempt notices it no longer owns the job and abandons rather than
submitting beside the retry. Every count comes from an observer armed before
the event (`QueueWriteWatcher`, the stub's own arrival log, the job's whole
event stream via `XRANGE`) and is read only after the system has gone idle: on
a broken implementation the second run starts *after* the terminal event a
browser stops at, so counting at the terminal event reads 1 either way.

Scenario C is the claim itself, and it is the one scenario here that needed
help from the agent to reach. (B) proves the fence works when the requeue lands
while the worker is parked *before* it. The fence used to be a bare `HGET`
followed, separately, by the `HSET` that writes `executing` and then the
submit — so a requeue landing between the read and the write was read as
"still mine", the phase was written over the retry's `queued`, and the
workflow went to ComfyUI beside the retry: the exact replay (B) exists to
prevent, one line further down. That window is microseconds wide, which is
why (B) cannot reach it and why no stub can widen it: both operations are
against Redis, and nothing outside the process sits between two of its Redis
calls. So this scenario runs an agent of its own with
`TEST_DELAY_BEFORE_CLAIM_S` set — a pause inside the window that exists in
`worker_agent.py` for this check alone, documented there, and never set in a
manifest. The agent pauses at the claim, the check deletes its heartbeat key
until the reaper has genuinely requeued the job, and the agent then reaches
the claim with the reap already stamped over the owner. The claim has to be a
compare-and-set that fails there (`CLAIM_EXECUTING_LUA`): ComfyUI is handed
the workflow once, and the one terminal event is the retry's.

**A reap that fails leaves the job recoverable, and is bounded**
(`check-37-reap-durability.py`). The reaper is the only code that ever writes a
terminal event for a job whose worker died, and its loop used to `RPOP`: the
entry left the processing list *before* the reap ran, so a reap that raised
anywhere in its body destroyed the only record that the job existed, and the
`except Exception: pass` above it promised a next tick that had nothing left to
retry. The check fabricates a stranded entry rather than killing anything —
what is under test is the reaper's own bookkeeping, not any particular way of
dying — and injects exactly one fault per scenario. Scenario A replaces the
job's event stream with a plain string so the terminal `XADD` raises
`WRONGTYPE`, asserts the reap failed *at that write and nowhere earlier* (the
terminal status is already on the hash, the stream is still a string), asserts
the entry is still on the processing list, then lifts the fault and requires
one terminal event on a later tick. Scenario B replaces the job's state hash
with a string, so every reap of that entry raises on its first write forever:
it must be given up on after a bounded number of attempts — more than one, or
scenario A could never recover either — and set aside on the capped, expiring
`comfy:reap:undeliverable` list rather than destroyed. Every count comes from
an observer armed before the reaper could act.

**A retry is not a promotion** (`check-55-retry-placement.py`). Q2's requeue and
Q1's fair queueing meet in one line of `requeue_orphaned_job()`: a requeued job
goes back through the same fair-queueing insert a first submission uses. The
queue is popped from the tail, so a requeue written as a plain `RPUSH` puts the
retried job ahead of every other submitter, and does it again on every death —
a submitter whose workers keep dying then starves the lanes that did nothing
wrong, which is the problem Q1 exists to solve arriving through a door
`check-50-fair-queue.py` never looks at, because nothing there ever dies. The
queue-jump is invisible to every other check here: the job is still requeued
exactly once, still completes, and `comfy:queue` still receives exactly one
write. Only its POSITION changes. So this check builds a two-lane backlog with
a real middle (`[A1 B1 A2 B2 A3 B3]` in service order), fabricates one stranded
job in a THIRD lane — nothing is killed; what is under test is where the reaper
puts a job, as in `check-37` — and asserts the whole service order afterwards:
the retried job must land third, at the back of its own lane's round, which is
neither end of the list. "Not at the front" alone would also be satisfied by a
requeue banished to the very back, which is equally wrong and for the same
reason. The agent is frozen throughout (a queue is only observable while
nothing drains it) and its heartbeat is held armed while it is, so the reaper
does not read the freeze as a death and start reaping the lists being measured.

**A closed socket resolves now, and both of its outcomes are real**
(`check-75-closed-socket.py`). websocket-client does not raise on a server-side
close: it returns the close frame from `recv()` as the empty string, and `""` is
a `str`, so it walks past the binary-frame guard, fails to parse as JSON, and
hits `continue` — the recv loop then spins on a dead socket until `JOB_TIMEOUT`
(1800s in production) with a card held and a browser watching a bar that will
never move. `worker_agent.py` has a two-line guard for exactly that value, and
until this check nothing in the suite executed it: `check-70`'s dead-ComfyUI
fixture closes with code 1006, which a server may not put on the wire, so
uvicorn drops the TCP connection instead and the client raises. Deleting the
guard outright left the whole suite green. Scenario A sends a real empty frame
mid-generation and holds the connection OPEN — the only shape in which the
guard's absence is visible, since a dropped TCP makes the next `recv()` raise
anyway — with `/history` never learning the prompt: the job must fail at once,
naming the lost connection rather than the deadline. Scenario B is the other
outcome of the same handler, and was equally unreached: the same close, but
`/history` has the prompt and its manifest by then, and the agent must ask once
and report `completed` with the outputs rather than turning a lost connection
into a lost generation. Both budgets are expressed in `JOB_TIMEOUT` rather than
in numbers the check picked.

**The estimated-wait gauge reads the end of the queue that is served next**
(`check-80-estimated-wait.py`, Q6). Its other four assertions all run against a
queue exactly one entry deep, where index `-1` and index `0` are the same entry
and a one-character edit moves the gauge from one to the other with everything
still green. They are not the same claim: the list is newest-first and a worker
pops the tail, so the tail's age is the wait a caller is actually queued behind
while the head is an entry nobody is waiting on yet — a gauge reading the head
reports ~0 on an hour-old backlog, which is the number an operator would size
the pool on. So a second job is submitted behind the manufactured stale one,
putting minutes between the two ends of the list, and the gauge is required to
match the served-next end. Both ages are read back off the queue itself rather
than restated from the constant the check chose.

**Path traversal is blocked** on the output endpoint — `/outputs/../../etc/passwd`
must not resolve. And a raw `executed` event cannot smuggle one past it:
`rewrite_image_urls()` builds the browser's URL from whatever `{filename,
subfolder}` ComfyUI put on the event, which the worker forwards verbatim, so
`check-10` writes an `executed` event straight onto a stream with a
traversal, an absolute subfolder, a separator inside `filename` and an empty
component, and requires each of those to arrive with no URL at all while
ordinary shapes still get one.

**The gateway refuses what it should, at the door** (`check-10-stream.py`). A
body that is not JSON is a 400, a body declared over `MAX_BODY_BYTES` is a 413
before a byte of it is read, a POST whose `Content-Type` is not
`application/json` is a 415 — the body was parsed as JSON whatever the header
said, so a `text/plain` cross-site form post could queue a job — and a
WebSocket for a job id that names nothing is closed with code 4404 instead of
being parked on a ping loop holding a Redis connection forever.

**Three limits hold under pressure, not just in prose**
(`check-15-gateway-limits.py`). Each is proven against a dedicated gateway
whose limits the check chose, the way `check-95` pins its own quota. A
WebSocket on a real job is closed by the server (code 4408) once
`EVENT_STREAM_TTL` has elapsed — the stream it tails expired then, and a
socket held past that is a connection held on nothing. `MAX_QUEUE_DEPTH`
holds under a dozen *simultaneous* submits against a ceiling of three, with
the one live agent frozen so the queue is observable: the depth check used to
be an `LLEN` two awaits before the insert, and every submit that raced the
read got in. And six back-to-back `/api/stats` calls cost Redis no more `SCAN`
commands than one cold call — counted off `MONITOR`, `queue_watch.py`'s
technique — because the endpoint is polled every five seconds per browser tab,
unauthenticated, and each call was a full-keyspace scan of a single-threaded
Redis.

**A requeue whose removal failed is removed, not re-decided**
(`check-37-reap-durability.py`, scenario C). The entry leaves a dead worker's
list only after its reap has finished, and for a retryable death "finished"
means the job is already back on the queue and running on a live worker. If
the `LREM` that follows raises, the entry is still parked and the next look at
it used to reap it again: stamp the ownership fence over the second attempt's
claim, read a phase that is now `executing`, and fail a job that was running
perfectly well — whose worker then discarded its own result because it no
longer owned the job. The fault is a Redis ACL rule denying `LREM` for the
instant the reaper needs it; the requeued job must complete with exactly one
terminal event and its owner untouched, and the entry must leave the list
within a few ticks.

**Under `AUTH_MODE=oauth`, outputs are the submitter's own**
(`check-66-output-scoping.py`). Q3's workspaces said reads were not scoped, on
the argument that under `AUTH_MODE=none` the identity is a header the caller
wrote. That argument is right for `none` and wrong for `oauth`, where the
proxy sets the header from a real login — and where any logged-in user could
read any other's images, because a workspace name is a pure function of a
username `/api/showback` listed. The check runs a dedicated `AUTH_MODE=oauth`
gateway and discovers alice's workspace the way `check-60` does, off the URL
the worker reports for a real job of hers — so a gateway whose mirrored
`workspace_name()` disagreed with the worker's would refuse alice her own
file. bob gets a 403, so does a request with no identity, a
`/outputs/<alice>/../<bob>/x` is refused on its *resolved* path, and the
`none` gateway still serves alice's file to bob, pinned because that is that
mode's documented behaviour. `/api/showback` under `oauth` names other
submitters only to callers listed in `SHOWBACK_OPERATORS`; everyone else gets
their own row and the totals.

**Outputs are per-submitter, and a submitter's name cannot escape.** Two users
submitting the identical workflow land in two different directories, which the
stub makes a real test rather than a tautology: `fake_comfy.py` reports the
same `{out_0001.png, ""}` for every job it is ever given, so the returned URLs
can only differ if the submitter's identity actually changed where the output
went (docs/10-roadmap.md, Q3). The rest of `check-60-user-workspaces.py` is
hostile-input testing, because the identity it names a directory from is a
request header: a username of `../../../../../../../../tmp/evil`, of
`/etc/passwd`, empty, and 2000 characters long must each either be refused at
submit or produce only a confined, namespaced, servable output — and, in the
other direction, an
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

**ComfyUI's own reported output is confined too, on both halves of the URL**
(`check-65-output-filename-confinement.py`). Everything above is about what
the *caller* supplied. The filename and subfolder come back from ComfyUI, and
they are not trusted either. Scenarios (a)–(d) reproduce, against the live
agent's own output, the escape that lived in how the URL's two halves were
joined — a confined subfolder with the raw filename concatenated onto it —
and then assert a hostile filename produces no image entry at all, that an
ordinary filename with parentheses still round-trips, and that a filename
merely containing a separator is refused. Five more scenarios were added in
the audit sweep, each fixing something the first four could not see:

- **(e) A preview is not an output.** The stub reports a `type: temp` entry
  beside a real one; only the `output` entry is served. A preview is
  ComfyUI's scratch, and serving it would mean serving whatever a node wrote
  to the temp directory.
- **(f) Filenames are percent-encoded.** A name with a space, a `#` and a `%`
  in it is served through a URL that a browser will actually fetch, rather
  than one that truncates at the fragment.
- **(g) An output inside another submitter's workspace is refused, not
  moved.** The old confinement moved anything reported outside the workspace
  *into* it. Moving a file out of a colleague's directory is not confinement,
  it is theft with a tidy log line; the job now fails naming the path.
- **(h) `..` is refused per component, before anything resolves.** A
  subfolder like `a/../b` resolves inside `OUTPUT_ROOT` and used to pass;
  the check asserts no image is reported, no `..` appears in any URL, and —
  the mechanism this is really pinning — `OUTPUT_ROOT`'s own mode is
  untouched and no directory named after `OUTPUT_ROOT` was created inside
  it. The unnormalised join used to produce exactly that sibling-of-every-
  workspace directory, and `ensure_workspace()`'s `chmod` walked through it.
- **(i) The forwarded `executed` event carries no paths.** The worker used to
  forward ComfyUI's per-node `executed` event verbatim, `data.output` and
  all, so the gateway's `rewrite_image_urls()` built browser URLs from raw,
  unconfined paths a beat before the confined terminal manifest arrived.
  The forwarded copy now names the node that finished and nothing else; the
  terminal event is the only place a path leaves the worker. The gateway keeps
  its own bare-component rule for the raw event (`check-10`) for older
  workers.

**A job the agent gives up on is interrupted at ComfyUI, and a ComfyUI that
will not stop costs the pod, not the queue**
(`check-67-job-timeout-interrupt.py`). ComfyUI executes one prompt at a time
and binds loopback, so this agent is the only thing that ever submits to it and
the only thing that can stop it. When a job hit `JOB_TIMEOUT` the agent
raised, reported the job failed, and took the next one — while ComfyUI was
still executing the prompt that timed out. The next prompt went into ComfyUI's
own queue behind it, received no event, and timed out too; and so did every
job after, with the liveness probe green, because it asks ComfyUI's HTTP
server and not its executor. One custom node wedged in a C call bricked a pod
with everything showing healthy. The check runs its own agent with a
`JOB_TIMEOUT` of seconds. Scenario A submits a prompt that never finishes but
honours `/interrupt` the way the real sampler does between steps, and asserts
the job fails naming the deadline, that ComfyUI received exactly one
`/interrupt` for it, that ComfyUI's queue is empty afterwards, and — the
assertion that fails on HEAD — that the **next** job completes normally on the
**same** worker. Scenario B submits a prompt that ignores `/interrupt`: the
agent must still fail the job, wait its bounded `INTERRUPT_DRAIN_TIMEOUT` for
the queue to empty, see it not, and then exit non-zero and deregister rather
than take the next job — in the pod, `start.sh` exits when either child does,
so the kubelet replaces a ComfyUI that cannot be interrupted with one that
can. The stub had to become serial for this (`fake_comfy.py`,
`execution_lock`): with the old concurrent stub the next job simply ran beside
the wedged one, and no assertion about it could fail. Every never-ending
prompt the check starts is released in its `finally`, because one left behind
would hold ComfyUI's slot for every later check.

**The agent keeps a liveness file fresh, idle and mid-job**
(`check-68-agent-liveness-file.py`). The pod's liveness probe asked ComfyUI
whether it was up and asked the agent nothing, and the two ways the agent stops
consuming the queue without dying — a Redis connection blackholed rather than
refused, so `BLMOVE` parks forever; a heartbeat thread that cannot reach Redis
and swallows the error by design — are both invisible to it. The pod sits
`Running`, holding a card, until somebody notices. So the agent touches
`AGENT_LIVENESS_FILE` once per pass of its poll loop and once per heartbeat
refresh, from the thread that would be the one wedged, and the manifest's exec
probe fails the pod when the file is older than 120 s — twice the production
heartbeat refresh, so one missed refresh is not a restart and two are. What is
asserted is the mtime *advancing*, while the agent idles and again while it is
inside a job: existence alone would pass on an agent that touched the file at
startup and wedged, and a touch that only happened between jobs would have the
kubelet restart the pod in the middle of every long render. Nothing here runs
the probe command itself; that lives in the manifest, and this suite reads no
manifest.

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
against a `noeviction` Redis. The 300-character identity also has to land on
the state hash — and therefore in the report — *clamped* to
`MAX_ENVELOPE_FIELD_CHARS`: the field count is capped in Redis, but a field
name is built from whatever `generate()` wrote on the job, and writing the raw
header there was a second, uncounted way to grow the one Hash the cap bounds.
What this cannot see is the accumulator's expiry, which is measured in months,
or its identity cap, which is a thousand; `scripts/lint.sh` pins both.

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

## The output path has been attacked, and the attack is in the suite

Every path that ends on the shared volume begins as something a caller chose.
The submitter's name arrives in a request header, a save node's
`filename_prefix` arrives inside the workflow, and the output filename comes
back from ComfyUI. All three are handled as hostile, and ten hostile strings
are driven through the running system on every `make test`: four usernames (an
eight-deep traversal, an absolute path, an empty value, 2000 characters), a
`filename_prefix` carrying `..`, two reported output filenames
(`../../OUTSIDE/secret.txt`, and a bare `sub/evil.png` that never spells `..`
at all), a `/outputs/../../etc/passwd` request, and two hostile submitter
identities pushed through the showback accounting. None of them escapes
`OUTPUT_ROOT`. The legitimate cases are asserted in the same files —
`alice.smith@example.com` and a filename with parentheses must still
round-trip to a real, servable URL — because sanitizing hard enough to break
the usernames an IdP actually issues is its own failure.

There are two layers, and the second one has already caught what the first one
missed. Sanitization makes a separator unrepresentable in a workspace name;
the join is then resolved and re-verified against `OUTPUT_ROOT`, and the
gateway independently refuses to serve any `/outputs/...` path that resolves
outside it. `check-65-output-filename-confinement.py` exists because subfolder
confinement alone was not enough: `output_subfolder()` confined the subfolder
correctly and `collect_outputs()` then concatenated the raw reported filename
onto it, so the escape lived in how the URL's two halves were joined rather
than in what either half returned. That check reproduces the escape against the
live agent's own output first, and only then asserts the fix — a hostile
filename produces no image entry at all, rather than one served from a
rewritten name.

One part of this is reasoned rather than tested, and it is worth saying so:
resolve-then-verify is what would catch a symlink already planted in the output
volume, which no string rule can see, and nothing in the suite plants one. The
order of operations is the argument there (`worker_agent.py`,
`workspace_path()`), not a green check.

## The assertions that could not fail, and what replaced them

An assertion nobody has watched fail is a decoration, and this suite has
caught ten of its own being exactly that, plus two behaviours no assertion
reached at all:

- Three assertions of the form "`comfy:queue` is empty afterwards" were
  **proven unable to fail** by mutation testing. One agent polls one queue
  here, so an entry a wrong implementation put back has already been popped
  again by the time a check looks — `LLEN` reads 0 either way. They were
  replaced by counting the command Redis actually executed
  (`enterprise/test/queue_watch.py`).
- The worker's guard for a server-side socket close was **deleted outright and
  the whole suite stayed green**: no check reached it, because the one
  dead-ComfyUI fixture closed in a way that made the client raise instead.
  That is what `check-75-closed-socket.py` was written for.
- A re-gate against a live mutation that really did requeue a rejected job
  found the assertion still passing, because the watcher was armed after the
  submit rather than before it (`check-20-failure-paths.py`, and the same
  construction in `check-70-oom-paths.py`).
- **"At least three progress events"** passed on an agent that filters progress
  and not terminals: the stub's foreign terminal event arrives *after* all
  three, so the job ends on somebody else's completion with three progress
  events already delivered. `check-10-stream.py` now counts foreign events over
  the job's whole stream and requires the one terminal event to carry this
  job's own `prompt_id`.
- **Every estimated-wait assertion — four of them — ran against a one-entry
  queue**, where index `-1` and index `0` are the same entry — so nothing could
  tell which end of the queue the gauge reads, and a gauge reading the head
  reports ~0 on an hour-old backlog. `check-80-estimated-wait.py` now submits
  a second job behind the manufactured stale one.
- **A requeue that jumps the queue is invisible to every other check**: the job
  is still requeued once, still completes, and `comfy:queue` still receives
  exactly one write. Only its position changes.
  `check-55-retry-placement.py` builds a two-lane backlog with a real middle
  and asserts the whole service order afterwards.

The harness itself had the same shape of hole, and it is worth naming beside
them: `run.sh` folded only a check's exit status, so a check that kept printing
its `FAIL` lines while its `sys.exit(1)` went missing left the suite — and CI,
which reads the same status — green over its own output. It now reads each
check's output for the shared `check()` helper's own line format as well.

So the useful thing to do with this suite is to distrust the authors and check.
Delete the `prompt_id` filter in `worker_agent.py`, or its SIGTERM handler, or
the workspace confinement, and run `make test`: something goes red, and which
assertion goes red tells you what that code was actually holding. It is a
faster way to find out what the suite is worth than reading it.
`docs/10-roadmap.md` states the standing rule in the other direction — an
existing assertion may be replaced only by one that is strictly stronger, in
the same commit, with both shown in review.

## The fair-enqueue cost was measured, not assumed

Fair queueing means placing a new job relative to the jobs already queued,
which means reading them, inside one Lua `EVAL`. Redis executes one command at
a time, so that read is time no other client gets — including every worker
parked in `BLMOVE`, which is to say the pool stops being handed work for the
duration. One submitter's insert is therefore a cluster-wide cost, and it was
measured rather than reasoned about.

| One `/api/generate`, 499-deep queue | ~26 KB workflow | ~103 KB workflow |
|---|---:|---:|
| Whole envelope in the list | 117.7 ms | 1235 ms |
| Ordering record in the list, workflow at `comfy:job:<id>:payload` | 1.6 ms | 2.2 ms |

The second column is the more useful one: what the insert reads per entry is
now fixed, so the cost has stopped tracking a size the *client* chooses.
`bench-fair-enqueue.py` (above) imports the real `hub.py` rather than
reimplementing the call shape, and uses its own key namespace, so the numbers
are re-measurable against any Redis — the recorded ones are an M-series laptop
with redis-server 8. This suite structurally cannot see this at all, since it
runs a queue three jobs deep with a two-node workflow where every version of
the insert is instant; `make lint` pins the shape the number depends on, and
`docs/09-engineering-handoff.md` section 3 has the row.

## The pytest layer (`enterprise/test/unit/`)

Everything above proves the system through a real submit against a real
Redis and a stub ComfyUI. `enterprise/test/unit/` is the other half: pure
functions in `hub.py` and `worker_agent.py`, called directly, in-process,
with no Redis and no ComfyUI at all -- `python3 -m pytest enterprise/test/unit`,
under a second. It exists for the functions where the interesting behaviour
is a return value for a given input rather than an effect on the system: the
queue envelope's round trip and its field clamp, `workspace_name()`'s
hostile-input and unicode handling, the Dec→Jan and leap-February boundaries
in `quota_period_reset()`/`showback_period()`, and the path-confinement
functions (`is_bare_filename()`, `output_subfolder()`, `output_url()`,
`locate_output()`) including a planted symlink, which this suite can arrange
far more cheaply than a two-pod EFS scenario. `enterprise/test/unit/README.md`
lists exactly what is covered and what is deliberately left to the checks
above instead. It is wired into `make test` (first, since it is the fastest
layer) and into CI as its own step.

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

**Shared helpers live in `harness.py`.** A check imports `check`, `GW`,
`COMFY`, `drain`, `state_key` and the rest from it rather than pasting its own
copy; `harness.py`'s docstring lists what is there and what a check may assume
from it. `GW` and `COMFY` are `Client`s already bound to the shared gateway and
the stub ComfyUI, `check()` is the single definition of the PASS/FAIL line
`run.sh` folds on, and `drain()`, `start_agent()` and `showback()` take
parameters for the few places two checks genuinely need different behaviour. A
check that needs a helper the module lacks adds it there, not inline — the
suite went from 31% to 6% duplicated code when the copies were folded, and the
FAIL marker stopped living in twenty-one files.

The convention:

| | |
|---|---|
| **Name** | `check-NN-slug.py` — `NN` a zero-padded ordinal, `slug` what it is about. |
| **Order** | Filename order, so leave gaps: `10`, `20`, `30`. Do **not** write `check4.py`; unpadded numbers sort wrong (`check10` lands between `check` and `check2`) and the ordering here is load-bearing. |
| **argv[1]** | The pid of a worker agent that is up and polling. Ignore it if you do not need it. |
| **Environment** | Inherited from `run.sh`: Redis on 6399, the gateway on 8100, the stub ComfyUI on 8999, and the shrunk `HEARTBEAT_TTL` / `REAPER_INTERVAL` / `JOB_TIMEOUT` that let worker-death assertions resolve in seconds. |
| **Exit status** | 0 for pass, non-zero for fail. Print one `PASS`/`FAIL` line per assertion through `harness.check()` — the one place the marker format is defined. |
| **Reported failure** | A printed failure fails the suite on its own, whatever the check exits. `run.sh` reads each check's output for the `check()` helper's own line format — two spaces, `FAIL`, two spaces, at the start of a line — so a check that keeps printing its FAIL lines but stops turning them into a non-zero exit (the `sys.exit(1)` lost in an edit, a failures list nothing reads any more) is still caught, instead of leaving the suite and CI green over its own output. The marker is anchored and padded rather than a search for the word, so ordinary prose — an assertion name that mentions failing, a Redis value echoed into a detail field, the `N FAILED: [...]` summary line — does not trip it. Keep using the shared helper and this is automatic. |
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
