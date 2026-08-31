# Roadmap

The twelve improvements listed at the end of `README.md`, turned into a work
plan: what each one actually touches, what proves it, what order they can
safely land in, and which of them cannot be finished without spending money on
a real cluster.

Nothing here is implemented. This document exists so that the next person to
pick up a line item does not have to re-derive its blast radius, and so that
several of them can be worked in parallel without three people editing
`hub.py` at once.

## What decides the order

Three facts about this repository, and every scheduling decision below falls
out of them.

**Five of the twelve contend on the same 886 lines of Python.** Priority
queues, retry, per-user workspaces, showback and the cost breaker all edit
`enterprise/gateway/hub.py` (464 lines) and `enterprise/worker/worker_agent.py`
(422). Worked in parallel, they conflict in the same functions. They are a
sequence, not a fan-out.

**There is a hard verification boundary.** `make test` and `make lint` prove
the queue, the gateway, path handling, signal handling and image UID hygiene
with no cluster, no GPU and no AWS account, in about a minute. KEDA actually
scaling, a machine pool provisioning a node, EFS, oauth-proxy and the GPU
itself prove nothing until there is a cluster at ~$2.04/hour. Nine of the
fourteen items below land on the cheap side of that line. Sort by it first.

**Thirteen invariants are load-bearing.** `docs/09-engineering-handoff.md` §3
lists the lines whose removal produces an intermittent, timing-dependent bug.
Four items touch one directly. Those need a stronger review than the rest, and
they need it before merge rather than after.

## Why twelve became fourteen

Two of the README's items are compound, and splitting them separates work of
genuinely different risk:

- **"Shrink the cold start"** is an image split — low risk, provable in CI —
  plus a placeholder pod and PriorityClass that hold a warm node, which is
  autoscaler semantics and cannot be verified without a cluster.
- **"Spot instances"** is blocked on retry existing. Retry is scheduled as its
  own item; spot follows it.

## The work items

Effort tags are the README's. Risk is assigned here by contact with the
invariants in `docs/09` §3.

| ID | Change | Effort | Risk | Lane | Proven by |
|---|---|---|---|---|---|
| Q1 | Priority queues | Small | Medium | Queue | New e2e assertion: a batch does not starve interactive users |
| Q2 | Bounded retry, then fail | Medium | **High** | Queue | e2e: node death retries once; a poison workflow does not loop |
| Q3 | Per-user output workspaces | Small | Medium | Queue | e2e, and the existing path-traversal assertion must still pass |
| Q4 | Showback report | Small | Low | Queue | e2e: GPU seconds attributed to the right user |
| Q5 | Cost circuit breaker | Medium | Medium | Queue | e2e including an unreachable Budgets API — fail-open vs fail-closed is a decision, not an accident |
| Q6 | Estimated-wait metric | Medium | Low | Queue | e2e on the gauge; the scaler half is I4 |
| I1 | Schedule the warm window | Small | Low | Infra | shellcheck + the bash 3.2 portability check; behaviour on cluster day |
| I2 | Split the worker image | Medium | Low | Infra | CI image build + the arbitrary-UID job |
| I3 | Placeholder pod + PriorityClass | Medium | Medium | Infra | Cluster day: node held warm, real job still preempts |
| I4 | Scale on wait, not depth | Medium | Medium | Infra | Cluster day; depends on Q6 |
| I5 | Local NVMe model staging | Medium | **High** | Infra | Cluster day; instance store is not mounted by default on RHCOS |
| I6 | NVIDIA time-slicing | Medium | Medium | Infra | Cluster day, after measuring peak VRAM per workflow |
| I7 | Spot GPU pool | Small | Medium | Infra | Cluster day; blocked on Q2 |
| S1 | Model lockfile + `.safetensors` gate | Medium | Low | Supply | Unit tests on the sync job; it rejects a `.ckpt` |
| S2 | OpenShift Pipelines build | Medium | Low | Supply | Manifests parse; a pipeline dry-run |

## Three lanes

**Queue lane — sequential.** `Q1 → Q2 → Q3 → Q4 → Q5 → Q6`. All six are
laptop-verifiable, so the loop is fast: write the assertion that fails on HEAD
into `enterprise/test/`, implement until it passes, run `make test && make
lint`, hand the merged tree to the next item. The order puts priority queues
first deliberately — it reshapes the pop, and retry then builds on the
reshaped pop instead of fighting it.

**Infra lane — parallel.** `I1`, `I2`, `I3`, `S1`, `S2` touch mostly disjoint
files and can be worked concurrently, then integrated at a single barrier. Use
separate branches or worktrees: three of them want to edit
`enterprise/setup.sh`, which is 559 lines and the most contended file outside
the Python. The cluster-only halves of `I3`–`I7` wait for the cluster day.

**Prose lane — after each merge.** Every merged change owes three edits: the
README section it changes, the invariant table in `docs/09` if it adds or
moves one, and a short rationale in the style of `docs/07-design-review.md` —
what the obvious implementation would have got wrong. Doing this per merge
rather than at the end is what keeps the documentation in one register.

## Gates

| Stage | Must be true before it lands |
|---|---|
| Every item | `make lint` and `make test` green |
| Queue lane | No existing assertion changed — especially path traversal and SIGKILL reaping |
| Infra lane | No manifest lost a GPU toleration or the 4h router timeout |
| High-risk items (Q2, Q3, Q5, I3, I5) | A second reviewer who has read `docs/09` §3, specifically checking that the change survives SIGKILL mid-job, two workers racing, and its dependency being unreachable |
| Cluster items | Verified on a real cluster, in one batched session |

## The cluster day

Everything that needs real hardware — `I3`, `I4`, `I5`, `I6`, `I7`, plus an
end-to-end pass over the merged queue and infra work — is batched into one
supervised session rather than a rebuild per item. At ~$2.04/hour a four-hour
sitting is about $8 of cluster time and answers every open question at once;
five separate cluster builds cost more in wall-clock time than in dollars, and
the wall-clock is the part that stops it happening.

Last step of the session is `make park` or `make down`, confirmed with
`make status`.

## Rules for this work, whoever or whatever is doing it

This repository can spend real money and publish real endpoints. Some of these
line items are good candidates for AI-assisted or parallel execution — the
queue lane in particular is test-first by construction — which makes it worth
stating the boundaries explicitly rather than assuming them.

- **IAM, quotas, machine pool edits and spot bids are proposed, not executed.**
  A person runs them.
- **Nothing merges to `master` unreviewed.** The output of a work item is a
  branch.
- **No `--force`, ever** — and specifically never
  `oc delete pod --force --grace-period=0`, which strands the volume it claims
  to free. `docs/08-stuck-volumes.md` exists because of this one.
- **An invariant is never relaxed to make a test pass.** If `--listen
  127.0.0.1`, the gateway's loopback rebind, the `prompt_id` filter or the 4h
  router timeout appears in a diff, that diff is wrong regardless of whether
  CI is green.
- **No cluster is left running.**

## Not on this list

`docs/06-enterprise-architecture.md` ends with the things that are deliberately
*not* here — Redis HA, multi-GPU workers, interrupting a running sampler, and
the reasoning for each omission. Read it before adding any of them: in several
cases the omission is the decision, and the roadmap above is not an argument
for reversing it.
