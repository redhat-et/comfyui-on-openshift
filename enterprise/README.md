# Multi-user configuration

The single-user path in the repo root gives one person one ComfyUI pod on one
GPU. This is the other configuration: many users, a queue, and a GPU pool that
scales between zero and N.

They share the cluster, the GPU operator, and the volumes. Pick one — do not run
both against the same namespace.

## Setup

```bash
# once, from the repo root — cluster, GPU operator, EFS
make cluster gpu storage        # needs STORAGE_MODE=rwx in .env

# then
./enterprise/setup.sh
```

That is the whole thing. It creates Redis, builds both images in-cluster,
installs KEDA, applies the manifests, turns on machine pool autoscaling, wires
up SSO, and prints the URL.

Re-run it after any change. It is idempotent.

## Configuration

Everything comes from the repo's `.env`. The variables this configuration adds:

| Variable | Default | Notes |
|---|---|---|
| `STORAGE_MODE` | — | **must be `rwx`**; setup refuses otherwise, see below |
| `AUTH_MODE` | `oauth` | `oauth` = cluster SSO. `none` = public, no login |
| `MAX_GPU_WORKERS` | `3` | ceiling for both the pod and node autoscalers |
| `SCALE_TO_ZERO` | `true` | `false` pins one warm worker all the time — and skips KEDA, the ScaledObject and machine-pool autoscaling with it |
| `WARM_WORKERS` | `0` (off) | hold this many workers during working hours; see below |
| `WARM_START` / `WARM_END` | `0 9 * * 1-5` / `0 18 * * 1-5` | the window, as cron expressions |
| `WARM_TIMEZONE` | `UTC` | IANA zone the window is read in |
| `ENABLE_MANAGER` | `false` | bake in ComfyUI-Manager; read the security note first |
| `COMFYUI_REF` | a commit SHA | the ComfyUI revision to build; the default is the commit `v0.32.0` points at, and a tag or branch works too |
| `QUOTA_GPU_SECONDS` | `0` (off) | per-user GPU-second quota per UTC month; over it, `/api/generate` refuses with 429 and says when it resets. Reads the same accounting `/api/showback` reports, and fails open |
| `GPU_HOURLY_RATE` | `0.976` | all-in $/GPU-hour pricing `/api/showback?format=focus`, the FOCUS 1.2 chargeback CSV. The default is docs/02's g6.xlarge figure; it prices a report and spends nothing. Garbled values fall back to the default, loudly |
| `SHOWBACK_OPERATORS` | empty | comma-separated identities, as oauth-proxy reports them, who may read every submitter's row of `/api/showback` under `AUTH_MODE=oauth`. Everyone else gets their own row plus the pool totals. Ignored under `none` |

Changing `AUTH_MODE` is also just an edit-and-re-run: switching oauth → none,
`setup.sh` detects the leftover oauth-proxy sidecar and recreates the gateway
without it.

### The scheduled warm floor

`SCALE_TO_ZERO` is all or nothing: either the first job of the day waits 8-17
minutes for a node, or a card bills around the clock. `WARM_WORKERS` is the
setting in between — hold N workers between `WARM_START` and `WARM_END`, scale
to zero outside them.

It is a KEDA `cron` trigger beside the queue trigger, not a schedule that edits
the machine pool. KEDA takes the maximum across triggers, so the queue still
decides everything outside the window and a busy afternoon still scales past
the floor to `MAX_GPU_WORKERS`. More to the point, the floor lives in `.env`:
re-running `setup.sh` reasserts it instead of resetting it, which is what a
cron job editing `min-replicas` could not do (`docs/10-roadmap.md`, I1 and I3).

Off by default, because it is the setting here that spends money while nobody
is watching. One `g6.xlarge` held 09:00-18:00 on weekdays is ~195 hours a month
at ~$0.80, about $155, on top of whatever the queue itself provokes. Set
`WARM_TIMEZONE` before `WARM_WORKERS`: the default window is a UTC working day.

It needs `SCALE_TO_ZERO=true`. The `false` path skips KEDA entirely, so there
is no ScaledObject for the trigger to live in; `setup.sh` warns if you set both.

### Two Redis users, not one

The `comfy-redis` Secret holds two passwords. `password` is the admin
credential — the gateway's, KEDA's, and the readiness probe's. `worker_password`
belongs to a Redis ACL user called `comfy-worker` that is allowed exactly the
commands `worker_agent.py` issues, against exactly the five key patterns it
names, and nothing else: no `FLUSHALL`, no `CONFIG`, no `SCAN`, no reading a key
outside its own.

That is the only Redis credential a GPU pod ever holds, which matters because a
GPU pod runs whatever custom-node Python is baked into the image and anything
running there can read its own environment. `setup.sh` generates both, and adds
the second one to a namespace deployed before this existed without rotating the
first. Delete the Secret to rotate both.

### The workers have no route to the internet

`06-network-policy.yaml` default-denies the namespace in both directions and
then allows four flows: worker → Redis and DNS, gateway ← the router and the
metrics scrapers, gateway → Redis and TLS control-plane ports for the SSO
sidecar, Redis ← the gateway, the workers and KEDA. Build pods keep their
egress; nothing else has any.

The worker rule is the one with a consequence: a GPU pod cannot reach anything
except Redis, so `ENABLE_MANAGER=true` cannot fetch its node list, install
anything, or download a model at runtime — it will hang. That was already this
configuration's documented position (`docs/06-enterprise-architecture.md`); the
policy makes it true rather than advisory.

If the app stops answering on its Route right after `setup.sh`, read the
`gateway-ingress` comment in that file first: it assumes the router is a pod in
`openshift-ingress`, which is right on ROSA and wrong on clusters that run the
router on the host network.

### Why `rwx` is not optional here

The gateway serves finished images off the same volume the workers write them
to, and those pods are on different nodes by construction — one on CPU, one on
GPU. A gp3 volume is `ReadWriteOnce` and cannot be mounted by both. `setup.sh`
fails fast on this rather than letting you find it as a pod stuck in
`ContainerCreating`. See `docs/03-storage.md`.

## Using it

Open the URL. Paste a workflow in **API format** — that is *Workflow → Export
(API)* in the ComfyUI editor, not the normal Save, which produces a different
shape the gateway will reject.

Or drive it from anywhere:

```bash
GW=https://$(oc get route comfy -n comfyui -o jsonpath='{.spec.host}')

JOB=$(curl -s -X POST "$GW/api/generate" \
        -H 'Content-Type: application/json' \
        -d @workflow_api.json | jq -r .job_id)

curl -s "$GW/api/jobs/$JOB"
curl -s "$GW/api/stats"
```

**The first job after an idle period takes 8–17 minutes.** A GPU node has to be
provisioned and a ~10 GB image pulled. Subsequent jobs start in seconds. This is
the deal scale-to-zero makes; `docs/06-enterprise-architecture.md` covers when
it is a bad deal and what to do instead.

## Watching it work

```bash
oc get pods -n comfyui -w                        # workers appear and vanish
oc get scaledobject,hpa -n comfyui               # what KEDA thinks
oc logs -n comfyui -l app=comfy-worker -f        # the agent
oc logs -n comfyui -l app=comfy-gateway -f       # the gateway
make status                                      # burn rate
```

The gateway also serves Prometheus gauges at `/metrics` (`comfy_queue_depth`,
`comfy_workers_registered`, `comfy_estimated_wait_seconds`), and `setup.sh`
applies a ServiceMonitor so OpenShift's user-workload monitoring can graph and
alert on them — "queue
deeper than N for 30 minutes" is the alert that catches a wedged pool before
a human does.

## Granting access

With `AUTH_MODE=oauth`, getting in requires being able to `get` the namespace:

```bash
oc adm policy add-role-to-user view alice -n comfyui     # grant
oc adm policy remove-role-from-user view alice -n comfyui # revoke
```

No separate user database, and access shows up in the cluster audit log. Each
job also records who submitted it — the gateway stamps the oauth-proxy's
authenticated username into the job state, so `GET /api/jobs/<id>` answers
"whose job is this?" when the GPU bill asks.

Under `oauth` that identity also scopes what a caller can read: `/outputs/...`
serves only files inside the caller's own workspace (anyone else's is a 403),
`GET /api/jobs/<id>`, `POST /api/jobs/<id>/cancel` and `/ws/<id>` answer only
the job's submitter (a stranger holding the id gets a 403, or a 4403 close),
and `/api/showback` returns only the caller's row and the pool totals unless
they are in `SHOWBACK_OPERATORS`, who pass everywhere — its
`?format=focus` CSV (the FinOps FOCUS chargeback export, priced at
`GPU_HOURLY_RATE`) is the same report serialized after that same scoping,
so it withholds exactly what the JSON withholds. Under `none` nothing is scoped, because the
identity is a header the caller wrote. The workspace a user lands in is a
pure function of their username — an allowlist slug plus twelve hex of its
`sha256`, computed after NFC-normalising the string so that the composed and
decomposed spellings of one accented name share one directory — and the same
function is mirrored into the gateway so it can make the check.

## Custom nodes

Put them in `app/src/custom_nodes/`, and their pip dependencies in
`app/requirements-extra.txt`. `setup.sh` copies both into the worker build
context, so one copy serves both configurations — a node pack that needs a pip
package now behaves the same in the pool as it does single-user, instead of
importing cleanly on one and failing on the other.

Do not rely on installing them at runtime through ComfyUI-Manager: the worker
pool scales to zero, so anything written to the container filesystem disappears
when the node is reclaimed. Baking them in is also what makes the image
reproducible and reviewable — which matters more once several people share it.

## Removing it

```bash
./enterprise/teardown.sh          # remove the app, keep Redis data and models
./enterprise/teardown.sh --all    # also delete Redis's volume and the secrets
```

To stop paying for the GPU rather than remove the app, `make park` or `make down`
from the repo root. `docs/02-cost.md`.

## Reading

- `docs/06-enterprise-architecture.md` — why hub and spoke, why Streams over
  pub/sub, what scale-to-zero actually costs, what is deliberately missing
- `docs/07-design-review.md` — what changed from the original design document
  and why
- `docs/05-troubleshooting.md` — the failures you will actually hit
