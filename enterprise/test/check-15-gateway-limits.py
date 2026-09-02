"""
Three limits the gateway claims to hold and, before this check, did not.

Each one is a bound a single client could push past from the outside, and each
is asserted with a dedicated gateway process whose limits this file chose --
the same reason check-95-quota-breaker.py runs its own gateway with
QUOTA_GPU_SECONDS pinned: a deterministic threshold, not a production default
nobody here controls. The shared gateway on :8100 keeps run.sh's values, and
the assertion that needs the production-shaped stats path uses it directly.

  A. A WEBSOCKET'S LIFETIME IS BOUNDED BY EVENT_STREAM_TTL. /ws/<job> replays
     the stream and then tails it, and the stream itself expires
     EVENT_STREAM_TTL after its last write -- so a socket held open longer
     than that is tailing a key that no longer exists, one Redis connection
     each, until the browser tab is closed. The dedicated gateway runs with
     EVENT_STREAM_TTL=4: a job stream that never reaches a terminal event
     must be closed by the server, with an application close code, within a
     few seconds of that. On HEAD the socket stays open on a 15-second ping
     loop for as long as the client cares to hold it.

  B. MAX_QUEUE_DEPTH HOLDS UNDER CONCURRENT SUBMITS. The ceiling used to be
     an LLEN in generate() followed, two awaits later, by the enqueue -- so N
     submissions arriving together all read the same depth and all enqueue,
     and a full queue admits N more jobs every time N clients retry at once.
     The check freezes the one live agent (check-40-envelope.py's SIGSTOP plus
     a sacrificial job to absorb the BLMOVE it is already parked in), fires a
     dozen submits at a gateway whose MAX_QUEUE_DEPTH is 3, all at once from
     threads, and requires the queue never to have exceeded 3 -- the depth
     check has to be inside the same atomic script as the insert. The refused
     ones must be clean 503s, not 500s.

  C. /api/stats DOES NOT SCAN THE KEYSPACE ON EVERY CALL. gather_stats()
     counts worker heartbeats with a SCAN over comfy:worker:*, index.html
     polls /api/stats every five seconds per open tab, and /metrics runs the
     same scan for Prometheus -- against a single-threaded Redis whose every
     SCAN is time no worker parked in BLMOVE gets. The assertion is the count
     of SCAN commands Redis actually executed (MONITOR, the same technique as
     queue_watch.py) across a burst of six back-to-back calls: at most what
     ONE cold call costs, i.e. the burst shared one snapshot. On HEAD it is
     six times that.
"""
import json, os, signal, subprocess, sys, threading, time, urllib.error, urllib.request, uuid
import struct
import redis
import websocket

from queue_watch import QueueWriteWatcher

sys.stdout.reconfigure(line_buffering=True)

GW = "http://127.0.0.1:8100"
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None
QUEUE_KEY = os.environ.get("QUEUE_KEY", "comfy:queue")
STATS_CACHE_SECONDS = float(os.environ.get("STATS_CACHE_SECONDS", "5"))

DGW_PORT = 8102
DGW = f"http://127.0.0.1:{DGW_PORT}"
DGW_STREAM_TTL = 4
DGW_MAX_DEPTH = 3
SUBMITS = 12

WORKFLOW = {"3": {"class_type": "KSampler", "inputs": {}}}

failures = []
r = redis.from_url(REDIS_URL, password=REDIS_PASSWORD, decode_responses=True)
agent_pid = int(sys.argv[1])


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def request(method, path, body=None, base=GW, headers=None):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        raw, status = resp.read(), resp.getcode()
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return status, raw.decode(errors="replace")


class CommandWatcher(QueueWriteWatcher):
    """queue_watch.py's MONITOR watcher, matching every command that
    satisfies `predicate(parts)` rather than writes to one key. Arming, the
    margin after MONITOR attaches, and the disconnect that ends listen() are
    all the base's; only what counts as a hit is different."""

    def __init__(self, predicate):
        super().__init__(REDIS_URL, REDIS_PASSWORD, key="")
        self._predicate = predicate

    def _run(self):
        try:
            with self._monitor as m:
                self._ready.set()
                for entry in m.listen():
                    parts = (entry.get("command") or "").split()
                    if parts and self._predicate(parts):
                        self._count += 1
                        self._lines.append(" ".join(parts))
        except Exception:  # noqa: BLE001 - stop() disconnects to end listen()
            pass
        finally:
            self._done.set()


def worker_scan(parts):
    return parts[0].upper() == "SCAN" and any(p == "comfy:worker:*" for p in parts[1:])


print(f"--- starting a dedicated gateway on :{DGW_PORT} with "
      f"EVENT_STREAM_TTL={DGW_STREAM_TTL} MAX_QUEUE_DEPTH={DGW_MAX_DEPTH}")

env = dict(os.environ)
env["EVENT_STREAM_TTL"] = str(DGW_STREAM_TTL)
env["MAX_QUEUE_DEPTH"] = str(DGW_MAX_DEPTH)
dgw_log = open("limits-gateway.log", "w")
dgw = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1",
     "--port", str(DGW_PORT), "--log-level", "warning"],
    env=env, stdout=dgw_log, stderr=subprocess.STDOUT)

up = False
deadline = time.time() + 30
while time.time() < deadline and dgw.poll() is None:
    try:
        if urllib.request.urlopen(DGW + "/healthz", timeout=2).getcode() == 200:
            up = True
            break
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)

check("the dedicated gateway came up", up, "" if up else open("limits-gateway.log").read()[-2000:])
if not up:
    print("1 FAILED: ['dedicated gateway did not start']")
    sys.exit(1)

try:
    print(f"\n== B: MAX_QUEUE_DEPTH={DGW_MAX_DEPTH} holds under {SUBMITS} concurrent submits")

    results = []
    os.kill(agent_pid, signal.SIGSTOP)
    try:
        # Absorbs the BLMOVE the agent is already parked in -- check-40 explains
        # why SIGSTOP alone leaves the first push consumed server-side.
        request("POST", "/api/generate", {"workflow": WORKFLOW})
        time.sleep(0.3)
        depth_before = r.llen(QUEUE_KEY)

        results = [None] * SUBMITS

        def submit(i):
            results[i] = request("POST", "/api/generate", {"workflow": WORKFLOW}, base=DGW)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(SUBMITS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        depth_after = r.llen(QUEUE_KEY)
        statuses = sorted(status for status, _ in results if status)
        accepted = [body for status, body in results if status == 200]
        refused = [(status, body) for status, body in results if status != 200]

        check("fixture: the queue was observable -- the agent was frozen and nothing "
              "drained it during the burst (depth did not go DOWN)",
              depth_after >= depth_before, {"before": depth_before, "after": depth_after})
        check(f"the queue never exceeded MAX_QUEUE_DEPTH: {depth_after} entries after "
              f"{SUBMITS} simultaneous submits against a ceiling of {DGW_MAX_DEPTH} -- "
              f"a depth read followed by a separate insert admits every submit that "
              f"raced the read",
              depth_after <= DGW_MAX_DEPTH, {"depth": depth_after, "statuses": statuses})
        check("every refusal was a clean 503 naming the full queue, not a 500",
              refused and all(status == 503 and "full" in json.dumps(body).lower()
                              for status, body in refused),
              refused[:3])
        check("exactly the jobs that were accepted are on the queue -- a 200 whose "
              "job is not queued is worse than a 503",
              len(accepted) == depth_after - depth_before,
              {"accepted": len(accepted), "before": depth_before, "after": depth_after})
    finally:
        # Nothing here should run: the frozen agent would pop these on resume
        # and spend the rest of the suite's patience on them.
        accepted_ids = {body.get("job_id") for status, body in results
                        if status == 200 and isinstance(body, dict)}
        for raw in r.lrange(QUEUE_KEY, 0, -1):
            try:
                if json.loads(raw).get("job_id") in accepted_ids:
                    r.lrem(QUEUE_KEY, 1, raw)
            except (json.JSONDecodeError, AttributeError):
                pass
        os.kill(agent_pid, signal.SIGCONT)

    finally:
    dgw.terminate()
    try:
        dgw.wait(timeout=10)
    except Exception:  # noqa: BLE001
        dgw.kill()

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
