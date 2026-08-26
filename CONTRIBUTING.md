# Contributing

## Running the tests

The gateway and worker have an end-to-end test that needs no cluster, no GPU,
and no AWS account — it runs the real code against a real Redis and a stub
ComfyUI on your machine.

```bash
pip install redis websocket-client fastapi 'uvicorn[standard]'
./enterprise/test/run.sh
```

24 assertions, ~40 seconds. `enterprise/test/README.md` explains what each one
is defending against and why. Run it before sending a change to `hub.py` or
`worker_agent.py`.

## Linting

```bash
shellcheck -x scripts/*.sh scripts/lib/*.sh enterprise/*.sh enterprise/worker/start.sh
python3 -m py_compile enterprise/gateway/hub.py enterprise/worker/worker_agent.py
```

Shell is Allman-braced with blank lines between logical sections; keep it that
way. Variable names are descriptive except for loop counters and well-known
short forms.

## Comments

Comments here explain *why*, and specifically why the obvious alternative is
wrong. A comment that restates the code is noise; a comment recording the
afternoon somebody lost to a silent failure mode is the most valuable thing in
the file. Several of them are the only reason a future reader will not
"simplify" a line back into a bug — see `docs/07-design-review.md` for the list
of things that already happened once.
