# Contributing

## Running the tests

The gateway and worker have an end-to-end test that needs no cluster, no GPU,
and no AWS account — it runs the real code against a real Redis and a stub
ComfyUI on your machine. You need `redis-server` on PATH (`brew install redis`
or `apt-get install redis-server`) and:

```bash
pip install -r enterprise/gateway/requirements.txt -r enterprise/test/unit/requirements-test.txt websocket-client
make test
```

That runs a pytest layer first (`python3 -m pytest enterprise/test/unit` —
in-process, no Redis, no ComfyUI: it imports `hub.py` and `worker_agent.py`
directly and calls their pure functions, things like the queue envelope's
round trip, `workspace_name()`'s hostile-input and unicode handling, and the
Dec→Jan/leap-Feb calendar-month math behind quotas and showback, under a
second), then the shell unit tests (`scripts/unit-tests.sh` — instant, no
dependencies, pins the parsing edge cases like AWS CLI's tab-separated
output), then the pytest layer (`enterprise/test/unit/`, the pure functions
in both Python files, under a second), then the e2e suite.

Install from the requirements file rather than a bare `pip install redis` —
the `redis<7` pin is load-bearing (redis-py 8 breaks blocking reads; the
requirements file says why).

40 shell unit assertions, 210 pytest cases, and 369 end-to-end assertions
across 21 check files, in about a minute. `enterprise/test/README.md`
explains what each one is defending against and why. Run it before sending a
change to `hub.py` or `worker_agent.py`.

Adding an assertion means adding a file: `run.sh` discovers every
`enterprise/test/check*.py`, so a new check needs no second edit anywhere. The
naming convention — and the two things a check may assume — are in
`enterprise/test/README.md`. Two of the shell unit tests prove that discovery
by running the real e2e suite with a deliberately broken check dropped into it
— one fixture per fold, because a fixture that breaks both rules at once
cannot say which one caught it. Both are named `check-00-*` so they sort ahead
of every real check and the suite they spawn stops at the first one, which
costs a suite startup and a single check rather than a full pass.

## Linting

```bash
make lint
```

That is shellcheck, bash syntax, a macOS-bash-3.2 portability check, Python
compilation, and the manifest and file shape checks — the logic lives in
`scripts/lint.sh`. The shape checks are where the file-level half of the
load-bearing invariants in `docs/09-engineering-handoff.md` section 3 is held:
a worker that lost its GPU toleration, a Route that lost `timeout-tunnel`, a
Service that regained the gateway's own port, a Containerfile that lost its
`chgrp 0` block, a GPU pod whose memory limit no longer fits the smallest
supported instance. The e2e suite runs no cluster and reads no manifest, so it
cannot see any of them. `scripts/lint.sh` also pins shapes in the Python and
shell the suite *does* run but cannot see the failure of — the ten greps
under "load-bearing file shapes", plus the retry counter, the fair-queueing
insert, the showback accumulator and the quota breaker's distance from
`readyz()`. Three blocks are mirrored verbatim between `hub.py` and
`worker_agent.py` because the two ship in different images — the queue
envelope, the showback accrual and the workspace naming (`BEGIN/END SHARED
WORKSPACE`) — and lint diffs each pair; a fourth rule compares the
`state_key`/`stream_key`/`payload_key` bodies, the `EVENT_STREAM_TTL` default
and the cancel field between the files by AST, so change both or neither. The
manifest rules parse the YAML for the shapes the audit added: every
Deployment selected by a NetworkPolicy, `automountServiceAccountToken: false`,
the worker's `comfy-worker` ACL user starting from `-@all` and not widened,
and a warm floor (`WARM_WORKERS`) that does not exceed `maxReplicaCount`.
Each has a fixture under `scripts/lint-fixtures/` that must fail.
CI (`.github/workflows/ci.yaml`) runs exactly `make lint` and `make test` on
every pull request — a red check on those means the same command fails for
you locally — plus two jobs that need more than a laptop usually has lying
around: `comfyui-smoke` boots the real pinned ComfyUI on CPU and asserts the
`--models-directory` path contract (runnable locally:
`MODELS_DIR=/tmp/m OUTPUT_DIR=/tmp/o scripts/ci-smoke-comfyui.sh`), and
`image-uid` builds the gateway image and runs it as an arbitrary high UID,
the way OpenShift's restricted-v2 SCC will, then scans it for known CVEs
without blocking the PR. The `lint` job also validates every manifest against
the Kubernetes, OpenShift and KEDA schemas with kubeconform. The two GPU
images are too large for a PR job; `nightly.yaml` builds each once a night,
boots it as an arbitrary UID on CPU, and runs the scan that does gate.

Shell is Allman-braced with blank lines between logical sections; keep it that
way. Variable names are descriptive except for loop counters and well-known
short forms.

Everything in `scripts/` must run under the stock macOS bash, which is 3.2:
no associative arrays (`declare -A`), no `wait -n`. Code that runs inside a
container (`enterprise/worker/start.sh`) may use newer bash. CI greps for the
common offenders.

## Comments

Comments here explain *why*, and specifically why the obvious alternative is
wrong. A comment that restates the code is noise; a comment recording the
afternoon somebody lost to a silent failure mode is the most valuable thing in
the file. Several of them are the only reason a future reader will not
"simplify" a line back into a bug — see `docs/07-design-review.md` for the list
of things that already happened once.
