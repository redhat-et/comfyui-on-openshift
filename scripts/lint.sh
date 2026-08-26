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

log "manifests parse"

if python3 -c "import yaml" 2>/dev/null; then
    if python3 - <<'EOF'
import glob, sys, yaml
bad = 0
for f in sorted(glob.glob('manifests/base/*.yaml')
                + glob.glob('enterprise/manifests/*.yaml')
                + glob.glob('.github/workflows/*.yaml')):
    try:
        list(yaml.safe_load_all(open(f)))
    except yaml.YAMLError as exc:
        print(f"  parse error in {f}: {exc}")
        bad = 1
sys.exit(bad)
EOF
    then
        ok "clean"
    else
        FAILURES=$(( FAILURES + 1 ))
    fi
else
    warn "pyyaml not installed — skipping manifest parse (CI runs it)."
    info "pip install pyyaml"
fi

# ---------------------------------------------------------------------------

printf '\n'

if (( FAILURES == 0 )); then
    log "Lint clean"
else
    die "$FAILURES lint section(s) failed above."
fi
