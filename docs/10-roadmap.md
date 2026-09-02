# Roadmap

The ideas the README used to list at its end, turned into a work plan: what
each one actually touches, what proves it, what order they can safely land in,
and which of them cannot be finished without spending money on a real cluster.
The README now keeps only a pointer here; "The list, as the README carried it"
below is the ledger of those twelve items and where each one went.

All four foundations have landed — F3, F1 and F2 in that order, then F4 after
the queue lane — and so has the whole queue lane: Q1, Q2, Q3, Q6, Q4 and Q5.
Of the infra and supply lanes, I1 has landed (the scheduled warm floor); I2,
I3, I4, I7, S1 and S2 have not, I5 is an unscoped spike, and I6 is decided
against. An audit sweep after the queue lane also landed a set of fixes that
were on no list — "Landed in the audit sweep" below records them so the
statuses here stay the whole of what changed. This document exists so that
the next person to pick up a line item does not have to re-derive its blast
radius, and so that several of them can be worked in parallel without three
people editing `hub.py` at once.

The first version of this file was written from the documentation rather than
from the source, and was wrong in seventeen places. This version was built from
a full read of the code; where it corrects the README's framing, it says so.

## What decides the order

This section is the plan as it was drawn, before the queue lane landed. It is
kept because it is the record of *why* the work landed in the order it did, and
the correction is written in line wherever a fact has since changed.

**Seven items contended on `hub.py`, five on `worker_agent.py`.** Not the same
five: Q5's entire contact with the queue path is one precondition in
`generate()`, and it never touches the worker. The collisions are also finer
than "the same file" — three regions are hot, `generate()`, the reaper pair,
and `gather_stats()`/`metrics()` — which is why worktrees do not help. The
queue work is a sequence. (What the six queue items actually touched, measured
from the commits: five of them edited `hub.py` — Q3 did not — and three edited
`worker_agent.py`.)

**The most contended file in the plan did not exist yet.** All six queue items
proposed creating `enterprise/test/check4.py`, and all six would have had to
edit `enterprise/test/run.sh`, which had **no check discovery**: `run.sh`
copied `*.py` into the work directory by glob but invoked checks by hardcoded
name, and threaded their exit codes by hand. A botched merge there **fails
open** — the new check is copied, never run, and the suite is green. That is
why F3 landed before any queue work. It now discovers every `check*.py`, and
every check file since is named under the convention F3 settled
(`enterprise/test/README.md`); `check4.py` was never written.

**There is a hard verification boundary.** `make test` and `make lint` prove the
queue, the gateway, path handling, signal handling and image UID hygiene with no
cluster, no GPU and no AWS account, in about a minute. KEDA actually scaling, a
machine pool provisioning a node, EFS, oauth-proxy and the GPU itself prove
nothing until there is a cluster at ~$2.04/hour. Only three of the seventeen
items are cluster-only for a failing assertion — I3, I4 and I7, whose "proven
by" column reads *cluster day* and nothing else. The other fourteen have a
laptop half.

**Twenty-six invariants are load-bearing** — `docs/09-engineering-handoff.md`
§3. (Fourteen when this was written; F1 added the fifteenth, Q2 the sixteenth,
Q3 the seventeenth, Q1 the eighteenth, Q4 the nineteenth and Q5 the twentieth,
the cross-wave sweep three more — the worker's per-process identity, F4's
keepalive-and-fence, and reap durability — and the audit sweep the last
three: the compare-and-set claim with the interrupt-and-drain, the
default-deny namespace with the worker's ACL user, and output and showback
scoping under `oauth`. That is what "changes an invariant" looks like in
practice.)
Touching an invariant is not a useful filter, because nearly every item does.
The filter that *is* useful: an item is high-risk if it **changes** an
invariant rather than merely working near one. Four have — F1, Q2, Q3 and F4,
and each of the four added a row to §3.

## Decisions already made

These were open questions. They are settled here so that nobody relitigates
them mid-implementation.

**Retry is narrow, not general.** Out-of-memory is common in ComfyUI workflows
and it arrives three different ways, only one of which is ambiguous. A VRAM
OOM is caught by ComfyUI, arrives as `execution_error`, and already becomes a
terminal `failed` carrying the real message — retrying it would burn a second
GPU-hour on a workflow that cannot fit. A host-RAM OOM kills the ComfyUI
process, takes the pod with it, and is **indistinguishable at the queue level**
from a node reclaim. So blanket "retry on reaper failure" retries the poison
pill by construction. Q2 therefore retries **only jobs that died before ComfyUI
ever saw the workflow**, and adds phase breadcrumbs so every other death is at
least diagnosable. The rest of the original non-goal in
`docs/06-enterprise-architecture.md` stands.

**Spot does not depend on retry.** The README implied it did. A spot
interruption gives two minutes of notice, the node is cordoned and drained, and
the existing SIGTERM drain finishes the job — for jobs that fit in two minutes.
I7's real trade is that longer generations are lost, which is a product
decision, not something retry fixes.

**Priority becomes fair queueing.** A priority lane has to be claimed by the
caller, and `hub.py` states that under `AUTH_MODE=none` the user header is
client-supplied and must never be treated as authorization — so everyone
declares themselves interactive and the starvation returns in a new form.
Round-robin across submitters solves the stated problem ("one person's batch of
200 starves everyone") with no trust decision at all, degrades to FIFO when
there is one submitter, and cannot be gamed, because claiming to be someone
else only shares your own slot.

**The cost breaker is a local quota, and it fails open.** Giving the
internet-facing gateway an AWS SDK and an IAM identity to enforce a budget puts
cloud credentials on the one pod `docs/09` calls the entire public attack
surface — and AWS Budgets lags real spend by hours anyway. A GPU-second quota
computed from the attribution Q4 already collects needs no credentials and caps
the thing you control. It fails **open**: a breaker that trips on an unreachable
dependency halts a cluster you are already paying for, while the risk it guards
against is slow. The budget alarm remains the backstop. It must not be wired
into `readyz()` — that drives the readiness probe, so tripping it would pull the
gateway out of service and kill the WebSockets reporting in-flight jobs.

**The assertion gate permits intentional replacement.** See Gates below.

## Foundations — these land first

Four items that are not in the README's list, that several later items
silently assume, and that are each worth doing on their own merits. F1, F2 and
F3 landed ahead of the queue lane; F4 is the exception — it was found *by* the
queue lane, in the door Q2's narrowing did not look at, and landed after it.

| ID | Change | Effort | Risk | Proven by |
|---|---|---|---|---|
| F1 | Worker resource sizing — **landed** | Small | **High** | A unit assertion that requests and limits are internally consistent and fit the target instance type — `scripts/unit-tests.sh` runs the real `scripts/lint.sh` against a fixture with the old shape |
| F2 | Versioned queue payload envelope — **landed** | Small | Low | `enterprise/test/check-40-envelope.py`: the reserved fields are on the wire with their defaults, an old-shape payload still completes, and an unknown field is not fatal — plus a lint check that the two copies of the envelope have not diverged |
| F3 | Test harness convention + manifest shape assertions — **landed** | Small | Low | A deliberately broken manifest fails `make lint`; a new check is discovered without editing two places |
| F4 | Heartbeat keepalive + job ownership fence — **landed** | Small | **High** | `enterprise/test/check-36-live-worker-fencing.py`, which kills nothing: a live worker parked past `HEARTBEAT_TTL` in `run_job()`'s prologue keeps its heartbeat armed and its job (zero writes to `comfy:queue`, one handoff to ComfyUI, one terminal event), and a live worker whose job IS requeued under it — its heartbeat deleted out from under it until the reaper acts — abandons rather than submitting the workflow beside the retry |

**F1 — worker resource sizing.** The diagnosis above held on inspection and
the item has landed; four things about it were narrower in this file than they
turned out to be in the source.

`enterprise/manifests/02-worker.yaml` requested `memory: 8Gi` and limited
`memory: 24Gi`, and the limit was unreachable: the container could never hit
its own cgroup ceiling, so the real ceiling was node pressure, which produces
an eviction or a kernel OOM kill rather than a clean container-level
`OOMKilled`. A burstable pod whose limit exceeds node capacity is also a prime
eviction candidate. **If host-RAM OOM is a problem in your workflows, this line
was the cause and retry is a workaround for it.**

- **It was not only the default instance.** 16 GiB of system RAM is the
  *floor* across every GPU type this repo prices in `scripts/06-status.sh` —
  `g6.xlarge`, `g5.xlarge` and `g4dn.xlarge` all have it; only `g6.2xlarge`
  and `g6e.xlarge` have 32 GiB. So the bug was not "the default is small", it
  was "no supported instance could ever satisfy that limit".
- **It was two manifests, not one.** `manifests/base/deployment.yaml` — the
  single-user path — carried the identical `8Gi`/`24Gi`/`1`/`3` block on the
  same instance type. An F1 that fixed only the enterprise worker would have
  left the bug in the configuration most people run first.
- **cpu had to move with memory.** Guaranteed QoS requires requests to equal
  limits on *every* resource, so `cpu: 1`/`cpu: 3` could not stay. Both
  manifests are now `10Gi`/`10Gi` and `2`/`2`: 10Gi is the largest round
  figure that provably fits after OpenShift's node reserve (~2.8 GiB on a
  16 GiB machine) and a GPU node's DaemonSets (~1.5-2 GiB), and it covers the
  ~7 GB SDXL-class checkpoint of `docs/03-storage.md` staged in host RAM plus
  the torch and CUDA host runtime. The full arithmetic is in the comment at
  the resources block; `scripts/lint.sh` holds the numbers.
- **The fix raised the requests, so it does not buy density.** See I6.

Sizing is still part of I3's admission arithmetic, and the resources comment in
`02-worker.yaml` is where that arithmetic is written down.

**F2 — the queue payload envelope.** The queue carried `{job_id, workflow}` and
exactly two files parsed it. Q1 wants a lane key, Q2 an attempt count and phase,
Q4 attribution, Q6 a submit timestamp — four items each independently rewriting
a contract two files must agree on, in the two most contended files in the
repository. It is now defined once, with a version field and tolerant parsing,
so an unbuilt field is simply absent. The workflow is already tens of kilobytes
in that payload, so four scalars cost nothing.

What landed:

- **The envelope is one block of code, mirrored verbatim.** `{schema_version,
  job_id, workflow, queue_key, attempt, user, submitted_at}`, produced by
  `build_envelope()` and consumed by `parse_envelope()`, between
  `BEGIN`/`END SHARED ENVELOPE` markers in both `hub.py` and
  `worker_agent.py`. There is nowhere to import it from — `enterprise/setup.sh`
  builds the two images from two different build contexts — so it follows the
  rule the processing-list key shape already followed, *change both or
  neither*, and `scripts/lint.sh` now diffs the two copies rather than trusting
  it.
- **The four fields are reserved, not implemented.** Each exists, has a
  default, and round-trips, and nothing reads any of them. `queue_key` is
  always `""`, because Q1 has not yet decided what a lane is; `user` carries
  the `X-Forwarded-User` the gateway was already recording on the job's state
  (writing a value the item will later read is reserving the field, not
  implementing Q4's report). Giving a reserved field its behaviour is a
  backwards-compatible change and must not bump `schema_version`.
- **Tolerance had to be made observable to be testable.** "The old shape still
  runs" is true of `HEAD` by construction — the old shape *was* the only shape —
  so an assertion that a version-less payload completes cannot fail before the
  change and proves nothing after it. The worker therefore records the version
  it actually parsed onto the job's state hash (`schema_version`, defaulting to
  `1`), which is both what makes the claim checkable and what an operator wants
  mid-rollout: a job that says `1` came from a not-yet-upgraded gateway or a
  queue entry written before the rollout.
- **Queue semantics are untouched.** Same `BLMOVE` into the same per-worker
  processing list, same depth gate, same `MAX_QUEUE_DEPTH`. The reaper's
  narrower `json.loads(raw)["job_id"]` needed no change: `job_id` is still a
  top-level key, which is part of why it is one.

**F3 — the harness.** Add check discovery to `enterprise/test/run.sh` so a new
check is a file and not also an edit in two hardcoded places, and settle the
naming convention before six items each create their own `check4.py`. Then
extend `scripts/lint.sh`'s manifest loop from `yaml.safe_load_all` to shape
assertions. Ten of §3's invariants name a manifest, a
Containerfile or a shell script in their "where" column rather than a line of
Python — a missing toleration, a Service that regained a port, a dropped Route
annotation, a Containerfile that lost its `chgrp 0` block — and the e2e suite
structurally cannot see any of them. This is also what turns the infra gate
below from a request for vigilance into a check. `scripts/lint.sh` now holds
twelve of those file shapes as greps, beside the manifest checks.

**F4 — the un-heartbeated window, and the fence under it.** Q2 narrowed retry
to deaths that happened before ComfyUI was handed the workflow, and left one
question unasked: what *is* a death? The reaper's whole liveness test is
whether one key exists, so the answer was "a heartbeat that is not there" —
and the heartbeat was refreshed only from inside the polling loop and the
post-submit receive loop. `run_job()`'s prologue runs between them and blocks
in three places: `ensure_workspace()` is an `mkdir` on the shared RWX volume
and is unbounded, `ws.connect()` and `submit_prompt()` are 30 seconds each. A
worker slow in there had a heartbeat that merely LAPSED, which read as a death;
its job was at `PHASE_DISPATCHED` — retryable by construction, since the whole
point of that phase is "ComfyUI has not seen it" — and it was requeued while
the worker was alive and about to submit it. Both attempts then ran: ComfyUI
was handed one workflow twice, a second `started`/`completed` landed on a
stream the browser had already closed at the first terminal event, and one
`job_id` was billed twice against a mechanism whose own comment says a job is
billed at most once. This is exactly the replay Q2's narrowing exists to
prevent, arriving through a door Q2 did not look at, and it needs nothing to
die.

Two halves, because the first one alone is not a fix:

  1. **The keepalive.** `start_heartbeat()` refreshes from a daemon thread for
     as long as the process lives, at a third of the TTL. The heartbeat becomes
     a property of the process being alive rather than of it being somewhere
     particular in its own code, which is what it was always claiming to mean.

  2. **The fence.** A keepalive shrinks the window; it cannot close it. A
     refresh that *cannot run* — the process stopped by the kernel, Redis
     unreachable for longer than the TTL, an EFS `mkdir` that takes minutes on
     a thread that is itself waiting on the same Redis — still looks exactly
     like a death. So the job carries an owner (`OWNER_FIELD`, in the shared
     envelope block): the worker writes its incarnation on the job in the same
     `HSET` that claims it, the reaper stamps `REAPED_OWNER` over it before it
     reads or decides anything, and the worker re-reads it at the two moments
     its next act stops being private — before the `/prompt` POST, and inside
     `finish()`, the single door every terminal outcome leaves by. Reaped means
     abandon: no submit, no terminal event, no accrual. Absence is deliberately
     not a fence — an unowned job proceeds — because a missing field that
     suppressed a terminal event would strand precisely the jobs this whole
     mechanism exists to stop stranding.

**What F4 does NOT do, precisely.** The fence is check-then-act, not a
compare-and-swap: the worker reads the owner and then submits, and a reap that
lands between those two operations is not caught. That window is two Redis
round trips wide instead of the tens of seconds the prologue used to be, and it
can only be entered at all by a worker the gateway has *already* wrongly
declared dead — but it is not zero, and calling it closed would be a claim
rather than a fix. Closing it needs the submit and the ownership test to be one
atomic operation, which they cannot be while the submit is an HTTP POST to
another process: the honest form is a lease with a fencing token that ComfyUI
itself validates, or an idempotency key on `/prompt` so a second submission of
one job is refused by the far side rather than declined by the near one. Both
are redesigns of the worker/ComfyUI contract, not edits to this file. The same
gap applies to `finish()` on the terminal side, where the consequence is
smaller: a second terminal event on a stream, not a second GPU.

**Corrected by the audit sweep: the submit-side window is closed.** The
paragraph above was right that closing it *inside the POST* is a redesign,
and wrong that the check-then-act had to stay. The two operations that raced
were both against Redis — read the owner, then `HSET` the phase — and two
Redis operations can be one Lua script. `claim_executing()`
(`CLAIM_EXECUTING_LUA` in `worker_agent.py`) now writes `executing` only if
the owner field still names this incarnation, atomically, and the worker
submits only on success; a reap that lands between the old read and the old
write now lands before or after a single command, and either way the worker
sees it. `check-36-live-worker-fencing.py` scenario C reaches the window with
a test-only pause (`TEST_DELAY_BEFORE_CLAIM_S`, never set in a manifest),
because at microseconds wide nothing external can. What remains is exactly
what the paragraph said it would be: the POST itself is still to another
process, so a reap landing between a successful claim and ComfyUI receiving
the request is not caught — that is the fencing-token or idempotency-key
redesign, and it is still not this file. The `finish()` side is unchanged.

Two smaller things F4 also does not do. The reaper's requeue still does not
accrue, so an attempt that held a card for a slow prologue and was then reaped
has that time billed to nobody — the second attempt's `started_at` overwrites
the first, and the fence stops the abandoning worker from claiming it. And
`still_ours()` reads Redis, so a worker that cannot reach Redis raises there
and fails its own job rather than abandoning it quietly; that is the existing
behaviour of every other Redis read on the job path (`cancelled()` included)
and was left alone deliberately.

## The work items

Effort and risk are assigned here from the source, not inherited from the
README. Where they differ from the README's tags, the README is the one that
was wrong.

| ID | Change | Effort | Risk | Lane | Proven by |
|---|---|---|---|---|---|
| Q1 | Fair queueing across submitters — **landed** | Medium | Medium | Queue | `enterprise/test/check-50-fair-queue.py`: a whole batch queued by one submitter does not delay a single job from a second submitter behind it — round-robin by `queue_key`, the pop itself (`BLMOVE` into the per-worker processing list) unchanged. `bench-fair-enqueue.py` separately measures what one insert costs Redis at a realistic queue depth |
| Q2 | Phase breadcrumbs + retry only pre-execution deaths — **landed** | Medium | **High** | Queue | `enterprise/test/check-30-sigkill.py` and `check-35-retry-doors.py`: a worker killed before ComfyUI ever saw the workflow is requeued exactly once; a worker killed mid-execution gets one terminal `failed` naming the dead worker and is never requeued; the `phase` breadcrumb is durable *before* the `/prompt` POST returns, not after; and a job the user already cancelled is neither requeued by the reaper nor ever submitted by a worker that pops it |
| Q3 | Per-user output workspaces — **landed**, laptop half | Medium | **High** | Queue + cluster | `enterprise/test/check-60-user-workspaces.py`: two submitters land in two places, a hostile username is confined rather than mangled or escaped, and an anonymous submission still works and does not alias onto a real user — plus lint shapes for the directory mode. The arbitrary-UID half is on the cluster-day list below |
| Q6 | Estimated-wait metric — **landed** | Small | Low | Queue | `enterprise/test/check-80-estimated-wait.py`: `comfy_estimated_wait_seconds` is exposed on `/metrics` in proper gauge form, reads zero or absent only with an empty queue, and reflects a manufactured entry's real `submitted_at` (grows with wall-clock time, not with a constant or queue depth) — the scaler half is I4 |
| Q4 | Showback report — **landed** | Small | Low | Queue | `enterprise/test/check-90-showback.py`: `/api/showback` reports GPU seconds against the submitting user and tracks real wall-clock duration rather than a constant, two users' totals do not bleed into each other, an anonymous submission lands in its own explicit bucket, a *failed* job is still billed, a job whose worker was SIGKILLed is accounted rather than lost, and the `comfy:showback:*` key count stays below the number of identities that fed it — plus lint shapes for the expiry and the identity cap, which a one-minute suite cannot see |
| Q5 | GPU-second quota breaker — **landed** | Medium | Medium | Queue | `enterprise/test/check-95-quota-breaker.py`: a submitter already over the ceiling produces ZERO writes to `comfy:queue` — armed before the request, counted, not inferred from `LLEN` afterwards — and is refused with a 429 that names the quota; a submitter under it, one with no accounting at all, and one whose accounting is present but unreadable are each queued exactly once and run to completion; and `/readyz` stays `{"ok": true}` throughout, with the over-quota identity sent on the readyz request itself. Plus the lint shape that keeps the breaker out of the readiness path — the half a green suite cannot see |
| I1 | Schedule the warm window — **landed** | Small | Low | Infra | `enterprise/manifests/03-autoscale.yaml`'s `cron` trigger, driven by `WARM_WORKERS`/`WARM_START`/`WARM_END`/`WARM_TIMEZONE`; `scripts/lint.sh` fails a floor above `maxReplicaCount` (a floor KEDA clamps and never reports), with a fixture in `scripts/lint-fixtures/manifests/`. Behaviour on cluster day |
| I2 | Split the worker image | Medium | Low | Infra | A new CI job — the existing one builds only the gateway image |
| I3 | Placeholder pod + PriorityClass | Medium | Medium | Infra | Cluster day: node held warm, real job still preempts |
| I4 | Scale on wait, not depth | Medium | Medium | Infra | Cluster day; depends on Q6 |
| I7 | Spot GPU pool *(optional)* | Medium | Medium | Infra | Cluster day; requires a hand-rebuilt pool |
| S1 | Model provenance gate | Medium | Low | Supply | Unit tests; it rejects a `.ckpt` |
| S2 | OpenShift Pipelines build | Medium | Low | Supply | Manifests parse; a pipeline dry-run |

### Corrections to the README's framing

- **Q1 is not Small.** The README says the pop is `BRPOP` on one list, so
  priority is "a small change to `worker_agent.py`". The source uses `BLMOVE`
  into a per-worker processing list — a §3 invariant the reaper depends on — so
  the change moves the pop, the depth gate, the KEDA trigger's single
  `listName`, and the client-visible `queue_position`/`position` shape
  together — deliberately not named `queue_depth`, which stays reserved for
  `/api/stats`' and `/metrics`' real backlog length; see FIX 4d in this
  branch's history for why the two were worth telling apart by name.
- **I2's proof does not exist yet.** CI has no GPU image build at all, by
  design, and the arbitrary-UID job covers only the gateway image — whose
  Containerfile deliberately has no `chgrp 0` block. I2 must **build** its own
  proof, not inherit one.
- **S1 has no home yet.** The only `aws s3 sync` in the repo is in the
  single-user overlay; the multi-user worker mounts the models PVC directly and
  has no sync path, and `oc rsync` from a laptop filters nothing. A gate written
  against "the sync job" would leave the shared writable model volume — the
  exact case `docs/06` worries about — completely ungated. Deciding which path
  is gated is the first half of the item.
- **I4 and I7 are not parallel infra work.** Both are blocked behind queue
  items (I4 on Q6, I7 on Q2's phase data), which the first version of this file
  acknowledged in one column and contradicted in another.
- **I1 and I3 contradicted each other.** Both wanted to own the GPU pool's
  min-replicas — I1 by cron during working hours, I3 by a placeholder pod
  holding a node — and any `setup.sh` re-run reset whatever cron did.
  Settled. I1 landed as a declarative KEDA trigger reading `.env`, so a
  `setup.sh` re-run reasserts the floor rather than resetting it; the
  min-replicas ownership conflict only existed for the cron-job
  implementation. I3 remains unscheduled.

### Found while writing the OOM checks

**A closed ComfyUI socket does not shortcut the job deadline.** `check-70`
kills the stub's connection mid-job (`__die__`, an abnormal close — code 1006);
the agent does not treat the dropped connection as terminal, waits out
`JOB_TIMEOUT`, and fails with the deadline as the reason. That is the
bounded-deadline invariant working, and at the time it was the only assertion
in the suite that exercised it.

In production this is mostly hidden: if the ComfyUI *process* dies, `start.sh`
waits on both children, so the container ends (restarted in the same pod by
`restartPolicy: Always`) and the gateway's reaper handles the stranded job in
seconds regardless. The exposure is the narrower case — a socket that closes
while ComfyUI is still alive — where a worker holds a GPU for the full
`JOB_TIMEOUT`, **1800 seconds by default**, doing nothing.

**Fixed.** A closed socket now re-checks `/history` once — the prompt may have
landed in the instant before the process went — and otherwise fails immediately
with a reason naming the lost connection. Measured in `check-70`: 65.0s to 0.2s.
The deadline stays as the backstop it was always meant to be.

**Correction, found later: the root cause was not what this entry first said.**
`__die__`'s abnormal close (1006) was never an empty frame — websocket-client
raises `WebSocketConnectionClosedException` straight out of `recv()` for it,
which is what the fix above catches. What actually cost `check-70` the full
`JOB_TIMEOUT` before the fix was a bug in the *stub*: `ws.close(code=1006)` was
called from the wrong asyncio task and silently failed to close anything, so
the connection sat open and the agent's own `except
WebSocketTimeoutException` branch — already correct — kept re-checking
`/history` every `RECV_TIMEOUT` until the deadline. That stub bug is what the
same commit fixed alongside the agent change (`dying_ws`, keyed on the socket
and consumed from inside the endpoint coroutine that owns it).

The *empty-frame* case this paragraph originally described — an ordinary
close (1000/1001) that leaves the connection open, so `recv()` returns `""`
once, a `str` that slips past the binary-frame guard, fails to parse, and
hits `continue` — is real and is what the `if raw == ""` guard exists for,
but no fixture reached it until `check-75-closed-socket.py`
(`__empty_frame__`) was written afterward. And even there, "spinning...at
full speed" overstates it: only the first failed parse is instant: nothing
more arrives after an ordinary close, so every later `recv()` blocks for
`RECV_TIMEOUT` and lands in the same timeout branch above, paced, not
spinning — the cost is JOB_TIMEOUT of that pacing, not a busy loop.

### Q6 landed — the contract it owes I4

Q6 is done: `hub.py`'s `estimated_wait_seconds()` reads `LINDEX comfy:queue -1`
— the tail, which `worker_agent.py`'s `BLMOVE ... src="RIGHT"` always pops
next, whatever fair queueing did to the rest of the list — and reports how
long *that* entry's `submitted_at` (F2) has been sitting there. `metrics()`
exports it as `comfy_estimated_wait_seconds`, a third gauge beside
`comfy_queue_depth` and `comfy_workers_registered`, picked up by the same
unfiltered ServiceMonitor `enterprise/setup.sh` already applies — no
`metricRelabelings` allowlist exists to update.

**What it means at zero workers, decided rather than left open.** The
scale-to-zero case — no worker running at all — is the case a user actually
hits, and the roadmap item that specified Q6 asked for a decision here rather
than a default: report a real, honest number, not "unknown". The alternative
(reporting the gauge absent whenever `workers_registered == 0`) was considered
and rejected, specifically because it is the one case I4 exists to act on: a
KEDA Prometheus trigger cannot fire off a series that is not there. Unlike a
depth × average-service-time forecast — which has no service-time sample to
build from until something has finished, and is a fabricated number at zero
workers — age-of-the-next-entry needs no such model. It is a directly measured
elapsed time that is simply true, and keeps growing, with nobody serving it.
An empty queue reports `0.0` (not absent) for the same reason: there is
nothing to be waiting on, which is itself the honest answer, not a stand-in
for "no data".

**The contract a Prometheus-scaler trigger (I4) needs from this gauge:**

- **Metric name and unit.** `comfy_estimated_wait_seconds`, already in wall-
  clock seconds — no unit conversion on the KEDA side.
- **Query surface.** OpenShift user-workload monitoring's Thanos Querier
  (`thanos-querier.openshift-monitoring.svc.cluster.local:9091` in-cluster),
  namespace-scoped to `$APP_NAMESPACE` — Thanos multi-tenancy requires the
  `namespace` field on the trigger, not just the query string. Authentication
  needs a bearer token (a ServiceAccount bound to a view role on that
  namespace's metrics), wired through a `TriggerAuthentication` the way
  `comfy-redis-auth` already wires the Redis password in
  `03-autoscale.yaml` — a `bearerAuth`, not a `secretTargetRef`, since there
  is no static secret to reference.
- **Activation from zero.** KEDA's `prometheus` trigger's `activationThreshold`
  is the field that matters here, distinct from `threshold`: it is what lets
  a `0.0` reading mean "stay at zero" while a real, growing value crosses it
  and scales `0 -> 1`. This only works because the gauge is a number at zero
  workers rather than absent — see above.
- **Add, don't replace.** A `ScaledObject` may hold more than one trigger
  (OR'd by default), so this is a second `type: prometheus` trigger added
  alongside the existing `type: redis` one in `03-autoscale.yaml`, not a
  replacement for it. The two are independent signals over the same queue —
  instantaneous depth vs. accumulated wait — and either firing should scale
  up.
- **Poll cadence bound.** The value only changes as fast as the ServiceMonitor
  scrapes it (`interval: 30s`, `configure_metrics()` in `enterprise/setup.sh`)
  plus Prometheus's own ingest lag. A `pollingInterval` on the trigger shorter
  than that queries data that has not moved yet; 30s or coarser is the honest
  floor, not KEDA's 15s default the Redis trigger uses.
- **Threshold value is a product decision, not a technical one.** It trades
  against the 8–17 minute cold start `03-autoscale.yaml` already documents:
  too low and a worker scales up for a job that would have been picked up in
  seconds anyway; too high and a user waits through avoidable idle-queue time
  before the pool even starts warming. Cluster day is where this gets tuned
  against a real cold start, the same way `cooldownPeriod: 600` was chosen
  against it.

None of the above is implemented. This section exists so I4 starts from a
contract instead of re-deriving one from `hub.py`.

### Q4 landed — the definition, the reaper decision, and the teardown

Q4 is done: `GET /api/showback` reports one UTC calendar month's GPU seconds
per submitter, read out of a single Redis Hash that both terminal paths write
into. The four things that were open when the item was written are settled
below, because a showback report is a document people argue with and every one
of them is a question somebody will ask.

**A GPU second is one second a worker held the card, and it over-counts the
sampler on purpose.** The interval measured is wall-clock time between the
instant a worker writes `running` on the job's state hash (`run_job()` in
`worker_agent.py`) and the instant that job reaches a terminal state. Inside
that number, deliberately: the checkpoint load — ~7 GB off EFS for an
SDXL-class model, and the first job on a cold node pays for an empty page
cache — the workspace `mkdir`, the ComfyUI WebSocket connect and the `/prompt`
round trip, and any stretch the agent spent parked on a ComfyUI that had gone
quiet. A job that *failed* after twenty minutes is billed twenty minutes.
Queue time is not in it: nothing was held before a worker picked the job up.

The alternative — "time actually spent inside ComfyUI's execution" — was
rejected for two reasons rather than one. It is not measurable from here
without trusting ComfyUI's own timings, and more importantly it would
under-count the expensive part: this pool runs one job per pod on a dedicated
card, so nobody else could have used the GPU while a checkpoint was loading,
and a definition under which that time belongs to no one is a definition that
does not add up to the bill. **An honest over-count that says what it includes
beats a precise number nobody can reproduce**, and the definition is written
into the code — `BEGIN SHARED SHOWBACK`, mirrored in both files — rather than
only into this document, because the person who needs it is reading the
accrual.

**The reaper path was the real question, and it goes to `excluded`.** A worker
that is SIGKILLed mid-generation (host-RAM OOM, node reclaim, spot) is
terminated by `hub.py`'s `fail_orphaned_job()`, which never calls
`worker_agent.py`'s `finish()` at all — so an implementation that instruments
only `finish()` silently drops exactly the most expensive jobs in the system,
the ones where a card was held and nothing came back. That time is therefore
recorded. It is **not** billed to the submitter:

- The gateway cannot know *when* the worker died, only when it noticed. The
  interval it can compute is inflated by up to `HEARTBEAT_TTL +
  REAPER_INTERVAL` — about 3.5 minutes on the defaults — and a figure that is
  mostly detection lag is exactly the figure an operator cannot defend in the
  meeting this report exists for.
- The user got nothing for it. Charging them for the cluster's failure is a
  policy choice dressed up as a measurement.
- Kept visible rather than dropped, `excluded_gpu_seconds` doubles as a
  cluster-health signal: it climbing is workers dying while holding cards.

So every second in `users` is bracketed by two timestamps one worker wrote,
which is what makes the column reproducible — and it is the column **Q5**
should compute a quota from. `excluded_gpu_seconds` is real spend and belongs
in a cost conversation; it does not belong in anyone's quota.

A job the reaper *requeues* is not accrued at all: the only deaths it requeues
are those before ComfyUI was handed the workflow (`RETRYABLE_PHASES`), which
spent no GPU time by construction, and the attempt that eventually runs starts
its own clock. An anonymous submission — no `X-Forwarded-User` at all, the
ordinary `AUTH_MODE=none` shape — goes to `anonymous_gpu_seconds`, its own
named line rather than a blank key an operator has to know to look for. Those
buckets exist so that there is no fourth, silent possibility.

**The key space is bounded three times over, and each bound is load-bearing.**
Redis here is `maxmemory-policy noeviction` at `--maxmemory 512mb`, so a key
nothing deletes is a key forever; and the identity every total is named from
is an `X-Forwarded-User` header that is entirely client-supplied whenever
`AUTH_MODE=none`. An accumulator taking one Redis key per submitter would let
an unauthenticated caller fill Redis by varying one header — the same "work
vanishing at random" the `noeviction` invariant exists to prevent, arriving by
a door the policy does not cover. So: **one Hash per period**, one field per
identity, `HINCRBYFLOAT` per job; **the Hash expires**, re-armed with `EXPIRE
... NX` on *every* accrual because `HINCRBYFLOAT` recreates a key that expired
mid-flight and a recreated key has no TTL at all; and **the field count is
capped**, with accruals past `SHOWBACK_MAX_USERS` going to one shared `other`
field that the report flags as `truncated: true`. Worst case is under a
megabyte across every period live at once, and it does not grow with
throughput. `check-90` asserts the key count from outside; `scripts/lint.sh`
pins the expiry and the cap, because a one-minute test run cannot tell a TTL
measured in months, or a cap of a thousand, from their absence.

**It is deliberately not a Prometheus metric.** A per-user series is one label
value per submitter, from a client-supplied header, and unbounded label
cardinality is how a monitoring stack is taken down from outside. The Redis
cap bounds this report; nothing bounds a metric's label set once it has been
scraped. `/metrics` keeps its three pool-level gauges.

**The report does not survive `make down` — capture it before the teardown.**
The accumulator is in Redis, Redis's PVC is `gp3`
(`enterprise/manifests/00-redis.yaml`), and gp3 dies with the cluster. On the
nightly-teardown habit `docs/09-engineering-handoff.md` §5 recommends as the
default — the one that takes the bill from ~$1,490/month to ~$370 — "last
month's report" is gone every morning, and the first person to notice will be
whoever asked for the report on the 1st. This is not fixable by moving the
accumulator: EFS is the volume that survives a teardown, and putting the
queue's Redis on it to save a monthly total would be a much worse trade. Make
the capture part of the teardown instead:

```bash
oc exec deploy/comfy-gateway -c gateway -- \
    curl -s localhost:8000/api/showback > "showback-$(date -u +%Y-%m).json"
make down
```

`make park` is safe — the cluster and the volume stay — and `periods_available`
in the response tells you what is actually still in Redis, which after a
teardown is "this month only, from the moment the cluster came back". If the
report matters monthly, that two-line habit is the whole fix; if it matters
more than monthly, the next step is shipping the same JSON to S3 from a
CronJob, which is a new item and not this one.

### Q5 landed — what it counts, which way it fails, and what it cannot do

Q5 is done: `QUOTA_GPU_SECONDS` is a per-submitter ceiling on GPU seconds
inside one showback period, enforced in `generate()` before a job is placed on
the queue and refused with `429` plus a message saying what happened and when
it resets. It is **off by default** (`0`), it lives in `.env.example` with the
rest of the configuration, and `enterprise/setup.sh` substitutes it into the
gateway Deployment the same way `MAX_GPU_WORKERS` is substituted into the
`ScaledObject`.

**It reads Q4's accounting and adds none of its own.** One `HGET` of
`comfy:showback:<period>`, field `u:<user>` — the field
`SHOWBACK_ACCRUE_LUA` writes and `/api/showback` reports. That means a
refusal is always explainable from a URL the person refused can be pointed
at, and it means the quota inherits Q4's definition of a GPU second whole,
including what it over-counts. It also inherits the reaper decision for free:
`excluded_gpu_seconds` is a different field, so time lost to workers dying
mid-generation is not counted against the user who submitted the job, exactly
as the Q4 section above says it must not be.

**Anonymous submissions count against the anonymous bucket rather than being
exempt.** Under `AUTH_MODE=none` that makes the ceiling one shared pool for
every caller who sends no header, which is a real consequence and is written
down here so nobody discovers it. The alternative — exempting the no-header
case — turns the breaker off in exactly the deployment shape where "anyone
with the URL can spend your GPU budget" is already true. Neither choice is a
security control: the identity is client-supplied, so varying a header buys a
fresh quota either way. This is a cost guardrail, and it uses the identity the
report uses.

**It fails open through four doors, loudly.** Redis unreachable, the field
absent, the field present but not a number, and `QUOTA_GPU_SECONDS` itself not
a number — every one of them proceeds with the submission. The first, third
and fourth print a line on the gateway log naming what could not be read; the
second does not, because "this submitter has spent nothing this period" is the
ordinary case and logging it would bury the three that matter. Absence and
unreadability are separate cases on purpose, and
`check-95-quota-breaker.py` drives them separately: a strict `float()` gets
the unreadable one backwards, raising a 500 where the requirement is to let
the job through. The env var is parsed tolerantly for the same reason, unlike
`MAX_QUEUE_DEPTH`'s `int()` — a breaker that crash-loops the gateway over its
own configuration is a worse outage than the spend it guards against.

**It is nowhere near `readyz()`, and that is enforced structurally.** The
roadmap's own sentence — "It must not be wired into `readyz()`" — is now the
twentieth §3 invariant and a `scripts/lint.sh` rule that walks `hub.py`'s
call graph: nothing reachable from `readyz()`, transitively, may mention the
quota, and `quota_refusal()` must be called from `generate()` and nothing
else. The rule also fails if the breaker is deleted, since every other clause
in it is an absence and an absence is trivially true of an empty file.
`check-95` asserts the runtime half (`/readyz` reads `{"ok": true}` while a
submitter is over quota, with that identity sent on the readyz request
itself), which is necessary and not sufficient: a health endpoint that read
the quota would still be green on any gateway whose reader is under the cap.

**What it deliberately does not do.** It does not touch jobs that are already
queued or running — it is admission control on new submissions, which is why
the refusal says so. And it is a ceiling on *past* accrual, not a reservation:
seconds land in the Hash when a job reaches a terminal state, so a submitter
who queues twenty jobs at once is under quota for all twenty and goes over
while they run. Bounding that would mean reserving an estimate at submit and
reconciling it at completion — a second accounting path, an estimate for a
workflow nobody has run yet, and a reconciliation that has to survive the
reaper. The roadmap chose one accounting path; the overshoot is bounded by
`MAX_QUEUE_DEPTH` and by the pool size, and the budget alarm remains the
backstop.

### Found by the cross-wave sweep, and landed without an item

Two fixes shipped that were on no list here, and they are recorded so that the
statuses above are the whole of what changed. Both are §3 rows and both are
pinned by `scripts/lint.sh`; neither is a roadmap item, because neither was a
feature.

**The worker's Redis identity names the process, not the pod.** The heartbeat
key and the processing list are now suffixed with a nonce chosen at process
start rather than with `HOSTNAME` alone. `restartPolicy: Always` restarts a
container *inside* its pod, so an identity taken from `HOSTNAME` is handed back
to the next incarnation, whose first heartbeat then answers the reaper's
liveness question on the dead one's behalf and hides its stranded job for as
long as the pod keeps restarting. `check-32-worker-restart.py` is the same
death as `check-30-sigkill.py`'s scenario B with the name reused.

**A failed reap leaves the job recoverable, and is bounded.** The reaper's loop
read the entry off the processing list before reaping it, so anything that
raised in the body destroyed the only record that the job had been queued. It
now removes the entry only after the reap returns, retries a reap that raised
on a later tick, and sets aside an entry that can never be reaped on a capped,
expiring list. `check-37-reap-durability.py` injects one fault per scenario.

### Found by the cross-wave sweep, done by the audit sweep

**The showback hash FIELD was not clamped, so the documented key-space bound
was too small.** `MAX_ENVELOPE_FIELD_CHARS` bounded the identity on the way
into the envelope, but the field written into the showback hash was the raw
one, so the per-period bound was larger than the comment claimed.

It was genuinely minor — the bound was still a bound, and `SHOWBACK_MAX_USERS`
caps the field count regardless — but it is worth keeping the note about how
it was almost fixed. A documentation pass changed one line so the *write* side
used the clamped identity, and left the quota *read* side using the raw one.
That is a quota bypass reachable from a client-supplied header: charges land
in one bucket while the check reads another. It was caught by the gate,
reverted, and recorded here because the lesson is more useful than the bug.

It has since been done the way this entry asked for: the clamped identity —
not the raw header — is what `generate()` writes to the job's state hash, so
the accrual and the quota read both derive their field name from the same
clamped value, and `check-90-showback.py` asserts a 300-character identity
lands on the hash, and therefore in the report, clamped to
`MAX_ENVELOPE_FIELD_CHARS`.

### Landed in the audit sweep

A six-pass audit after the queue lane produced a set of fixes that were on no
list here. None is a roadmap item, because none was a feature; they are
recorded so that the statuses above are the whole of what changed, and each
has a check or a lint rule that fails on the tree before it.

| Where | What changed | Proven by |
|---|---|---|
| Worker | A prompt the agent gives up on — `JOB_TIMEOUT`, a closed socket, any exception out of the receive loop — is sent ComfyUI's `/interrupt`, and the agent waits up to `INTERRUPT_DRAIN_TIMEOUT` (60 s) for `/queue` to empty before its next `BLMOVE`; a ComfyUI that never drains makes the agent exit non-zero and deregister so the pod restarts | `check-67-job-timeout-interrupt.py`; `fake_comfy.py` made serial for it |
| Worker | The `executing` claim is a Lua compare-and-set on the owner field (the F4 correction above) | `check-36`, scenario C |
| Worker | Output confinement, five more ways: previews (`type != output`) are not served; an output reported inside another submitter's workspace is refused rather than moved; subfolder components are validated like filenames; URLs are percent-encoded; the raw `data.output` manifest is stripped from forwarded `executed` events, and the gateway drops a URL from any raw event whose components are not bare names | `check-65`, (e)–(i); `check-10` |
| Worker | Explicit `REDIS_SOCKET_TIMEOUT` and `REDIS_CONNECT_TIMEOUT`, and an `AGENT_LIVENESS_FILE` touched on every idle pass, every in-job receive pass and every keepalive refresh, which the manifest's combined exec liveness probe checks the age of (120 s) | `check-68-agent-liveness-file.py` |
| Gateway | Under `AUTH_MODE=oauth`, `/outputs` is scoped to the caller's workspace and `/api/showback` to the caller's row unless they are in `SHOWBACK_OPERATORS`; `AUTH_MODE` is read by `hub.py` for the first time; `workspace_name()` is mirrored as a `SHARED WORKSPACE` block lint diffs | `check-66-output-scoping.py` |
| Gateway | `/api/stats` and `/metrics` are one cached snapshot per `STATS_CACHE_SECONDS` (5) instead of a keyspace `SCAN` per call; `REDIS_MAX_CONNECTIONS` (200) bounds the pool; a WebSocket for an unknown job closes 4404, one on somebody else's job (under oauth) closes 4403, and one that has lived `EVENT_STREAM_TTL` closes 4408; `/api/jobs/<id>`, its cancel and its stream are scoped to the submitter like `/outputs` | `check-15-gateway-limits.py`, `check-10`, `check-66` |
| Gateway | `POST /api/generate` requires `Content-Type: application/json` (415), malformed JSON is 400, a body over `MAX_BODY_BYTES` is 413, and the `MAX_QUEUE_DEPTH` check moved inside the fair-enqueue Lua so concurrent submits cannot exceed it | `check-10`, `check-15` |
| Gateway | A job the reaper requeued remembers which entry it came from, so a second reap of the same processing-list entry removes the entry instead of failing the live retry; the clamped identity lands on the state hash; `EVENT_STREAM_TTL` and `REAPER_INTERVAL` are validated at import; shutdown awaits the reaper and closes the pool; a second lint rule compares `state_key`/`stream_key`/`payload_key`, the TTL default and the cancel field between the two files by AST | `check-37`, scenario C; `check-90`; `scripts/lint.sh` |
| Manifests | Namespace default-deny with six allow policies (`06-network-policy.yaml`); the worker connects as the least-privilege `comfy-worker` ACL user (`00-redis.yaml`, `setup.sh` patches the existing Secret); `automountServiceAccountToken: false` on Redis, gateway and worker; worker `RollingUpdate` one pod at a time with no surge; PDBs on worker and Redis, and the gateway's moved to `01-gateway.yaml` so it applies under `SCALE_TO_ZERO=false` too; preferred anti-affinity for the gateway pair; read-only root filesystem where feasible | Lint rules for policy coverage, the ACL allowlist, the automount, and the worker's Redis user, each with a fixture |
| Manifests | **I1 — the scheduled warm floor** (`WARM_WORKERS` and friends) as a KEDA `cron` trigger | The I1 row above |
| Images | ComfyUI and ComfyUI-Manager pinned to commit SHAs everywhere (`c2bcbecd…` = v0.32.0, `da5e88aa…` = Manager 4.2.2); the worker image installs `app/requirements-extra.txt`; the `chmod g=u` narrowed to the directories the process writes; `start.sh` forwards `"$@"` and honours `AGENT_DISABLED=1` for CI | `nightly.yaml` boots both GPU images as an arbitrary UID on CPU |
| CI | `permissions: contents: read`, per-job `timeout-minutes`, cancel-in-progress concurrency, SHA-pinned actions, kubeconform against real Kubernetes/OpenShift/KEDA schemas, Trivy after the UID proof and non-blocking on PRs (the nightly scan gates), torch pinned in the CPU smoke to the Containerfile versions | The PR that carried it is the first run |
| Scripts | `04-storage.sh` treats only `MountTargetConflict` as "already exists"; `99-teardown.sh` clears EFS mount targets before the stack delete and reports failed resources instead of dying silently; `06-status.sh` warns on an unknown instance rate and prices autoscaled pools from live node count; `ASSUME_YES` comes only from `--yes`; `MANAGER_REF` and the `TORCH_*` build args are plumbed through `.env.example` and `05-deploy.sh` | `scripts/unit-tests.sh` (40 assertions) |

### Deferred, with reasons

**I6 — NVIDIA time-slicing: do not do this.** Time-slicing gives **no memory
isolation**; co-resident processes share the full 24 GB with nothing
partitioning it, so two workflows' peak VRAM simply sum. Given how easily
ComfyUI exceeds VRAM on a single tenant, this converts a deterministic
per-workflow failure into a non-deterministic one that depends on what the
neighbouring job is doing, and the victim is whichever process allocates
second — not the greedy one. MIG partitions memory properly but is not
available on L4. Independently, the density win is zero, and F1 did not change
that in the direction this paragraph originally implied: F1 raised the worker's
memory request from 8Gi to 10Gi, so two workers fit on a `g6.xlarge` even less
well than before — a 16 GiB node offers one pod about 10.5 GiB, and one worker
takes it. Density on this hardware is not a sizing problem to be solved; it is
the node. Revisit only on MIG-capable hardware with more host RAM.

**I5 — local NVMe model staging: a spike, not a work item.** It cannot be
scoped from this repository. ROSA HCP exposes no MachineSet to edit, and the
three plausible routes — Ignition/MachineConfig, a privileged DaemonSet with
`hostPath`, or the Local Storage Operator — differ enormously in blast radius,
with the middle one colliding head-on with the arbitrary-UID and
`restricted-v2` posture both Containerfiles treat as architectural. Which is
permitted on ROSA HCP is an external-documentation question that must be
answered **before** cluster day, not on it.

**I7 — spot: optional, and it compounds.** ROSA accepts spot only at pool
creation, and `scripts/02-cluster.sh` skips a pool that already exists, so an
existing on-demand pool cannot be converted by re-running anything — it must be
deleted and recreated by a person, which the rules below forbid the lane from
doing. It also silently invalidates three quota checks, which all test the
on-demand G/VT code that spot does not draw on, so preflight passes while the
spot request is denied; and it invalidates the cost model in
`scripts/06-status.sh`. `docs/08-stuck-volumes.md` already argues against spot
for GPU workers because reclaim strands the volume. Worth it only if losing
long generations is acceptable to you.

## Landing order

```
F1 → F2 → F3 → I1 → S1 → I2 → S2 → Q1 → Q2 → Q3 → Q6 → Q4 → Q5 → I4 → I3 → [I7]
```

Foundations first, then the infra items that touch neither Python file, then
the queue lane in sequence, then the cluster-only work. `I5` is a spike run
alongside; `I6` is not scheduled.

What actually happened, since the plan and the record are worth telling apart:
F3 → F1 → F2, then Q1 → Q2 → Q3, then Q6 → Q4 → Q5, then F4 — which did not
exist when this order was drawn — and the sweep fixes beside it, then the
audit sweep, which carried I1. S1, I2 and S2 are still ahead of the queue
lane in the plan and behind it in the history.

## Three lanes

**Queue lane — sequential, and landed.** The plan expected all six items to
edit `hub.py` and five of them to also edit `worker_agent.py`, whose key shapes
must change together or not at all; measured from the commits, five edited
`hub.py` (Q3 did not) and three edited `worker_agent.py`. All were
laptop-verifiable except Q3's arbitrary-UID half. The loop was: write the
assertion that fails on HEAD, implement until it passes, run `make test && make
lint`, hand the merged tree on.

**Infra lane — parallel, with three exceptions.** Seven items touch
`enterprise/setup.sh` (591 lines), but they hit mostly disjoint regions and can
be worked in branches or worktrees. The exceptions must be sequenced: I2 before
S2 (both rewrite the image build and its call sites), I2 before its CI job, and
I3 only on top of I1's trigger rather than beside it — the min-replicas
ownership conflict above is settled by I1 owning the floor declaratively.

**Prose lane — after each merge.** Every merged change owes three edits: the
README section it changes, the §3 invariant table in `docs/09` if it adds or
moves a row, and a short rationale in the style of `docs/07-design-review.md` —
what the obvious implementation would have got wrong. Two items *delete*
entries from the "deliberately not here" list in `docs/06`; land those
adjacently so one review settles whether that list should shrink.

## Gates

| Stage | Must be true before it lands |
|---|---|
| Every item | `make lint` and `make test` green |
| Every item | The new check is **actually invoked** and its status reaches the suite's exit code — a check that is copied but never run leaves the suite green |
| Queue lane | An existing assertion may be **replaced** only by one that is strictly stronger, in the same commit, with old and new both shown in review. Two items require this: Q2 must rewrite the assertion that a hard-killed job ends `failed`, which is the behaviour it changes, and Q3 must rewrite the pinned flat output URL. Neither touches the path-traversal assertion, which must survive untouched. |
| Infra lane | No manifest lost a GPU toleration, a Route timeout annotation, or a Service port restriction — enforced by F3, not by hoping |
| Invariant-changing items (F1, Q2, Q3, F4) | A second reviewer who has read `docs/09` §3, checking specifically that the change survives a hard kill mid-job, two gateway replicas racing, and its dependency being unreachable |
| Cluster items | Verified on a real cluster, in one batched session |

## The cluster day

Everything needing real hardware — I3, I4, the cluster halves of Q1 and Q3,
optionally I7, plus an end-to-end pass over the merged work — is batched into
one supervised session rather than a rebuild per item. At ~$2.04/hour a
four-hour sitting is about $8 of cluster time and answers every open question
at once.

Two cluster-only halves that are easy to miss: Q1's KEDA trigger names a single
list, and nothing on a laptop proves the pool still scales 0 → 1 → 0 against it
with fair-queueing reordering live (the pop itself is unchanged, and the insert
only ever adds one entry, so `LLEN` is monotonic across a submit — but a
cluster-only claim is verified here, not assumed); and Q3's real failure mode is
arbitrary-UID, unprovable
anywhere but a cluster. Precisely what Q3 still owes, since the code is
otherwise landed: run a job as some user, note the mode and group of the
`/output/<workspace>` directory the worker created (expect `2775` and GID 0),
then get a SECOND worker pod — a scale-up, or a rollout, so the UID differs —
to run another job for the SAME user, and confirm it writes into that existing
directory rather than failing with `EACCES`. Then confirm the file ComfyUI
wrote inside it is group-owned by GID 0 (that is the setgid bit doing its job,
not the mkdir) and that the gateway, mounting the same EFS volume read-only as
a third arbitrary UID, can still serve it. EFS specifically, not gp3: the
whole point is two pods on two nodes.

Last step of the session is `make park` or `make down`, confirmed with
`make status`.

## Rules for this work, whoever or whatever is doing it

This repository can spend real money and publish real endpoints. Several line
items are good candidates for AI-assisted or parallel execution — the queue
lane is test-first by construction — which makes it worth stating the
boundaries explicitly rather than assuming them.

- **IAM, quotas, machine pool edits and spot bids are proposed, not executed.**
  A person runs them.
- **Nothing merges to `master` unreviewed.** The output of a work item is a
  branch.
- **No `--force`, ever** — and specifically never
  `oc delete pod --force --grace-period=0`, which strands the volume it claims
  to free. `docs/08-stuck-volumes.md` exists because of this one.
- **An invariant is never relaxed to make a test pass.** If the worker's
  loopback bind, the gateway's loopback rebind, the `prompt_id` filter or a
  Route timeout annotation appears in a diff, that diff is wrong regardless of
  whether CI is green.
- **No cluster is left running.**

## The list, as the README carried it

The README's "Ideas worth doing next" was ordered by payoff per unit of work
and kept struck items in place — shipped ones because a roadmap that never
visibly moves is a wish list, and decided-against ones because the reasoning
is the useful part. It now lives here, in the same order, each item mapped to
its ID above.

1. **Schedule the warm window instead of pinning it** — I1, **landed**, as
   `WARM_WORKERS`/`WARM_START`/`WARM_END`/`WARM_TIMEZONE` driving a KEDA
   `cron` trigger rather than as the two cron lines first proposed. The
   cold-start-free morning without paying for a card overnight; still the
   single highest-value setting for a design team.
2. **Shrink the cold start itself** — I2 and I3. The ~10 GB image pull
   dominates node warm-up. Split the worker image so the CUDA + torch layers
   are a stable base that rarely changes, and keep a low-priority placeholder
   pod on the GPU pool so the autoscaler holds one warm node without a real
   job occupying the card. *(Medium — the placeholder/priority-class pattern
   is standard cluster-autoscaler practice.)*
3. **Stage models on the node's local NVMe** — I5, a spike, not yet a work
   item. `g6` instances have instance store, and an init container copying
   the active checkpoint from EFS to local disk turns every later load into a
   local read. It cannot be scoped from this repository: ROSA HCP exposes no
   MachineSet, and the three plausible routes differ enormously in blast
   radius, one colliding head-on with the arbitrary-UID posture. Answer that
   before cluster day.
4. ~~**Narrow retry**~~, and spot separately — Q2 **shipped**, I7 open. These
   looked like one item and are not; "Decisions already made" above has both
   halves.
5. ~~**NVIDIA time-slicing**~~ — I6, **not doing this**; "Deferred, with
   reasons" above keeps the argument.
6. ~~**Per-user output workspaces**~~ — Q3, **shipped**, laptop half; the
   arbitrary-UID half is on the cluster-day list. Reads are scoped under
   `AUTH_MODE=oauth` since the audit sweep, and deliberately not under `none`
   (`docs/06-enterprise-architecture.md`).
7. ~~**Showback from the data you already collect**~~ — Q4, **shipped**;
   "Q4 landed" above has the definition, the reaper decision and the
   teardown caveat.
8. ~~**Fair queueing**~~ — Q1, **shipped**, with the insert cost measured
   rather than assumed (`bench-fair-enqueue.py`; §3 of `docs/09`).
9. **Scale on queue *wait*, not queue depth** — I4, with the gauge half (Q6)
   **landed**: `comfy_estimated_wait_seconds` is on `/metrics` and the
   contract a Prometheus-scaler trigger needs is written up above. Pointing
   KEDA at it still needs a cluster.
10. **A model lockfile** — S1. `models.lock` next to `COMFYUI_REF`, enforced
    by the S3 sync job, so an image tag and a model set pin together and a
    workflow that rendered last quarter still renders. Reject anything that is
    not `.safetensors` while you are there — `.ckpt` files are Python pickles
    and loading one executes whatever is inside it. *(Medium; "S1 has no home
    yet" above says which path to gate.)*
11. ~~**A cost circuit breaker in the gateway**~~ — Q5, **shipped** as the
    quota half, deliberately without the AWS Budgets half; "Q5 landed" above.
12. **Build the images with OpenShift Pipelines** — S2. Bumping `COMFYUI_REF`
    becomes a pipeline run with a signed output rather than a laptop running
    `setup.sh`. *(Medium, and the right move once more than one person owns
    this.)*

## Not on this list

`docs/06-enterprise-architecture.md` ends with the things that are deliberately
*not* here — Redis HA, multi-GPU workers, a gateway that reaches into ComfyUI —
and the reasoning for each. Read it before adding any of them: in several
cases the omission is the decision. Two items here (Q2 and Q3) do reverse
entries on that list, and both say why.
