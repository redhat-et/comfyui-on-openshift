# Engineering handoff

You are taking this over. This document is the thing the previous owner would
tell you across a desk in an hour: what you now own, why it is shaped the way
it is, which parts are load-bearing, what will break, and what you should
change first.

It repeats some of `README.md` on purpose. The README argues the case to
someone deciding whether to adopt this. This argues it to the person who now
has to keep it running, which is a different set of facts.

One of those facts runs underneath all the others, and it is worth holding on
to while you read the rest: **your on-call surface is a FastAPI process, a
Python agent, one Redis, and a bill.** Not a control plane, not a CUDA driver
build, not an ingress controller, not an identity provider, not a certificate
rotation. Section 1 draws that line row by row, section 3 is what is left on
your side of it, section 5 is the bill, section 10 sorts everything on your
side into what you may move and what you may not, and section 13 comes back to
why the line is where it is. If you have operated GPU inference on raw EC2 or
bare Kubernetes, you already know what is on the other side of that line and
what it costs to carry. You are about to stop carrying it. That is the reason
this runs where it runs, and most of the design follows from it rather than
from anything in the application.

---

## 0. Where this stands

This is a working system rather than a proof of concept, and it is also not a
product. Both configurations run end to end: the single-user path puts one
ComfyUI pod on one GPU behind an authenticated port-forward, and the
multi-user path puts a queue, cluster SSO and a GPU pool that scales to zero
in front of the same cluster.

And this has actually been run. Both configurations have been stood up on a
real cluster, on real GPUs, driving real ComfyUI workflows end to end — not a
stub, not a laptop. That deserves a sentence of its own because it is not
recoverable from the code or from the git history, and *"has this ever
actually run?"* is one of the first questions a new owner asks and one of the
hardest to answer from a repository. What that sentence does not cover is
[`10-roadmap.md`](10-roadmap.md)'s cluster-day list: the specific checks that
still need hardware, Q3's arbitrary-UID half chief among them, which one agent
running as one UID on one filesystem cannot reproduce at all. Those are owed,
not done.

The parts that are easy to get wrong and miserable to debug — progress
streaming, cancellation, worker death, path
handling — are covered by an end-to-end suite that runs against a real Redis
and a stub ComfyUI on your laptop in about a minute, and by four CI jobs on
every pull request. What has *not* happened is scale: nobody has run this past
a handful of concurrent users or more than three GPU workers, and the numbers
in [`02-cost.md`](02-cost.md) are on-demand list prices from August 2026 rather
than a bill you have actually received. Treat the architecture as settled and
the operating envelope as unmeasured.

The history matters more than it usually would, so it is worth two sentences.
This repository began as a design document, and roughly half of that
document's code would not have run — a dead import, a hub that was never in
its own image, a WebSocket connected after the prompt was submitted — while
the manifests quietly failed to implement several things the prose claimed,
including the scale-to-zero it led with. [`07-design-review.md`](07-design-review.md)
is the written record of every one of those, and it is the single most useful
hour you can spend here. Read it early, because it tells you the specific
failure mode this repository is built to defend against: infrastructure code
that reads plausibly and has never been executed. Almost every defensive-looking
thing in [`../enterprise/worker/worker_agent.py`](../enterprise/worker/worker_agent.py)
and [`../enterprise/gateway/hub.py`](../enterprise/gateway/hub.py) — the bounded
`recv`, the `prompt_id` filter, the SIGTERM drain, the heartbeat — is there
because its absence produced a real, intermittent, hard-to-reproduce bug. None
of it is defensive programming for its own sake, and the numbered comment
blocks at the top of both files tell you which bug each one answers.

Be confident about three things and nervous about three others. Confident:
the security posture is architectural rather than configured, so it does not
degrade when someone edits a YAML file — the GPU pods bind loopback and have
no Service and no Route, and there is no supported path by which raw ComfyUI
becomes reachable ([`04-exposing.md`](04-exposing.md)). Confident: the cost
controls are real and are one command each, and `make status` will tell you
your live burn rather than making you model it. Confident: the code paths
under test are genuinely under test — break one and `make test` fails in under
a minute. Nervous: the cold start is a real user-facing weakness and the first
thing a designer will complain about, which section 8 addresses and
[`10-roadmap.md`](10-roadmap.md) schedules. Nervous: Redis is a single instance
with AOF persistence, which survives a pod restart and not a zone outage.
Nervous: a job that dies *mid-render* is failed, not requeued — deliberately,
for a reason given in [`06-enterprise-architecture.md`](06-enterprise-architecture.md)
that a node reclaim does not change, so a reclaim mid-render still costs a user
their generation and you will hear about it before you hear about anything else
on this list. (Only the narrow case is retried: a worker that died before
ComfyUI was ever handed the workflow, where there is nothing to replay. Section
3 has the row.)

---

## 1. What you own, and what you do not

You own **one namespace**. You do not own a control plane, a CUDA driver
build, an ingress controller, an identity provider, or a certificate rotation.

That division is the whole reason this runs on OpenShift, and it is worth
internalising before you touch anything, because it tells you where to look
when something breaks:

| Thing | Who fixes it |
|---|---|
| API server, etcd, scheduler, control-plane upgrades | Red Hat SRE (ROSA HCP) |
| NVIDIA driver compiled against the RHCOS kernel | NVIDIA GPU Operator |
| Node provisioning, reclaim, replacement | Machine pool autoscaler |
| TLS on the Route, SSO, session cookies | OpenShift + `oauth-proxy` |
| The queue, the workers, the images, the money | **You** |

The practical consequence: your on-call surface is a FastAPI process, a Python
agent, a Redis, and a bill. Everything underneath is somebody else's pager. If
you have previously operated GPU inference on raw EC2 or bare Kubernetes, that
row-by-row comparison is the case for this platform, and it is most of why
this repo is only ~9,700 lines of Python instead of a distributed system.

**Inventory of what is actually yours:**

```
scripts/            13 numbered pipeline scripts, idempotent, bash 3.2-safe
manifests/base/     single-user Deployment + Service (kustomize)
enterprise/         the multi-user configuration
  gateway/hub.py    2,370 lines — the entire public attack surface
  worker/worker_agent.py  1,891 lines — the only path in or out of a GPU pod
  manifests/        Redis, gateway, worker, KEDA, oauth-proxy, Routes
  test/             e2e suite: real Redis, stub ComfyUI, no cluster needed
app/Containerfile   the single-user ComfyUI image
docs/               01-08, plus this file
.env                24 variables; the only configuration surface
```

---

## 2. The mental model, in five minutes

**Single-user configuration.** One pod, one GPU, one PVC, reached by
`oc port-forward`. There is no Route. That is deliberate: ComfyUI has no
authentication and its custom-node system executes arbitrary Python by design,
so an exposed ComfyUI is an unauthenticated RCE endpoint on a node holding
cloud credentials. Read `docs/04-exposing.md` before you expose anything.

**Multi-user configuration.** Hub and spoke, with Redis as the *entire*
interface between the browser and the card:

```
Browser → Route (SSO) → Gateway (CPU, ×2) → Redis → Worker (GPU, loopback) → EFS
                              ↑                ↓                              │
                              └──── XREAD ─────┴──── XADD (progress) ─────────┘
                                       gateway reads EFS read-only for images
```

Three properties follow from that shape, and every one of them is something
you would otherwise have had to build:

- **The workers are unaddressable.** ComfyUI binds `127.0.0.1`; the worker pod
  has no Service and no Route. The agent is the only way in or out.
- **Workers are disposable.** Nothing holds a session to one, so a card can
  appear for one job and be reclaimed ten minutes later with no connection
  state to repair. *This is what makes scale-to-zero possible at all* — it is
  not a bonus, it is the reason the shape is the shape.
- **The gateway's attack surface is a JSON parser.** It accepts one workflow
  object, pushes it onto a list, tails a stream, and serves files from a
  read-only mount. It never opens a connection to a worker.

**Scale-to-zero is two independent layers, and only one saves money.** KEDA
sets worker *pod* replicas from `LLEN comfy:queue`; the ROSA machine pool
autoscaler removes the *node* once nothing is scheduled. An idle GPU node bills
identically whether a pod sits on it or not, so if you only ever look at the
ScaledObject you will not understand the bill. `enterprise/manifests/03-autoscale.yaml`
says this in a comment; believe it.

---

## 3. Load-bearing invariants — break these and something silently breaks

These are the lines a well-meaning change will remove. Each one is here because
removing it produces a bug that is intermittent, timing-dependent, and
miserable to reproduce.

| Invariant | Where | What happens if you break it |
|---|---|---|
| ComfyUI binds `127.0.0.1` in the worker | `enterprise/worker/start.sh` | You have published unauthenticated arbitrary code execution on a node with an instance role and a writable model volume. This is the security model, not a network preference. |
| Workers have no Service and no Route | `enterprise/manifests/02-worker.yaml` | Same as above, by a different door. |
| The gateway is rebound to loopback under `AUTH_MODE=oauth` | `05-oauth-proxy-patch.yaml` | Anything in the cluster reaches the gateway without logging in. The proxy stops being a control. |
| The Service exposes only the proxy port | `05-oauth-proxy.yaml` | The gateway's own port is **8000**, not 8188 — leave 8000 on the Service list and anything in the cluster reaches the gateway without logging in, whatever the pod binds. (8188 is ComfyUI's port and never appears on this Service.) |
| Connect the ComfyUI WebSocket **before** submitting the prompt | `worker_agent.py`, note 1 | Every event in the gap is lost. Short jobs show no progress; long ones start at 30%. |
| Filter every event by `prompt_id` | `worker_agent.py`, note 2 | One ComfyUI multiplexes all prompts onto one socket. Another job's terminal event ends yours and reports success on work still running. |
| Bounded `recv()` timeout and a job deadline | `worker_agent.py`, note 3 | A wedged ComfyUI parks the agent forever. The pod stays `Running` and `Ready`, stops consuming the queue, reports nothing — and KEDA, seeing a growing queue, adds *more* workers beside the dead one. |
| SIGTERM trap that drains the running job | `worker_agent.py`, note 4 / `start.sh` | The pool scales to zero, so termination is routine. Without the trap, every scale-down discards whatever was rendering. |
| `BLMOVE` into a per-worker processing list + TTL'd heartbeat | `worker_agent.py`, note 5 | SIGKILL (OOM, node reclaim) strands the job with no terminal event. The gateway's reaper depends on the heartbeat lapsing. |
| The heartbeat key and the processing list are named from a per-process **incarnation** id — `HOSTNAME` plus a boot nonce — and never from `HOSTNAME` alone | `worker_agent.py`, note 9 / `BEGIN WORKER IDENTITY` / `scripts/lint.sh` | The row above is a claim of the form "the process holding *this* list is still alive", and the reaper tests it by pairing the two keys **by name** — so whatever that name identifies is what the reaper's word "alive" means. `HOSTNAME` identifies the *pod*, and `restartPolicy: Always` restarts a container **inside** its pod: an OOM-killed worker comes back holding the identity it died with, its first heartbeat answers the reaper's liveness question on the dead incarnation's behalf, and the reaper's `continue` skips that incarnation's processing list — not for a while, but for as long as the pod keeps restarting. Every consequence the reaper exists to prevent then happens at once to that job: no terminal event (the browser's bar never moves), its GPU seconds reach neither the submitter's line nor the excluded one, and its processing entry sits with no TTL in a `noeviction` Redis. This is the one row here whose failure is invisible to the *rest* of the suite by construction — `check-30-sigkill.py` proves the reaper works, and proves it with a replacement worker under a **different** name, which is the only case that was ever exercised; `check-32-worker-restart.py` is the same death with the name reused. Note the shape this is deliberately *not*: `WORKER_ID` stays the bare pod name, because that is the string a failure message shows and the one `oc describe pod` takes. Two identities, one displayed and one keyed, rather than one id doing both jobs badly. |
| The `phase` breadcrumb is written **before** the transition it describes — for `executing`, before the POST is sent and not after it returns — the reaper checks `cancel_requested` **before** requeueing, the retry counter moves only by `HINCRBY`, and `retry` is not in `TERMINAL_TYPES` | `worker_agent.py`, note 6 / `hub.py`, the reaper | Four ways to break one mechanism. A breadcrumb written after the fact leaves a window in which the job is executing and the record says it is not — which is exactly when the reaper replays a workflow that already killed one worker onto the next one. ComfyUI has the workflow from the moment the POST is *written*, so "after `submit_prompt()` returns" is after the fact: the round trip is the window, it is as long as a loaded ComfyUI takes to answer, and `check-35-retry-doors.py` kills a worker inside it. Erring early costs a death inside the POST its retry; erring late costs a poison workflow walking the pool. A cancelled job is at a retryable phase *because* the cancel stopped it early, so a reaper that does not look at the flag requeues precisely the work the user withdrew, resets its status to `queued`, and hands it to a second worker. A counter read-then-written is a lost update between the two gateway replicas' reapers: both read 0, both believe they are the first attempt, one job becomes two on one GPU pool (only one reaper ever holding a given entry bounds failing a job once and does **not** bound requeueing it once). And `retry` joining the terminal set closes every tailing browser on the retry, so the second attempt runs to completion and the user is looking at a failure. |
| The worker's heartbeat is refreshed by a **thread that runs for the whole process**, not from inside its loops — and a job carries an **owner** the reaper stamps over before it touches a stranded entry, which the worker re-reads before the `/prompt` POST and inside `finish()` | `worker_agent.py`, note 10 / `start_heartbeat()` / `still_ours()`, `hub.py`'s `reap_stranded_job` | Two halves of one failure, and it needs nothing to die. The reaper's entire liveness test is whether one key exists, so a heartbeat that merely LAPSED is a death — and `run_job()`'s prologue blocks in three places outside both loops: `ensure_workspace()` is an `mkdir` on the shared RWX volume and is unbounded, `ws.connect()` and `submit_prompt()` are 30 seconds each. Refresh only from the loops and a slow worker is declared dead at `PHASE_DISPATCHED`, which is retryable *by construction* — that phase means "ComfyUI has not seen it" — so its job is requeued while it is alive and about to submit. Both attempts then run: one workflow handed to ComfyUI twice at GPU prices, a second `started`/`completed` on a stream the browser closed at the first terminal event, and one `job_id` billed twice against the row above's own claim that a job is billed at most once. It is the exact replay the narrow retry exists to prevent, arriving through a door the phase breadcrumb does not cover. The owner is the half that survives being wrong about the first: a keepalive shrinks the window and cannot close it (a refresh that cannot run — kernel stop, Redis unreachable past the TTL — still looks like a death), so a reaped attempt must be able to find out it was reaped and abandon rather than write a second set of outcomes. Absence of an owner is NOT a fence: an unowned job proceeds, because a missing field that suppressed a terminal event would strand exactly the jobs this mechanism exists to stop stranding. `enterprise/test/check-36-live-worker-fencing.py` kills nothing and fails on both halves separately; the fence's residual check-then-act window, and why closing it is a redesign rather than an edit, is written down in `docs/10-roadmap.md` under F4. |
| A stranded entry is removed from its processing list **only after its reap has returned**, a reap that raised is retried on a later tick, and an entry that can never be reaped is **bounded and set aside**, never dropped | `hub.py`, `BEGIN REAP DURABILITY` / `reap_processing_list()` / `check-37-reap-durability.py` | The reaper is the only code that ever writes a terminal event for a job whose worker died, so a reap that dies halfway is the job's last chance. The loop used to `RPOP`: the entry came off the list *before* the reap ran, so anything that raised in the body — the terminal `XADD`, a Redis that went away between two commands, a bug — destroyed the only record that the job had ever been queued, and the `except Exception: pass` wrapped around it promised a retry that had nothing left to retry. The job then reaches no terminal state at all and the browser sits on a bar that never moves: the same work-vanishing the `noeviction` row below exists to prevent, through a door `noeviction` does not cover, reproducible with one injected transient error. Reading instead of popping costs the exclusion `RPOP` gave for free — two reapers on one tail would both fail the same job, and on a retryable one both would reach the `HINCRBY` claim, where one requeues and the other terminates what it just requeued — so the exclusion is now an explicit per-entry `SET NX` claim held for the whole reap. The trade is at-least-once for at-most-once: a gateway dying between a reap and its `LREM` costs a duplicate terminal event on a stream the browser stopped reading at the first one, where the old shape's equivalent window cost the job. And the bound is not optional in either direction: an entry left until its reap works is retried forever if it never can, with everything behind it stuck, while an entry *dropped* on its first failure is the original bug with extra steps. |
| Redis Streams, not pub/sub | `hub.py` | Pub/sub delivers only to subscribers connected at publish time. The POST and the WebSocket open are two round trips — the gap is the common case. |
| `maxmemory-policy noeviction` + `MAX_QUEUE_DEPTH` | `00-redis.yaml`, `hub.py` | The default evicts queued jobs, which presents as work vanishing at random. |
| The fair-queueing insert **adds one entry with `LINSERT` and never rewrites the list**, and the entry it adds **carries no workflow** | `hub.py`, `FAIR_ENQUEUE_LUA` / `fair_enqueue_call()` / `scripts/lint.sh` | Two ways to break one script, and it runs on every `/api/generate`. Redis executes one command at a time, so whatever this script does, every other client waits for it — including every worker parked in `BLMOVE`, which is to say the pool stops being handed work. Placing a job fairly means reading every job already queued, so what each entry costs to read is what a submit costs everybody: with 26 KB workflows in the list, one submit against a 499-deep queue measured ~118 ms of exclusive Redis time, and ~1.2 s with 103 KB ones. The workflow therefore lives at `comfy:job:<id>:payload` and the list carries a few hundred bytes of ordering record, which puts both cases at ~2 ms and makes the cost independent of a size the *client* chooses (up to `MAX_BODY_BYTES`). Separately: Redis does not roll back a partial script, so a version that `DEL`s the queue and pushes it back has a window in which any error loses **every queued job at once** — that is the same "work vanishing at random" the row above exists to prevent, arriving by a door `noeviction` does not cover, and it is not hypothetical: an error injected after the `DEL` empties a 10-deep queue. `LINSERT` splices against a pivot and touches nothing else, so there is no such window and `LLEN` is monotonic across a submit (which is also what the KEDA trigger reads). Both halves are invisible to `make test`, which runs a three-deep queue of two-node workflows where every version of this script is instant and correct. |
| `haproxy.router.openshift.io/timeout: 4h` **and `timeout-tunnel`** on every Route | `enterprise/manifests/` | HAProxy's 30-second default kills long generations mid-render and reads exactly like an application bug. On edge and reencrypt Routes only `timeout-tunnel` governs the upgraded WebSocket, against a one-hour router default — setting `timeout` alone still drops long jobs. Both annotations, or neither works. |
| The `chgrp 0` / `chmod g=u` block in both Containerfiles | `app/Containerfile`, `enterprise/worker/Containerfile` | OpenShift runs the container as an arbitrary high UID with GID 0. Without it, ComfyUI cannot write `temp/`, `input/`, `user/` and the pod crash-loops. This is the single most common OpenShift containerisation failure. |
| GPU pods sized to fit the smallest supported instance, requests equal to limits | `enterprise/manifests/02-worker.yaml`, `manifests/base/deployment.yaml` | The 16 GiB of *host* RAM on a `g6.xlarge` is not the card's 24 GB of VRAM. A memory limit above what one pod can hold on that node is unreachable, so the real ceiling becomes node pressure: an eviction, or a kernel OOM kill of ComfyUI, instead of a clean container-level `OOMKilled`. Unequal requests and limits make the pod Burstable, which is evicted before Guaranteed pods and carries a far more attractive `oom_score_adj` — on a pod holding a GPU mid-generation. |
| A per-submitter output workspace is **sanitized, then joined, then resolved, then verified inside `OUTPUT_ROOT`**, and created with an explicit group-writable setgid mode | `worker_agent.py`, note 7 / `scripts/lint.sh` | Two ways to break one mechanism. The header the workspace is named from is client-supplied under `AUTH_MODE=none`, so a join whose containment is never re-checked — or a `resolve()` done *before* the join, which proves nothing about the joined path — is an arbitrary filesystem write from an unauthenticated request. Separately, the mode is not cosmetic: OpenShift gives each pod an arbitrary high UID that is not stable across pods, so a directory created at runtime by one worker is unwritable by the next one without `g+w`, and the files ComfyUI writes inside it do not get GID 0 without `setgid`. `mkdir`'s own mode argument is masked by umask and yields `0755`, so this must be an explicit `chmod` — and it is invisible to `make test`, which runs one agent as one UID on a local filesystem where the creator owns everything. |
| The showback accumulator is **one Hash per period, whose expiry every accrual re-arms, with a capped field count** | `hub.py` / `worker_agent.py`, `BEGIN SHARED SHOWBACK` / `scripts/lint.sh` | Three ways to break one mechanism, and all three end at the `noeviction` row above. The identity every GPU-second total is named from is the `X-Forwarded-User` header, which is entirely client-supplied under `AUTH_MODE=none` — so an accumulator that takes a new *Redis key* per submitter (or per job) lets an unauthenticated caller fill a 512 MB `noeviction` instance by varying one header, which presents as queued work vanishing at random. One Hash per UTC month, `HINCRBYFLOAT` per job, is what bounds it. The expiry is the second half: `HINCRBYFLOAT` recreates a key that expired mid-flight and a recreated key has **no TTL at all**, so arming the bucket once at creation is not the same as arming it, and `EXPIRE ... NX` on every accrual is what keeps a bucket's lifetime measured from its first write instead of pushed forward forever by a busy month. The cap is the third: without `HLEN` guarding a new field, the Hash grows one field per distinct header value and the problem has only moved down a level. `make test` can see none of this — it drives ten identities for one minute, where a TTL measured in months and a cap of a thousand look exactly like their own absence — so `scripts/lint.sh` pins the shape. Separately, the block is **mirrored verbatim** between the two files for the same reason the queue envelope is: GPU time is written from two terminal paths in two different images (the worker's `finish()`, and the reaper, which never calls it), and a gateway and a worker that disagree about the period string or the field prefix both keep running while the month's total quietly splits in two. |
| The GPU-second quota breaker is **not reachable from `readyz()`**, and **fails open** | `hub.py`, `BEGIN QUOTA BREAKER` / `scripts/lint.sh` | Two ways to turn a cost control into the outage it exists to prevent. `/readyz` is the gateway's readiness probe (`01-gateway.yaml`): a quota read inside it — or inside anything it calls, which is why the lint rule walks the call graph rather than grepping one function — takes the whole gateway out of its Service the moment ONE submitter crosses their ceiling. Every browser WebSocket reporting an in-flight job is dropped, nobody can submit, and the GPU pool keeps running and keeps costing money. The breaker has exactly one caller, `generate()`, where the worst it can do is refuse one submission with a 429 that says when the quota resets. The other half is the direction it fails: the field missing, the value corrupt, the env var garbled, or the read itself raising — every one of those lets the submission through, because a breaker that trips on an unreachable dependency halts a cluster you are already paying for while the spend it guards against is slow. That is only defensible if it is *loud*, so every fail-open prints a line naming what it could not read; a control that stops enforcing silently has quietly stopped existing. This is narrower than "Redis unreachable": `generate()`'s backpressure check (a plain `LLEN`, unprotected) runs before this one and is not softened the same way, so a Redis outage that takes the whole instance down surfaces as a 500 there rather than as a logged quota fail-open — the breaker's own fail-open fires on a read that fails on its own, not on the instance being fully down. `make test` sees the runtime half (`check-95-quota-breaker.py` asserts an over-quota submission produces zero writes to `comfy:queue`, and that `/readyz` stays healthy while that submitter is over); it cannot see the shape, because a health page that reads the quota is green on any gateway whose reader happens to be under the cap. |
| `STORAGE_MODE=rwx` for the multi-user configuration | `enterprise/setup.sh` refuses otherwise | The gateway serves images off the volume the workers write to, and they are on different nodes by construction. gp3 is `ReadWriteOnce`. |

The file-level half of this table is now mechanical. `make lint` fails on a
pod that requests a GPU without tolerating the GPU taint, a GPU pod that is
Burstable or asks for more memory or cpu than the smallest supported GPU
instance can give one pod, a Route missing
either timeout annotation, a Service that regained the gateway's own 8000
beside the proxy port, a Service or Route pointed at the workers, an oauth
patch that no longer rebinds the gateway to loopback, a Redis without
`noeviction`, a drain window shorter than `JOB_TIMEOUT`, a `start.sh` that
stopped defaulting ComfyUI to loopback, and either Containerfile losing its
`chgrp 0` / `chmod g=u` block. The e2e suite can see none of those — it runs
no cluster and reads no manifest — so if you are about to argue with a lint
failure naming one of them, read its row above first. The check exists
precisely because the edit looks harmless.

Six more shapes are pinned there for the opposite reason: they are in the
Python and the shell the suite *does* run, and the suite still cannot see
five of them. `make lint` fails on a retry counter written by anything other
than `HINCRBY` — whose failure mode needs the two gateway replicas
`01-gateway.yaml` runs and `enterprise/test/run.sh` starts one — and on a
`TERMINAL_TYPES` that gained a member, which `check-30-sigkill.py` would also
catch but which is pinned anyway, because "add the new event type to the
terminal set" is precisely the tidying that arrives in a diff about something
else. It also fails on an output workspace whose directory mode is no longer
group-writable and setgid, or that is no longer set by an explicit `chmod`,
and on a `start.sh` that hardcodes ComfyUI's `--output-directory` instead of
taking it from the same `OUTPUT_ROOT` the agent reads. Those three are
the arbitrary-UID row above: one agent, one UID, one local filesystem is a
configuration in which every one of them looks fine.

Two of them are the fair-queueing insert's row: `make lint` fails on a
`FAIR_ENQUEUE_LUA` that stopped splicing with `LINSERT` or that calls anything
destructive on the queue, and on a `fair_enqueue_call()` that stopped building
the list entry with `queue_record()` — i.e. that put the workflow back in the
list. Both are things a diff can restore while every test still passes and
every reading of the code still looks right, because the suite's queue is
three jobs deep and its workflows have two nodes. Their cost is a
hundred-millisecond stall on every client in the cluster, and a window in
which one error empties the whole queue.

Q4's showback accumulator added two more, and they are the ones a one-minute
run is least able to see: `make lint` fails on an accrual script that stopped
re-arming the bucket's `EXPIRE`, stopped capping new fields with `HLEN`,
stopped adding into a Hash field with `HINCRBYFLOAT`, or touched any Redis key
other than the two it is handed — and on a `showback_key()` that names the key
from anything except the period. It also diffs the `BEGIN SHARED SHOWBACK`
block between `hub.py` and `worker_agent.py`, the way it already diffs the
queue envelope. A TTL measured in months and a cap of a thousand identities
are both invisible to a suite that runs for a minute against ten of them,
which is exactly why the accumulator's row above is enforced here rather than
there.

Q5's quota breaker added the nineteenth, and it is the only row here whose
failure mode is caused by the safety feature itself: `make lint` fails if
anything reachable from `readyz()` — transitively, not just its own body —
mentions the quota, and if `quota_refusal()` is called from anywhere other
than `generate()`. It also fails if the breaker has been deleted outright,
because every other clause in that rule is an absence, and an absence is
trivially true of a file with nothing in it.

The worker-identity row added three more, and they are the odd ones out here:
unlike every rule above, the e2e suite *can* see this one —
`check-32-worker-restart.py` restarts a worker under its own name and asserts
the stranded job still reaches a terminal state. They are pinned anyway,
because the diff that undoes them does not look like a bug fix being reverted.
It looks like tidying: the nonce makes the Redis key names noisy, `HOSTNAME`
is already unique per pod, and `WORKER_ID` is sitting right there. So
`make lint` fails on a `WORKER_INCARNATION` that stopped carrying a
per-process nonce, and on a `WORKER_KEY` or `PROCESSING_KEY` named from
anything but that incarnation — the pair has to name the same thing, and that
thing has to be the process, or the reaper's one-line liveness test is not a
liveness test.

`docs/07-design-review.md` is the long form: every one of these traces back to
a specific bug in the design this was built from. Read it once, early. It is
the fastest way to understand why the code looks the way it does, and it is
the document that will stop you "simplifying" something back into a defect.

This table is not the whole of what is fixed here, and it is emphatically not
the whole of what is *decided* here. Section 10 sorts this table, the settled
decisions in `docs/10-roadmap.md`, and the deliberate omissions at the end of
`docs/06-enterprise-architecture.md` into one list of five bins, so that the
question "may I change this?" has an answer that does not depend on which of
three documents you happened to read.

---

## 4. Day one: prove it works before you spend anything

```bash
make test        # real gateway + real agent + real Redis + stub ComfyUI, ~1 min
make lint        # exactly what CI runs
```

No cluster, no GPU, no AWS account. The suite asserts the behaviours that are
easy to get wrong and impossible to notice in a demo: a late subscriber loses
nothing, a reconnect replays identically, a foreign prompt's terminal event
does not end your job, SIGTERM drains, a SIGKILLed worker's job fails loudly
naming the dead worker, failures carry ComfyUI's own message, and
`/outputs/../../etc/passwd` does not resolve.

That is your regression net. If you change `hub.py` or `worker_agent.py`, this
suite is what tells you whether you broke an invariant from section 3.

Two things about how it reports, because a regression net you cannot trust is
worse than none. A check fails the suite by exiting non-zero **or** by printing
a failed assertion — `run.sh` reads both, so a check that keeps printing its
`FAIL` lines while its exit status quietly stops reflecting them (an edit that
drops the `sys.exit(1)`, a failures list nothing reads any more) cannot leave
the suite, or CI, green over its own output. And when you add or change an
assertion, break the behaviour it is about and watch it fail before you believe
it: four assertions have shipped here unable to fail, each found only by
someone deliberately breaking the feature and noticing nothing went red.

Then, when you want a cluster:

```bash
make preflight   # read-only; catches the GPU quota, which has a multi-day lead time
make status      # what is running and the live hourly burn
make login       # how to reach the cluster
```

---

## 5. The money is part of the job

You have inherited a workload where the dominant cost is a card that is idle
most of the day. The platform gives you three states and one command each:

| State | $/hour | Back in | Command |
|---|---:|---|---|
| Running | ~2.04 | — | `make up` |
| GPU parked | ~1.06 | ~5 min | `make park` |
| Cluster gone | ~0.05 | ~15 min | `make down` |
| Everything gone, VPC and IAM too | ~0.00 | ~50 min | `make destroy` |

Parking removes the card. **Only `down` removes the bill** — parked still
carries $1.06/hour of control plane, base workers and NAT gateway, which is
$760/month of doing nothing. The reason HCP is the right choice here is
precisely that a ~15-minute rebuild makes `down` a default rather than a last
resort; on a 40-minute-rebuild cluster nobody tears down and everybody
overpays.

By habit: ~$1,490/month running 24/7, ~$800 parking nightly, **~$370 tearing
down nightly**, ~$85 for a day a week. The crontab lines are in
`docs/02-cost.md`. Two things to check on your first week:

1. `BUDGET_ALERT_EMAIL` is set to *you*, not to the person who left.
2. The teardown cron, if one exists, runs on an always-on box. A laptop asleep
   at 19:00 parks nothing.

**Before your first `make down`, know where the models live.** gp3 dies with
the cluster; EFS and the S3 sync path survive it. `docs/03-storage.md`. A
teardown that turns into a two-hour model re-download will stop you doing
teardowns, which costs far more than the storage did.

**The showback report does not survive a teardown either — capture it first.**
`GET /api/showback` (docs/10-roadmap.md, Q4) answers "who spent the card this
month" out of Redis, and Redis's PVC is gp3, so the *same* sentence applies:
gp3 dies with the cluster. On the nightly-`down` habit this section
recommends, last month's report is gone every morning, and the person who
finds out is whoever asks for it on the 1st. Two lines, in that order, in
whatever runs your teardown:

```bash
oc exec deploy/comfy-gateway -c gateway -- \
    curl -s localhost:8000/api/showback > "showback-$(date -u +%Y-%m).json"
make down
```

`make park` needs none of this — the cluster and the volume stay. The
`periods_available` field in the response is the honest answer to "what is
still in there", and after a teardown it is "this month, since the cluster
came back".

---

## 6. The failure modes you will actually meet

Ordered by likelihood. Full detail in `docs/05-troubleshooting.md`.

| Symptom | Cause | Action |
|---|---|---|
| `Multi-Attach error`, pod `Terminating` for hours | A dead pod never released the volume | `./scripts/08-unstick-storage.sh --repair`. **Never** `oc delete pod --force --grace-period=0` — it deletes the record while the container may still run, which strands the volume permanently and can schedule a second writer onto it. `docs/08-stuck-volumes.md`. |
| GPU pool stuck `Provisioning` | Quota (fixable, slow) or AWS capacity (fixable, fast) | `make preflight` distinguishes them. For capacity, change region or GPU family — `g5` and `g6` draw from separate pools. |
| `ClusterPolicy` never ready | Usually just slow | Under 20 minutes it is not stuck; it compiles a driver and pulls several GB. Past that, check NAT egress to `nvcr.io` and the NFD label. |
| Pod `CrashLoopBackOff`, `Permission denied` | Arbitrary UID | The fix is in the image, not the manifest. Bottom third of `app/Containerfile`. |
| Progress bar never moves, job is fine | An invariant from section 3 was broken | Reproduce in `make test` first — it is faster than a cluster and it is where the assertion already exists. |
| Queue grows, workers idle or absent | KEDA cannot reach Redis, or the pool cannot scale | `oc get scaledobject,hpa`; the scaler dials from `openshift-keda`, so the Redis address must be fully qualified. |
| Everything works, generation is slow | Model loading over EFS, or the wrong instance type | `docs/05-troubleshooting.md`; also idea 3 in the README. |

Set the alert before you need it: the gateway exports `comfy_queue_depth` and
`comfy_workers_registered`, `setup.sh` applies a ServiceMonitor, and *"queue
deeper than N for 30 minutes"* is the alert that catches a wedged pool before a
human does.

---

## 7. The knobs, and which ones matter

Everything is in `.env`. The ones that change behaviour rather than naming:

| Variable | Why you would touch it |
|---|---|
| `SCALE_TO_ZERO` | `false` pins one warm worker. See section 8 — this is the main UX/cost dial. |
| `MAX_GPU_WORKERS` | Ceiling for both the pod and node autoscalers. Your burst budget. |
| `STORAGE_MODE` | `rwx` is mandatory for multi-user and is what lets models outlive `make down`. |
| `COMFYUI_REF` | Pinned on purpose. Bumping it is a deliberate, reviewable act. |
| `ENABLE_MANAGER` | Leave `false` on a shared cluster. It gives every UI user arbitrary code execution on a node with cloud credentials, and anything it writes disappears when the node is reclaimed anyway. |
| `QUOTA_GPU_SECONDS` | Off by default. A per-user GPU-second ceiling per UTC month, computed from the showback accounting the cluster already keeps — the cheapest way to stop one person's batch script from being the whole month's bill. It fails open, so it is a guardrail and not a guarantee. |
| `AUTH_MODE` | `none` exists for a solo test cluster. Route hostnames appear in certificate transparency logs within minutes, so `none` means anyone who looks can spend your GPU budget. |
| `cooldownPeriod` (in `03-autoscale.yaml`) | 600s. The first number to tune if the economics feel wrong. |

---

## 8. The cold start, and the one configuration change that removes it

The honest weakness of scale-to-zero is the first job after an idle period:

| What is cold | First-job wait | Removed by |
|---|---:|---|
| No node — provision + ~10 GB image pull | 6–13 min | machine pool pinned at 1 during work hours |
| Node warm, no pod — CUDA init + checkpoint load | 1.5–4 min | a warm worker pod (`SCALE_TO_ZERO=false`) |
| Warm worker | seconds | — |

For batch and out-of-hours work this costs nothing — nobody is watching. For
**interactive iteration it lands in the middle of a creative loop**, and that
is the case you will hear about.

**One warm machine removes it entirely.** `SCALE_TO_ZERO=false` pins a single
worker; KEDA still scales 1..N above it, so bursts still work. The cost is one
GPU node for as long as it is up — **~$195/month pinned weekdays 9–6**, which
is less than one designer waiting fifteen minutes each morning.

And note the shape of the problem: with a warm worker held during working
hours, **the cold start happens at most once a day, before anyone sits down** —
not once per creative iteration. That is the configuration to recommend to a
design team, and it is one variable, not a redesign. Scale-to-zero still covers
the other fourteen hours and the weekend, which is where the savings actually
were.

The next refinement is to schedule the warm window rather than pin it — see
idea 1 in the README's "Ideas worth doing next", which is two cron lines.

---

## 9. What I would do next, if I were staying

The full list is in the README under **Ideas worth doing next**, and
[`10-roadmap.md`](10-roadmap.md) turns it into a work plan — what each item
touches, what proves it, what order they can safely land in, and which of them
need a real cluster. The short version, in the order I would take them —
shipped and decided-against items kept on the list rather than deleted, the
way the README's version keeps them, because the reasoning is the useful part:

1. **Schedule the warm window** (two cron lines). Removes the morning cold
   start without paying overnight. Highest payoff per unit of work on the list,
   and the only one of these five still entirely ahead of you.
   `10-roadmap.md` carries it as I1.
2. **Fair queueing — landed** (`10-roadmap.md`, Q1). The problem was that the
   pop is a single-list `BLMOVE` (`worker_agent.py:1787`), so one overnight
   batch of 200 jobs starved every interactive user. What shipped round-robins
   the *insert* across submitters rather than ranking them, which fixes the
   starvation without introducing a priority claim a caller could forge; the
   pop is still one `BLMOVE`, which is what the reaper depends on. It has a row
   in section 3, because placing a job fairly means reading the queue inside
   one Lua `EVAL` and that is time no other client gets.
3. **Retry-once-then-fail — landed narrow** (Q2); **spot — still open** (I7).
   Retry was absent because a workflow that OOM-killed one worker will kill the
   next. What shipped retries only deaths where ComfyUI never saw the workflow,
   which is the one case with no poison pill to replay; everything else is
   still failed, not requeued. Spot turned out not to depend on it — an
   interruption gives two minutes of notice and the SIGTERM drain finishes
   anything that fits in two minutes — so I7's real trade is losing longer
   generations, which is a product decision rather than something retry fixes.
4. **Per-user output workspaces — landed, laptop half** (Q3). The submitter's
   identity is stamped on job state and now threads through into the output
   path, which is what made showback (Q4) cheap. The arbitrary-UID half cannot
   be proven off a cluster and is on the cluster-day list.
5. **NVIDIA time-slicing — decided against** (I6). The argument for it is that
   an L4 running SD1.5-class workflows does not use 24 GB. The argument against
   it won: time-slicing gives no memory isolation, so co-resident peaks simply
   sum and the victim is whichever process allocates second rather than the
   greedy one, and MIG — which partitions memory properly — is not available on
   this card. Section 10, bin 3, has the rest of it.

`docs/06-enterprise-architecture.md` ends with the complementary list — what is
deliberately *not* here and the reasoning for each omission. Read it before you
add any of it, because in several cases the omission is the decision.

---

## 10. What you can change

This project is moving between engineering groups, and it will be lobbied in
several directions at once. The answer to "what is load-bearing and what is
negotiable" already exists, but it is spread across three documents — section 3
of this file, the settled decisions in [`10-roadmap.md`](10-roadmap.md), and the
deliberately-not-here list at the end of
[`06-enterprise-architecture.md`](06-enterprise-architecture.md) — so an
incoming group has to reconstruct it before it can argue with any of it. Here
it is in one place, in five bins. The bin matters more than the item, because
the bin says what kind of argument would move it.

### Bin 1 — Constraints, not preferences

Three lines, and they are one constraint by three doors:

- ComfyUI in the worker binds `127.0.0.1` — `enterprise/worker/start.sh`.
- The worker pods have no Service and no Route —
  `enterprise/manifests/02-worker.yaml`.
- Under `AUTH_MODE=oauth` the gateway rebinds to loopback and the Service
  exposes only the proxy port — `05-oauth-proxy-patch.yaml`,
  `05-oauth-proxy.yaml`.

ComfyUI has no authentication, and its custom-node system executes arbitrary
Python by design, on a node that holds an instance role, sits inside your VPC,
and has the shared model volume mounted writable. Change any of these three and
you do not have a different design with a different trade-off. You have an
unauthenticated remote-code-execution endpoint on a node holding cloud
credentials. There is no workload for which that is the better call, which is
why `make lint` fails on each of them rather than trusting a reviewer to spot
it in a diff about something else.

### Bin 2 — Fixed bugs; reversing these reintroduces them

Section 3's table, every row of it. Each row traces to a specific failure that
already happened once here — in the design this repository was built from, or
in the work that has landed since — and
[`07-design-review.md`](07-design-review.md) names it: the
WebSocket connected *after* the prompt was submitted, `prompt_id` fetched and
never used, `ws.recv()` with no timeout, no SIGTERM handling, pub/sub dropping
messages into an empty room, an image that would not have started on OpenShift
at all, a 30-second router default cutting every long generation at exactly the
same place. The later rows are the same process applied to the work in
[`10-roadmap.md`](10-roadmap.md) as it landed — the phase breadcrumb written
before the POST rather than after it, the per-process incarnation id, the reap
that reads before it removes, the insert that splices instead of rewriting the
list.

These look like ceremony, and that is precisely their failure mode: each is a
line whose purpose is invisible in the diff that deletes it, and each was
written by somebody who had just spent a day finding out what its absence does.
Removing one is not a simplification, it is a re-introduction, and the row says
which symptom you get back. Read the row before you argue with the lint rule —
the rule exists because the edit looks harmless.

### Bin 3 — Settled with reasoning; revisit only with new evidence

These are decisions, not constraints, and they are overturnable. What an
incoming group should know is *which argument it is answering*, because each
one has a written argument and overturning it without answering that argument
means running the same reasoning again at higher cost.

| Decision | The argument it makes | Written down in |
|---|---|---|
| Retry is narrow — only deaths before ComfyUI ever saw the workflow | A host-RAM OOM is indistinguishable *at the queue layer* from a node reclaim, so a general retry walks a workflow that killed one worker across the whole pool, in sequence, at GPU prices | `10-roadmap.md`, "Retry is narrow, not general"; `06`, "General job retry" |
| Fair queueing instead of priority lanes | A priority lane is a claim the caller makes, and `X-Forwarded-User` is client-supplied under `AUTH_MODE=none` — everyone declares themselves interactive and the starvation returns wearing a new name. Round-robin solves the stated problem with no trust decision at all | `10-roadmap.md`, "Priority becomes fair queueing"; `06`, "Job priority" |
| The quota breaker fails **open**, and is unreachable from `readyz()` | A breaker that trips on an unreachable dependency halts a cluster you are already paying for, while the spend it guards against is slow. Inside the readiness path it pulls the gateway out of its Service the moment one submitter crosses a ceiling | `10-roadmap.md`, "The cost breaker is a local quota"; section 3's row |
| No NVIDIA time-slicing | It gives no memory isolation, so co-resident workflows' peak VRAM simply sums and the victim is whichever process allocates second, not the greedy one. The density win is zero anyway: a 16 GiB node offers one pod ~10.5 GiB and one worker takes it | `10-roadmap.md`, "I6 — do not do this" |
| `ENABLE_MANAGER=false` in a shared pool | It hands every user with UI access code execution on a node with cloud credentials — and it is not even durable, because the pool scales to zero and anything it wrote goes with the node | `06`, "ComfyUI-Manager and auto-downloaders" |
| One Redis, AOF, no HA | At this scale, three Redis nodes protect against a failure less likely than the ones nobody has fixed yet | `06`, "Redis HA" |

Notice the last row, because it shows what "new evidence" means here. Its
argument is explicitly conditional on scale: it does not need a better idea to
fall over, it needs a different number of users. Several of the others are the
same shape. A group that arrives with a measurement is having a different
conversation from a group that arrives with a preference, and these rows are
written so you can tell which one you are in.

### Bin 4 — Genuinely open, and expected to change

Nothing below is settled, and treating it as settled is the opposite mistake
from reversing bin 2.

- **The scaling work.** Scheduling the warm window rather than pinning it, a
  low-priority placeholder pod holding a node warm, scaling on estimated wait
  rather than queue depth, `cooldownPeriod`, `MAX_GPU_WORKERS`.
  `10-roadmap.md` carries these as I1, I3 and I4 with efforts, risks and a
  landing order attached. None of them has landed. `cooldownPeriod: 600` is
  called out in `06` as "a starting point, not a truth".
- **The storage layout.** EFS is required by *this* shape — the gateway serves
  images off the volume the workers write to, and those pods are on different
  nodes by construction — but the shape itself is not required. Writing outputs
  to S3 and returning presigned URLs removes the `ReadWriteMany` requirement
  outright. Model staging on node-local NVMe is an unscoped spike rather than a
  work item (`03-storage.md`; `10-roadmap.md`'s I5).
- **Kueue instead of the hand-rolled queue.** The fair queueing here is correct
  and measured, and it is also a small scheduler written by hand. Kueue is the
  Kubernetes-native one, with quota-aware queueing and fair sharing across
  teams. The README puts the crossover at hundreds of GPUs; that number is a
  judgement, not a measurement, and it is the kind of judgement a new group is
  entitled to make differently.
- **GPU selection.** `g5` versus `g6`, L4 versus L40S versus the eight-card
  H100 nodes, on-demand versus spot. This is a VRAM-and-price question that
  moves with the workflows and with AWS's price list, and the worker sizing in
  `02-worker.yaml` is calibrated for a 4-vCPU 16 GiB machine, so it moves with
  the answer.
- **ROSA specifically, versus OpenShift generally.** Almost nothing here is
  ROSA — the Routes, `oauth-proxy`, KEDA, the GPU Operator and the
  arbitrary-UID posture are OpenShift. What is ROSA-specific is the machine-pool
  commands in `scripts/`, the ~15-minute rebuild that makes `make down` a habit
  rather than a last resort, and the HCP restriction that there is no MachineSet
  to edit — which is exactly what blocks the NVMe spike above.

Saying this out loud is the point of the section. A repository commented this
heavily reads as untouchable, and a group that treats all of it as load-bearing
will end up paying for a decision that nobody actually made.

### Bin 5 — Would be a different project

Not forbidden. Just a new thing rather than a continuation, and worth calling
by its real name at the start rather than discovering it halfway through.

- **Reachable workers.** A Service, a Route, `--listen 0.0.0.0`, ComfyUI's own
  UI put in front of a user. This is the property the rest is built on, and the
  only one that cannot be recovered after the fact.
- **Replacing ComfyUI.** A great deal here is shaped by ComfyUI specifically:
  one socket multiplexing every prompt, no authentication, arbitrary Python in
  custom nodes, one long-lived process with a single fixed
  `--output-directory`. A different engine invalidates the reasoning, not just
  the code.
- **Becoming a model server.** Batching requests onto one card, holding weights
  resident across tenants, an inference API with latency targets. That is a
  different product with different failure modes, and the queue in front of it
  would be the smallest part of the work.

### Why bins 1 and 2 can be trusted by people who did not build them

Those two bins ask the most of you: they ask you to accept a claim about a bug
you never saw, made by a group you are replacing. You do not have to accept it.
They are the two bins that are **checkable**, and checking is cheaper than
arguing:

- **The assertions have been mutation-tested, and some of them failed that
  test.** `make test` runs 289 assertions — 29 shell unit assertions and 260 in
  the end-to-end suite across 17 check files — and the count is the least
  interesting thing about it. Assertions here have been caught unable to fail,
  each found by someone deliberately breaking the feature and noticing that
  nothing went red; what replaced them is in the README under *"The assertions
  that could not fail"*, and section 4 above states the standing rule. A suite
  that has caught itself is a different kind of evidence from a suite with a
  large number attached.
- **The one performance claim was measured, not reasoned.**
  `enterprise/test/bench-fair-enqueue.py` is in the repository and you can run
  it yourself: one submit against a 499-deep queue of ~26 KB workflows cost
  117.7 ms of exclusive Redis time with the whole envelope in the list, and
  1.6 ms with an ordering record in the list and the workflow beside it. That
  number is why the fair-queueing row in section 3 reads the way it does.
- **Every claim about a bug names where to check it.** Section 3's rows cite
  the file and the numbered comment block — `worker_agent.py`, note 3;
  `BEGIN WORKER IDENTITY`; `FAIR_ENQUEUE_LUA` — and, where one exists, the
  check that proves it by name (`check-36-live-worker-fencing.py`,
  `check-37-reap-durability.py`). `07-design-review.md` shows the original code
  beside what replaced it. You can go and read the code a claim is about
  instead of the sentence making it.
- **Twelve shape rules in `scripts/lint.sh` pin section 3's file-level
  invariants mechanically** — the block is titled after that section. They are
  greps, deliberately: no parser and no dependency, because the two they were
  written for produce a crash-loop or an unauthenticated RCE rather than a test
  failure. Each carries in its own failure message the reason it exists and the
  section 3 row it pins.

So the honest instruction to an incoming group is: distrust the previous one
and check. Delete the `prompt_id` filter in `worker_agent.py`, or its SIGTERM
handler, or the workspace confinement, and run `make test`. Something goes red
in about a minute, and *which* assertion goes red tells you what that line was
actually holding. That is a faster way to find out what bins 1 and 2 are worth
than reading this document — which is the point. Neither bin is asking for your
good faith.

---

## 11. Reading order

1. `README.md` — the shape and the case for it.
2. **This file.**
3. `docs/07-design-review.md` — why the code looks like this. The single most
   useful hour you will spend.
4. `docs/06-enterprise-architecture.md` — hub and spoke, Streams over pub/sub,
   scale-to-zero economics, and the explicit non-goals.
5. `docs/02-cost.md` — because the bill is part of the design.
6. `docs/03-storage.md`, `docs/04-exposing.md` — the two decisions that are
   hard to reverse later.
7. `docs/10-roadmap.md` — where the work goes next, with lanes and gates.
8. `docs/05-troubleshooting.md`, `docs/08-stuck-volumes.md` — bookmark, do not
   read cover to cover.

Then run `make test`, read `hub.py` and `worker_agent.py` top to bottom (they
are ~4,260 lines together and both open with a numbered list of the things the
obvious implementation gets wrong), and you have the whole system.

---

## 12. Support and escalation

- **Cluster-level** (API server unreachable, node lifecycle, upgrades): Red Hat
  + AWS joint support on the ROSA support plan. Check the plan is still active
  and still billed to a live cost centre — it bills whether or not a cluster
  exists, and it is the easiest thing in the world to forget.
- **This repository**: GitHub issues; security findings via private
  vulnerability reporting (`SECURITY.md`). Note the scope line there — ComfyUI
  and its custom-node ecosystem are upstream, and this repo's mitigation for
  that entire class is architectural: keep ComfyUI unreachable and
  `ENABLE_MANAGER=false`.
- **CI**: four jobs on every PR (lint, e2e, real-ComfyUI path contract,
  arbitrary-UID image test). They delegate to the same `make` targets you run
  locally, so local and CI cannot drift. If CI is red, the answer is in
  `make lint` or `make test` on your own machine.

---

## 13. Last word

The temptation with a repository like this one is to simplify it. Much of what
is here looks like ceremony until you know what it is for: a bounded socket
timeout that appears to guard against nothing, a job parked in a second Redis
list rather than simply popped, a probe that shells into the container instead
of doing the obvious HTTP GET, an image that spends a layer on `chgrp 0` and
`chmod g=u`. Every one of those is load-bearing, section 3 says what breaks
when it goes, and [`07-design-review.md`](07-design-review.md) says which
specific bug put it there. If you find yourself removing something on the
grounds that it looks unnecessary, that is the moment to run `make test` first
and read the comment block second — the tests were written from the bugs, so
they will usually tell you before a cluster does.

Which returns this document to where it started. The platform is doing more
work here than the code is: the reason this repository is ~9,700 lines of
Python and not a distributed system is that cluster SSO, the driver lifecycle,
node autoscaling to zero, audit logging, arbitrary-UID isolation and TLS
rotation are not in it — they are underneath it, and they are somebody else's
pager. That is the whole argument for where this runs, and it is worth being
glad about rather than merely resigned to: a FastAPI process, a Python agent,
one Redis and a bill is a small enough surface that one person can hold all of
it in their head, which is not true of any version of this that owns its own
control plane. When you are deciding whether to add something, the first
question is therefore whether OpenShift already does it, because in this
problem domain it usually does, and the version you would write would be the
version nobody maintains.

The corollary is the one thing that would genuinely undermine the design, and
it is bin 1 of section 10 in a sentence: if you ever find yourself making a GPU
worker reachable — a Service, a Route, `--listen 0.0.0.0` — to solve a problem,
stop and solve it at the gateway instead. That single property is what the rest
of this is built on, and it is the only one that cannot be recovered after the
fact. Everything below that line is somebody else's pager. Everything above it
is now yours, and section 10 says which parts of it you are free to move.
