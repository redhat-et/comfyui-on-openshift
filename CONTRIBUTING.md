# Contributing

## Running the tests

The gateway and worker have an end-to-end test that needs no cluster, no GPU,
and no AWS account — it runs the real code against a real Redis and a stub
ComfyUI on your machine. You need `redis-server` on PATH (`brew install redis`
or `apt-get install redis-server`) and:

```bash
pip install -r enterprise/gateway/requirements.txt websocket-client
make test
```

That runs the shell unit tests first (`scripts/unit-tests.sh` — instant, no
dependencies, pins the parsing edge cases like AWS CLI's tab-separated
output), then the e2e suite.

Install from the requirements file rather than a bare `pip install redis` —
the `redis<7` pin is load-bearing (redis-py 8 breaks blocking reads; the
requirements file says why).

31 assertions, about a minute. `enterprise/test/README.md` explains what each
one is defending against and why. Run it before sending a change to `hub.py`
or `worker_agent.py`.

## Linting

```bash
make lint
```

That is shellcheck, bash syntax, a macOS-bash-3.2 portability check, Python
compilation, and manifest parsing — the logic lives in `scripts/lint.sh`.
CI (`.github/workflows/ci.yaml`) runs exactly `make lint` and `make test` on
every pull request — a red check on those means the same command fails for
you locally — plus two jobs that need more than a laptop usually has lying
around: `comfyui-smoke` boots the real pinned ComfyUI on CPU and asserts the
`--models-directory` path contract (runnable locally:
`MODELS_DIR=/tmp/m OUTPUT_DIR=/tmp/o scripts/ci-smoke-comfyui.sh`), and
`image-uid` builds the gateway image and runs it as an arbitrary high UID,
the way OpenShift's restricted-v2 SCC will.

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
