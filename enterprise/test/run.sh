#!/usr/bin/env bash
# End-to-end: real redis, stub ComfyUI, real worker_agent.py, real hub.py.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"; cp "$(dirname "${BASH_SOURCE[0]}")"/*.py "$WORK/"
PASS=testpass123

export REDIS_URL="redis://127.0.0.1:6399/0"
export REDIS_PASSWORD="$PASS"
export COMFY_HOST=127.0.0.1 COMFY_PORT=8999
export OUTPUT_ROOT="$WORK/output"
export BOOT_TIMEOUT=30 RECV_TIMEOUT=5 JOB_TIMEOUT=60
# The Makefile exports .env, so a developer's AUTH_MODE=oauth would put this
# shared gateway in oauth mode and break every check that submits as several
# users. check-66 starts its own oauth-mode gateway on another port.
export AUTH_MODE=none
unset SHOWBACK_OPERATORS
# Shrunk so the SIGKILL test (check-30-sigkill.py) resolves in seconds, not the
# production-default minutes. TTL must still exceed RECV_TIMEOUT above.
export HEARTBEAT_TTL=10 REAPER_INTERVAL=2

# The absolute path to the real worker agent. Every check gets a live agent's
# pid as argv[1] (see start_agent below), but check-30-sigkill.py's retry
# assertions (docs/10-roadmap.md, Q2) kill that agent mid-check and then need
# to prove a *second*, freshly-started one drains the retried job — something
# only possible between checks otherwise. $WORK holds copies of the checks and
# fixtures, not the agent itself, so the check needs this to spawn one.
export WORKER_AGENT="$REPO/enterprise/worker/worker_agent.py"

# Wall-clock ceiling per check. A kill switch, not a budget: every check here
# finishes in well under a minute, and one that does not has hung. It is a
# single number on purpose — a check needing a different one would be a second
# place to edit, which is exactly what the discovery below removes.
CHECK_TIMEOUT="${CHECK_TIMEOUT:-240}"

rm -rf "$WORK/output" "$WORK"/*.log
mkdir -p "$OUTPUT_ROOT"
echo "fake png bytes" > "$OUTPUT_ROOT/out_0001.png"

pkill -f 'uvicorn (fake_comfy|hub)' 2>/dev/null
pkill -f worker_agent 2>/dev/null
redis-cli -p 6399 -a "$PASS" --no-auth-warning shutdown nosave 2>/dev/null
sleep 1

echo "--- redis"
# --save "" and an explicit --dir, for the same reason demo-local.sh passes
# them: Redis's default save points fire during a suite this write-heavy,
# its default dir is the CALLER'S cwd (the repo root, under `make test`),
# and the dump.rdb left there is silently loaded as the starting keyspace
# by the next Redis launched from that directory — the demo's, or this
# suite's own next run. Observed as another run's keys in the demo gateway.
redis-server --port 6399 --requirepass "$PASS" --appendonly no \
    --save "" --dir "$WORK" --daemonize yes
for _ in $(seq 1 20); do
  redis-cli -p 6399 -a "$PASS" --no-auth-warning ping 2>/dev/null | grep -q PONG && break
  sleep 0.5
done
redis-cli -p 6399 -a "$PASS" --no-auth-warning ping || { echo "redis failed"; exit 1; }

echo "--- stub comfyui"
cd "$WORK" || exit 1
python3 -m uvicorn fake_comfy:app --host 127.0.0.1 --port 8999 --log-level warning > comfy.log 2>&1 &
COMFY=$!
for _ in $(seq 1 30); do
  curl -sf -m 2 http://127.0.0.1:8999/system_stats >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf -m 2 http://127.0.0.1:8999/system_stats >/dev/null || { echo "comfy failed"; cat comfy.log; exit 1; }
echo "    up (pid $COMFY)"

echo "--- gateway"
cp "$REPO/enterprise/gateway/hub.py" "$WORK/hub.py"
mkdir -p "$WORK/static" && cp "$REPO/enterprise/gateway/static/index.html" "$WORK/static/"
python3 -m uvicorn hub:app --host 127.0.0.1 --port 8100 --log-level warning > gw.log 2>&1 &
GW=$!
for _ in $(seq 1 30); do
  curl -sf -m 2 http://127.0.0.1:8100/healthz >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf -m 2 http://127.0.0.1:8100/healthz >/dev/null || { echo "gateway failed"; cat gw.log; exit 1; }
echo "    up (pid $GW)"

# ---------------------------------------------------------------------------
# The worker agent, and the checks that run against it.
#
# Checks are DISCOVERED, not listed: every enterprise/test/check*.py is copied
# into $WORK by the glob at the top of this file, run here in filename order,
# and its exit status folded into the suite's own. Adding a check is adding a
# file — there is no second place to edit, which is the point. The convention
# a new check must follow is in enterprise/test/README.md.
#
# Two guarantees the hardcoded sequence this replaced used to provide, which
# discovery has to keep:
#
#   Order. Checks run in filename order, which is why the convention is a
#   zero-padded ordinal (check-10-, check-20-, check-30-): an unpadded
#   "check10.py" sorts between "check.py" and "check2.py" and would silently
#   reorder the suite.
#
#   A live agent, and its pid. Every check is handed the pid of an agent that
#   is up and polling, because some of them signal it directly
#   (check-20 SIGTERMs it, check-30 SIGKILLs it). A check may therefore
#   legitimately leave the agent dead, so liveness is re-established before
#   each check rather than assumed — this is what the old "restarted for the
#   SIGKILL test" block did, generalised so that it holds for a check nobody
#   has written yet. A check that does not care about the agent simply ignores
#   argv[1].
#
# The suite still stops at the first failing check: later checks starting from
# the wreckage of an earlier failure report noise, not findings.
#
# TWO WAYS A CHECK FAILS, and both are folded. Exit status is the contract
# (enterprise/test/README.md), but it is a contract a check can hold up to and
# still lie: every check in this directory prints one PASS/FAIL line per
# assertion through the same four-line check() helper, collects the failures in
# a list, and turns that list into its exit code in its LAST few lines. A check
# that loses those lines — an edit that drops the `sys.exit(1)`, a `raise
# SystemExit` under an `if` that stopped being true, a helper that appends to a
# list nothing reads — prints its FAIL lines exactly as loudly as ever and
# exits 0, and a suite that folds only `$?` stays green while its own output
# says otherwise. CI reads that same exit code, so nobody sees the FAIL lines
# either. So the output is read as well, and a printed failure fails the suite
# on its own.
#
# THE MARKER is the check() helper's own line format, anchored: a line that
# begins with exactly two spaces, then FAIL, then two more spaces
# (FAIL_MARKER below). It is not a bare search for the word, which would fire
# on a check's legitimate prose — an assertion NAME containing "must not FAIL",
# a Redis value echoed into a detail field, the `N FAILED: [...]` summary line
# every check prints (that one starts with a digit, not with the marker, and
# only ever appears beside a non-zero exit anyway). Anchoring at the start of a
# line and requiring the helper's exact two-space padding means the only thing
# that trips it is a check reporting a failed assertion in the format this
# suite defines for reporting one. A green run prints no such line at all.
# ---------------------------------------------------------------------------

# Two spaces, FAIL, two spaces, at the start of a line — see above. Kept as one
# named value because the README documents it as the reporting contract a new
# check has to follow, and a marker spelled twice is a marker that is wrong in
# one of the places.
FAIL_MARKER='^  FAIL  '

AGENT=""
AGENT_N=0
AGENT_PIDS=""

start_agent()
{
  AGENT_N=$(( AGENT_N + 1 ))
  local log="agent${AGENT_N}.log"

  python3 "$REPO/enterprise/worker/worker_agent.py" > "$log" 2>&1 &
  AGENT=$!
  AGENT_PIDS="$AGENT_PIDS $AGENT"

  for _ in $(seq 1 30); do
    grep -q "ready, polling" "$log" 2>/dev/null && break
    sleep 0.5
  done
  grep -q "ready, polling" "$log" || { echo "agent failed"; cat "$log"; return 1; }
  echo "    up (pid $AGENT, $log)"
}

# `kill -0` alone is not enough: a child that bash has not yet reaped is a
# zombie and still answers to it. Only state Z is conclusive here —
# an empty state (no ps, a race) is read as alive on purpose, because starting
# a second agent beside a live one would put two workers in the registered
# count and no assertion about worker death can see through that.
agent_alive()
{
  [ -n "$AGENT" ] || return 1
  kill -0 "$AGENT" 2>/dev/null || return 1
  case "$(ps -p "$AGENT" -o state= 2>/dev/null)" in
    Z*) return 1 ;;
  esac
  return 0
}

echo "--- worker agent"
start_agent || exit 1

echo "--- assertions"
RC=0
RAN=0

for check in check*.py; do
  [ -e "$check" ] || break        # nothing matched the glob

  if ! agent_alive; then
    echo "--- worker agent (the previous check left it dead — restarting)"
    start_agent || { RC=1; break; }
  fi

  echo "--- $check (agent pid $AGENT)"

  # tee, not a redirect: the output still streams to the terminal (and to CI's
  # log) as it is produced, and a copy is kept so the FAIL marker can be read
  # off it. PIPESTATUS[0] rather than $?, because $? is tee's status here.
  # stderr is folded in so a traceback lands in the same place as the rest.
  CHECK_OUT="${check%.py}.out"
  timeout "$CHECK_TIMEOUT" python3 "$check" "$AGENT" 2>&1 | tee "$CHECK_OUT"
  RC=${PIPESTATUS[0]}
  RAN=$(( RAN + 1 ))

  PRINTED_FAILURES=$(grep -c "$FAIL_MARKER" "$CHECK_OUT" 2>/dev/null || true)
  PRINTED_FAILURES=${PRINTED_FAILURES//[^0-9]/}

  # Which one fired is named, because they are different bugs: a non-zero exit
  # is a check that failed, and a printed failure with rc 0 is a check whose
  # own exit status stopped reporting what its assertions found.
  if [ "$RC" -ne 0 ]; then
    echo "    $check FAILED (rc $RC, ${PRINTED_FAILURES:-0} printed FAIL line(s))"
    break
  fi

  if [ "${PRINTED_FAILURES:-0}" -gt 0 ]; then
    echo "    $check FAILED (${PRINTED_FAILURES} printed FAIL line(s) but exited 0"
    echo "    — the check's own exit status no longer reports what it asserted)"
    RC=1
    break
  fi
done

if [ "$RC" -eq 0 ] && [ "$RAN" -eq 0 ]; then
  echo "no checks discovered — enterprise/test/check*.py matched nothing"
  RC=1
fi

echo
echo "--- agent logs"
cat agent*.log 2>/dev/null
echo "--- gateway log (errors only)"
grep -iE 'error|traceback' gw.log | head -20

echo "--- cleanup"
# shellcheck disable=SC2086  # deliberate word splitting: one pid per agent
kill $AGENT_PIDS "$GW" "$COMFY" 2>/dev/null
sleep 1
# shellcheck disable=SC2086
kill -9 $AGENT_PIDS "$GW" "$COMFY" 2>/dev/null
redis-cli -p 6399 -a "$PASS" --no-auth-warning shutdown nosave 2>/dev/null

exit $RC
