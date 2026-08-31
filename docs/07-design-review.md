# Review of the original design document

The architecture in the source document is right. Hub and spoke, Redis between
the frontend and the GPU workers, workers bound to loopback, EFS for shared
models, oauth-proxy for SSO, scale-to-zero on queue depth — that is the correct
shape for this problem, and the reasoning for each piece holds up.

What follows is what changed in the implementation and why. Roughly half of it
is code that would not run; the rest is claims the manifests did not actually
implement.

---

## Would not run

### `aioredis` is dead

```python
import aioredis                       # original
redis = aioredis.from_url(...)
```

`aioredis` was merged into `redis-py` and archived. The standalone package fails
to import on Python 3.11+ with `TypeError: duplicate base class TimeoutError` —
it predates `asyncio.TimeoutError` becoming an alias for the builtin. Any
example using it is from before that merge.

Now `redis.asyncio`, which is the same library's supported home.

### `hub.py` was never in the image

The Dockerfile copies `worker_agent.py` and `start.sh`. The hub Deployment then
runs `uvicorn hub:app`. There is no `hub.py` in the image, so the frontend
`CrashLoopBackOff`s immediately on `ModuleNotFoundError`.

Now two images from two build contexts — which also fixes the next one.

### The frontend ran the 10 GB GPU image

Both Deployments used `comfyui-production:v5`. The document's own argument for
the split is that the frontend runs on "cheap CPU nodes"; giving it a CUDA +
torch image costs minutes of pull time per pod, gigabytes of node disk, and
makes a gateway rollout a heavyweight operation.

The gateway image is now UBI9 Python at roughly 200 MB.

### `prompt_id` was fetched and never used

```python
res = submit_to_comfy(workflow)
prompt_id = res["prompt_id"]          # assigned, never referenced again
...
if msg["type"] == "executing" and msg["data"]["node"] is None:
    break                             # ends on ANY prompt's completion
```

One ComfyUI instance multiplexes every prompt onto one WebSocket. Without
filtering on `prompt_id`, another job's terminal event ends this job's stream.
Under load, jobs report complete when they are not.

Every event is now filtered by `prompt_id` before it is acted on.

### The WebSocket connected after submitting

```python
res = submit_to_comfy(workflow)       # ComfyUI starts emitting immediately
ws.connect(...)                       # we start listening some time later
```

Everything ComfyUI emitted in that window is lost. Short jobs appear to produce
no progress at all; long ones start partway through the bar.

Connect first, submit second.

### `ws.recv()` with no timeout

A ComfyUI process that dies or wedges parks the agent forever. The pod stays
`Running` and `Ready`, silently stops consuming the queue, and reports nothing.
With autoscaling on queue depth this is worse than a crash: KEDA sees a growing
queue and adds more workers alongside the dead one.

Now a bounded `recv` timeout, a total job deadline, and a `/history` check on
each timeout to catch a completion whose event was missed.

### No SIGTERM handling

```dockerfile
CMD python3 main.py ... & python3 worker_agent.py
```

Three problems, all of which only appear in a cluster:

- PID 1 is the shell. Kubernetes' SIGTERM goes to it, not to the agent, so the
  agent is killed mid-generation at the end of the grace period.
- If ComfyUI dies, the agent keeps running and keeps claiming jobs it cannot
  execute.
- Nothing reaps the background process.

This matters far more here than in a normal deployment, because the whole design
scales workers to zero — termination is routine, not exceptional. Now a
`start.sh` that traps SIGTERM, drains the current job, and exits if either
process dies.

### The pods would not have started on OpenShift at all

The `pytorch/pytorch` base runs as root and writes to `/opt/ComfyUI`. OpenShift's
`restricted-v2` SCC assigns an arbitrary high UID with GID 0 supplementary and
ignores the image's `USER`. ComfyUI writing its `temp/`, `input/` and `user/`
directories fails with `Permission denied`.

This is the single most common reason a container that works under `docker run`
crash-loops on OpenShift, and neither the Dockerfile nor the YAML addressed it.
The worker image now does the `chgrp 0` / `chmod g=u` / setgid treatment, and
runs on a UBI9 base where that is the expected pattern.

### `ecr-pull-secret` expires every 12 hours

ECR authorisation tokens are valid for 12 hours. A static `imagePullSecrets`
entry works on the day you create it and starts failing pulls the next morning —
which, on a pool that scales to zero, means the first job of each day fails to
schedule.

Sidestepped: images are built in-cluster into the internal registry, so there is
no external registry credential to rotate. If you do want ECR, a CronJob that
refreshes the secret is the standard fix.

---

## Claimed but not implemented

### Scale-to-zero

The document leads with it. The manifests contain `replicas: 1`, no KEDA
`ScaledObject`, no `MachineAutoscaler`, and no autoscaling on the machine pool.
As written, one GPU node runs continuously.

Now a real `ScaledObject` on `LLEN comfy:queue` with `minReplicaCount: 0`, plus
`rosa edit machinepool --enable-autoscaling --min-replicas 0` — because the pod
half alone saves nothing. An idle GPU node bills the same whether a pod is on it
or not; only removing the node changes the number.

### The Route pointed at the wrong thing

The GPU worker Deployment had `--listen 0.0.0.0`, a Service, and an edge Route
directly to port 8188. The document's own security argument — "no user can ever
connect to it directly" — was contradicted by its own YAML, which published an
unauthenticated ComfyUI with arbitrary Python execution on the public internet.

The workers now have no Service and no Route. The Route terminates at the
gateway.

### oauth-proxy

Named in the summary as the SSO mechanism; absent from every manifest. Now a
real sidecar, with the ServiceAccount redirect annotation, the service-serving
certificate, a SubjectAccessReview for authorisation, and — the part usually
forgotten — the gateway rebound to `127.0.0.1` so the proxy cannot be bypassed
from inside the cluster.

### No probes anywhere

Every pod was `Ready` the instant the container started, so the Service routed
traffic to a ComfyUI still loading CUDA, and a wedged process was never
restarted.

Probes are awkward here because ComfyUI listens on loopback and the kubelet
cannot reach it — which is presumably why they were omitted. The answer is
`exec` probes, which run inside the container's network namespace. Startup
budget is deliberately large: a cold node takes minutes before ComfyUI answers.

### Nothing returned the images

The pipeline streams progress faithfully and then stops. Generated images sit on
the worker's filesystem; the worker is unreachable; the hub never learns where
they are. There is no path from a finished render back to the browser.

Now the worker publishes the output manifest from `/history`, and the gateway
serves the files from the shared EFS volume it mounts read-only — which is the
concrete reason the multi-user configuration requires `ReadWriteMany` storage
rather than gp3.

### Redis pub/sub drops messages

Covered at length in `06-enterprise-architecture.md`. Short version: pub/sub
delivers only to subscribers connected at publish time, and the browser's POST
and WebSocket open are two separate round trips. The gap is the common case, not
the edge case. Replaced with Redis Streams, where `XREAD` from `0-0` gives
replay and live tail in one call.

---

## Smaller things

- **`git clone --branch v0.17.2`** — ComfyUI moved to the `Comfy-Org` org and is
  well past that. Now a build arg defaulting to a current tag, so pinning stays
  a deliberate choice rather than an accident.
- **No backpressure.** A stuck worker pool turns the queue into an unbounded
  Redis list, and the first symptom is Redis OOM rather than a slow queue. The
  gateway now rejects submissions past a configurable depth, and Redis runs
  `maxmemory-policy noeviction` — the default would silently evict queued jobs,
  which looks exactly like jobs disappearing at random.
- **Redis was unauthenticated and unrestricted.** Any pod that could resolve the
  Service could pop jobs off the queue. Now a generated password plus a
  NetworkPolicy admitting only the gateway and the workers.
- **Router timeout.** OpenShift's HAProxy default is 30 seconds. Generations run
  for minutes and the WebSocket must outlive them, so every Route carries
  `haproxy.router.openshift.io/timeout: 4h`. Without it long jobs lose their
  connection at exactly 30 seconds, every time, which reads like an application
  bug.
- **A hardcoded AWS account ID** (`243724843493`) appears throughout the source
  document. Parameterised.
- **"Ghost nodes: `oc delete pod --force --grace-period=0`"** — this one earns
  its own page (`docs/08-stuck-volumes.md`), because it is not merely bad
  advice, it is the cause of the symptom it claims to cure.

  A volume is released when the kubelet on the node that mounted it reports the
  unmount. Force-delete does not terminate anything; it deletes the API object
  while the container may still be running, so the kubelet never unmounts,
  never reports, and you have destroyed the only record of what still needs
  releasing. The volume is now stuck with nothing to point at. If the node is
  actually alive, you also have a running container writing to a filesystem
  Kubernetes now believes is free — and it will schedule a second pod onto it.

  The supported mechanism for a genuinely dead node is the
  `node.kubernetes.io/out-of-service=nodeshutdown:NoExecute` taint, which
  force-detaches its volumes. It is dangerous on a live node, so
  `scripts/08-unstick-storage.sh --repair` confirms the EC2 instance is actually
  terminated before offering it — `Ready=False` means the kubelet stopped
  answering, which is not the same as the machine having stopped running.

  Prevention is the SIGTERM drain plus `noresvport` on the EFS mount, both of
  which are now in place.

---

## Later decisions, in the same spirit

Everything above reviews the document this repository was built from. The items
in [`10-roadmap.md`](10-roadmap.md) get the same treatment as they land, and for
the same reason: the version that looks obvious on the page is usually the one
that reads fine, passes its own test, and fails intermittently in a cluster.

### F2 — four items each extending the queue payload

The obvious way to give the queue payload a lane key, an attempt count, a
submitter and a timestamp is to let the four items that want them each add
theirs. Every one of those changes is two lines and reviews cleanly on its own,
which is exactly the problem: the payload is a contract between a gateway
running on the CPU nodes and a worker running on a GPU node that scales to zero,
and the two are deployed as separate images. **Both halves of a rollout are live
at once, always.** So each independent extension opens its own window in which a
gateway writes a key the worker beside it has never heard of, or a worker demands
one the gateway did not write — four windows, four different failure shapes, and
nothing on the wire that says which vintage a queue entry came from. The one that
bites is the second kind: `payload["attempt"]` on a leftover entry raises
`KeyError`, the entry is discarded as malformed, and the user's job disappears
with no terminal event at all.

So the envelope is defined once, ahead of the items that need it, and the four
fields are **reserved rather than implemented** — each has a default, round-trips
through Redis, and is read by nothing. An item then changes what reads its field,
not what is on the wire, and needs no version bump to do it. Parsing is tolerant
in both directions: a payload with no `schema_version` is the pre-F2 shape and is
version 1 by definition, and a key this side does not recognise is carried rather
than rejected, because refusing it would strand exactly the work versioning
exists to protect.

Two smaller things that were not obvious until the code was written:

- **"The old shape still works" is unfalsifiable if you only assert that the job
  completes.** Before the change the old shape is the *only* shape, so that
  assertion passes for a reason that has nothing to do with tolerance, and keeps
  passing if tolerance is later removed. The worker therefore writes the version
  it actually parsed onto the job's state hash. That is what turns "it did not
  crash" into something a test — or an operator watching a rollout — can read.
- **There is nowhere to put a shared module.** The two files ship in two images
  built from two different contexts, so the envelope is duplicated verbatim, the
  same "change both or neither" rule the processing-list key shape already
  carried. That rule is exactly the kind that survives review and dies six months
  later, so `make lint` now diffs the two copies. Divergence there is silent in
  the way this document's other entries are silent: both files still compile,
  the suite still passes against whichever half it happens to exercise, and the
  failure appears only when a gateway and a worker of different vintages meet on
  the queue.

### Q2 — retry, and the three things that make it narrow

The obvious implementation is four lines in the reaper: where it fails a
stranded job, put the job back on the queue instead, and count attempts so it
cannot loop. It reviews cleanly, it passes a test that kills a worker and
watches the job finish, and it is wrong in three separate ways, each of which
only appears in a cluster.

**Retryable is a phase, not an exception.** The tempting reading of "retry a
failed job" is "retry unless the failure looks permanent", and there is no
such signal here. A host-RAM OOM kills the ComfyUI process, takes the pod with
it, and reaches the gateway as a lapsed heartbeat — byte for byte the same
thing a node reclaim produces. So a reaper that decides from *what it can see*
is deciding from nothing, and the poison workflow gets replayed onto every
worker in the pool in sequence at GPU prices. The only question with an answer
is **how far the job got**, and the only process that knows is the one that
died. Hence a breadcrumb written ahead of the fact rather than a diagnosis
attempted after it: the agent records `dispatched` when it takes the job and
`executing` before it hands ComfyUI the workflow, and the reaper retries the
first and never the second. The ordering there is the whole content of the
mechanism — a breadcrumb written *after* the transition it describes leaves a
window in which the job is executing and the record says it is not, and that
window is precisely when a retry replays the poison pill.

It also has to live on the job's state hash rather than on the queue entry,
which is where a first draft naturally puts it: the entry is a static copy of
what the gateway pushed, no worker ever rewrites it, and it is the only thing
the reaper is holding. The reserved `attempt` field on the envelope carries the
count forward across a requeue; it cannot carry the phase, because by the time
anyone wants to read the phase the entry is already stale by a whole job.

**"RPOP is atomic" is an argument about failing, not about requeueing.** The
reaper's existing safety note is correct and does not survive the change of
verb. Two gateway replicas each run a reaper; RPOP hands a given stranded entry
to exactly one of them, which is what makes "failed at most once" true. A
requeued job, though, goes back on the queue, is picked up by another worker,
and can be stranded a *second* time — a different entry, on a different
processing list, seen on a different pass, quite possibly by the other replica.
"Is this the first attempt?" is then a question about shared state, and the
natural implementation of it — read the count, compare, write count+1 — is a
lost update: both replicas read 0, both believe they are first, and one job
becomes two on one GPU pool. `HINCRBY` returns the value *after* incrementing,
so the decision is taken from the return of the atomic operation itself and
exactly one caller can ever see `1`. None of this can be exhibited by
`enterprise/test/run.sh`, which starts one gateway, so `scripts/lint.sh` pins
the shape instead: the counter must be bumped by `HINCRBY`, and must never be
written by `HSET`.

**A retry event that ends the stream is worse than no retry at all.** The
gateway's WebSocket stops reading at the first event in `TERMINAL_TYPES`. Add
`retry` to that set — or emit the retry as a `failed` followed by a fresh
`queued` — and the browser closes on the retry while the second attempt runs to
completion behind it. The user sees a job that failed; the cluster spent a GPU
finishing it successfully. `retry` is therefore deliberately *not* terminal, on
both sides: the gateway does not stop on it, and `index.html` reports it without
calling `done()` — the one arm of that switch which announces something has gone
wrong and deliberately keeps the socket open. The `TERMINAL_TYPES` line in
`hub.py` is pinned by lint too, because "add the new event type to the terminal
set" is exactly the tidying a later reader would do, in a diff about something
else.

Three smaller things that were not obvious until it was written:

- **A requeue is not a promotion.** Pushing the job to the front of the queue
  is the natural way to write it and re-introduces the starvation Q1 exists to
  remove, by a new door: a submitter whose workers keep dying takes slots from
  lanes that did nothing wrong. It goes back through the same fair-queueing
  insert as a first submission, and it is subject to the same
  `MAX_QUEUE_DEPTH` — a pool dying faster than it drains must not be the one
  path allowed to grow the queue past its ceiling.
- **Re-arming a TTL is how a job becomes immortal.** `HSET` and `HINCRBY`
  recreate a key that expired mid-flight, and a recreated key has no expiry at
  all, in a Redis deliberately configured `noeviction`. The retry path
  therefore uses `EXPIRE ... NX`: give the job an expiry if it has none, never
  extend one it has. The retry cap bounds this too, but the cap is a number
  someone may raise and the `NX` is a property of the code.
- **The gateway's blindness is not the operator's.** The reaper genuinely
  cannot tell a host-RAM OOM from a reclaim. F1 made the pod tell the
  difference — sized Guaranteed and within what the node can hand one pod, it
  now terminates `OOMKilled` instead of vanishing into node pressure — so every
  failure the reaper emits points at `oc describe pod`. The ambiguity is total
  at the queue layer and nowhere else, and a failure message that says only
  "the worker died" spends that fact.

### Q2, corrected — a rule you state and then round off is not a rule

Q2 shipped with the paragraph above written correctly and the code a step
behind it, in two places. Both are worth recording, because both are the same
mistake: the mechanism was reasoned about at the level of the sentence and
implemented at the level of the call.

**"Before the transition" meant before the POST, and the code wrote it after
`submit_prompt()` returned.** ComfyUI has the workflow the moment the request
is written to the socket, not when it answers — so an agent that records
`executing` on the *return* has left the whole round trip inside the retryable
phase. That is not a theoretical sliver: it is as long as a loaded ComfyUI
takes to answer a POST, it widens exactly when the cluster is unhealthy, and a
worker that dies inside it hands a workflow ComfyUI is already running to a
second GPU. The window was known and written down when Q2 landed, which is the
part worth being uncomfortable about — a disclosed window is still an open one,
and "small" is not a property the reaper can read. The fix is ordering rather
than narrowing: the `HSET` moves above the call. The cost is that a death
inside the POST no longer earns its retry, which is the direction to err — one
user resubmits, versus a poison workflow walking the pool.

**A cancelled job is at a retryable phase precisely because it was cancelled
early.** The reaper read the breadcrumb and nothing else, so a job the user
cancelled while it was queued or dispatched, whose worker then died, was
requeued with its status reset to `queued` — the retry mechanism spending a GPU
on work whose owner had already withdrawn it, and telling them it was queued
again. The reaper now reads `cancel_requested` alongside the phase and ends
such a job `cancelled`: terminal, payload reclaimed, no retry spent.

That second one had a pre-existing half, which is now closed too. `run_job()`
did not consult the cancel flag until *after* `submit_prompt()`, so a job
cancelled while it sat in the queue was submitted to ComfyUI by the next
healthy worker to pop it and then interrupted — which is not what
`cancel()`'s own docstring promises ("a job that has not been picked up yet
never starts"), and is GPU time spent on a job nobody wanted. One check
immediately before the POST closes it. The two checks are not redundant: this
one covers the ordinary path, the reaper's covers the death path, and either
alone leaves a door.

Both are asserted end to end by `check-35-retry-doors.py`, against the stub's
record of every workflow it has actually been handed — because "did ComfyUI
get it" is the question the whole mechanism turns on, and until the stub
recorded it, the suite could only infer the answer from timing. The same
record turned `check-30-sigkill.py`'s pre-execution scenario from an
assumption into an assertion, and showed that its kill point had been on the
wrong side of the line all along: it killed the agent inside `submit_prompt()`
and asserted a retry, which is the replay this mechanism exists to prevent.
It now kills the agent where a pre-execution death actually lives — parked in
the ComfyUI WebSocket connect, before anything has been sent — and asserts
ComfyUI never saw the workflow.

### Q3 — the username becomes a path, and the part that cannot be tested here

The one-line version of per-user output workspaces is
`OUTPUT_ROOT / user / filename`, and it is a path-traversal bug written in a
single expression. `X-Forwarded-User` is a request header, client-supplied
whenever `AUTH_MODE=none`, and `hub.py` says so in three places — this item is
where that sentence stops being a caveat about attribution and becomes the
security property of a filesystem write. Everything below follows from taking
that seriously.

**Sanitize, then join, then resolve, then verify — in that order.** Each step
covers a different failure, and reordering them silently removes one. Resolving
before the join tells you about a path nobody is going to use. Joining without
re-verifying containment trusts the sanitizer absolutely, and the sanitizer is
a string rule that cannot see a symlink someone left in the output volume. The
`resolve()`-then-`is_relative_to()` pair is the same shape `hub.py`'s
`output_file()` already uses on the way *out*, and the two are deliberately
independent: one refuses to build an escaping path, the other refuses to serve
one.

**The sanitizer's real risk is the opposite of the obvious one.** A pure
allowlist — strip everything outside `[A-Za-z0-9]` — is safe and quietly
wrong: `a/b` and `a-b` become the same directory, and so do two long usernames
that share a prefix. That is the flat shared directory again, for two
unlucky people, and now much harder to see. A pure hash is injective and
unusable: an operator looking at EFS, or at a ticket, cannot tell whose
directory anything is. So it is both — an allowlist slug for the human and 12
hex of `sha256(user)` for uniqueness. Rejecting unusual usernames was the
third option and is the wrong one *for the username*: an oauth-proxy identity
is an email, `@` and `.` are the ordinary case rather than the hostile one,
and a user whose IdP spells their name unusually cannot fix that. Rejection is
used one layer down, for a workflow's `filename_prefix` — ComfyUI treats it as
a subpath of the output directory, the caller wrote it, `..` in it has no
legitimate reading, and the caller can change it.

**Rewriting the prefix is necessary and is not sufficient.** ComfyUI is one
long-lived process started with one fixed `--output-directory`, so there is no
per-job flag to set; the only per-job control is the save node's
`filename_prefix`, which ComfyUI treats as a subpath. Rewriting it is what
makes the common case free — the file is written in the right place, nothing
is copied. But it is best-effort by construction: a custom node that hardcodes
its own path, or spells the input differently, ignores it entirely. Since the
agent is the only way out of the pod, the agent enforces the same confinement
again on what actually came out, moving anything reported outside the
workspace into it and refusing to name anything that resolves outside
`OUTPUT_ROOT` at all. "Every output of this job is inside its submitter's
workspace" is then a property of the agent rather than a hope about nodes.

**Workspaces are not access control, and saying so is the decision.** The
tempting next step is to scope reads by the same header. That produces a
control that works under `AUTH_MODE=oauth` and evaporates under
`AUTH_MODE=none`, where anyone can set the header — an isolation guarantee
that exists exactly where it is not needed. A guarantee that quietly degrades
is worse than a documented absence, because people rely on it. Reads are
therefore unchanged, and `docs/06-enterprise-architecture.md` now says so in
the "deliberately not here" list rather than leaving it to be inferred.

**And the half a laptop cannot prove.** The workspaces are created at runtime
by whichever worker pod happens to run a job first, and OpenShift gives each
pod an arbitrary high UID that is not stable across pods. So the mode matters
as much as the path: without `g+w` the *next* pod cannot write into a
directory this one created, and without `setgid` the files ComfyUI creates
inside it belong to the creating pod's group instead of GID 0. Neither shows
up in `make test`, which runs one agent as one UID on one local filesystem
where the process that made the directory owns it. The code therefore sets the
mode **explicitly** — `mkdir`'s own mode argument is masked by umask and would
produce `0755` — rather than inheriting a default that happens to work here,
and `scripts/lint.sh` pins both the constant and the `chmod` call, because a
default that happens to work locally is precisely the kind of thing a later
diff tidies away. The behaviour itself is on the cluster-day list in
`docs/10-roadmap.md`: two pods, two UIDs, one EFS volume.

---

## What was right and worth keeping

- Hub and spoke with a queue between them. Correct, and the reasoning given for
  it is the reasoning that matters.
- Workers on loopback with no ingress. The strongest control in the design.
- EFS as a single source of truth for models. Right call, and the "7 GB
  downloaded once" argument is the real payoff.
- oauth-proxy for SSO rather than an application-level user system.
- Scaling on queue depth rather than CPU. CPU is meaningless for GPU work.
- A dedicated, tainted GPU machine pool separate from the base pool.

The gap between this document and a working system was never the architecture.
It was that the manifests and the Python had not been run.
