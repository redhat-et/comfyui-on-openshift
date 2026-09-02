# Lint fixtures

Small, deliberately-broken manifests and container definitions used only by
`scripts/unit-tests.sh`, to prove that `scripts/lint.sh`'s manifest step
asserts shape and not merely that the YAML parses. Each fixture here is valid
YAML — `yaml.safe_load_all` accepts every one of them — that violates one
load-bearing invariant from `docs/09-engineering-handoff.md` section 3.

These files are never applied to a cluster and are not part of the real
manifest set — `scripts/lint.sh`'s globs do not read this directory. The unit
tests copy one at a time into `enterprise/manifests/` under a `zz-fixture-`
name, run the real `scripts/lint.sh` unmodified, and delete the copy
afterwards, so the fixture content is what a real regression would look like
without ever touching the manifests that actually ship.

| Fixture | Invariant it violates |
|---|---|
| `manifests/worker-no-gpu-toleration.yaml` | GPU worker Deployment with no `nvidia.com/gpu` toleration |
| `manifests/route-missing-timeout-tunnel.yaml` | Route with `timeout` but no `timeout-tunnel` |
| `manifests/gateway-svc-exposes-container-port.yaml` | Gateway Service listing the gateway's own container port (8000) alongside the proxy port |
| `manifests/warm-floor-above-max-replicas.yaml` | `ScaledObject` whose `cron` warm-floor trigger asks for more workers than `maxReplicaCount` — KEDA clamps and the floor silently never arrives (I1, `docs/10-roadmap.md`) |
| `manifests/deployment-no-network-policy.yaml` | A Deployment in the multi-user namespace that no `NetworkPolicy` `podSelector` names — cut off by the namespace default-deny, with a Ready pod and no event saying so |
| `manifests/worker-holds-admin-redis-password.yaml` | Worker Deployment taking `comfy-redis/password` — the admin Redis credential — instead of the least-privilege ACL user's `worker_password` (W4) |
| `manifests/worker-redis-url-has-no-user.yaml` | Worker Deployment whose `REDIS_URL` names no user, so `redis.from_url()` authenticates as `default` and the ACL user is bypassed with nothing failing (W4, the silent half) |
| `manifests/worker-mounts-sa-token.yaml` | Worker Deployment with no `automountServiceAccountToken: false` — a projected API-server token in a pod whose only client is Redis, readable by whatever custom-node Python runs on that GPU node |
| `manifests/worker-memory-exceeds-smallest-instance.yaml` | GPU worker Deployment whose memory `limits` do not fit the smallest GPU instance type this repo supports, and which is Burstable rather than Guaranteed (F1, `docs/10-roadmap.md`). Not a hypothetical regression: this is the `8Gi`/`24Gi` shape `enterprise/manifests/02-worker.yaml` and `manifests/base/deployment.yaml` both shipped with until F1 fixed them |

The fourth case — a Containerfile losing its `chgrp 0` / `chmod g=u` block —
has no fixture file here. `scripts/lint.sh` does not scan Containerfiles by
any glob or fixed list today, so a fixture dropped anywhere would not be
"discovered" by any plausible future check either; the only faithful test is
against the two real Containerfiles the invariant names
(`app/Containerfile`, `enterprise/worker/Containerfile`). That assertion in
`scripts/unit-tests.sh` mutates one of those files, runs `scripts/lint.sh`,
and restores the original content immediately afterward.
