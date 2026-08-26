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
