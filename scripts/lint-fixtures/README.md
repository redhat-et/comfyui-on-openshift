# Lint fixtures

Small, deliberately-broken manifests and container definitions used only by
`scripts/unit-tests.sh` to prove a gap in `scripts/lint.sh`'s manifest step:
today it only calls `yaml.safe_load_all` on every file the manifest glob
finds, so it accepts any syntactically valid YAML regardless of shape. Each
fixture here is valid YAML that violates one load-bearing invariant from
`docs/09-engineering-handoff.md` section 3.

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

The fourth case — a Containerfile losing its `chgrp 0` / `chmod g=u` block —
has no fixture file here. `scripts/lint.sh` does not scan Containerfiles by
any glob or fixed list today, so a fixture dropped anywhere would not be
"discovered" by any plausible future check either; the only faithful test is
against the two real Containerfiles the invariant names
(`app/Containerfile`, `enterprise/worker/Containerfile`). That assertion in
`scripts/unit-tests.sh` mutates one of those files, runs `scripts/lint.sh`,
and restores the original content immediately afterward.
