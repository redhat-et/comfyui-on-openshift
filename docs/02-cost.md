# Cost

## The floor

Smallest ROSA HCP cluster that can run one ComfyUI pod on an L4, us-east-2,
on-demand, August 2026:

| Line item | $/hour |
|---|---:|
| ROSA HCP control plane fee | 0.250 |
| ROSA service fee — 2 base workers, 8 vCPU | 0.342 |
| EC2 — 2 × m5.xlarge | 0.384 |
| ROSA service fee — GPU node, 4 vCPU | 0.171 |
| EC2 — 1 × g6.xlarge (L4, 24 GB) | 0.805 |
| NAT gateway + load balancer + EBS | ~0.088 |
| **Running** | **~2.04** |
| GPU pool parked at 0 | ~1.06 |
| Cluster deleted, VPC kept | ~0.05 |
| Everything deleted | ~0.00 |

The ROSA service fee is $0.171 per 4 worker vCPUs per hour. It applies to the
GPU node too, which surprises people — you pay Red Hat for the GPU node's vCPUs
on top of what you pay AWS for the card.

`make status` computes this live from your actual machine pools.

## What that means in practice

| Pattern | Monthly |
|---|---:|
| Left running 24/7 | ~$1,490 |
| Weekdays 9–6, `make park` nightly | ~$800 |
| Weekdays 9–6, `make down` nightly | ~$370 |
| Occasional — up for a day a week | ~$85 |

Plus AWS Business support at the greater of $100/month or 10% of usage, which
bills whether or not a cluster exists.

## Park vs down

`make park` scales the GPU pool to zero. Takes seconds, comes back in ~5
minutes, keeps your volumes and models. But it only removes $0.98/hour — the
control plane fee, two base workers, and NAT gateway are $1.06/hour on their
own, which is $760/month of doing nothing.

`make down` deletes the cluster. Takes ~10 minutes, comes back in ~15, and drops
you to ~$0.05/hour. It destroys gp3 volumes, so your models go with it.

**Park at lunch. Down overnight.** The reason HCP is the right architecture here
is precisely that a 15-minute rebuild makes "down" a reasonable default rather
than a last resort. On ROSA Classic, with a 40-minute build and 5 extra nodes,
nobody tears down and everybody overpays.

To make `down` painless, put your models somewhere that outlives the cluster —
`STORAGE_MODE=rwx` (EFS) or an S3 bucket. See `03-storage.md`.

## Where the money actually goes if you are not careful

- **NAT gateway: ~$32/month plus $0.045/GB processed.** It bills while the
  cluster exists and while it does not, if you only ran `make down`. Pulling
  multi-gigabyte model files and driver images through it is real money.
  `make destroy` removes it.
- **Load balancers: ~$16/month each.** ROSA creates them for ingress. Deleting
  the cluster removes them; failed deletions sometimes leave them behind.
  `make down` lists stragglers.
- **Unattached EBS volumes.** A deleted pod with a Retain policy leaves a
  100 GiB gp3 volume billing at ~$8/month forever.
- **The support plan.** Easiest thing in the world to forget. If you stop using
  ROSA, downgrade it in the console.

## Cheaper alternatives, honestly

| Option | $/hour | Trade-off |
|---|---:|---|
| ROSA HCP, this repo | 2.04 | managed OpenShift, the real thing |
| Self-managed SNO on g6.2xlarge | 1.61 | real OpenShift, you operate it, no support plan needed |
| Plain EC2 g6.xlarge + podman | 0.81 | no OpenShift at all — only useful if OpenShift is not what you are testing |
| Lambda / RunPod / Vast L4 | 0.40–0.80 | no OpenShift, no AWS integration, minutes to provision |

If OpenShift semantics are what you are validating — SCCs, operators, the GPU
operator, OpenShift networking — SNO is the value pick. If the ROSA managed
service itself is under test, pay for ROSA. If neither, you are on the wrong
platform for this workload.
