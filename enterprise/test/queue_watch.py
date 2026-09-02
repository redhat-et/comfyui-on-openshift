"""Shared helper: prove a write to comfy:queue did, or did not, happen -- by
watching the command Redis actually executed, not by reading LLEN afterwards.

Why LLEN after the fact cannot do this job: this suite runs one worker agent
against one queue, so the same process that would wrongly requeue a job is
also the only thing polling that queue. By the time a check has drained a job
to its terminal event, any entry a wrong implementation put back has
necessarily already been popped again -- LLEN reads 0 whether the requeue
never happened, or happened and was undone before anyone looked. Three
assertions built on that read ("comfy:queue is empty after...") were proven
unable to fail by mutation testing; see check-20-failure-paths.py and
check-30-sigkill.py for what replaced them.

MONITOR sees the command the instant Redis executes it, popped or not -- and
that includes the LPUSH/RPUSH/LINSERT a Lua script issues on the caller's
behalf. A fair-queueing insert runs as one EVALSHA, but MONITOR renders the
script's own redis.call()s too (tagged with client "lua"), so watching for the
real list-mutating commands catches a requeue whether it goes through the
front door (a plain LPUSH, the empty-queue case) or the fair-queueing script's
LINSERT -- without needing to know which one a given change will pick.
"""
import threading
import time

import redis

# The only commands that put an entry ON a list -- i.e. that could requeue a
# job. LPOP/RPOP/BLMOVE/BRPOP name the same key when POPPING one and must not
# be counted; LLEN/LRANGE are reads and never appear here. LINSERT is what
# FAIR_ENQUEUE_LUA actually calls for the ordinary case (hub.py); LPUSH/RPUSH
# are its empty-queue and served-first fallbacks.
WRITE_COMMANDS = {"LPUSH", "RPUSH", "LPUSHX", "RPUSHX", "LINSERT"}


def is_queue_write(parts, key):
    """The default test: one of WRITE_COMMANDS naming `key` as an argument.
    `parts` is the executed command split on whitespace, its first element
    the command name as MONITOR rendered it."""
    return bool(parts) and parts[0].upper() in WRITE_COMMANDS and key in parts[1:]


class QueueWriteWatcher:
    """Counts writes to `key` between start() and stop(), via a dedicated
    MONITOR connection -- a command log, so it cannot miss a write that gets
    popped again before anything reads the list's length.

    `matches` is the predicate applied to every command MONITOR renders,
    called as matches(parts, key) with the command split on whitespace. It
    defaults to is_queue_write(), so an existing caller sees exactly the
    behaviour above; a check counting something else on the same command
    log -- the gateway's check-15 counts SCANs -- passes its own rather than
    overriding _run()."""

    def __init__(self, redis_url, password, key, matches=is_queue_write):
        self._key = key
        self._matches = matches
        self._conn = redis.from_url(redis_url, password=password, decode_responses=True)
        self._monitor = self._conn.monitor()
        self._count = 0
        self._lines = []
        self._ready = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            with self._monitor as m:
                self._ready.set()
                for entry in m.listen():
                    cmd = entry.get("command") or ""
                    parts = cmd.split()
                    if self._matches(parts, self._key):
                        self._count += 1
                        self._lines.append(cmd)
        except Exception:  # noqa: BLE001 - stop() closes the socket to end
            pass          # listen(); the resulting error lands here, not a bug
        finally:
            self._done.set()

    def start(self, timeout=5):
        """Block until MONITOR is actually armed server-side, plus a short
        margin -- a write issued the instant start() returns must not land in
        the gap between the MONITOR command being sent and this thread
        actually being scheduled to read from the socket."""
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"QueueWriteWatcher did not attach within {timeout}s")
        time.sleep(0.1)
        return self

    def stop(self, timeout=5):
        """Stop watching. Returns (count, [commands]) -- how many writes to
        `key` were observed, and what they were, for a failing check's
        detail."""
        try:
            self._monitor.connection.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._done.wait(timeout)
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        return self._count, list(self._lines)
