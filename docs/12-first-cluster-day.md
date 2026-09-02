# First cluster day

Everything `make test` proves, it proves without a cluster. This page is the
other half: the one run on real hardware that turns the README's derived
numbers into measured ones, and the three moments worth recording while it
happens. It is a checklist, in order, with the commands, what each step costs,
and where every number you write down belongs afterwards.

Budget the day, not the hour: the GPU quota approval is the long pole and can
take days, the cluster build is about forty-five minutes, and the first
scale-from-zero is the one measurement nobody can take for you.

## Before the day

| | Command | Cost |
|---|---|---|
| Tools | `make tools` (`INSTALL_DIR=/opt/homebrew/bin make tools` on a Mac without sudo) | none |
| Configuration | `cp .env.example .env`, then set `STORAGE_MODE=rwx`, `WARM_WORKERS=2`, `WARM_TIMEZONE` to your zone, `BUDGET_ALERT_EMAIL` | none |
| Credentials | `aws configure --profile rosa-admin`; `rosa login --token …` | none |
| Preflight | `make preflight` | none — read-only |
| Quota + alarm | `make account` | none — files the GPU quota request and creates the budget alarm; approval takes days |

`make preflight` must be clean before `make account`, and `make account`'s
quota request must be approved before `make cluster` will place a GPU node.
`make preflight` again on the day shows whether it has been.

## The day

Run these in order; each prints what it did and what it cost.

```bash
make cluster      # ~20 min: ROSA HCP control plane, VPC, GPU machine pool
make gpu          # ~20 min: NFD + NVIDIA GPU Operator, and a smoke test on the card
make storage      # EFS for /models and /output (STORAGE_MODE=rwx)
make enterprise   # Redis, gateway, oauth-proxy, KEDA, the worker pool, the warm floor
```

The gateway URL prints at the end of `make enterprise`. Log in with your
cluster identity; that is the SSO.

## What to measure, and where it goes

Write each of these down the first time it happens. They are the numbers the
README and `docs/11-scaling.md` currently derive from vendor figures and
component estimates, and one measured line beside each is worth more than the
derivation.

| Measurement | How | Where it lands |
|---|---|---|
| **Cold start from zero** — Queue Prompt to the first progress event, with the pool at 0 | `make status` shows 0 workers; submit from ComfyUI; the gateway job page timestamps `queued` → `started` | `docs/11-scaling.md`, next to the 8–17 minute range; the README's "Where this loses" |
| **Warm start** — the same, with the floor holding two workers inside the window | Submit during `WARM_START`–`WARM_END` | Same page; this is the number the sizing table assumes |
| **A render** — `started` → `completed` for a stock SDXL workflow on the L4 | The job page, or `GET /api/jobs/<id>` (`started_at`, `gpu_seconds`) | `docs/02-cost.md`'s duty-cycle assumption (2.5 min) |
| **Scale-in** — last job done to the pool back at the floor, then to zero after `WARM_END` | `make status` every few minutes; KEDA's `cooldownPeriod` is 600 s | `docs/06-enterprise-architecture.md`, the scale-to-zero section |
| **Image pull** — how long a fresh node takes to pull the ~10 GB worker image | `oc describe pod` on the first worker: `Pulling` → `Pulled` | `docs/11-scaling.md`, the cold-start breakdown |
| **The bill** — `make status` at the end of the day, and the AWS console the next morning | Both | `docs/02-cost.md`'s hourly table, as a "measured" column |

## What to verify, because CI cannot

Each of these is schema-valid and lint-checked but has never been applied to a
cluster by CI. Ten minutes covers all of them.

- **The Route reaches the gateway** under the namespace default-deny. If it
  does not, the router is host-networked on this cluster; the policy already
  admits host-network sources, and `enterprise/manifests/06-network-policy.yaml`
  names the one-command escape hatch if it still fails.
- **The worker reaches Redis and nothing else.** `oc rsh` into a worker and
  `curl -m 5 https://example.com` should time out; `python3 -c "import
  redis"`-style connectivity to `redis:6379` should succeed as the
  `comfy-worker` ACL user.
- **The warm floor holds.** Inside the window, `oc get scaledobject` shows the
  cron trigger and the pool does not drop below `WARM_WORKERS`; outside it, it
  drains.
- **The liveness probe is quiet.** `oc get events` shows no `Unhealthy` for
  the worker while a long render runs; the agent touches its liveness file
  from inside the job loop.
- **Showback names the right person.** `GET /api/showback` after two users
  have each run a job: each sees their own row and the totals; a name in
  `SHOWBACK_OPERATORS` sees both.
- **Scoping refuses a stranger.** As user B, request user A's job id:
  `GET /api/jobs/<id>` is 403 and the WebSocket closes with 4403.

## What to record for the write-up

Three moments make the whole argument in twenty seconds of screen recording:

1. Queue Prompt in stock ComfyUI — the designer's loop is unchanged.
2. The job row appearing at the gateway, with the queue position and the
   estimated wait.
3. `make status` showing the pool go from 0 to 1 (or from the floor to
   floor + 1), and the render completing.

Record the gateway tab and a terminal side by side; the cold-start wait is the
honest part of the recording, so do not cut it — speed it up and label it.

## Stop paying

```bash
make park      # GPU pool to 0 nodes; ~$1.06/hr; back in ~5 min
make down      # delete the cluster; ~$0.05/hr; back in ~15 min
make destroy   # everything but the EFS models volume, which outlives the cluster by design
```

Park at lunch, down overnight. `docs/02-cost.md` has the crontab lines.

## Afterwards

Put the measured numbers in the pages named above, mark this file's table
rows with the date, and open one pull request titled with the cold-start
figure. That is the release note for v0.2.0.
