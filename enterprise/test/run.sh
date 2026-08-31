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
redis-server --port 6399 --requirepass "$PASS" --appendonly no --daemonize yes
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
# ---------------------------------------------------------------------------

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
  timeout "$CHECK_TIMEOUT" python3 "$check" "$AGENT"
  RC=$?
  RAN=$(( RAN + 1 ))

  [ "$RC" -eq 0 ] || { echo "    $check FAILED (rc $RC)"; break; }
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
