"""Shared helper: find the Redis keys that belong to a worker IDENTITY.

A worker's heartbeat key and its processing list are named from its
INCARNATION -- the pod's HOSTNAME plus a nonce the process chooses at startup
(`worker_agent.py`, note 8) -- rather than from the HOSTNAME alone. A
container that restarts inside the same pod keeps that HOSTNAME, so an id
taken straight from it is reused by the new incarnation, whose heartbeat then
answers the gateway reaper's liveness question on the DEAD incarnation's
behalf and hides its stranded job forever (`check-32-worker-restart.py`).

The consequence for a check is small but real: "is a worker with this
HOSTNAME registered?" can no longer be `EXISTS comfy:worker:<hostname>`,
because the key carries a suffix the check cannot predict. These two
functions answer it instead, by matching the identity and every incarnation
of it.

Both shapes are matched -- the bare `comfy:worker:<host>` and the suffixed
`comfy:worker:<host>#<nonce>` -- on purpose. The bare one is what a worker
that has NOT been fixed writes, and a helper that refused to see it would
make `check-32` fail for the wrong reason (a check that cannot find the
worker at all, rather than a reaper that skipped it).
"""

# The separator between the display identity and the incarnation nonce. Not a
# character a Kubernetes pod name can contain (RFC 1123: lowercase alnum and
# '-'), so "everything before the first one" is unambiguously the pod name and
# a glob on it can never straddle two identities.
INCARNATION_SEP = "#"

WORKER_PREFIX = "comfy:worker:"
PROCESSING_PREFIX = "comfy:processing:"


def _keys_for(r, prefix, hostname):
    exact = f"{prefix}{hostname}"
    found = set(r.keys(f"{exact}{INCARNATION_SEP}*"))

    if r.exists(exact):
        found.add(exact)

    return sorted(found)


def heartbeat_keys(r, hostname):
    """Every live heartbeat key belonging to this identity. One per live
    incarnation, so len() > 1 means a restart whose predecessor's TTL has not
    yet lapsed."""
    return _keys_for(r, WORKER_PREFIX, hostname)


def processing_keys(r, hostname):
    """Every processing list belonging to this identity. Redis deletes a list
    when its last entry is removed, so a key here holds at least one job."""
    return _keys_for(r, PROCESSING_PREFIX, hostname)
