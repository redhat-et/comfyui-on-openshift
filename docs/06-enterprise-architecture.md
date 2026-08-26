# Multi-user architecture

Design rationale for the `enterprise/` configuration. No commands here — those
are in `enterprise/README.md`, and there is one of them.

## The problem this solves

ComfyUI is a single-player application wearing a web UI. Its server holds
per-client state and streams progress over a WebSocket keyed to a `clientId`
the browser picks. Put ten people in front of one instance and they share one
queue, one set of preview frames, and one progress bar — user A watches user B's
generation, and prompts land in an order nobody chose.

The instinct is to give each user their own pod. That fails on economics before
it fails on anything else: a GPU is indivisible under Kubernetes, so ten users
means ten GPUs, idle most of the day, at roughly $0.80/hour each.

## Hub and spoke

Separate the thing users touch from the thing that costs money.

```mermaid
flowchart TB
    subgraph browsers[" "]
        U1[Browser]
        U2[Browser]
        U3[Browser]
    end

    R[["OpenShift Route<br/>edge/reencrypt TLS"]]
    P["oauth-proxy sidecar<br/>cluster SSO + SAR"]

    subgraph gw["Gateway · CPU nodes · 2 replicas"]
        G["FastAPI<br/>queue jobs, tail streams,<br/>serve finished images"]
    end

    subgraph rd["Redis · the entire interface"]
        Q["List comfy:queue<br/>(work)"]
        S["Streams comfy:job:*:events<br/>(progress, replayable)"]
    end

    subgraph gpu["Workers · GPU nodes · 0..N"]
        W1["agent ⇄ ComfyUI<br/>127.0.0.1 only"]
        W2["agent ⇄ ComfyUI<br/>127.0.0.1 only"]
    end

    E[("EFS · models + outputs<br/>ReadWriteMany")]
    K{{"KEDA<br/>watches queue depth"}}

    U1 & U2 & U3 --> R --> P --> G
    G -- LPUSH --> Q
    Q -- BRPOP --> W1 & W2
    W1 & W2 -- XADD --> S
    S -- "XREAD BLOCK" --> G
    W1 & W2 -- write --> E
    E -- "read-only" --> G
    Q -.-> K -.->|"sets replicas 0..N"| gpu
```

**The gateway** runs on cheap CPU nodes. It accepts a workflow, puts it on a
Redis list, and holds the browser's WebSocket. It never talks to a worker.

**The workers** run on GPU nodes. Each is one pod containing ComfyUI bound to
`127.0.0.1` and an agent process. The agent pops a job, drives the local
ComfyUI, and writes progress back to Redis. It is the only route in or out.

**Redis** is the whole interface between the two. That is the point: with no
direct connection between a browser and a worker, workers can appear and vanish
freely, which is what makes scale-to-zero possible at all.

## Why the workers are unreachable

Every GPU pod binds ComfyUI to loopback and has no Service and no Route.

This is not defence in depth, it is the primary control. ComfyUI has no
authentication and its custom-node system executes arbitrary Python by design.
A reachable ComfyUI pod is an unauthenticated remote code execution endpoint on
a node that holds an instance role, sits inside your VPC, and has the shared
model volume mounted writable.

The gateway, by contrast, accepts exactly one thing — a workflow JSON object —
and does nothing with it but push it onto a list. Its attack surface is a JSON
parser.

If you change `--listen 127.0.0.1` to `0.0.0.0` in `enterprise/worker/start.sh`,
you have removed the security model, not just a network binding.

## Why Redis Streams, not pub/sub

The obvious implementation publishes progress to a pub/sub channel and has the
gateway subscribe. It works in a demo and drops messages in production, because
pub/sub delivers only to subscribers connected at the moment of publication, and
there are two ordinary situations where the gateway is not:

- The browser POSTs the job and *then* opens the WebSocket. Those are separate
  round trips. A worker that picks the job up in between publishes into an empty
  room.
- The WebSocket drops — a laptop sleeps, a gateway pod is rolled during a
  deploy — and reconnects. Everything that happened meanwhile is gone.

Both produce the same user-visible symptom: a progress bar that never moves on a
job that is running perfectly well. It is intermittent, it depends on timing,
and it is horrible to debug.

A Redis Stream is an append-only log with an ID per entry. `XREAD` from `0-0`
replays the whole history and then blocks for new entries — replay and live tail
through one call, with no window between them where an event can be lost. A
reconnecting browser gets the full history and catches up. Streams carry a TTL
so finished jobs expire on their own.

The cost is a few megabytes of Redis and one extra concept. It buys away an
entire category of bug.

## Scale to zero, honestly

Two independent layers, and only one of them saves money.

**Pods.** KEDA watches `LLEN comfy:queue` and sets the worker Deployment's
replica count, including to zero. On its own this saves nothing: an idle GPU
*node* bills identically whether a pod is scheduled on it or not.

**Nodes.** The GPU machine pool is autoscaled with `--min-replicas 0`. When the
last worker pod goes away the node is reclaimed; when a worker pod goes Pending
for want of a GPU, a node is provisioned. This is where the ~$0.98/hour goes.

ROSA permits zero on this pool specifically because it is tainted and is not the
cluster's only untainted pool — ROSA requires one untainted pool with at least
two replicas, which the base pool provides. `enterprise/setup.sh` attempts zero
and falls back to one with a loud warning if the API refuses.

### The cold start is real

From a fully idle cluster, the first job waits for:

| | |
|---|---:|
| Machine pool provisions an EC2 instance and it joins the cluster | 3–5 min |
| Pull a ~10 GB CUDA + torch + ComfyUI image onto a fresh node | 3–8 min |
| CUDA init, custom node scan, ComfyUI ready | 1–2 min |
| Checkpoint load into VRAM | 0.5–2 min |
| **First image** | **8–17 min** |

Subsequent jobs on a warm worker start in seconds.

`cooldownPeriod: 600` in the ScaledObject is the number that trades these off:
too short and someone taking a coffee break pays the cold start repeatedly, too
long and you buy idle GPU. Ten minutes is a starting point, not a truth. It is
the first thing to tune.

**When scale-to-zero is the wrong choice:** interactive iteration, where a
designer runs a prompt, looks at it, adjusts, and runs again over an afternoon.
There, the cold start lands in the middle of a creative loop and the honest
configuration is a warm worker during work hours and `make down` at night — a
cron on the machine pool, not KEDA. Scale-to-zero earns its keep for bursty,
batch, and out-of-hours work.

## Storage: why this configuration requires EFS

The gateway serves finished images. The workers produce them. Those pods are on
different nodes by construction — one on CPU, one on GPU.

A gp3 volume is `ReadWriteOnce`: one node at a time, full stop. So the
multi-user configuration needs `ReadWriteMany`, which on AWS means EFS.
`enterprise/setup.sh` refuses to run without `STORAGE_MODE=rwx` rather than
letting you discover this as a pod stuck in `ContainerCreating`.

EFS costs roughly 4× gp3 per GB and is slower for large sequential reads, which
matters when a checkpoint is 7 GB. Two things make that acceptable here: the
model is read once per worker start rather than per job, and the volume outlives
the cluster, so `make down` no longer means re-downloading a model library.

`docs/03-storage.md` has the full comparison including the S3-sync middle path.

## Authentication

`AUTH_MODE=oauth` puts an `oauth-proxy` sidecar in the gateway pod and rebinds
the gateway itself to loopback, so there is no port on the pod that bypasses the
login. Users authenticate against whatever identity provider the cluster is
wired to; authorisation is a SubjectAccessReview — you must be able to `get` the
application namespace.

Grant access by granting a role, revoke it by removing one, and the access shows
up in the cluster audit log. No separate user database.

`AUTH_MODE=none` exists for a solo test cluster. It publishes the gateway with
no login. The GPU pods stay unreachable either way, so this is not catastrophic,
but anyone who finds the hostname can spend your GPU budget — and Route
hostnames appear in certificate transparency logs within minutes.

## ComfyUI-Manager and auto-downloaders

Off by default (`ENABLE_MANAGER=false`), for two reasons.

**Security.** Manager installs arbitrary Python from the internet and runs it in
the worker pod. In a single-user sandbox that is a convenience. In a shared
cluster it hands every user with UI access code execution on a node with cloud
credentials — which undoes the isolation the rest of this design is built
around.

**It does not survive.** The worker pool scales to zero. Anything Manager writes
to the container filesystem is gone when the node is reclaimed. The durable path
for custom nodes is `app/src/custom_nodes/` and a rebuild, which is also the
path that gets you a reproducible image and a review step.

The same logic applies to workflow-driven model downloaders. Beyond pulling
gigabytes through a NAT gateway at $0.045/GB on someone's whim, `.ckpt` files
are Python pickles: loading one executes whatever is inside it. `.safetensors`
is the format that does not have this property, and a shared writable model
volume is exactly the wrong place to relax about it.

If you want auto-download, put it behind a job that validates the source and the
format rather than behind a button in a shared UI.

## What is deliberately not here

- **Per-user workspaces.** Every user shares one output directory. Adding
  identity-scoped paths means threading the authenticated user from the
  oauth-proxy headers through the gateway into the output path — worth doing,
  not done here.
- **Job priority.** `BRPOP` on one list is FIFO. Priority means multiple lists
  and a worker that checks them in order.
- **Multi-GPU workers.** One pod, one GPU. A worker with four cards would need
  ComfyUI's own batching or four ComfyUI processes.
- **Redis HA.** One instance with AOF persistence. It survives a pod restart; it
  does not survive a zone outage. At one-GPU scale, adding three Redis nodes
  protects against a failure less likely than the ones you have not fixed yet.
- **Job retry.** A worker that dies without warning — OOM, node reclaim — does
  not lose its job silently: the agent parks each job in a per-worker
  processing list (BLMOVE) and holds a TTL'd heartbeat, and the gateway fails
  stranded jobs loudly once the heartbeat lapses. But failed means *failed*,
  not requeued: a workflow that OOM-killed one worker would OOM-kill each
  worker it was retried on, in sequence, at GPU prices. The user resubmits.
- **Interrupting a running sampler.** Cancel is cooperative — it stops a queued
  job and asks a running one to stop between events. Truly interrupting mid-step
  is ComfyUI's `/interrupt`, and the workers are not reachable from the gateway.

## Sources

- [KEDA Redis Lists scaler](https://keda.sh/docs/2.12/scalers/redis-lists/)
- [Custom Metrics Autoscaler on OpenShift](https://docs.redhat.com/en/documentation/openshift_dedicated/4/html/nodes/automatically-scaling-pods-with-the-custom-metrics-autoscaler-operator)
- [ROSA machine pools](https://docs.redhat.com/en/documentation/red_hat_openshift_service_on_aws/4/html/cluster_administration/manage-nodes-using-machine-pools)
- [ROSA pricing](https://aws.amazon.com/rosa/pricing/)
