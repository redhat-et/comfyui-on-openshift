# Roadmap

The improvements listed at the end of `README.md`, turned into a work plan:
what each one actually touches, what proves it, what order they can safely land
in, and which of them cannot be finished without spending money on a real
cluster.

All three foundations have landed — F3, F1 and F2, in that order. Nothing else
here is implemented. This document exists so that the next person to
pick up a line item does not have to re-derive its blast radius, and so that
several of them can be worked in parallel without three people editing
`hub.py` at once.

The first version of this file was written from the documentation rather than
from the source, and was wrong in seventeen places. This version was built from
a full read of the code; where it corrects the README's framing, it says so.

## What decides the order

**Seven items contend on `hub.py`, five on `worker_agent.py`.** Not the same
five: Q5's entire contact with the queue path is one precondition in
`generate()`, and it never touches the worker. The collisions are also finer
than "the same file" — three regions are hot, `generate()`, the reaper pair,
and `gather_stats()`/`metrics()` — which is why worktrees do not help. The
queue work is a sequence.

**The most contended file in the plan does not exist yet.** All six queue items
propose creating `enterprise/test/check4.py`, and all six must edit
`enterprise/test/run.sh`, which has **no check discovery**: `run.sh` copies
`*.py` into the work directory by glob but invokes checks by hardcoded name, and
threads their exit codes by hand. A botched merge there **fails open** — the new
check is copied, never run, and the suite is green. That is why F3 below comes
before any queue work.

**There is a hard verification boundary.** `make test` and `make lint` prove the
queue, the gateway, path handling, signal handling and image UID hygiene with no
cluster, no GPU and no AWS account, in about a minute. KEDA actually scaling, a
machine pool provisioning a node, EFS, oauth-proxy and the GPU itself prove
nothing until there is a cluster at ~$2.04/hour. Only five items are
cluster-only for a failing assertion; ten have a laptop half.

**Seventeen invariants are load-bearing** — `docs/09-engineering-handoff.md`
§3. (Fourteen when this was written; F1 added the fifteenth, Q2 the sixteenth
and Q3 the seventeenth, which is what "changes an invariant" looks like in
practice.)
Thirteen of the items touch at least one, so "the risky ones get a second
reviewer" is not a useful filter. The filter that *is* useful: an item is
high-risk if it **changes** an invariant rather than merely working near one.
Three do — Q2, Q3 and F1.

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

Three items that are not in the README's list, that several later items
silently assume, and that are each worth doing on their own merits.

| ID | Change | Effort | Risk | Proven by |
|---|---|---|---|---|
| F1 | Worker resource sizing — **landed** | Small | **High** | A unit assertion that requests and limits are internally consistent and fit the target instance type — `scripts/unit-tests.sh` runs the real `scripts/lint.sh` against a fixture with the old shape |
| F2 | Versioned queue payload envelope — **landed** | Small | Low | `enterprise/test/check-40-envelope.py`: the reserved fields are on the wire with their defaults, an old-shape payload still completes, and an unknown field is not fatal — plus a lint check that the two copies of the envelope have not diverged |
| F3 | Test harness convention + manifest shape assertions | Small | Low | A deliberately broken manifest fails `make lint`; a new check is discovered without editing two places |

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
assertions. Eleven of the seventeen §3 invariants are properties of files rather
than of a running system — a missing toleration, a Service that regained a
port, a dropped Route annotation, a Containerfile that lost its `chgrp 0` block
— and the e2e suite structurally cannot see any of them. This is also what
turns the infra gate below from a request for vigilance into a check.

## The work items

Effort and risk are assigned here from the source, not inherited from the
README. Where they differ from the README's tags, the README is the one that
was wrong.

| ID | Change | Effort | Risk | Lane | Proven by |
|---|---|---|---|---|---|
| Q1 | Fair queueing across submitters — **landed** | Medium | Medium | Queue | `enterprise/test/check-50-fair-queue.py`: a whole batch queued by one submitter does not delay a single job from a second submitter behind it — round-robin by `queue_key`, the pop itself (`BLMOVE` into the per-worker processing list) unchanged. `bench-fair-enqueue.py` separately measures what one insert costs Redis at a realistic queue depth |
| Q2 | Phase breadcrumbs + retry only pre-execution deaths — **landed** | Medium | **High** | Queue | `enterprise/test/check-30-sigkill.py` and `check-35-retry-doors.py`: a worker killed before ComfyUI ever saw the workflow is requeued exactly once; a worker killed mid-execution gets one terminal `failed` naming the dead worker and is never requeued; the `phase` breadcrumb is durable *before* the `/prompt` POST returns, not after; and a job the user already cancelled is neither requeued by the reaper nor ever submitted by a worker that pops it |
| Q3 | Per-user output workspaces — **landed**, laptop half | Medium | **High** | Queue + cluster | `enterprise/test/check-60-user-workspaces.py`: two submitters land in two places, a hostile username is confined rather than mangled or escaped, and an anonymous submission still works and does not alias onto a real user — plus lint shapes for the directory mode. The arbitrary-UID half is on the cluster-day list below |
| Q6 | Estimated-wait metric | Small | Low | Queue | e2e on the gauge; the scaler half is I4 |
| Q4 | Showback report | Small | Low | Queue | e2e: GPU seconds attributed to the right user, with a bounded key set |
| Q5 | GPU-second quota breaker | Medium | Medium | Queue | e2e including quota exhausted and quota data missing (must fail open) |
| I1 | Schedule the warm window | Small | Low | Infra | New unit test on a pure helper; behaviour on cluster day |
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
- **I1 and I3 contradict each other.** Both want to own the GPU pool's
  min-replicas — I1 by cron during working hours, I3 by a placeholder pod
  holding a node — and any `setup.sh` re-run resets whatever cron did. Pick one
  mechanism before either branches. This is a design conflict, not a merge
  conflict.

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

## Three lanes

**Queue lane — sequential.** All six items edit `hub.py` and five of them also
edit `worker_agent.py`, whose key shapes must change together or not at all.
All are laptop-verifiable except Q3's arbitrary-UID half. The loop: write the
assertion that fails on HEAD, implement until it passes, run `make test && make
lint`, hand the merged tree on.

**Infra lane — parallel, with three exceptions.** Seven items touch
`enterprise/setup.sh` (559 lines), but they hit mostly disjoint regions and can
be worked in branches or worktrees. The exceptions must be sequenced: I2 before
S2 (both rewrite the image build and its call sites), I2 before its CI job, and
I1 with I3 only after the min-replicas ownership conflict above is settled.

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
| Invariant-changing items (F1, Q2, Q3) | A second reviewer who has read `docs/09` §3, checking specifically that the change survives a hard kill mid-job, two gateway replicas racing, and its dependency being unreachable |
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

## Not on this list

`docs/06-enterprise-architecture.md` ends with the things that are deliberately
*not* here — Redis HA, multi-GPU workers, interrupting a running sampler — and
the reasoning for each. Read it before adding any of them: in several cases the
omission is the decision. Two items here (Q2 and Q3) do reverse entries on that
list, and both say why.
