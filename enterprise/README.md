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
| `SCALE_TO_ZERO` | `true` | `false` pins one warm worker — see the cold-start note |
| `ENABLE_MANAGER` | `false` | bake in ComfyUI-Manager; read the security note first |
| `COMFYUI_REF` | `v0.32.0` | the ComfyUI tag to build |

Changing `AUTH_MODE` is also just an edit-and-re-run: switching oauth → none,
`setup.sh` detects the leftover oauth-proxy sidecar and recreates the gateway
without it.

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
`comfy_workers_registered`), and `setup.sh` applies a ServiceMonitor so
OpenShift's user-workload monitoring can graph and alert on them — "queue
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

## Custom nodes

Put them in `app/src/custom_nodes/`. `setup.sh` copies that into the worker
build context, so one copy serves both configurations.

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
