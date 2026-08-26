#!/usr/bin/env bash
#
# Install the four CLIs everything else needs: aws, rosa, oc, jq.
# Idempotent — skips anything already on PATH.
#
# Red Hat's mirror has used several naming conventions for the darwin and arm64
# tarballs over the years, so rather than hardcoding one guess this probes a
# list of candidate filenames and takes the first that exists. If the mirror
# renames things again, add the new name to the candidate list.
#
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
MIRROR_BASE="https://mirror.openshift.com/pub/openshift-v4/clients"

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"

case "$ARCH_NAME" in
    x86_64|amd64)  ARCH_SLUG="" ;;
    aarch64|arm64) ARCH_SLUG="arm64" ;;
    *)             die "Unsupported architecture: $ARCH_NAME" ;;
esac

case "$OS_NAME" in
    Linux)  OS_SLUGS=("linux") ;;
    Darwin) OS_SLUGS=("mac" "darwin") ;;
    *)      die "Unsupported OS: $OS_NAME" ;;
esac

sudo_if_needed()
{
    if [[ -w "$INSTALL_DIR" ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

url_exists()
{
    curl -fsSL -o /dev/null --head --max-time 20 "$1" 2>/dev/null
}

# Build the candidate list for one product, most-likely first.
candidate_urls()
{
    local path_prefix="$1" file_stem="$2"
    local os_slug

    for os_slug in "${OS_SLUGS[@]}"; do
        if [[ -n "$ARCH_SLUG" ]]; then
            printf '%s/%s/%s-%s-%s.tar.gz\n' "$MIRROR_BASE" "$path_prefix" "$file_stem" "$os_slug" "$ARCH_SLUG"
        fi

        printf '%s/%s/%s-%s.tar.gz\n' "$MIRROR_BASE" "$path_prefix" "$file_stem" "$os_slug"
    done
}

install_from_mirror()
{
    local binary_name="$1" path_prefix="$2" file_stem="$3"
    local url found=""

    if command -v "$binary_name" >/dev/null 2>&1; then
        ok "$binary_name already installed ($(command -v "$binary_name"))"
        return 0
    fi

    log "Installing $binary_name"

    while read -r url; do
        if url_exists "$url"; then
            found="$url"
            break
        fi

        info "not found: ${url##*/}"
    done < <(candidate_urls "$path_prefix" "$file_stem")

    if [[ -z "$found" ]]; then
        warn "could not locate a $binary_name tarball for ${OS_NAME}/${ARCH_NAME}."
        info "Browse ${MIRROR_BASE}/${path_prefix}/ and install it by hand."
        return 1
    fi

    info "$found"
    curl -fsSL "$found" | sudo_if_needed tar xz -C "$INSTALL_DIR" "$binary_name"
    sudo_if_needed chmod +x "${INSTALL_DIR}/${binary_name}"

    ok "$binary_name installed"
}

# ---------------------------------------------------------------------------

install_from_mirror rosa "rosa/latest"   "rosa"             || true
install_from_mirror oc   "ocp/stable"    "openshift-client" || true

# ---------------------------------------------------------------------------

log "jq"

if command -v jq >/dev/null 2>&1; then
    ok "already installed"
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y jq && ok "installed"
elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y jq && ok "installed"
elif command -v brew >/dev/null 2>&1; then
    brew install jq && ok "installed"
else
    warn "install jq by hand — https://jqlang.github.io/jq/download/"
fi

# ---------------------------------------------------------------------------

log "aws CLI"

if command -v aws >/dev/null 2>&1; then
    ok "already installed ($(aws --version 2>&1))"
elif [[ "$OS_NAME" == "Darwin" ]]; then
    if command -v brew >/dev/null 2>&1; then
        brew install awscli && ok "installed"
    else
        warn "install with: brew install awscli"
    fi
else
    workdir="$(mktemp -d)"
    trap 'rm -rf "$workdir"' EXIT

    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH_NAME}.zip" \
        -o "${workdir}/awscliv2.zip"
    unzip -q "${workdir}/awscliv2.zip" -d "$workdir"
    sudo_if_needed "${workdir}/aws/install" --update

    ok "$(aws --version 2>&1)"
fi

# ---------------------------------------------------------------------------

log "Summary"

for tool in aws rosa oc jq; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$(printf '%-6s %s' "$tool" "$(command -v "$tool")")"
    else
        warn "$tool is still missing"
    fi
done

cat <<'EOF'

Next:
  cp .env.example .env && $EDITOR .env
  make preflight
EOF
