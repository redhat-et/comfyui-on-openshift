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

echo "--- worker agent"
python3 "$REPO/enterprise/worker/worker_agent.py" > agent.log 2>&1 &
AGENT=$!
for _ in $(seq 1 30); do
  grep -q "ready, polling" agent.log 2>/dev/null && break
  sleep 0.5
done
grep -q "ready, polling" agent.log || { echo "agent failed"; cat agent.log; exit 1; }
echo "    up (pid $AGENT)"

echo "--- assertions"
timeout 90 python3 check.py && timeout 150 python3 check2.py "$AGENT"
RC=$?

echo
echo "--- agent log"
cat agent.log
echo "--- gateway log (errors only)"
grep -iE 'error|traceback' gw.log | head -20

echo "--- cleanup"
kill "$AGENT" "$GW" "$COMFY" 2>/dev/null
sleep 1
kill -9 "$AGENT" "$GW" "$COMFY" 2>/dev/null
redis-cli -p 6399 -a "$PASS" --no-auth-warning shutdown nosave 2>/dev/null

exit $RC
