# ComfyUI on OpenShift

[![ci](https://github.com/redhat-et/comfyui-on-openshift/actions/workflows/ci.yaml/badge.svg)](https://github.com/redhat-et/comfyui-on-openshift/actions/workflows/ci.yaml)

A ComfyUI (PyTorch) inference backend on GPU nodes, built for **ROSA** — Red
Hat OpenShift Service on AWS — and portable to any OpenShift 4.x cluster you
already have.

ComfyUI is a single-user desktop application wearing a web UI. It has no
authentication, its custom-node system executes arbitrary Python by design, and
it runs on the most expensive idle resource in your cloud account. Put it in
front of a team and you own two problems at once: an unauthenticated
remote-code-execution endpoint, and a GPU per person because a GPU is
indivisible under Kubernetes.

**OpenShift already solves both, and this repo is the wiring.** Cluster SSO,
a queue that lets GPU workers appear and vanish, a machine pool that autoscales
to *zero nodes*, driver lifecycle, arbitrary-UID isolation, network policy,
audit logging, in-cluster image builds with no registry credential to rotate —
none of that is written here. It is the platform. What is written here is the
~8,700 lines that connect ComfyUI to it, and every place that connection turns
out to be subtle.

Two configurations share one cluster, one GPU operator, and one set of volumes:
a single-user pod for one person and one GPU, and a multi-user configuration
with a queue, cluster SSO, and a GPU pool that scales to zero. The queue and
gateway logic is covered by an end-to-end test suite that runs on your laptop
in about a minute — no cluster, no GPU, no AWS account (`make test`).

## What changes for the people using it

Almost nothing. This is stock ComfyUI — the upstream project at a pinned tag,
not a reimplementation and not a wrapper — so the loop a designer already has
survives intact:

| Their loop | On this platform |
|---|---|
| Load a template | Stock ComfyUI, the same workflow JSON. Identical. |
| Find errors and missing models | ComfyUI-Manager lists exactly what the workflow needs and you do not have. Identical. |
| Load the models | One click — into `/models`, a volume that outlives the cluster, pulled at datacenter bandwidth rather than over a home connection. Same action, better outcome. |
| Run a complicated node graph | Stock ComfyUI on an L4 24 GB. For most designers, a bigger card than the one under their desk. |
| Load the next template, repeat | Identical. |

Two things are genuinely different, and both are better known up front than
discovered. **Model downloads persist; runtime custom-*node* installs do not** —
Manager will tell you a node is missing, but making it stick means
`app/src/custom_nodes/` and a rebuild. And in the multi-user configuration **the
gateway is not the ComfyUI canvas**: authoring stays in ComfyUI, and the gateway
is where a finished workflow goes to run on a shared GPU.

The reason any of this is worth doing is where the time actually goes:

```mermaid
flowchart LR
    A["Load a template"] --> B["Find errors and<br/>missing models"]
    B --> C["Load the models<br/>one click, onto shared storage"]
    C --> D["Wire and fix<br/>the node graph"]
    D --> E["Run it<br/>~2 min"]
    E --> A

    classDef nogpu fill:#e8eefc,stroke:#5b7bc4,color:#12233f
    classDef gpu fill:#fde8e2,stroke:#d6552b,color:#4a1608
    class A,B,C,D nogpu
    class E gpu
```

Four of those five steps never touch a GPU. Setting a scenario up — finding a
template, chasing the models it needs, wiring and fixing the graph — takes far
longer than the inference, which is often a couple of minutes or less. A
designer needs a GPU for a small fraction of their working day, so ten designers
do not need ten GPUs. They need one pool, a queue, and a card that turns off in
between.

## How it fits together

One pod and one GPU for a single person. For a team, a FastAPI gateway on cheap
CPU nodes, Redis as the queue and progress bus, and GPU workers that are
unreachable by design and scale between 0 and N on queue depth:

```mermaid
flowchart LR
    B[Browsers] --> R["Route<br/>cluster SSO"]
    R --> G["Gateway<br/>CPU nodes, 2 replicas"]
    G -- jobs --> Q[("Redis<br/>queue + progress log")]
    Q -- jobs --> W["Workers 0..N<br/>agent ⇄ ComfyUI on loopback<br/>GPU nodes"]
    W -- progress --> Q
    Q -- replayable tail --> G
    W -- writes --> E[("EFS<br/>models + outputs")]
    E -. read-only .-> G
    K{{KEDA}} -. queue depth<br/>scales pods and nodes .-> W
```

![The gateway: paste a workflow, watch progress, collect the images](docs/images/gateway.png)

Nothing in that picture is a component this repo invented. The queue is Redis,
the scaling is KEDA and a machine pool, the login is the cluster's own identity
provider, and the thing doing the generating is stock ComfyUI.

## Which path are you on?

| You | Do this |
|---|---|
| No AWS account yet | `docs/01-aws-account.md` — ~20 minutes of browser work — then the quickstart below |
| Your own ComfyUI on a GPU, managed for you | The single-user quickstart below |
| A team sharing a GPU pool, with SSO and scale-to-zero | The multi-user quickstart below |
| Already have an OpenShift cluster | `PLATFORM=openshift` in `.env`, `oc login`, then `make gpu storage deploy` (or `make gpu storage enterprise`) — nothing in those steps is ROSA-specific |
| Just evaluating the code | `make test` runs the gateway and worker against a real Redis and a stub ComfyUI, locally |
| Taking this over from someone | `docs/09-engineering-handoff.md` |

## Why this platform, specifically

This repo will run on any OpenShift, but ROSA with hosted control planes is
where it is designed to feel best. Seven reasons, each of which is a file in
this repository rather than a claim.

### Cost — the platform can turn the GPU off, and turn the cluster off

- **Scale-to-zero means zero *nodes*, not zero pods.** KEDA watches Redis
  queue depth and drops the worker Deployment to 0 replicas; the ROSA machine
  pool autoscaler then reclaims the GPU node underneath it. Only the second
  layer saves money — an idle GPU node bills identically whether a pod is
  scheduled on it or not — and it is the layer most tutorials skip.
  (`enterprise/manifests/03-autoscale.yaml`)
```mermaid
flowchart LR
    Q[("Redis queue<br/>goes empty")] --> K{{"KEDA"}}
    K -->|"worker replicas to 0"| P["Pods gone<br/><b>saves nothing</b>"]
    P --> A{{"Machine pool<br/>autoscaler"}}
    A -->|"nothing scheduled,<br/>node reclaimed"| N["GPU node gone<br/><b>saves ~$0.98/hr</b>"]

    classDef nil fill:#e8eefc,stroke:#5b7bc4,color:#12233f
    classDef real fill:#fde8e2,stroke:#d6552b,color:#4a1608
    class P nil
    class N real
```

- **A ~15-minute rebuild makes teardown a habit instead of a heroic act.**
  HCP's flat $0.25/hour control-plane fee and fast build are what turn
  `make down` into a cron line. On a cluster that takes forty minutes to
  rebuild, nobody tears down and everybody overpays. (`docs/02-cost.md`)
- **The bill is a command, not a spreadsheet.** `make status` reads your live
  machine pools and prints the hourly burn — including the ROSA service fee
  that people forget also applies to the GPU node's vCPUs.
  (`scripts/06-status.sh`)
- **A budget alarm before the first dollar.** `make account` files the GPU
  quota request and arms an AWS budget alarm in the same step, because human
  discipline is exactly what an alarm exists to distrust.

### Utilization — one pool serves the team

- **A queue instead of a card per person.** A FastAPI gateway on cheap CPU
  nodes holds every browser session; workers pull jobs from a Redis list. Ten
  users stop meaning ten GPUs and start meaning a deeper queue.
- **Redis is the entire interface.** No browser ever holds a connection to a
  worker, so workers can vanish mid-shift with no connection state to repair.
  That property is what makes scale-to-zero possible at all.
- **Elastic to a ceiling you set.** One queued job asks for one worker, up to
  `MAX_GPU_WORKERS`. Burst rendering gets parallel cards; a quiet afternoon
  gets none.

### Security — the strongest control is architectural

- **The GPU pods are unreachable by construction.** Every worker binds ComfyUI
  to `127.0.0.1` and has no Service and no Route. This is not defence in depth,
  it is the primary control, and it removes ComfyUI's entire vulnerability
  class from the network. (`docs/04-exposing.md`)
```mermaid
flowchart LR
    B["Browsers"] --> R["Route<br/>edge/reencrypt TLS"]
    R --> P["oauth-proxy<br/>cluster SSO + SubjectAccessReview"]
    P --> G["Gateway<br/>accepts one workflow JSON,<br/>pushes it onto a list"]
    G -- "Redis is the only path" --> W["Workers<br/>ComfyUI bound to 127.0.0.1<br/>no Service, no Route"]

    classDef exposed fill:#fde8e2,stroke:#d6552b,color:#4a1608
    classDef sealed fill:#e8eefc,stroke:#5b7bc4,color:#12233f
    class B,R,P,G exposed
    class W sealed
```

Everything on the left is reachable and authenticated. The worker on the right
is reachable by nothing at all — which is why ComfyUI having no login of its own
stops being a problem rather than becoming one.

- **SSO you already own.** An `oauth-proxy` sidecar puts the cluster's own
  identity provider in front of the gateway, and rebinds the gateway to
  loopback so the login cannot be bypassed from inside the cluster either.
- **Authorization is a role, not a user database.** Access is a
  SubjectAccessReview against the namespace: grant with
  `oc adm policy add-role-to-user`, revoke by removing it, and read the whole
  history in the cluster audit log. Nothing to build, secure, or forget to
  secure.
- **Least privilege all the way down.** Redis carries a generated password and
  a NetworkPolicy admitting only the gateway and the workers; the S3 model role
  is read-only so a compromised pod cannot corrupt the canonical store; the
  gateway blocks path traversal on the output endpoint.

### Operations — the hard parts are somebody else's job

- **The control plane is SRE-managed**, under the joint Red Hat + AWS support
  model. You operate one namespace, not a cluster.
- **Nobody hand-installs a CUDA driver.** NFD labels the cards, the NVIDIA GPU
  Operator compiles and rolls the driver against the running RHCOS kernel, and
  a smoke test proves `nvidia-smi` before you deploy anything.
- **No registry credential to rotate.** Images build in-cluster into the
  internal registry, which sidesteps the ECR trap: a 12-hour pull token that
  works the day you create it and fails the first job of every morning after.
- **Termination is routine, and handled.** The worker traps SIGTERM and drains
  the running job; a TTL'd heartbeat plus a per-worker processing list means
  even an OOM kill surfaces a real failure rather than a progress bar that
  never moves.

### Time to value

- **Four commands to a working GPU cluster** — `make cluster gpu storage deploy`,
  roughly 55 minutes, mostly unattended. Every script is idempotent;
  re-running skips what is already done.
- **Preflight catches the multi-day blocker first.** A new AWS account has a
  GPU quota of exactly zero and the increase takes days. `make preflight` is
  read-only and tells you before, not after, a twenty-minute cluster build.
- **Evaluate it with no cluster, no GPU, and no AWS account.** `make test`
  runs the real gateway and the real worker agent against a real Redis and a
  stub ComfyUI, in about a minute.
- **The eight-step EFS setup is scripted.** RWX storage on an STS cluster is
  eight things that must all be right simultaneously; getting six right yields
  a PVC stuck in `Pending` with no useful event. That is why it is a script.
- **The failure modes are written down** — eight documents covering what
  actually goes wrong, including a dedicated page on why the force-delete
  everyone reaches for causes the symptom it claims to cure.

### Correctness — the bugs that only appear in a cluster, already fixed

- **Redis Streams, not pub/sub.** `XREAD` from `0-0` replays history and then
  tails live in one call, so a browser that opens its socket a beat after the
  POST — the common case, not the edge case — loses nothing.
- **Every event filtered by `prompt_id`.** One ComfyUI multiplexes all prompts
  onto one socket; without the filter, another job's terminal event ends yours
  and reports success on work still running.
- **Backpressure instead of a silent Redis OOM.** The gateway rejects
  submissions past a configurable depth and Redis runs `noeviction` — the
  default would quietly evict queued jobs, which looks exactly like work
  disappearing at random.
- **Long jobs keep their connection.** HAProxy's 30-second default kills a
  generation mid-render and reads like an application bug. Every Route carries
  a four-hour timeout.

### Portability and oversight

- **Not a ROSA lock-in.** `PLATFORM=openshift`, `oc login`, and the same GPU,
  storage and deploy steps run against any OpenShift 4.x cluster — on-prem,
  bare metal, vSphere.
- **Metrics the cluster already knows how to graph.** The gateway exports
  `comfy_queue_depth` and `comfy_workers_registered`, and `setup.sh` applies a
  ServiceMonitor so user-workload monitoring can alert on a wedged pool before
  a human notices.
- **Every job has a name attached.** The gateway stamps the authenticated
  username onto job state, so when the GPU bill asks whose job this was, there
  is an answer.
- **Reproducible by policy.** ComfyUI is pinned to a tag both images build,
  custom nodes are baked in from `app/src/custom_nodes/`, and runtime
  installers are off by default — so the image that passed review is the image
  that renders.

## Single user — one pod, one GPU

```bash
cp .env.example .env && $EDITOR .env

make tools        # aws, rosa, oc, jq
make preflight    # checks everything, changes nothing
make account      # quota requests + budget alarm   <-- run this first, it has a lead time
make cluster      # ROSA HCP + GPU machine pool     (~20 min)
make gpu          # NFD + NVIDIA GPU Operator       (~20 min)
make storage      # model and output volumes
make deploy       # build and run ComfyUI           (~15 min)
make forward      # http://localhost:8188
```

## Multi user — queue, SSO, GPU pool that scales to zero

Same cluster, different last step:

```bash
# STORAGE_MODE=rwx in .env — the gateway and the workers share a volume
make cluster gpu storage
make enterprise                        # one script does the rest
```

The shape of it is in **How it fits together** above.

`enterprise/README.md` to run it, `docs/06-enterprise-architecture.md` for why
it is shaped that way.

<details>
<summary><b>What happens when things fail</b> — every failure mode, what the system does, and what the user sees</summary>

<br>

Most of this repository's design is failure handling, so it is worth being able
to read it in one place. Nothing below is aspirational: each row is a code path
you can find, and most are covered by an assertion in `enterprise/test/`.

#### Out of memory — three different failures that people call one thing

ComfyUI makes it easy to exceed memory, and the three ways it happens are not
handled alike. Knowing which one you hit is most of the diagnosis.

| | What actually happens | What the user sees |
|---|---|---|
| **VRAM / CUDA OOM** — the common one: a resolution, batch size or model stack that does not fit the card | ComfyUI catches it and emits `execution_error`. The agent (`worker_agent.py:279`) turns that into a terminal `failed` carrying ComfyUI's own exception message. The worker stays healthy and takes the next job. | `failed: Allocation on device ...` — the real message, not a generic error. Nothing is retried, because the same workflow would fail the same way on any card. |
| **Host RAM OOM** — a large checkpoint load, a VAE decode, a wide batch | The container hits its own memory limit and the kernel kills ComfyUI inside that cgroup. `start.sh` waits on both children, so the pod exits rather than limping on with a dead ComfyUI and a live agent claiming jobs it cannot run. | The job is stranded, then failed by the gateway's reaper (below) once the worker's heartbeat lapses. At the queue level this is still indistinguishable from infrastructure death — but the pod is not: it terminates as `OOMKilled` and `oc describe pod` names the reason. |
| **Node-level pressure** — eviction rather than a container kill | The kubelet evicts with a grace period, so SIGTERM arrives first and the drain below applies. A GPU pod is Guaranteed QoS, so it is the last thing evicted, not the first. | Usually nothing: the job finishes before the pod goes. |

That the second row says `OOMKilled` and not "the node decided" is a property of
the manifest, not of luck: the GPU pod's memory limit is set to something the
node can actually give it, so the container reaches its own ceiling first. A
limit larger than the node — the shape this repo shipped with, `24Gi` on a
16 GiB `g6.xlarge` — can never be reached, which turns the second row into the
third and costs you the attribution. `scripts/lint.sh` fails a GPU pod that
drifts back to it.

```mermaid
flowchart TD
    X{"Out of memory"} --> V["<b>VRAM</b> — the card is full<br/>resolution, batch, model stack"]
    X --> H["<b>Host RAM</b> — the node is full<br/>checkpoint load, VAE decode"]

    V --> V1["ComfyUI catches it,<br/>emits execution_error"]
    V1 --> V2["failed, carrying ComfyUI's<br/>own message.<br/>Worker healthy, takes the next job."]

    H --> H1["Container hits its own limit,<br/>kernel kills ComfyUI"]
    H1 --> H2["Pod exits OOMKilled —<br/>oc describe pod names the reason"]
    H2 --> H3["Job stranded, then failed by the<br/>gateway's reaper once the<br/>heartbeat lapses"]

    classDef clean fill:#e8eefc,stroke:#5b7bc4,color:#12233f
    classDef rough fill:#fde8e2,stroke:#d6552b,color:#4a1608
    class V,V1,V2 clean
    class H,H1,H2,H3 rough
```

#### Worker death

| Failure | Handling | User-visible result |
|---|---|---|
| **Graceful termination** — scale-to-zero, a node drain, a rolling deploy, a spot interruption notice | The agent traps SIGTERM, stops accepting new work, and **finishes the job in flight** before exiting (`worker_agent.py`, note 4). Termination is routine on a pool that scales to zero, so this is the common path, not the exceptional one. | The generation completes normally. Asserted by the e2e suite. |
| **Hard kill** — SIGKILL, kernel OOM, node death | The agent parks each job in a per-worker processing list with `BLMOVE` and holds a TTL'd heartbeat (note 5). When the heartbeat lapses, the gateway's reaper fails the stranded job **naming the dead worker**. | `failed: worker comfy-worker-xxxx died` — loudly, rather than a progress bar that never moves. Deliberately failed and not requeued: a workflow that OOM-killed one worker would OOM-kill the next one too, at GPU prices. Asserted by the e2e suite. |
| **ComfyUI wedges or dies mid-job** | The agent's `recv()` is bounded and each job carries a deadline (`JOB_TIMEOUT`, 1800s). On every timeout it re-checks `/history` in case a completion event was simply missed. | The job fails with a reason instead of the pod sitting `Running` and `Ready` while silently consuming nothing — which is worse than a crash, because KEDA sees a growing queue and adds more workers beside the dead one. |

#### The job itself

| Failure | Handling | User-visible result |
|---|---|---|
| **Invalid workflow** — wrong format, missing input, unknown node | ComfyUI rejects it at submit and the agent propagates the rejection verbatim. | `failed: required input is missing: ckpt_name` rather than `failed`. The difference is most of the support burden. |
| **Job runs too long** | The per-job deadline fires. | `failed: job exceeded 1800s`. Raise `JOB_TIMEOUT` if your workflows legitimately run longer. |
| **Cancel** | Cooperative: a queued job is removed, a running one is asked to stop between events. Truly interrupting a running sampler would need ComfyUI's `/interrupt`, and the workers are not reachable from the gateway by design. | Queued jobs stop immediately; running ones stop at the next event boundary. |

#### The connection

| Failure | Handling | User-visible result |
|---|---|---|
| **The browser opens its WebSocket after the job already started** | Progress lives in a Redis Stream, not pub/sub. `XREAD` from `0-0` replays the whole history and then tails live in one call. | Nothing is lost. This is the common case — the POST and the socket open are two separate round trips — not an edge case. |
| **Laptop sleeps, network drops, gateway pod rolls** | Same mechanism: reconnecting replays identically from the beginning. A PodDisruptionBudget keeps one gateway serving through drains and upgrades so both replicas cannot go at once. | The progress bar picks up where it was. |
| **A long generation outlives the router's idle timeout** | Every Route carries both `haproxy.router.openshift.io/timeout: 4h` and `timeout-tunnel` — on edge and reencrypt Routes only the tunnel timeout governs the upgraded WebSocket. | Long jobs keep their connection. Without both annotations they drop at a fixed point every time, which reads exactly like an application bug. |

#### The system

| Failure | Handling | User-visible result |
|---|---|---|
| **Queue grows faster than the pool drains** | The gateway refuses submissions past `MAX_QUEUE_DEPTH`, and Redis runs `maxmemory-policy noeviction`. | An explicit rejection. The default eviction policy would silently drop queued jobs instead, which presents as work disappearing at random. |
| **Redis restarts** | AOF persistence. | Queued and in-flight state survives a pod restart. It does not survive a zone outage — Redis is a single instance, deliberately, at one-GPU scale. |
| **A dead pod will not release its volume** | `scripts/08-unstick-storage.sh --repair`, which confirms the EC2 instance is genuinely terminated before force-detaching. | Repairable. Do **not** reach for `oc delete pod --force --grace-period=0`: it strands the volume permanently instead of freeing it. `docs/08-stuck-volumes.md`. |
| **No GPU capacity, or no quota** | The worker pod stays `Pending`. `make preflight` distinguishes the two — quota is a multi-day fix, capacity is a region or instance-family change. | `docs/05-troubleshooting.md`. |
| **First job after an idle period** | Not a failure, but it looks like one: a node has to be provisioned and a ~10 GB image pulled. | 8–17 minutes, and the gateway says so rather than leaving a bar that has not moved. Removed entirely by one warm worker — see "Where this loses". |
| **Someone requests a path outside the output directory** | Resolved and compared against the output root before anything is served. | `/outputs/../../etc/passwd` does not resolve. Asserted by the e2e suite. |

</details>

## One GPU, ten people

The sharpest number here, using this repo's own rates.

A `g6.xlarge` worker node is **$0.976/hour all-in** — $0.805 to AWS for the
card, $0.171 to Red Hat for its vCPUs. A pod per person means a card per
person, because a GPU is indivisible under Kubernetes:

| Ten users | GPU line, per month |
|---|---:|
| A pod per person, running | ~$7,100 |
| One autoscaled pool, ~4 GPU-hours/day of real generation | ~$120 |

That is not a discount, it is a structural consequence of separating the thing
users touch from the thing that costs money. The ratio moves with your
utilization; the direction does not.

## The numbers, briefly

The smallest useful cluster — one ComfyUI pod on an L4, us-east-2, on-demand:

| State | $/hour | Getting back |
|---|---:|---|
| Running | ~2.04 | — |
| GPU parked (`make park`) | ~1.06 | ~5 min |
| Cluster torn down (`make down`) | ~0.05 | ~15 min |

```mermaid
stateDiagram-v2
    Running: Running · ~$2.04/hr
    Parked: GPU parked · ~$1.06/hr
    Down: Cluster deleted · ~$0.05/hr
    Gone: Everything deleted · $0

    [*] --> Running: make up
    Running --> Parked: make park
    Parked --> Running: back in ~5 min
    Running --> Down: make down
    Down --> Running: back in ~15 min
    Down --> Gone: make destroy
```

| Habit | Monthly |
|---|---:|
| Left running 24/7 | ~$1,490 |
| Weekdays 9–6, `make park` nightly | ~$800 |
| Weekdays 9–6, `make down` nightly | ~$370 |
| Up one day a week | ~$85 |

**A single cron line is a 75% cut.** Not a migration and not a rewrite — a
scheduled `make down` and a Monday-morning `make up`, with models on EFS or S3
so the rebuild costs nothing but time you were asleep for. Three things to do
about cost:

1. **`make account` first.** A new AWS account has a GPU quota of exactly
   zero, and the increase is the one step with a lead time measured in days.
2. **Set `BUDGET_ALERT_EMAIL` in `.env`.** The budget alarm is free and it is
   the guardrail between you and a four-figure surprise.
3. **Park at lunch, down overnight.** `make park` and `make down` — and
   `docs/02-cost.md` has crontab lines that make the habit automatic.

The full accounting — every fee, the monthly patterns, and where money leaks
— is in `docs/02-cost.md`.

## Where this loses, and what to do about it

Both boundaries are narrow, both are priced, and both have a flag that changes
them.

**Interactive iteration against a cold pool.** The first job after an idle
period is 8–17 minutes: provision a node, pull a ~10 GB image, initialise CUDA,
load the checkpoint. For a designer adjusting a prompt every four minutes,
that lands in the middle of a creative loop.

```mermaid
gantt
    dateFormat X
    axisFormat %M min
    todayMarker off
    section Cold pool
    Provision a GPU node        :0, 300
    Pull the ~10 GB image       :300, 780
    CUDA init, custom node scan :780, 900
    Load the checkpoint         :900, 1020
    Generate                    :crit, 1020, 1140
    section Warm worker
    Generate                    :crit, 0, 120
```

The fix is a warm worker, and it is one variable:

```bash
SCALE_TO_ZERO=false     # in .env — pins one worker; KEDA still scales 1..N above it
```

That removes the cold start entirely — the first job of the day starts in
seconds, and the pool still bursts to `MAX_GPU_WORKERS` under load. It costs
one GPU node for as long as you leave it up: **~$195/month if you pin it
weekdays 9–6 and `make park` or `make down` at night**, which is less than one
designer waiting fifteen minutes every morning.

Worth being precise about what "cold" costs, because the two layers are
separable:

| What is cold | Cost of the first job | Removed by |
|---|---:|---|
| No node — provision + ~10 GB image pull | 6–13 min | pinning the machine pool at 1 during work hours |
| Node warm, no pod — CUDA init + checkpoint load | 1.5–4 min | a warm worker pod (`SCALE_TO_ZERO=false`) |
| Warm worker | seconds | — |

So the honest shape for a design team is not "scale-to-zero or don't". It is:
**one warm card during working hours, zero at night and at weekends.** The
cold start then happens at most once, before anyone is at their desk, and
scale-to-zero still does its job for the other fourteen hours a day. See
"Ideas worth doing next" for scheduling that automatically.

Scale-to-zero earns its keep unmodified on bursty, batch and out-of-hours work,
where a ten-minute wait for a job nobody is watching costs nothing.

**One person, one GPU, nothing to share.** If you are not exercising OpenShift
semantics and there is no team to serve, a plain EC2 instance with podman is
$0.81/hour and this is a heavier answer than the question. `docs/02-cost.md`
says so itself, with a comparison table.

## Ideas worth doing next

Ordered by payoff per unit of work. Nothing here is implemented; each is a
concrete next change, not a wish. `docs/10-roadmap.md` is the worked version —
it adds three foundation items this list does not have (worker resource
sizing, a versioned queue payload, and test-harness discovery), corrects the
effort tags below where a full read of the source disagreed with them, and
says which two of these should not be done at all.

1. **Schedule the warm window instead of pinning it.** A pair of cron lines —
   `rosa edit machinepool gpu --min-replicas 1` at 08:30 and `--min-replicas 0`
   at 18:30 on weekdays, with the KEDA `minReplicaCount` following it — gives
   the cold-start-free morning above without paying for a card overnight. This
   is the single highest-value change on the list for a design team, and it is
   two cron entries and one patch. *(Small.)*
2. **Shrink the cold start itself.** The ~10 GB image pull dominates node
   warm-up. Split the worker image so the CUDA + torch layers are a stable base
   that rarely changes, and keep a low-priority placeholder pod on the GPU pool
   so the autoscaler holds one warm node without a real job occupying the card.
   *(Medium — the placeholder/priority-class pattern is standard cluster
   autoscaler practice.)*
3. **Stage models on the node's local NVMe.** `g6` instances have instance
   store. An init container that copies the active checkpoint from EFS to local
   disk turns every subsequent load from an NFS read into a local read, which
   is the difference EFS costs you today. *(Medium.)*
4. **Narrow retry, and spot separately.** These looked like one item and are
   not. Blanket retry re-runs the poison pill: a host-RAM OOM kills the pod and
   is indistinguishable at the queue level from a node reclaim, so "retry on
   worker death" retries the workflow that will kill the next worker too. Retry
   only jobs that died *before* ComfyUI ever saw the workflow, and add phase
   breadcrumbs so the rest are at least diagnosable. Spot does not need retry at
   all: an interruption gives two minutes of notice and the existing SIGTERM
   drain finishes anything that fits in them — its real trade is that longer
   generations are lost. *(Medium; see `docs/10-roadmap.md` before starting.)*
5. **NVIDIA time-slicing — listed here, and not recommended.** The device
   plugin can advertise several replicas of one card, but time-slicing provides
   **no memory isolation**: co-resident workflows share the full 24 GB and their
   peak VRAM sums. ComfyUI exceeds VRAM easily on a single tenant, so this turns
   a deterministic per-workflow failure into a non-deterministic one where the
   victim is whichever job allocates second. MIG partitions memory properly and
   is not available on L4. *(Revisit only on MIG-capable hardware.)*
6. **Per-user output workspaces.** The gateway already records the
   authenticated user on each job; threading that into the output path is the
   remaining half. Gets you per-user galleries and makes the next item trivial.
   *(Small.)*
7. **Showback from the data you already collect.** Job attribution plus GPU
   seconds is a monthly "who spent the card" report. In most organisations this
   changes behaviour faster than any technical control. *(Small.)*
8. **Fair queueing.** The pop is a single-list `BLMOVE`, so one person's
   overnight batch of two hundred jobs starves the interactive users.
   Round-robin the pop across submitters instead of ranking them: it fixes the
   starvation without inventing a priority claim that a caller could simply
   assert about itself. *(Medium — it moves the pop, the depth gate and the
   KEDA trigger together, not just `worker_agent.py`.)*
9. **Scale on queue *wait*, not queue depth.** Depth is a proxy; what a user
   feels is time-to-first-pixel. Export an estimated wait from the gateway and
   point KEDA's Prometheus scaler at it. *(Medium.)*
10. **A model lockfile.** `models.lock` next to `COMFYUI_REF`, enforced by the
    S3 sync job, so an image tag and a model set pin together and a workflow
    that rendered last quarter still renders. Reject anything that is not
    `.safetensors` while you are there — `.ckpt` files are Python pickles and
    loading one executes whatever is inside it. *(Medium.)*
11. **A cost circuit breaker in the gateway.** Read month-to-date spend from
    AWS Budgets and refuse new submissions past a threshold, or give each user
    a GPU-second quota. The budget alarm currently emails you *after* the
    money is gone. *(Medium.)*
12. **Build the images with OpenShift Pipelines.** Bumping `COMFYUI_REF`
    becomes a pipeline run with a signed output rather than a laptop running
    `setup.sh`. *(Medium, and the right move once more than one person owns
    this.)*

`docs/10-roadmap.md` turns this list into a work plan — what each item
touches, what proves it, what order they can safely land in, and which of them
cannot be finished without a real cluster. `docs/06-enterprise-architecture.md`
has the complementary list: what is deliberately *not* here and why, including
Redis HA, multi-GPU workers, and interrupting a running sampler.

## What you actually operate

Worth being concrete about, because it is the difference between running this
and running a Kubernetes cluster:

```mermaid
flowchart TB
    subgraph theirs["Somebody else's pager"]
        C["API server, etcd, scheduler,<br/>control-plane upgrades"]
        D["NVIDIA driver, compiled against<br/>the running RHCOS kernel"]
        N["Node provisioning,<br/>reclaim and replacement"]
        T["Route TLS, cluster SSO,<br/>certificate rotation"]
    end
    subgraph yours["Yours"]
        G["A FastAPI process"]
        A["A Python agent"]
        R["One Redis"]
        B["The bill"]
    end

    classDef theirs fill:#e8eefc,stroke:#5b7bc4,color:#12233f
    classDef yours fill:#fde8e2,stroke:#d6552b,color:#4a1608
    class C,D,N,T theirs
    class G,A,R,B yours
```

That division is why this repository is around nine thousand lines and not a
distributed system. When you are deciding whether to add something, the first
question is whether OpenShift already does it — because in this problem domain
it usually does, and the version you would write is the version nobody
maintains. `docs/09-engineering-handoff.md` is the long form, for whoever picks
this up next.

## What is in here

```
.env.example              all configuration, one file
Makefile                  one target per step
scripts/
  00-tools.sh             install aws / rosa / oc / jq
  00-preflight.sh         read-only readiness check
  01-bootstrap-account.sh quota requests, budget alarm
  02-cluster.sh           IAM roles, VPC, ROSA HCP cluster, GPU pool
  03-gpu-operators.sh     NFD + NVIDIA GPU Operator + nvidia-smi smoke test
  04-storage.sh           gp3 (default) or EFS RWX
  05-deploy.sh            in-cluster build + deploy
  06-status.sh            what is running and the hourly burn
  07-login.sh             how to reach the cluster
  08-unstick-storage.sh   repair a volume a dead pod never released
  09-s3-models.sh         S3 bucket + IAM role so models outlive everything
  10-push-models.sh       rsync local models into the cluster
  99-teardown.sh          park | cluster | all
  lint.sh                 everything CI checks, runnable locally
  unit-tests.sh           the parsing edge cases, pinned
  ci-smoke-comfyui.sh     real ComfyUI on CPU proving the model-path contract
manifests/base/           single-user Deployment and Service, kustomize
app/
  Containerfile           OpenShift-compatible ComfyUI image
  src/                    >>> your code goes here <<<
enterprise/               the multi-user configuration
  setup.sh                one script: Redis, images, KEDA, SSO, route
  teardown.sh
  gateway/                FastAPI hub — queue jobs, stream progress, serve images
  worker/                 ComfyUI + Redis agent, bound to loopback
  manifests/
  test/                   the e2e suite — real Redis, stub ComfyUI, no cluster
docs/
  01-aws-account.md       the browser-only steps
  02-cost.md              the numbers, and how to keep them down
  03-storage.md           gp3 vs EFS vs S3, and why models are the hard part
  04-exposing.md          how to let other people reach it, safely
  05-troubleshooting.md   the failures you will actually hit
  06-enterprise-architecture.md   hub and spoke, Streams, scale-to-zero economics
  07-design-review.md     what changed from the original design doc, and why
  08-stuck-volumes.md     when a dead pod will not release the volume
  09-engineering-handoff.md  taking ownership: invariants, runbook, open items
  10-roadmap.md           the ideas below, as a work plan with lanes and gates
.github/workflows/ci.yaml lint + the e2e suite, on every PR
```

Scripts are idempotent. Re-running any of them is safe and skips what is
already done.

## Proof, not promises

Four CI jobs run on every pull request, and the local `make` targets are the
same ones CI invokes, so local and CI checks cannot drift.

- **End-to-end with real components.** The real gateway and real worker agent
  against a real Redis and a stub ComfyUI. It asserts that a late subscriber
  loses nothing, a reconnect replays identically, SIGTERM drains rather than
  drops, and a SIGKILLed worker's job fails loudly with the dead worker named.
- **Real ComfyUI, on CPU.** The pinned tag boots with the images' own path
  flags and asserts that checkpoints are visible and custom nodes load.
- **The arbitrary-UID trap, without a cluster.** The gateway image is run as
  UID 1000670000 with GID 0, exactly as `restricted-v2` will run it.
- **Lint.** shellcheck, YAML, Python, and pinned parsing edge cases.

The file-level half of those guarantees is now **mechanically enforced rather
than reviewed**: `scripts/lint.sh` fails the build on a worker that lost its GPU
toleration, a Route that lost `timeout-tunnel`, a Service that regained the
gateway's own port, a Containerfile that lost its `chgrp 0` block, or a GPU pod
whose memory limit no longer fits the node. The end-to-end suite runs no cluster
and reads no manifest, so it can see none of them.

`docs/07-design-review.md` is the most useful artifact here: a written account
of every claim the original design's manifests did not implement and every line
of its Python that would not have run. It is the list of things you would
otherwise have discovered at 2am.

## Configuration

Everything lives in `.env`. The ones you are most likely to change:

| Variable | Default | Notes |
|---|---|---|
| `PLATFORM` | `rosa` | `openshift` to use a cluster you already have |
| `AWS_REGION` | `us-east-2` | good G-family capacity, cheaper than us-east-1 |
| `GPU_INSTANCE_TYPE` | `g6.xlarge` | L4 24 GB, ~$0.80/hr — fits SDXL comfortably |
| `STORAGE_MODE` | `rwo` | `rwx` for EFS shared across pods — see docs/03-storage.md |
| `COMFYUI_IMAGE` | empty | empty means build in-cluster from `app/Containerfile` |
| `COMFYUI_REF` | `v0.32.0` | ComfyUI tag both images build — pinning is deliberate |
| `SCALE_TO_ZERO` | `true` | `false` pins one warm worker — see "Where this loses" |
| `ENABLE_MANAGER` | `false` | **Turn this on for the single-user path.** It is what keeps the familiar loop intact: load a workflow, Manager lists every model you are missing, one click puts them on the persistent volume. Leave it off for the shared pool — there it hands every UI user code execution on a node with cloud credentials, and nothing it writes survives the node being reclaimed. `app/Containerfile` has the full reasoning. |
| `BUDGET_ALERT_EMAIL` | empty | **set this** |

## Three things that will bite you

**The GPU node is tainted** `nvidia.com/gpu=true:NoSchedule`. Anything you
deploy onto it needs the matching toleration. The manifests here have it; your
own pods will not.

**OpenShift runs your container as an arbitrary UID**, not the one in your
Dockerfile. Every path the process writes to must be a mounted volume or be
group-writable and group-root-owned at build time. This is the single most
common reason an image that works under `podman run` crash-loops here — see the
bottom third of `app/Containerfile` for the fix.

**First GPU Operator run takes 10–20 minutes.** It compiles a driver against
the running RHCOS kernel and pulls multi-gigabyte images. It is not hung.

## Sources

- [Set up to use ROSA](https://docs.aws.amazon.com/rosa/latest/userguide/set-up.html)
- [Create a ROSA HCP cluster using the ROSA CLI](https://docs.aws.amazon.com/rosa/latest/userguide/getting-started-hcp.html)
- [ROSA pricing](https://aws.amazon.com/rosa/pricing/)
- [ROSA endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/rosa.html)
- [rosa create network](https://access.redhat.com/articles/7096266)
- [ROSA with NVIDIA GPU workloads](https://cloud.redhat.com/experts/rosa/gpu/)
