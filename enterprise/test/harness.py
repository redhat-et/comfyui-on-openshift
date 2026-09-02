"""Shared helpers for enterprise/test/check-*.py.

Every check-*.py here used to carry its own copy of the same handful of
things: a four-line PASS/FAIL printer, a pair of tiny urllib wrappers for
GET/POST, a WebSocket drain loop, the two Redis key builders, a subprocess
liveness test, a poll-until-true loop, and the showback accounting readers.
`ripwire --clones` found 19+ copies of the printer alone. This module is the
one definition of each, imported instead of pasted -- alongside
queue_watch.py's QueueWriteWatcher and worker_ids.py's heartbeat_keys() /
processing_keys(), which were already factored out the same way and are the
style this module follows.

`run.sh` copies every `enterprise/test/*.py` into its work directory with a
single `cp *.py`, so this file rides along with each check for free -- no
change to run.sh's discovery was needed.

THE FAIL_MARKER CONTRACT. `check()` below prints the exact line format
run.sh's FAIL_MARKER greps for -- two spaces, PASS or FAIL, two more spaces --
and that format now exists in exactly one place. Do not reformat this
printer's output; enterprise/test/README.md documents the contract and
run.sh's own comment block explains why the marker is anchored and padded
rather than a bare search for the word.

A MODULE-LEVEL `failures` LIST IS SHARED PROCESS-WIDE, ON PURPOSE. Every
check-*.py that imports this module shares the same `failures` list object,
which would be a bug in a long-lived process -- but run.sh execs each
check-*.py as its own fresh `python3 check.py` subprocess (see the `for check
in check*.py` loop), so there is exactly one check's assertions in this list
for the lifetime of any process that imports it. A check's own final summary
(the "N FAILED: [...]" line every check prints) reads this same list.

WHAT A CHECK MAY ASSUME FROM THIS MODULE: `GW` and `COMFY` are Client
instances already bound to the shared gateway (:8100) and the stub ComfyUI
(COMFY_HOST:COMFY_PORT, both exported by run.sh); `r` is not provided here
because several checks need more than one Redis connection or a specific
error-handling shape around it -- call connect_redis() instead, which reads
the same REDIS_URL / REDIS_PASSWORD env vars run.sh exports. A check that
starts its OWN gateway (check-15, check-66, check-90's dedicated-port
variants, check-95) constructs its own `Client("http://127.0.0.1:810X")`
locally rather than reusing `GW` -- `GW`/`COMFY` name the two servers run.sh
itself starts, nothing else.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import redis
import websocket

from worker_ids import heartbeat_keys

# ---------------------------------------------------------------------------
# Environment run.sh exports for every check (enterprise/test/README.md's
# "Environment" row). Read once here instead of once per file.
# ---------------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None
QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
COMFY_HTTP = f"http://{os.environ.get('COMFY_HOST', '127.0.0.1')}:{os.environ.get('COMFY_PORT', '8999')}"

# The absolute path to the real worker agent (run.sh's WORKER_AGENT export --
# see run.sh's own comment on why a check that kills the suite's agent needs
# this to start a replacement). Every check that imports this module gets a
# live agent's pid as argv[1] regardless of whether it also starts its own.
WORKER_AGENT = os.environ["WORKER_AGENT"]


def connect_redis():
    """A Redis connection using the same env vars run.sh exports for the
    real gateway and worker to use -- REDIS_URL / REDIS_PASSWORD,
    decode_responses=True so every check reads strings, not bytes."""
    return redis.from_url(REDIS_URL, password=REDIS_PASSWORD, decode_responses=True)


# ---------------------------------------------------------------------------
# The PASS/FAIL printer every check's assertions go through.
# ---------------------------------------------------------------------------
failures = []


def check(name, cond, detail=""):
    """Print one PASS/FAIL line -- run.sh's FAIL_MARKER is anchored on this
    exact format, two spaces / PASS-or-FAIL / two spaces -- and record a
    failed assertion's name onto the shared `failures` list (see the module
    docstring for why sharing it process-wide is fine here)."""
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# HTTP. Every check-*.py had its own get()/post() pair, twelve and six
# copies respectively, differing only in which base URL they hit and
# (check-50/check-55) an optional X-Forwarded-User header. Client bundles
# both behind the one thing that actually varies: the base URL.
# ---------------------------------------------------------------------------
class Client:
    """A tiny HTTP client bound to one base URL. Checks that only ever talk
    to the shared gateway or the shared stub ComfyUI use the module-level
    `GW` / `COMFY` instances below; a check that starts a second gateway of
    its own (check-15's :8102, check-66's :8103, check-95's :8101) builds a
    second Client for it instead of threading a `base=` kwarg through every
    call, the way several of the pre-refactor copies did."""

    def __init__(self, base_url, timeout=10):
        self.base_url = base_url
        self.timeout = timeout

    def get(self, path):
        return json.loads(urllib.request.urlopen(self.base_url + path, timeout=self.timeout).read())

    def post(self, path, body=None, headers=None, user=None):
        """POST `body` as JSON (or an empty object if `body` is None -- some
        endpoints, like /api/cancel, take no body). `user`, when given, sets
        X-Forwarded-User -- the AUTH_MODE=none identity header
        check-50-fair-queue.py and check-55-retry-placement.py submit jobs
        under; a falsy-but-not-None user (e.g. "") still sets the header,
        matching check-55's original `if user is not None` copy rather than
        check-50's `if user` one -- no check here ever passes a falsy user,
        so the two were never actually distinguishable in practice."""
        data = json.dumps(body).encode() if body is not None else b"{}"
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if user is not None:
            hdrs["X-Forwarded-User"] = user
        req = urllib.request.Request(self.base_url + path, data=data, headers=hdrs)
        return json.loads(urllib.request.urlopen(req, timeout=self.timeout).read())


# The two servers run.sh itself starts for every check (README's
# "Environment" row: "the gateway on 8100, the stub ComfyUI on 8999").
GW = Client("http://127.0.0.1:8100")
COMFY = Client(COMFY_HTTP)


# ---------------------------------------------------------------------------
# Redis key builders. Ten copies of state_key()/stream_key() alone.
# ---------------------------------------------------------------------------
def state_key(job_id):
    return f"comfy:job:{job_id}:state"


def stream_key(job_id):
    return f"comfy:job:{job_id}:events"


def payload_key(job_id):
    return f"comfy:job:{job_id}:payload"


# The terminal event/status types every drain loop and reap-durability check
# stops at. A set (not a tuple) to match the three pre-existing TERMINAL_TYPES
# copies (check-36/37/67) exactly, since `in` on a set is what several
# comparisons here were already written against.
TERMINAL_TYPES = frozenset({"completed", "failed", "cancelled"})


# ---------------------------------------------------------------------------
# WebSocket drain: tail a job's event stream to its first terminal event.
# ---------------------------------------------------------------------------
def drain(job_id, timeout, ws_url=None, terminal_types=TERMINAL_TYPES, verbose=False):
    """Tail the gateway WebSocket for job_id to its first terminal event.

    Returns (seen, terminal): every event TYPE seen, in order, and the
    terminal event itself (or None if `timeout` elapsed first).

    `timeout` is an absolute wall-clock deadline, recomputed before every
    recv() rather than handed to ws.settimeout() once and left there --
    hub.py's own /ws/{job_id} sends a 'ping' every 15s whenever nothing new
    is on the stream, and a ping resets ws.recv()'s per-call timeout right
    back to its own full length, so settimeout() called once is not actually
    a ceiling on the wait: a run against a broken implementation that never
    emits a terminal event would hang past `timeout` for as long as pings
    keep arriving inside it (see check-30-sigkill.py's original docstring,
    which first spelled this out).

    Six of the ten pre-refactor copies (check-30/32/36/67/70/75) already
    computed the deadline this way; four short-timeout copies
    (check-20/60/65/66, all 15-20s) called ws.settimeout() once instead.
    Consolidating onto the deadline form changes nothing for any of those
    four: none of their assertions ever waits through a 15s ping inside a
    15-20s window, so every passing result is identical either way, and a
    hung job is now caught at the stated `timeout` instead of a multiple of
    it -- a strictly tighter bound, never a looser one.

    `verbose`, when set, prints "no terminal event ... gave up" on timeout,
    matching check-30/32/36's original copies; the other seven returned
    silently on timeout. Kept as a parameter rather than dropped since it is
    a real (if minor) behavioural difference between the original copies.
    """
    ws = websocket.WebSocket()
    ws.connect(ws_url or f"ws://127.0.0.1:8100/ws/{job_id}", timeout=10)
    deadline = time.time() + timeout
    seen, terminal = [], None
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            if verbose:
                print(f"  drain({job_id}): no terminal event within {timeout}s "
                      f"-- gave up; events seen so far: {seen}")
            break
        ws.settimeout(max(1.0, remaining))
        try:
            m = json.loads(ws.recv())
        except Exception:  # noqa: BLE001 - closed or timed out; the deadline decides
            break
        if m.get("type") == "ping":
            continue
        seen.append(m["type"])
        if m["type"] in terminal_types:
            terminal = m
            break
    ws.close()
    return seen, terminal


# ---------------------------------------------------------------------------
# Process liveness and generic polling.
# ---------------------------------------------------------------------------
def alive(pid):
    """Same test as run.sh's agent_alive(): kill -0 alone answers for a
    child bash has not yet reaped (a zombie), so state Z is the conclusive
    check, not bare kill -0."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    state = subprocess.run(["ps", "-p", str(pid), "-o", "state="],
                           capture_output=True, text=True).stdout.strip()
    return not state.startswith("Z")


def wait_gone(pid, timeout=30):
    """Block until `pid` is no longer alive() (see above), or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


def wait_for(predicate, timeout=30, interval=0.2):
    """Poll `predicate()` until it returns true, or timeout. Exceptions from
    `predicate` are swallowed and treated as "not yet" -- check-32-worker-
    restart.py's original copy did this (useful when the predicate reads
    state that may not exist yet); check-35-retry-doors.py's copy did not
    catch, but its own predicates never raised, so the two were never
    actually distinguishable in practice."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False


def until(predicate, timeout, on_wait=None, interval=0.2):
    """Poll `predicate()` until it returns true, or timeout; report whether
    it ever did. The caller asserts on state it reads itself afterwards --
    this only decides how long to wait.

    `on_wait`, when given, runs once per failed poll before sleeping.
    check-55-retry-placement.py's copy called a heartbeat-rearming function
    on every tick, to keep a deliberately-frozen agent from being read by the
    reaper as a real death while this polls; passed here as `on_wait` rather
    than folded in, since that side effect is specific to that check's own
    fixture."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if on_wait:
            on_wait()
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Job event-stream reads (check-36-live-worker-fencing.py and
# check-37-reap-durability.py both had these). `r` is taken as an explicit
# parameter -- the same style worker_ids.py's heartbeat_keys()/
# processing_keys() already use -- rather than this module owning a shared
# connection, since several checks need more than one Redis client or want
# their own error handling around it.
# ---------------------------------------------------------------------------
def events_of(r, job_id, wanted):
    """The TYPE of every event on job_id's stream that is in `wanted`, over
    its whole history, in order."""
    kinds = []
    for _entry_id, fields in r.xrange(stream_key(job_id)):
        try:
            kind = json.loads(fields["data"]).get("type")
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if kind in wanted:
            kinds.append(kind)
    return kinds


def terminal_events(r, job_id, terminal_types=TERMINAL_TYPES):
    """Every terminal event on job_id's stream, over its whole history. A
    tailing browser stops at the first; this is what came after it too."""
    return events_of(r, job_id, terminal_types)


# ---------------------------------------------------------------------------
# Starting and stopping a worker agent of a check's own (check-30, check-32,
# check-35, check-36, check-67 each start at least one).
# ---------------------------------------------------------------------------
def start_agent(hostname, env_extra=None, timeout=30, tag=None, ready="heartbeat", r=None):
    """Start a worker agent under HOSTNAME=`hostname`, plus any `env_extra`,
    and block until it is up (or `timeout` elapses / it dies on startup).
    Returns the subprocess.Popen either way -- a dead-on-startup process is
    left for the caller's later assertions to notice, not raised here.

    Logs to agent-<hostname>.log (or agent-<hostname>-<tag>.log when `tag` is
    given, to avoid colliding with a first incarnation's log under the same
    hostname), matching run.sh's own `agent*.log` naming so a failing run's
    `cat agent*.log` picks it up.

    `ready` picks how readiness is decided:

      "heartbeat" (the default) waits for heartbeat_keys(r, hostname)
      (worker_ids.py) to return non-empty -- what check-36-live-worker-
      fencing.py's and check-67-job-timeout-interrupt.py's copies did.
      Requires `r`.

      "log" waits for "ready, polling" in the agent's own log instead --
      what check-32-worker-restart.py's copy did, and the only option that
      works there: that check starts a SECOND incarnation under an identity
      whose heartbeat key from the first incarnation may not have expired
      yet, so EXISTS/heartbeat_keys() cannot tell "the replacement is up"
      from "the predecessor's key hasn't lapsed" -- see check-32's own
      docstring on start_agent's role in that fixture.
    """
    env = dict(os.environ)
    env["HOSTNAME"] = hostname
    env.update(env_extra or {})
    log_path = f"agent-{hostname}-{tag}.log" if tag else f"agent-{hostname}.log"
    log = open(log_path, "w")
    proc = subprocess.Popen([sys.executable, WORKER_AGENT], env=env,
                            stdout=log, stderr=subprocess.STDOUT)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return proc  # died on startup; later assertions will show it
        if ready == "log":
            try:
                with open(log_path) as fh:
                    if "ready, polling" in fh.read():
                        return proc
            except OSError:
                pass
        elif heartbeat_keys(r, hostname):
            return proc
        time.sleep(0.1)
    return proc


def stop_agent(proc, timeout=10):
    """Terminate a check's own agent, falling back to SIGKILL if it does not
    exit within `timeout` seconds (check-67-job-timeout-interrupt.py's copy
    used timeout=15, to give its slower shutdown path room)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Showback accounting reads (check-32-worker-restart.py and
# check-90-showback.py both had these; check-90's is the more defensive
# pair, checking `data` is actually a dict before indexing it, so that is
# the version kept here).
# ---------------------------------------------------------------------------
def showback(client, path="/api/showback"):
    """The report as it stands right now, read via `client` (a Client bound
    to the gateway under test). A missing/broken endpoint, or a response
    that is not a JSON object, reads as all-zero rather than raising, so
    callers can take before/after snapshots unconditionally -- a check's own
    "the endpoint exists at all" assertion is what actually catches the
    endpoint being absent; every later assertion here is about the VALUES."""
    try:
        data = client.get(path)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def users_of(data):
    u = data.get("users")
    return u if isinstance(u, dict) else {}


def user_total(data, user):
    try:
        return float(users_of(data).get(user, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def anon_total(data):
    try:
        return float(data.get("anonymous_gpu_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def excluded_total(data):
    try:
        return float(data.get("excluded_gpu_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
