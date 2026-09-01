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

log "the queue payload envelope is mirrored, not diverged (docs/10-roadmap.md F2)"

# hub.py produces the queue payload and worker_agent.py consumes it, and the
# two files ship in two different images built from two different contexts, so
# there is nowhere for them to import a shared definition from. The envelope is
# therefore duplicated verbatim between the BEGIN/END SHARED ENVELOPE markers —
# the same "change both or neither" rule the processing-list key shape already
# follows, and the same rule that is impossible to keep by memory alone once
# four roadmap items each want to add a field. This makes it a check.
#
# Divergence here is silent in exactly the way section 3's other entries are:
# both files still compile, the suite still passes on whichever half of the
# contract the test happens to exercise, and the failure appears only when a
# gateway and a worker of different vintages meet on the queue.

if python3 - <<'EOF'
import sys

BEGIN = "# BEGIN SHARED ENVELOPE"
END = "# END SHARED ENVELOPE"
FILES = ("enterprise/gateway/hub.py", "enterprise/worker/worker_agent.py")

def block(path):
    lines = open(path).read().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(BEGIN)]
    ends = [i for i, line in enumerate(lines) if line.startswith(END)]

    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        print(f"  {path}: expected exactly one {BEGIN} ... {END} block, found "
              f"{len(starts)} begin and {len(ends)} end marker(s) — the queue "
              "payload envelope must be present in both files, delimited, and "
              "identical")
        return None

    return lines[starts[0]:ends[0] + 1]

blocks = [block(path) for path in FILES]

if any(b is None for b in blocks):
    sys.exit(1)

if blocks[0] != blocks[1]:
    import difflib
    print(f"  the shared envelope block differs between {FILES[0]} and "
          f"{FILES[1]} — change both or neither:")
    for line in difflib.unified_diff(blocks[0], blocks[1], FILES[0], FILES[1],
                                     lineterm="", n=1):
        print(f"    {line}")
    sys.exit(1)

sys.exit(0)
EOF
then
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
# Ten of the fifteen load-bearing invariants in
# docs/09-engineering-handoff.md section 3 are properties of a FILE rather than
# of a running system — a missing toleration, a Service that regained a port, a
# dropped Route annotation. The e2e suite structurally cannot see any of them:
# it runs no cluster and reads no manifest. This is where they are held, and
# the rule for adding one is that it must be decidable from the YAML alone.
#
# Each check below names the invariant it enforces and what breaks without it,
# because the failure this guards against is a well-meaning edit, not a typo.
import glob, os, re, sys, yaml

# The smallest GPU instance type this repo supports. scripts/06-status.sh's
# price table is the only enumeration of instance types in the repository:
# g4dn.xlarge, g5.xlarge, g6.xlarge, g6.2xlarge and g6e.xlarge. The first
# three carry 16 GiB of system RAM on 4 vCPU and are the floor; g6.xlarge is
# also .env.example's GPU_INSTANCE_TYPE default, so the floor is the default.
# Every number here is HOST RAM. It is unrelated to the 24 GB of VRAM on the
# L4 or A10G, and confusing the two is how the 24Gi limit got written.
GPU_NODE_MEMORY_GI = 16
GPU_NODE_VCPU = 4

# What one pod may actually claim on that node. Of the 16 GiB, the kubelet
# never offers all of it: OpenShift's automatic node sizing reserves roughly
# 2.8 GiB on a 16 GiB machine plus a 100 MiB eviction threshold, and a GPU
# node additionally runs the DaemonSets a GPU node runs — ovn-kubernetes,
# machine-config, node-exporter, the GPU operator's driver, container
# toolkit, device plugin and DCGM exporter, and NFD — whose own requests are
# on the order of 1.5-2 GiB and several hundred millicores. ~10Gi and 2 cores
# is the largest round figure that provably leaves that room.
#
# A request ABOVE this does not get you more memory; it gets you a pod that
# is Pending on a node that already exists, which reads exactly like the
# scale-from-zero failure 02-worker.yaml's nodeSelector comment describes and
# is not fixable by provisioning more nodes.
GPU_POD_MEMORY_CEILING_GI = 10
GPU_POD_CPU_CEILING = 2

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

_MEMORY_SCALE = {None: 1, 'k': 10 ** 3, 'M': 10 ** 6, 'G': 10 ** 9,
                 'T': 10 ** 12, 'Ki': 2 ** 10, 'Mi': 2 ** 20,
                 'Gi': 2 ** 30, 'Ti': 2 ** 40}

def memory_bytes(value):
    """A Kubernetes memory quantity in bytes, or None if it is not one."""
    if value is None:
        return None
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?',
                         str(value).strip())
    return float(match.group(1)) * _MEMORY_SCALE[match.group(2)] if match else None

def cpu_cores(value):
    """A Kubernetes cpu quantity in whole cores, or None if it is not one."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(m?)', text)
    if not match:
        return None
    cores = float(match.group(1))
    return cores / 1000.0 if match.group(2) else cores

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

    # A GPU pod must be sized to the node it lands on, and it must be
    # Guaranteed QoS (docs/09-engineering-handoff.md section 3).
    #
    # Two different failures, both silent. A memory LIMIT above what the node
    # can give one pod is unreachable, so the container never hits its own
    # cgroup ceiling and the real ceiling becomes node memory pressure: an
    # eviction, or a kernel OOM kill of ComfyUI, instead of a clean
    # container-level OOMKilled you can read off `oc describe`. And a pod
    # whose requests differ from its limits is Burstable, not Guaranteed:
    # the kubelet evicts it ahead of Guaranteed pods the moment it is above
    # its request, and gives it an oom_score_adj derived from that request
    # (~500 at 8Gi of 16 GiB) instead of Guaranteed's -997 — so the process
    # holding an $0.80/hour card mid-generation is a better kernel OOM victim
    # than most of the node's own daemons. Equal requests and limits cost the
    # burst headroom; on a node with one GPU and therefore one GPU pod, that
    # headroom was only ever borrowed from the kubelet and the DaemonSets
    # whose starvation is what turns this into a node-level event.
    for c in containers:
        if not wants_gpu(c):
            continue

        resources = c.get('resources') or {}
        requests = resources.get('requests') or {}
        limits = resources.get('limits') or {}
        where = f"{kind}/{name} container {c.get('name')}"

        # Memory and cpu overrun differently, so they get different reasons:
        # memory is incompressible and the kernel kills for it, cpu is
        # compressible and merely throttles — what an over-large cpu number
        # actually costs you, once requests must equal limits, is a pod the
        # scheduler cannot place.
        for kind_of, request, limit, ceiling, readable, overrun in (
                ('memory', memory_bytes(requests.get('memory')),
                 memory_bytes(limits.get('memory')),
                 GPU_POD_MEMORY_CEILING_GI * 2 ** 30,
                 f"{GPU_POD_MEMORY_CEILING_GI}Gi",
                 "the limit is unreachable, so the container never hits its "
                 "own cgroup ceiling and the real ceiling is node memory "
                 "pressure — an eviction, or a kernel OOM kill of ComfyUI, "
                 "instead of a clean container-level OOMKilled"),
                ('cpu', cpu_cores(requests.get('cpu')),
                 cpu_cores(limits.get('cpu')), GPU_POD_CPU_CEILING,
                 f"{GPU_POD_CPU_CEILING} cores",
                 "with requests equal to limits the scheduler has to find "
                 "that many cores — the pod stays Pending on a node that "
                 "already exists, which reads like the scale-from-zero failure "
                 "and is not fixable by provisioning more nodes")):

            if request is None or limit is None:
                bad(f, f"{where} requests a GPU without a readable {kind_of} "
                       f"request and limit — an unsized pod holding a GPU is "
                       "BestEffort or Burstable and is evicted first")
                continue

            if limit > ceiling:
                bad(f, f"{where} limits {kind_of} to "
                       f"{limits.get(kind_of)!r}, above the {readable} one pod "
                       f"can hold on the smallest GPU instance type this repo "
                       f"supports ({GPU_NODE_MEMORY_GI} GiB of system RAM and "
                       f"{GPU_NODE_VCPU} vCPU, less the kubelet's reserve and "
                       f"the GPU node's DaemonSets) — {overrun}")

            if request != limit:
                bad(f, f"{where} requests {kind_of} {requests.get(kind_of)!r} "
                       f"but limits it to {limits.get(kind_of)!r} — a pod "
                       "holding a GPU must be Guaranteed QoS (requests equal "
                       "to limits, cpu and memory both) so that node pressure "
                       "evicts and OOM-kills everything else first")

    # No pod in the multi-user namespace may mount a ServiceAccount token it
    # does not use. Neither hub.py nor worker_agent.py imports a Kubernetes
    # client or opens the API server — Redis is the only thing either dials —
    # and Redis itself speaks Redis. A projected token in a pod with no API
    # client is a credential lying in the filesystem for whatever else ends up
    # running there, which on the worker is arbitrary custom-node Python on a
    # node inside your VPC.
    #
    # Scoped to enterprise/manifests on purpose: manifests/base is the
    # single-user overlay, whose optional S3 model sync runs an init container
    # under an IRSA ServiceAccount and DOES need its token.
    #
    # The exception is written where the exception is created:
    # 05-oauth-proxy-patch.yaml sets it back to true in the same file that adds
    # the oauth-proxy sidecar, which really does call TokenReview and
    # SubjectAccessReview. That file is a bare patch with no kind, so it is not
    # a Deployment and this rule does not reach it.
    if kind == 'Deployment' and f.startswith('enterprise/manifests/'):
        mounts_token = dig(doc, 'spec', 'template', 'spec',
                           'automountServiceAccountToken')
        if mounts_token is not False:
            bad(f, f"Deployment/{name} does not set "
                   "automountServiceAccountToken: false. Nothing in this "
                   "namespace's own pods calls the Kubernetes API, so the "
                   "projected token is a credential with no reader except "
                   "whatever else ends up executing in the pod")

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

# Q2's non-terminal retry event (docs/10-roadmap.md). check-30-sigkill.py would
# also catch this one, and it is pinned anyway: "add the new event type to the
# terminal set" is the kind of tidying that arrives inside a diff about
# something else, and the row above says what it costs.
shape_require enterprise/gateway/hub.py \
    '^TERMINAL_TYPES = \{"completed", "failed", "cancelled"\}$' \
    "the set of event types that END a progress stream is exactly these three. Adding the retry event to it (or renaming one of them) makes every tailing browser stop reading at the retry and sit on a dead socket while the second attempt runs to completion behind it — the job succeeds and the user never sees it"

# Q3's per-user output workspaces (docs/10-roadmap.md). Both halves below are
# invisible to enterprise/test/run.sh by construction: it runs one agent, as
# one UID, on one laptop filesystem, where a directory the same process created
# is writable whatever its group bits say. The failure they guard against needs
# two pods with two different arbitrary UIDs on EFS — cluster day — and it
# presents as the SECOND worker failing to write a user's output with a
# permission error that reads like a storage fault.
shape_require enterprise/worker/worker_agent.py \
    '^WORKSPACE_DIR_MODE = 0o2775$' \
    "runtime-created output workspaces must be setgid and group-writable. OpenShift gives each pod an arbitrary high UID with GID 0 and does not keep the UID stable across pods, so g+w is what lets the next worker write into a directory this one created, and the setgid bit is what makes ComfyUI's files inside it inherit GID 0 rather than the creating pod's group"

shape_require enterprise/worker/worker_agent.py \
    'os\.chmod\(path, WORKSPACE_DIR_MODE\)' \
    "the mode above must be applied EXPLICITLY. mkdir's own mode argument is masked by umask (022 in this image, which yields 0755 — group-readable, not group-writable), so a workspace created without this chmod looks correct locally and is unwritable by the next pod on the cluster"

# The worker's two identities (worker_agent.py, note 9 / BEGIN WORKER IDENTITY).
# The heartbeat key and the processing list must both be named from the
# INCARNATION — HOSTNAME plus a nonce chosen at process start — because the
# gateway's reaper's entire liveness test is pairing those two keys by that
# suffix, and `restartPolicy: Always` restarts a container inside its pod with
# HOSTNAME unchanged. Named from HOSTNAME alone, a restarted worker heartbeats
# under the id its predecessor died holding and hides that predecessor's
# stranded job from the reaper for as long as the pod keeps restarting.
#
# enterprise/test/check-32-worker-restart.py DOES catch this, and it is pinned
# anyway for the same reason TERMINAL_TYPES is: "the nonce makes the key names
# noisy, and HOSTNAME is already unique per pod" is a true-sounding sentence
# that arrives inside a diff about something else, and the row in
# docs/09-engineering-handoff.md section 3 says what it costs.
shape_require enterprise/worker/worker_agent.py \
    '^WORKER_INCARNATION = f"\{WORKER_ID\}\{INCARNATION_SEP\}\{uuid\.uuid4\(\)\.hex\[:8\]\}"$' \
    "the worker's Redis identity must carry a nonce chosen at process start. HOSTNAME identifies the POD, and a container restarted inside its pod keeps it — so an identity taken from HOSTNAME alone is reused by the next incarnation, whose heartbeat then answers the reaper's liveness question on behalf of the dead one and strands its job with no terminal event, no GPU seconds in either bucket, and a processing entry with no TTL in a noeviction Redis"

shape_require enterprise/worker/worker_agent.py \
    '^WORKER_KEY = f"comfy:worker:\{WORKER_INCARNATION\}"$' \
    "the heartbeat key is named from the incarnation, not from WORKER_ID. WORKER_ID is the display identity — the pod name a failure message shows an operator so they can describe that pod — and it is deliberately NOT unique across restarts"

shape_require enterprise/worker/worker_agent.py \
    '^PROCESSING_KEY = f"comfy:processing:\{WORKER_INCARNATION\}"$' \
    "the processing list is named from the same incarnation as the heartbeat key above. The reaper pairs the two BY NAME, so a pair naming two different things is not a liveness test at all — and the half that would be wrong here is the one holding the stranded job"

# The pattern deliberately starts after the leading dashes: shape_require hands
# it straight to `grep -qE`, which would read "--output-directory..." as flags.
shape_require enterprise/worker/start.sh \
    'output-directory "\$OUTPUT_ROOT"' \
    "ComfyUI's output directory and the agent's OUTPUT_ROOT must be the same one variable. ComfyUI is long-lived with a single fixed --output-directory and the agent computes every submitter's workspace underneath it; hardcoding one side lets the pod start with the agent naming paths under a directory ComfyUI is not writing to, which 404s every generation and logs nothing"

# The GPU worker image's ENTRYPOINT is start.sh, so anything appended to
# `docker run <image> ...` lands in start.sh's positional parameters and
# nowhere else. CI has no GPU and boots this image with `--cpu`; without the
# forward that flag is swallowed silently, ComfyUI looks for a card that is not
# there, and the failure reads like a broken image rather than a runner with no
# card. Nothing in the e2e suite can see this: it runs worker_agent.py directly
# against a stub, never through the entrypoint.
shape_require enterprise/worker/start.sh \
    '^[[:space:]]*"\$@" &$' \
    "start.sh is the worker image's ENTRYPOINT and must forward its own arguments to ComfyUI. Nightly CI boots this image with --cpu because GitHub runners have no GPU; swallowed, the flag produces a CUDA failure that looks like an image regression"

(( SHAPE_BAD == 0 )) && ok "clean" || FAILURES=$(( FAILURES + 1 ))

# ---------------------------------------------------------------------------

log "the retry counter moves only by HINCRBY (docs/10-roadmap.md, Q2)"

# The other half of the same invariant, and the one the e2e suite structurally
# cannot reach: enterprise/test/run.sh starts ONE gateway, and
# enterprise/manifests/01-gateway.yaml runs two, each with its own reaper.
#
# "only one reaper can be holding a given entry" is what bounds FAILING a
# stranded job to once — RPOP's atomicity once, and since the reap stopped
# destroying the entry it was reaping, the per-entry claim in
# reap_processing_list(). It does not bound REQUEUEING one to once: a
# requeued job goes back on the queue, is picked up by another worker, and can
# be stranded a second time — a different entry, a different processing list,
# quite possibly the other replica's reaper. "Is this the first attempt?" is
# then a question about shared state, and read-modify-write on shared state
# (HGET the count, compare, HSET count+1) is a lost update: both replicas read
# 0, both believe they are first, and one job becomes two on one GPU pool at
# GPU prices. HINCRBY returns the post-increment value, so the decision is
# taken from the atomic operation itself and exactly one caller can see 1.
#
# A line-oriented grep is not enough here — the wrong version is naturally
# written as a multi-line hset(..., mapping={...}) — so this reads the source
# and asserts every mention of the counter outside its own definition is on a
# hincrby.

if python3 - <<'EOF'
import re, sys

PATH = "enterprise/gateway/hub.py"
FIELD = "ATTEMPT_COUNT_FIELD"

source = open(PATH).read().splitlines()

# The shared envelope block defines the name; everything after it uses it.
try:
    end = next(i for i, line in enumerate(source) if line.startswith("# END SHARED ENVELOPE"))
except StopIteration:
    print(f"  {PATH}: no END SHARED ENVELOPE marker — cannot tell the counter's "
          "definition from its uses")
    sys.exit(1)

bad, seen_hincrby = [], False

for n, line in enumerate(source[end:], start=end + 1):
    if FIELD not in line:
        continue

    if re.search(r"hincrby\(", line):
        seen_hincrby = True
    elif not line.lstrip().startswith("#"):
        bad.append((n, line.strip()))

if bad:
    print(f"  {PATH}: the retry counter must be written only by HINCRBY, whose "
          "return value the retry decision is taken from. These lines touch it "
          "some other way:")
    for n, line in bad:
        print(f"    {n}: {line}")
    sys.exit(1)

if not seen_hincrby:
    print(f"  {PATH}: nothing increments the retry counter with HINCRBY — the "
          "bound on requeue-once has gone")
    sys.exit(1)

sys.exit(0)
EOF
then
    ok "clean"
else
    FAILURES=$(( FAILURES + 1 ))
fi

# ---------------------------------------------------------------------------

log "the fair-queueing insert splices, and carries no workflow (docs/10-roadmap.md, Q1)"

# Two properties of hub.py's FAIR_ENQUEUE_LUA that nothing else can see.
#
# The suite cannot see either one: it runs a queue three jobs deep with a
# two-node workflow, where a script that walks megabytes and a script that
# walks kilobytes both finish instantly and both produce the same order.
#
#   1. It never unmakes the queue. This script runs on every submit and Redis
#      does not roll back a script's partial effects, so a version that DELs
#      comfy:queue and pushes it back has a window in which an error loses
#      every queued job at once — the "work vanishing at random" that
#      `maxmemory-policy noeviction` exists to prevent, arriving by a door
#      that policy does not cover. Injecting an error after the DEL in the
#      version this replaced emptied a 10-deep queue; injecting one anywhere
#      in this version leaves it untouched. LINSERT is what makes that true:
#      it splices one entry against a pivot and touches nothing else.
#
#   2. The list entry carries no workflow. Placing a job fairly means reading
#      every job already queued, Redis is single-threaded, and a workflow is
#      the one field whose size the client chooses (up to MAX_BODY_BYTES). In
#      the list, one submit against a 499-deep queue of 26 KB workflows cost
#      ~118 ms of exclusive Redis time — a stall on every other client,
#      including every worker parked in BLMOVE — and ~1.2 s at 103 KB. Beside
#      the list, both are ~2 ms. queue_record() is what keeps the workflow out
#      of the entry; a "simplification" back to json.dumps(envelope) restores
#      the whole cost.

if python3 - <<'EOF'
import re, sys

PATH = "enterprise/gateway/hub.py"
source = open(PATH).read()

problems = []

match = re.search(r'FAIR_ENQUEUE_LUA = """(.*?)"""', source, re.S)

if match is None:
    print(f"  {PATH}: no FAIR_ENQUEUE_LUA script to check")
    sys.exit(1)

script = "\n".join(line for line in match.group(1).splitlines()
                   if not line.lstrip().startswith("--"))

if "LINSERT" not in script:
    problems.append("the insert no longer uses LINSERT. Placing an entry any "
                    "other way means rewriting the list around it")

for destructive in ("DEL", "LTRIM", "LSET", "LREM", "LPOP", "RPOP"):
    if re.search(r"'%s'" % destructive, script):
        problems.append(f"the insert calls {destructive} on the queue. This "
                        "script must only ever ADD one entry: Redis does not "
                        "roll back a partial script, so anything that unmakes "
                        "the list can lose every queued job at once")

call = re.search(r"def fair_enqueue_call\(.*?\n\n\n", source, re.S)

if call is None:
    problems.append("fair_enqueue_call() is gone — the queue entry and the "
                    "workflow beside it are built in one place on purpose")
elif "queue_record(" not in call.group(0):
    problems.append("fair_enqueue_call() no longer builds the list entry with "
                    "queue_record(). The entry on comfy:queue must not carry "
                    "the workflow: this script reads every queued entry on "
                    "every submit, and Redis is single-threaded")

for problem in problems:
    print(f"  {PATH}: {problem}")

sys.exit(1 if problems else 0)
EOF
then
    ok "clean"
else
    FAILURES=$(( FAILURES + 1 ))
fi

# ---------------------------------------------------------------------------

log "the showback accumulator is mirrored, not diverged (docs/10-roadmap.md, Q4)"

# The same argument as the queue envelope above, for a second contract the two
# files must agree on. GPU time is written from TWO terminal paths in two
# different images: worker_agent.py's finish(), and hub.py's reaper, which
# never calls finish() at all. They must agree on the Redis key, the period,
# the field names, the identity cap and the expiry, and there is nowhere to
# import a shared definition from.
#
# Divergence here is silent in the specific way that matters for a REPORT: a
# gateway and a worker computing different period strings, or different field
# prefixes, both still run, every check still passes, and the monthly total is
# quietly split across two keys or two fields. Nobody finds that until the
# month is over and the numbers do not add up.

if python3 - <<'EOF'
import difflib, sys

MARKERS = ("# BEGIN SHARED SHOWBACK", "# END SHARED SHOWBACK")
FILES = ("enterprise/gateway/hub.py", "enterprise/worker/worker_agent.py")

def block(path, begin, end):
    lines = open(path).read().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(begin)]
    ends = [i for i, line in enumerate(lines) if line.startswith(end)]

    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        print(f"  {path}: expected exactly one {begin} ... {end} block, found "
              f"{len(starts)} begin and {len(ends)} end marker(s) — the GPU-second "
              "accumulator must be present in both files, delimited, and identical")
        return None

    return lines[starts[0]:ends[0] + 1]

blocks = [block(path, *MARKERS) for path in FILES]

if any(b is None for b in blocks):
    sys.exit(1)

if blocks[0] != blocks[1]:
    print(f"  the shared showback block differs between {FILES[0]} and "
          f"{FILES[1]} — change both or neither:")
    for line in difflib.unified_diff(blocks[0], blocks[1], FILES[0], FILES[1],
                                     lineterm="", n=1):
        print(f"    {line}")
    sys.exit(1)

sys.exit(0)
EOF
then
    ok "clean"
else
    FAILURES=$(( FAILURES + 1 ))
fi

# ---------------------------------------------------------------------------

log "the showback accumulator is period-bucketed, expiring and capped (docs/10-roadmap.md, Q4)"

# Three properties of SHOWBACK_ACCRUE_LUA that the e2e suite structurally
# cannot see, against a Redis that is `maxmemory-policy noeviction` at
# `--maxmemory 512mb` (00-redis.yaml), keyed off a header that is entirely
# client-supplied whenever AUTH_MODE=none.
#
#   1. ONE KEY PER PERIOD. check-90-showback.py does assert the key count
#      stays below the number of identities that fed it — but it drives nine
#      identities through a suite that runs for a minute, so it cannot tell
#      "one Hash per month" from "one key per identity, and this run was
#      short". A key built from the SUBMITTER is the failure: an
#      unauthenticated caller varying one header fills Redis, which presents
#      as queued work vanishing at random.
#
#   2. THE KEY EXPIRES. Nothing in a one-minute suite can observe a TTL that
#      is measured in months, so a bucket written with no expiry at all looks
#      identical to a correct one for the whole life of the test. Under
#      `noeviction` a key nothing deletes is a key forever.
#
#   3. THE FIELD COUNT IS CAPPED. Same blindness: the cap is a thousand
#      identities and the suite drives ten. Without HLEN guarding a new
#      field, the Hash itself is the unbounded thing and point 1 has only
#      moved the problem down one level.

if python3 - <<'EOF'
import re, sys

PATH = "enterprise/gateway/hub.py"
source = open(PATH).read()

problems = []

match = re.search(r'SHOWBACK_ACCRUE_LUA = """(.*?)"""', source, re.S)

if match is None:
    print(f"  {PATH}: no SHOWBACK_ACCRUE_LUA script to check")
    sys.exit(1)

script = "\n".join(line for line in match.group(1).splitlines()
                   if not line.lstrip().startswith("--"))

for needed, why in (
    ("HINCRBYFLOAT", "the accumulator must add into a FIELD of the period's "
                     "Hash. A per-submitter key is unbounded growth from a "
                     "client-supplied header"),
    ("EXPIRE", "every accrual must re-arm the bucket's expiry. HINCRBYFLOAT "
               "recreates a key that expired mid-flight, and a recreated key "
               "has no TTL at all — so arming it once, at creation, is not "
               "the same as arming it"),
    ("HLEN", "a new submitter may only be given its own field while the "
             "bucket is under the identity cap. Without this the Hash grows "
             "one field per distinct header value, forever"),
):
    if needed not in script:
        problems.append(f"the accrual script no longer calls {needed}: {why}")

# Every Redis key this script touches must be one it was HANDED — the job's
# state hash or the period bucket — never one it builds. A key computed
# inside the script from a submitter is exactly the shape all three points
# above exist to prevent.
for command, key in re.findall(r"redis\.call\('([A-Z]+)',\s*([^,)]+)", script):
    if key.strip() not in ("state", "bucket"):
        problems.append(f"the accrual script calls {command} on `{key}`, which "
                        "is not one of the two keys it is handed (KEYS[1], the "
                        "job's state hash, and KEYS[2], the period bucket). "
                        "This script must never address a key of its own "
                        "making")

# And the bucket key itself is named from the PERIOD and from nothing else.
key_fn = re.search(r"def showback_key\(period: str\) -> str:(.*?)\n\n", source, re.S)

if key_fn is None:
    problems.append("showback_key(period) is gone — the accumulator's key must "
                    "be built in one place, from the period")
else:
    # The fixed namespace prefix, plus the period, and nothing else.
    fields = set(re.findall(r"\{([^}]*)\}", key_fn.group(1))) - {"SHOWBACK_KEY_PREFIX"}

    if fields != {"period"}:
        problems.append(f"showback_key() interpolates {sorted(fields)} beside "
                        "the namespace prefix, rather than the period alone. "
                        "One Hash per period is the bound: a key carrying the "
                        "submitter is one Redis key per identity")

for problem in problems:
    print(f"  {PATH}: {problem}")

sys.exit(1 if problems else 0)
EOF
then
    ok "clean"
else
    FAILURES=$(( FAILURES + 1 ))
fi

# ---------------------------------------------------------------------------

log "the quota breaker is not reachable from readyz() (docs/10-roadmap.md, Q5)"

# The one thing about Q5 that a green test run cannot tell you.
#
# /readyz is the gateway's readiness probe (enterprise/manifests/01-gateway.yaml).
# A quota check inside it — or inside anything it calls — would take the whole
# gateway out of its Service the moment ONE submitter went over their
# GPU-second ceiling: every browser WebSocket reporting an in-flight job
# dropped, no new submissions from anybody, on a GPU pool that is still
# running work and still costing money. An outage caused by the control that
# exists to prevent one, and the roadmap names it: "It must not be wired into
# readyz()".
#
# enterprise/test/check-95-quota-breaker.py asserts /readyz stays healthy
# while a submitter is over quota, which is the runtime half. It cannot see
# the shape: a future edit that reads the quota inside a helper readyz()
# happens to call — for a "one health page shows everything" dashboard, say —
# passes that check on any gateway whose Redis is answering and whose reader
# is under the cap, and fails in production, once, at the worst moment. So the
# separation is held here as a property of the call graph instead.
#
# It is deliberately more than a grep for "quota" inside readyz's body: the
# walk below follows every module-level function readyz reaches, transitively,
# because "readyz calls health_summary() which calls quota_refusal()" is the
# same outage with one more hop in it.

if python3 - <<'EOF'
import ast, sys

PATH = "enterprise/gateway/hub.py"
source = open(PATH).read()
tree = ast.parse(source)

problems = []

# Every module-level def, sync or async, by name.
functions = {node.name: node
             for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def names_in(node):
    """
    Every name mentioned anywhere under a node: bare names, and the attribute
    half of a dotted access. Names, not source text — a docstring that
    discusses the quota is not a reference to it, and the point of this rule
    is what the code REACHES.
    """
    found = set()

    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)

    return found


def quota_names(names):
    """
    The breaker's surface, recognised by name rather than by a list kept here:
    QUOTA_GPU_SECONDS, quota_refusal(), and anything added to the block later
    is covered without anybody remembering to come back and add it.
    """
    return sorted(name for name in names if "quota" in name.lower())


# Vacuity guards. Every check below this point is an ABSENCE — "the readiness
# path does not touch the breaker" — and an absence is trivially true of a
# file with no breaker in it. These are what make the rule fail loudly if Q5
# is deleted or renamed, instead of passing most completely.
if "quota_refusal" not in functions:
    problems.append("quota_refusal() is gone. The breaker must have exactly "
                    "one entry point, or 'is the breaker in the readiness "
                    "path?' stops being a question about one call")

if "QUOTA_GPU_SECONDS" not in names_in(tree):
    problems.append("QUOTA_GPU_SECONDS is not mentioned in this file at all — "
                    "the per-user ceiling this rule keeps out of the "
                    "readiness path does not exist, so the rule is guarding "
                    "nothing")

if "readyz" not in functions:
    problems.append("readyz() is gone or has been renamed — this rule can no "
                    "longer see the readiness path it is guarding")

# The positive half: the breaker IS called, and only from the submit path.
# Without this the rule below would pass most completely if Q5 were deleted.
callers = {name for name, node in functions.items()
           if "quota_refusal" in names_in(node)}

if "quota_refusal" in functions and callers != {"generate"}:
    problems.append(f"quota_refusal() is called from {sorted(callers) or 'nothing'}, "
                    "not from generate() alone. The quota is admission "
                    "control on submission: one caller, in the one place that "
                    "can refuse before anything is written to the queue")

# The negative half, transitively: nothing readyz() reaches may touch it.
if "readyz" in functions:
    reachable, pending = set(), ["readyz"]

    while pending:
        name = pending.pop()

        if name in reachable or name not in functions:
            continue

        reachable.add(name)
        pending.extend(names_in(functions[name]) & set(functions))

    for name in sorted(reachable):
        touched = quota_names(names_in(functions[name]))

        if touched:
            via = "" if name == "readyz" else f" (reached from readyz() via {name}())"
            problems.append(f"{name}() references {touched}{via}. The quota "
                            "breaker must not be reachable from the readiness "
                            "probe: one submitter over their ceiling would "
                            "otherwise pull the whole gateway out of service "
                            "and drop every WebSocket reporting an in-flight "
                            "job")

for problem in problems:
    print(f"  {PATH}: {problem}")

sys.exit(1 if problems else 0)
EOF
then
    ok "clean"
else
    FAILURES=$(( FAILURES + 1 ))
fi

# ---------------------------------------------------------------------------

printf '\n'

if (( FAILURES == 0 )); then
    log "Lint clean"
else
    die "$FAILURES lint section(s) failed above."
fi
