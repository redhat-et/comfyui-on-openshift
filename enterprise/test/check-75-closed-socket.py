"""The socket goes away mid-job: the agent resolves it NOW, and both ways.

`worker_agent.py`'s recv loop has a two-line guard whose whole job is one
value:

    raw = ws.recv()
    if raw == "":
        raise websocket.WebSocketConnectionClosedException(...)

A server-side close is not an exception to the client library. websocket-client
returns the close frame from `recv()` as the EMPTY STRING, and "" is a `str`,
so it walks straight past the binary-frame guard, fails to parse as JSON, and
hits `continue` — the loop spins on a dead socket until the job deadline, which
is `JOB_TIMEOUT` (1800s by default) of a GPU doing nothing while the browser
watches a bar that will never move.

That guard was never once executed by this suite. check-70-oom-paths.py's
`__die__` fixture closes with code 1006 — the reserved "abnormal closure" code
a server may not put on the wire — so uvicorn refuses to serialize it, drops
the TCP connection instead, and websocket-client raises out of `recv()`. The
except clause runs; the `if raw == ""` line above it does not, and deleting it
outright leaves every check in this suite green. Reaching it needs an ORDINARY
close code, which is what `fake_comfy.py`'s two knobs here send.

Two scenarios, because the handler has two outcomes and both were unreached:

  A. THE PROMPT IS GONE WITH IT. An empty frame arrives mid-generation and
     /history never learns the prompt existed. The agent must fail the job
     immediately, naming the lost connection — not park until the deadline.

     The connection is deliberately held OPEN after the empty frame (see
     fake_comfy.py). That is the only shape in which the guard's absence is
     observable: close the TCP as well and the very next `recv()` raises
     anyway, so a missing guard costs one loop iteration and nothing else. Held
     open, a missing guard costs the whole of JOB_TIMEOUT — which is exactly
     the bug, and exactly what this measures.

  B. THE WORK ACTUALLY LANDED. The same close, but /history has the prompt and
     its output manifest by the time the socket goes. The agent asks once
     before giving up, and must report `completed` WITH the outputs — the
     branch that turns a lost connection into a lost generation if it is
     removed. Nothing else in this suite reaches it either: every other
     completion in the suite arrives through the terminal WebSocket event,
     which is precisely the event this scenario never sends.

Both budgets are read from `JOB_TIMEOUT` rather than picked: what is being
asserted is "resolved without waiting for the deadline", so the deadline is
the number the assertion has to be expressed in.
"""
import os, time

from harness import GW, check, comfy_received_nodes, drain, failures

get, post = GW.get, GW.post

# hub.py and worker_agent.py read these the same way; a run.sh that shrinks
# JOB_TIMEOUT moves this check's budget with it.
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
RECV_TIMEOUT = int(os.environ.get("RECV_TIMEOUT", "30"))


def received_nodes():
    """Every workflow node key the stub ComfyUI has ever been handed. This is
    how "ComfyUI really did have this workflow" is asserted rather than
    assumed."""
    return comfy_received_nodes()


def error_of(terminal):
    return ((terminal or {}).get("data") or {}).get("error", "")


# The budget for a scenario that RESOLVES correctly. Comfortably above the
# handful of seconds each takes, and comfortably below JOB_TIMEOUT, which is
# where a broken one lands -- so the drain still returns something to assert
# on in both cases rather than hanging.
RESOLVE_BUDGET = min(JOB_TIMEOUT, max(RECV_TIMEOUT * 2, 20))


# ---------------------------------------------------------------------------
print("\n== an empty frame is a closed socket, and a closed socket is not a wait")

node = "__empty_frame__"
job = post("/api/generate", {"workflow": {node: {"class_type": "KSampler"}}})
job_id = job["job_id"]

started = time.time()
kinds, terminal = drain(job_id, timeout=JOB_TIMEOUT + 30)
elapsed = time.time() - started

check("fixture: the stub really was handed this workflow, so the socket died "
      "mid-generation rather than before anything started",
      node in received_nodes())

check("a socket that closes mid-job still produces a terminal event",
      terminal is not None, terminal)
check("that terminal event is a failure, not a silent completion",
      terminal and terminal["type"] == "failed", terminal and terminal["type"])
check("the reason names the lost connection rather than the job deadline -- "
      "the empty frame WAS the close, and the agent read it as one",
      "closed the connection" in error_of(terminal), error_of(terminal)[:200])
check(f"and it resolves at once rather than at the deadline: an empty frame "
      f"the agent does not recognise leaves it spinning on a dead socket for "
      f"the whole of JOB_TIMEOUT ({JOB_TIMEOUT}s) while holding a card",
      elapsed < JOB_TIMEOUT * 0.5, f"{elapsed:.1f}s of a {JOB_TIMEOUT}s deadline")

follow = post("/api/generate", {"workflow": {"9": {"class_type": "SaveImage"}}})
_kinds, follow_terminal = drain(follow["job_id"], timeout=RESOLVE_BUDGET)
check("the worker survives the closed socket and takes the next job",
      follow_terminal and follow_terminal["type"] == "completed",
      follow_terminal and follow_terminal["type"])


# ---------------------------------------------------------------------------
print("\n== and a socket that closes AFTER the work landed still delivers it")

node = "__close_after_history__"
job = post("/api/generate", {"workflow": {node: {"class_type": "KSampler"}}})
job_id = job["job_id"]

started = time.time()
kinds, terminal = drain(job_id, timeout=JOB_TIMEOUT + 30)
elapsed = time.time() - started

check("fixture: the stub was handed this workflow too", node in received_nodes())
check("fixture: the terminal WebSocket event never arrived -- this completion "
      "can only have come from the /history lookup on the closed socket",
      "executing" not in kinds and "execution_success" not in kinds, kinds)

check("a job whose socket closed after ComfyUI finished it is COMPLETED, not "
      "failed: the agent asks /history once before giving up",
      terminal and terminal["type"] == "completed",
      {"type": terminal and terminal["type"], "error": error_of(terminal)[:120]})

images = ((terminal or {}).get("data") or {}).get("images", [])
check("and it carries the outputs /history reported, so the generation the "
      "user paid for is not lost with the connection",
      len(images) == 1, images)

check("this one resolves at once as well", elapsed < JOB_TIMEOUT * 0.5,
      f"{elapsed:.1f}s of a {JOB_TIMEOUT}s deadline")

follow = post("/api/generate", {"workflow": {"9": {"class_type": "SaveImage"}}})
_kinds, follow_terminal = drain(follow["job_id"], timeout=RESOLVE_BUDGET)
check("the worker takes the next job after that too",
      follow_terminal and follow_terminal["type"] == "completed",
      follow_terminal and follow_terminal["type"])


print("\nall assertions passed" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
