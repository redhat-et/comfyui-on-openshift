# ComfyUI on OpenShift

A ComfyUI (PyTorch) inference backend on GPU nodes, on ROSA or on any
OpenShift 4.x cluster you already have.

Two configurations sharing one cluster, one GPU operator, and one set of volumes.

### Single user — one pod, one GPU

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

### Multi user — queue, SSO, GPU pool that scales to zero

Same cluster, different last step:

```bash
# STORAGE_MODE=rwx in .env — the gateway and the workers share a volume
make cluster gpu storage
make enterprise                        # one script does the rest
```

A FastAPI gateway on cheap CPU nodes, Redis as the queue and progress bus, and
GPU workers that are unreachable by design and scale between 0 and N on queue
depth. `enterprise/README.md` to run it, `docs/06-enterprise-architecture.md`
for why it is shaped that way.

---

Already have an OpenShift cluster? Set `PLATFORM=openshift` in `.env`, `oc login`,
then `make gpu storage deploy` (or `make gpu storage enterprise`). Nothing in
those steps is ROSA-specific.

Stop paying: `make park` (GPU to zero) or `make down` (delete the cluster).

---

## Before you put a card in: read this

You need an AWS account first, and that part cannot be scripted — signup wants a
card, an SMS code, and an email code through a browser. `docs/01-aws-account.md`
walks it, about 20 minutes.

But **ROSA on a personal AWS account is an expensive way to do this**, for three
reasons that only surface after you have committed:

1. **AWS Business support.** AWS's own ROSA setup page lists a Business /
   Enterprise On-Ramp / Enterprise plan under prerequisites; Red Hat's
   prerequisites call it *recommended*. Clusters do generally build on Basic —
   but Business is the greater of $100/month or 10% of usage (~$150/month here),
   and without it you have no escalation path on the day Red Hat SRE needs one.
   Budget for it or accept the risk knowingly; do not discover it later.
2. **A new account has a GPU vCPU quota of exactly zero.** The increase is a
   support ticket with a lead time of 30 minutes to several business days, and
   nothing you can do speeds it up.
3. **The floor is ~$2.04/hour, ~$1,500/month** if left running. Parking the GPU
   pool only gets you to ~$1.06/hour, because the control plane fee, the two
   base workers, and the NAT gateway keep billing.

### Check these first

You work at Red Hat. Before a personal card:

- **Red Hat Demo Platform** (`demo.redhat.com`) — AWS open environments with a
  budget attached, and ROSA catalog items. This is the intended path for
  "I need to test something on ROSA."
- **An OCTO or team AWS sub-account.** Under Red Hat's AWS org it inherits the
  org support plan, which makes problem #1 disappear entirely, and usually has
  quotas already raised — which makes problem #2 disappear too.
- **Internal GPU capacity.** If an existing internal OpenShift cluster with
  NVIDIA nodes can give you a namespace, your whole problem collapses to
  `PLATFORM=openshift && make gpu storage deploy`.

### And consider whether you need ROSA at all

If what you are testing is "does our ComfyUI backend behave correctly on
OpenShift," a self-managed **Single Node OpenShift** on one `g6.2xlarge` gives
you a real OpenShift API, real SCCs, and the same GPU operator for **~$1.61/hour**
— no ROSA service fee, no per-cluster control-plane fee, and no support-plan
question at all. You already have OpenShift entitlements. The `make gpu`,
`make storage`, and `make deploy` steps here work unchanged against it.

Use ROSA when the managed service itself is the thing under test.

---

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
  99-teardown.sh          park | cluster | all
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
docs/
  01-aws-account.md       the browser-only steps
  02-cost.md              the numbers, and how to keep them down
  03-storage.md           gp3 vs EFS vs S3, and why models are the hard part
  04-exposing.md          how to let other people reach it, safely
  05-troubleshooting.md   the failures you will actually hit
  06-enterprise-architecture.md   hub and spoke, Streams, scale-to-zero economics
  07-design-review.md     what changed from the original design doc, and why
  08-stuck-volumes.md     when a dead pod will not release the volume
```

Scripts are idempotent. Re-running any of them is safe and skips what is
already done.

## Configuration

Everything lives in `.env`. The ones you are most likely to change:

| Variable | Default | Notes |
|---|---|---|
| `PLATFORM` | `rosa` | `openshift` to use a cluster you already have |
| `AWS_REGION` | `us-east-2` | good G-family capacity, cheaper than us-east-1 |
| `GPU_INSTANCE_TYPE` | `g6.xlarge` | L4 24 GB, ~$0.80/hr — fits SDXL comfortably |
| `STORAGE_MODE` | `rwo` | `rwx` for EFS shared across pods — see docs/03-storage.md |
| `COMFYUI_IMAGE` | empty | empty means build in-cluster from `app/Containerfile` |
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
