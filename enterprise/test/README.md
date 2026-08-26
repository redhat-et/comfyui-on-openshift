# End-to-end test

Runs the real `hub.py` and the real `worker_agent.py` against a real Redis and a
stub ComfyUI, on your laptop. No cluster, no GPU, no AWS.

```bash
./enterprise/test/run.sh
```

Needs `redis-server` on PATH and `pip install redis websocket-client fastapi
'uvicorn[standard]'`.

## What it actually asserts

The point is not coverage for its own sake — it is the handful of behaviours
that are easy to get wrong, impossible to notice in a demo, and miserable to
debug in a cluster. Each of these corresponds to a bug in the original design,
documented in `docs/07-design-review.md`.

**Progress is filtered by `prompt_id`.** The stub deliberately emits events for
a second, unrelated prompt on the same socket, including a terminal
`executing: node=null`. An agent that does not filter ends the wrong job's
stream and reports success on a job still running. The test asserts no foreign
events leak through and that the foreign terminal event does not end the job
early.

**A late subscriber loses nothing.** The test waits three seconds — long enough
for the job to finish — before opening the WebSocket, and asserts it still
receives the full history from `queued` onwards. This is the case Redis pub/sub
silently drops, and it is the common case rather than an edge case.

**A reconnect replays identically.** A second WebSocket to the same job gets the
same event sequence. Browsers sleep and gateway pods roll.

**Failures surface with their reason.** A workflow ComfyUI rejects produces a
`failed` event carrying ComfyUI's own message, not a generic error. The
difference between "failed" and "failed: required input is missing: ckpt_name"
is most of the support burden.

**Cancel works** on a job in flight.

**SIGTERM drains rather than drops.** The test starts a slow job, sends SIGTERM
to the agent mid-generation, and asserts the job still reaches `completed` and
the agent then exits on its own. This one matters more than it looks: the worker
pool scales to zero, so termination is routine. Without it, every scale-down
throws away whatever was rendering and leaves a browser on a progress bar that
never moves.

**SIGKILL still surfaces a terminal event.** SIGTERM is the polite case; an OOM
kill or node reclaim gives no warning at all. The test SIGKILLs the agent
mid-job and asserts the gateway's reaper fails the stranded job with a reason
naming the dead worker (the agent parks each job in a per-worker processing
list and holds a TTL'd heartbeat — see point 5 in `worker_agent.py`), and that
the dead worker drops out of `/api/stats` on its own. Deliberately `failed`,
not requeued: a workflow that OOM-killed one worker would kill the next too.

**Path traversal is blocked** on the output endpoint — `/outputs/../../etc/passwd`
must not resolve.

## What it does not cover

Anything requiring a real cluster: KEDA actually scaling, the machine pool
provisioning a node, oauth-proxy, EFS, the GPU itself. Those need
`enterprise/setup.sh` against a real cluster.
