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
    Q -- BLMOVE --> W1 & W2
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

It also means more than budget once output workspaces (above) are in the
picture. `X-Forwarded-User` is client-supplied in this mode — `hub.py` says
so in three places — so nothing stops a caller from setting it to someone
else's identity. The worker does not know the difference: it writes into
whatever workspace that header names, and an existing file of the same name
there is silently overwritten. Under `AUTH_MODE=none` this is not a
bypass of anything — nobody is authenticated, so there is no "other user"
this mode was ever protecting — but it does mean a caller can overwrite
another *named* user's prior outputs, not just spend GPU time, with nothing
but a header of their choosing. `AUTH_MODE=oauth` is what makes
`X-Forwarded-User` trustworthy again: the proxy sets it from a real login,
so a caller can no longer simply assert someone else's name.

## ComfyUI-Manager and auto-downloaders

Off by default (`ENABLE_MANAGER=false`), for two reasons. (The single-user
configuration honors the same flag, where the calculus differs: one person
behind an authenticated port-forward is the "single-user sandbox" below, and
Manager's missing-model downloads land on the persistent volume. The
reasoning here is about *this* configuration — a shared cluster.)

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

- **Per-user *isolation* of outputs.** The workspaces themselves are here now —
  `docs/10-roadmap.md` (Q3) landed them — so this entry is no longer "every
  user shares one output directory". Each job writes into
  `/output/<workspace>/`, named from the submitter's identity by the worker
  agent, and every output that comes back out is confined to that directory
  whether or not the save node cooperated.

  What is still deliberately absent is **access control on reads**. The
  workspaces are organisational and confining, not a security boundary: the
  gateway serves `/outputs/...` to any caller who has the URL, exactly as it
  did before. That is a decision, not an omission. The only identity this
  system has is `X-Forwarded-User`, and under `AUTH_MODE=none` it is
  client-supplied — `hub.py` says in three places that it must never be
  treated as authorization. Scoping reads on it would be a control that works
  under `AUTH_MODE=oauth` and silently evaporates under `AUTH_MODE=none`,
  which is worse than a documented absence, because the illusion is what
  people would rely on. It would also break the thing users actually do with
  these URLs, which is send them to each other. Real read isolation needs an
  identity the gateway can trust in *both* modes; that is a different item
  from this one, and it is not here.

  Three smaller consequences worth knowing. Usernames are sanitized to an
  allowlist slug plus a hash of the original, so an output directory is
  readable ("alice-smith-example-com-7dcd3a39ad3a") without two different
  usernames ever sharing one; nothing is rejected for the *shape* of a
  username, because an IdP's spelling of a person's name is not something
  they can fix. And a workflow's `filename_prefix` **is** rejected if it
  contains `..` or an absolute path: ComfyUI treats that prefix as a subpath
  of its output directory, the caller wrote it, and it has an unambiguous
  safe form.

  The third is the sharper way to say what "not a security boundary" means
  in practice: `workspace_name()` (`worker_agent.py`) is a **pure, publicly
  computable function of the username** — allowlist-slug the string, append
  12 hex characters of `sha256(username)`, nothing else. The algorithm is
  in this file's git history and in the worker's own source, and it takes no
  secret as input. So a caller does not need to have SEEN one of alice's URLs
  to read her outputs; knowing (or guessing) her username is enough to
  *compute* `workspace_name("alice@example.com")` offline and construct the
  path directly, the same way anyone can today. That is a strictly stronger
  reach than "the URL is unguessable but discoverable" — it needs no
  discovery step at all. It is still the deliberate trade this section
  opens with (organisation, not isolation), not a bug: fixing it means
  answering the identity question above, not hashing the workspace name
  harder.
- **Job priority.** Still not here, on purpose, and the reasoning changed once
  `docs/10-roadmap.md` (Q1) landed fair queueing. A priority lane needs a claim
  from the caller — "I'm interactive" — and the only identity this gateway can
  read, `X-Forwarded-User`, is exactly that self-declared and unauthenticated
  under `AUTH_MODE=none`; a priority lane would just be a header everyone sets
  on themselves. Round-robin sidesteps the trust decision entirely instead of
  solving it: each submitter identity hub.py already records is a lane, lanes
  take turns, and impersonating someone else only shares their turns, not a
  faster one. It also did not need the second list this bullet used to say
  priority would require — the pop is still one `BLMOVE` from one list into a
  per-worker processing list the reaper depends on; a new job is inserted at
  its fairness-computed position within that *same* list (`hub.py`'s
  `fair_enqueue_script`, one atomic Lua `EVAL`) rather than always at the
  back. What is still deliberately absent is an actual priority claim — no
  caller, authenticated or not, can ask to go first.
- **Multi-GPU workers.** One pod, one GPU. A worker with four cards would need
  ComfyUI's own batching or four ComfyUI processes.
- **Redis HA.** One instance with AOF persistence. It survives a pod restart; it
  does not survive a zone outage. At one-GPU scale, adding three Redis nodes
  protects against a failure less likely than the ones you have not fixed yet.
- **General job retry.** Still not here, and for the reason this bullet always
  gave — but `docs/10-roadmap.md` (Q2) has since carved out the one death that
  reason does not cover, so it is worth saying exactly where the line now
  falls. A worker that dies without warning still does not lose its job
  silently: the agent parks each job in a per-worker processing list (BLMOVE)
  and holds a TTL'd heartbeat, and the gateway's reaper acts on stranded jobs
  once the heartbeat lapses. What it does now depends on a `phase` breadcrumb
  the agent writes on the job's own state — `dispatched` until ComfyUI has been
  handed the workflow, `executing` afterwards — and on nothing else.

  A job whose worker died at `executing` is still failed, not requeued, on the
  original argument unchanged: a host-RAM OOM kills the pod and is
  indistinguishable *at the queue layer* from a node reclaim, so requeueing it
  would walk a workflow that killed one worker across the whole pool, in
  sequence, at GPU prices. The user resubmits. A job whose worker died at
  `dispatched` is requeued once, because nothing ran: there is no poison pill
  to replay, and the death was about the cluster rather than about the
  workflow. Everything the agent itself reports — a rejected workflow, an
  `execution_error` (which is how a VRAM OOM arrives), a job deadline — is
  terminal as it always was; retryable is a phase, never an exception.

  Two things follow. The retry is announced as a non-terminal `retry` event, so
  a browser tailing the job reads past it into the second attempt rather than
  stopping; and the attempt counter is a Redis `HINCRBY` whose return value the
  decision is taken from, because the gateway runs two replicas and the
  reaper's per-entry claim — which is what a stranded entry is handed to
  exactly one reaper by, now that the entry is read rather than popped —
  bounds failing a job once without bounding requeueing it once. What
  is still deliberately absent is retry as a general policy — nothing retries a
  workflow that has been anywhere near a GPU. The ambiguity that argument rests
  on is also no longer total for the *operator*: since the worker is sized
  Guaranteed and within what its node can give one pod, a host-RAM OOM
  terminates the pod `OOMKilled` and `oc describe pod` names it. The gateway
  cannot see that; the person reading the failure can, and the failure text
  says so.
- **Interrupting a running sampler.** Cancel is cooperative — it stops a queued
  job and asks a running one to stop between events. Truly interrupting mid-step
  is ComfyUI's `/interrupt`, and the workers are not reachable from the gateway.

## Sources

- [KEDA Redis Lists scaler](https://keda.sh/docs/2.12/scalers/redis-lists/)
- [Custom Metrics Autoscaler on OpenShift](https://docs.redhat.com/en/documentation/openshift_dedicated/4/html/nodes/automatically-scaling-pods-with-the-custom-metrics-autoscaler-operator)
- [ROSA machine pools](https://docs.redhat.com/en/documentation/red_hat_openshift_service_on_aws/4/html/cluster_administration/manage-nodes-using-machine-pools)
- [ROSA pricing](https://aws.amazon.com/rosa/pricing/)
