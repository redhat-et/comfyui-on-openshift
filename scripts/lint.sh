#!/usr/bin/env bash
#
# Everything CI checks, runnable locally: shellcheck, bash syntax, the
# macOS-bash-3.2 portability grep, Python compilation, and manifest parsing.
# `make lint` runs this; .github/workflows/ci.yaml runs this; if they ever
# disagree, this file is the one that drifted.
#
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

cd "$REPO_ROOT" || exit 1

FAILURES=0

SHELL_FILES=(scripts/*.sh scripts/lib/common.sh enterprise/*.sh
             enterprise/worker/start.sh enterprise/test/run.sh)

PYTHON_FILES=(enterprise/gateway/hub.py enterprise/worker/worker_agent.py
              enterprise/test/*.py)

# ---------------------------------------------------------------------------

log "shellcheck"

if command -v shellcheck >/dev/null 2>&1; then
    # warning severity: the info-level notes (SC1091 dynamic sourcing, SC2016
    # literal $ in printf) are deliberate in this codebase.
    if shellcheck -x --severity=warning "${SHELL_FILES[@]}"; then
        ok "clean"
    else
        FAILURES=$(( FAILURES + 1 ))
    fi
else
    warn "shellcheck not installed — skipping (CI runs it)."
    info "brew install shellcheck / apt-get install shellcheck"
fi

# ---------------------------------------------------------------------------

log "bash syntax"

SYNTAX_BAD=0

for f in "${SHELL_FILES[@]}"; do
    bash -n "$f" || { warn "syntax error: $f"; SYNTAX_BAD=1; }
done

(( SYNTAX_BAD == 0 )) && ok "clean" || FAILURES=$(( FAILURES + 1 ))

# ---------------------------------------------------------------------------

log "macOS bash 3.2 portability (scripts/ runs on the operator's laptop)"

# Comment lines and flags like `--wait -n` are excluded by the patterns.
if grep -rnE '^[^#]*declare -A|^[^#]*[[:space:]]wait -n' scripts/; then
    warn "bash-4-only construct in scripts/ — breaks stock macOS bash 3.2"
    FAILURES=$(( FAILURES + 1 ))
else
    ok "clean"
fi

# ---------------------------------------------------------------------------

log "python compiles"

if python3 -m py_compile "${PYTHON_FILES[@]}"; then
    ok "clean"
else
    FAILURES=$(( FAILURES + 1 ))
fi

# ---------------------------------------------------------------------------

log "manifests parse and hold their shape"

if python3 -c "import yaml" 2>/dev/null; then
    if python3 - <<'EOF'
# Parse, then assert shape.
#
# Nine of the fourteen load-bearing invariants in
# docs/09-engineering-handoff.md section 3 are properties of a FILE rather than
# of a running system — a missing toleration, a Service that regained a port, a
# dropped Route annotation. The e2e suite structurally cannot see any of them:
# it runs no cluster and reads no manifest. This is where they are held, and
# the rule for adding one is that it must be decidable from the YAML alone.
#
# Each check below names the invariant it enforces and what breaks without it,
# because the failure this guards against is a well-meaning edit, not a typo.
import glob, os, sys, yaml

problems = []

def bad(where, msg):
    problems.append((where, msg))

def dig(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj

def containers_of(pod):
    return [c for c in (dig(pod, 'containers') or []) if isinstance(c, dict)]

def env_of(container, name):
    for entry in (container.get('env') or []):
        if isinstance(entry, dict) and entry.get('name') == name:
            return entry.get('value')
    return None

def wants_gpu(container):
    resources = container.get('resources') or {}
    return any('nvidia.com/gpu' in (resources.get(k) or {})
               for k in ('requests', 'limits'))

# ---------------------------------------------------------------------------
# Load. A document may be a bare strategic-merge patch with no kind
# (05-oauth-proxy-patch.yaml), and the workflow files are not Kubernetes at
# all, so every check below decides for itself what it applies to.
docs = []
for f in sorted(glob.glob('manifests/base/*.yaml')
                + glob.glob('enterprise/manifests/*.yaml')
                + glob.glob('.github/workflows/*.yaml')):
    try:
        for doc in yaml.safe_load_all(open(f)):
            if isinstance(doc, dict):
                docs.append((f, doc))
    except yaml.YAMLError as exc:
        bad(f, f"parse error: {exc}")

for f, doc in docs:
    kind = doc.get('kind')
    name = dig(doc, 'metadata', 'name')
    annotations = dig(doc, 'metadata', 'annotations') or {}
    pod = dig(doc, 'spec', 'template', 'spec')
    containers = containers_of(pod) if isinstance(pod, dict) else []

    # A pod that asks for a GPU must tolerate the GPU node's taint. Without
    # it the pod is unschedulable forever and — because the cluster
    # autoscaler builds its from-zero template from the pool's taints too —
    # no GPU node is ever provisioned for it either. It presents as a pod
    # stuck Pending with no error naming the cause.
    if any(wants_gpu(c) for c in containers):
        tolerations = [t for t in (pod.get('tolerations') or [])
                       if isinstance(t, dict)]
        if not any(t.get('key') == 'nvidia.com/gpu' for t in tolerations):
            bad(f, f"{kind}/{name} requests nvidia.com/gpu but tolerates no "
                   "nvidia.com/gpu taint — it can never schedule onto a GPU node")

    # The SIGTERM drain needs a window longer than the job it is draining.
    # The pool scales to zero, so termination is routine; a grace period
    # shorter than JOB_TIMEOUT means the kubelet SIGKILLs the agent partway
    # through the drain and the work is thrown away anyway.
    for c in containers:
        job_timeout = env_of(c, 'JOB_TIMEOUT')
        if job_timeout is None:
            continue
        grace = pod.get('terminationGracePeriodSeconds')
        try:
            over = grace is not None and int(grace) > int(job_timeout)
        except (TypeError, ValueError):
            over = False
        if not over:
            bad(f, f"{kind}/{name} container {c.get('name')} sets "
                   f"JOB_TIMEOUT={job_timeout} but "
                   f"terminationGracePeriodSeconds={grace} — a job may legally "
                   "run past the drain window and be SIGKILLed mid-render")

    # Both Route annotations or neither works. On edge and reencrypt routes
    # only timeout-tunnel governs the upgraded WebSocket, against a one-hour
    # router default; plain timeout alone still drops long generations, and
    # it reads exactly like an application bug.
    if kind == 'Route':
        for annotation in ('haproxy.router.openshift.io/timeout',
                           'haproxy.router.openshift.io/timeout-tunnel'):
            if annotation not in annotations:
                bad(f, f"Route/{name} has no {annotation} — long generations "
                       "die at the router's default timeout")

        if dig(doc, 'spec', 'to', 'name') == 'comfy-worker':
            bad(f, f"Route/{name} points at the GPU workers, which have no "
                   "Service and no Route on purpose")

    if kind == 'Service':
        ports = [p.get('port') for p in (dig(doc, 'spec', 'ports') or [])
                 if isinstance(p, dict)]

        # The oauth-proxied Service exposes the proxy port and nothing else.
        # 8000 is the gateway's own port, bound to loopback in the pod by
        # 05-oauth-proxy-patch.yaml; putting it back on this list lets
        # anything in the cluster reach the gateway without logging in,
        # whatever the pod binds.
        if 8443 in ports and 8000 in ports:
            bad(f, f"Service/{name} exposes the oauth-proxy port 8443 and the "
                   "gateway's own 8000 — 8000 bypasses the login")

        if dig(doc, 'spec', 'selector', 'app') == 'comfy-worker':
            bad(f, f"Service/{name} selects the GPU workers, which are "
                   "unaddressable by design — the agent is the only way in")

    # Redis must not evict. The default policy silently drops queued jobs
    # under memory pressure, which presents as work vanishing at random.
    for c in containers:
        if c.get('name') != 'redis':
            continue
        args = [str(a) for a in (c.get('args') or [])]
        if 'noeviction' not in args:
            bad(f, "the redis container does not set maxmemory-policy "
                   "noeviction — the default evicts queued jobs")

    # Under AUTH_MODE=oauth the gateway itself must be rebound to loopback,
    # or the proxy is a formality: anything in the cluster reaches :8000
    # directly. This is the half of the invariant that lives in the patch.
    if os.path.basename(f) == '05-oauth-proxy-patch.yaml':
        gateways = [c for c in containers if c.get('name') == 'gateway']
        command = [str(x) for x in (gateways[0].get('command') or [])] if gateways else []
        host = None
        if '--host' in command:
            after = command.index('--host') + 1
            host = command[after] if after < len(command) else None
        if host != '127.0.0.1':
            bad(f, "the oauth patch does not rebind the gateway container to "
                   "--host 127.0.0.1 — the proxy can be bypassed inside the cluster")

for where, msg in problems:
    print(f"  {where}: {msg}")
sys.exit(1 if problems else 0)
EOF
    then
        ok "clean"
    else
        FAILURES=$(( FAILURES + 1 ))
    fi
else
    warn "pyyaml not installed — skipping manifest checks (CI runs them)."
    info "pip install pyyaml"
fi

# ---------------------------------------------------------------------------

log "load-bearing file shapes (docs/09-engineering-handoff.md section 3)"

# The rest of section 3's file-level invariants, in files that are not YAML and
# so have no parser to hang a check on. A grep is enough here and needs no
# dependency — which matters, because these two are the ones whose absence
# produces a crash-loop or an unauthenticated RCE rather than a test failure.

SHAPE_BAD=0

shape_require()
{
    local file="$1" pattern="$2" why="$3"

    if [[ ! -f "$file" ]]; then
        warn "missing: $file — $why"
        SHAPE_BAD=1
    elif ! grep -qE "$pattern" "$file"; then
        warn "$file no longer matches /$pattern/ — $why"
        SHAPE_BAD=1
    fi
}

for containerfile in app/Containerfile enterprise/worker/Containerfile; do
    shape_require "$containerfile" 'chgrp -R 0' \
        "OpenShift runs the container as an arbitrary high UID with GID 0. Without the chgrp 0 / chmod g=u block ComfyUI cannot write temp/, input/ or user/, and the pod crash-loops on a permission error that reads like a storage problem"
    shape_require "$containerfile" 'chmod -R g=u' \
        "the group-writable half of the same block — group-root ownership alone does not make the paths writable"
done

shape_require enterprise/worker/start.sh 'COMFY_HOST:-127\.0\.0\.1' \
    "ComfyUI in the GPU pod must default to loopback. The pod has no Service and no Route, so the agent beside it is the only way in; binding 0.0.0.0 publishes unauthenticated arbitrary code execution on a node holding cloud credentials"

(( SHAPE_BAD == 0 )) && ok "clean" || FAILURES=$(( FAILURES + 1 ))

# ---------------------------------------------------------------------------

printf '\n'

if (( FAILURES == 0 )); then
    log "Lint clean"
else
    die "$FAILURES lint section(s) failed above."
fi
