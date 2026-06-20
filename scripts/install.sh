#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
CONFIRM_TOKEN="INSTALL_PILOTTUNNEL"

DEFAULT_REPO_URL="https://github.com/CapoLab/PilotTunnel.git"
DEFAULT_REF="main"
DEFAULT_INSTALL_DIR="/opt/pilottunnel"
DEFAULT_LAYER="layer4"
DEFAULT_PROVIDER_REPO="CapoLab/PilotTunnel-Binaries"
DEFAULT_PROVIDER_TAG="pt-binaries-20""26-06-20"
DEFAULT_MANIFEST_NAME="provider-manifest.json"
DEFAULT_RELEASES_SEGMENT="releases"
DEFAULT_DOWNLOAD_SEGMENT="download"
DEFAULT_PROVIDER_HOSTS="github.com,github-releases.githubusercontent.com,objects.githubusercontent.com,release-assets.githubusercontent.com"

ROLE=""
LAYER="$DEFAULT_LAYER"
REPO_URL="$DEFAULT_REPO_URL"
REF="$DEFAULT_REF"
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
DRY_RUN=0
CONFIRM_VALUE=""
MANIFEST_URL=""
MANIFEST_FILE=""
ALLOW_PROVIDER_HOST="$DEFAULT_PROVIDER_HOSTS"
WITH_BINARIES=1

OS_ID="unknown"
OS_NAME="Unknown Linux"
OS_LIKE=""
PKG_MANAGER=""
LAYER_SUPPORTED=0
PYTHON_BIN=""

usage() {
  cat <<EOF
PilotTunnel production-oriented Linux bootstrap helper

Usage:
  bash ${SCRIPT_NAME} --role <controller|worker> --layer layer4 --dry-run
  bash ${SCRIPT_NAME} --role <controller|worker> --layer layer4 --confirm ${CONFIRM_TOKEN}

Options:
  --role <ROLE>           Required in non-interactive mode. Use controller or worker.
  --layer <LAYER>         Optional. Defaults to layer4.
  --repo-url <REPO_URL>   Optional. Defaults to the public PilotTunnel repo.
  --ref <REF>             Optional. Defaults to main.
  --install-dir <DIR>     Optional. Defaults to /opt/pilottunnel.
  --with-binaries         Download, import, and verify required provider binaries.
  --without-binaries      Skip binary download/import/verify during bootstrap.
  --manifest-url <URL>    Optional provider manifest URL override.
  --manifest-file <FILE>  Optional local provider manifest file override.
  --allow-provider-host <HOST[,HOST...]>
                          Optional provider manifest/artifact allowlist.
  --dry-run               Print the safe bootstrap plan without cloning or writing files.
  --confirm <TOKEN>       Required for apply mode. Must be ${CONFIRM_TOKEN}.
  --help                  Show this help text.

Safety:
  - No service start, stop, restart, enable, or disable is performed.
  - No daemon reload is performed.
  - No firewall, route, or interface changes are performed.
  - No tunnel adapter binaries are executed.
  - Layer 4 is runnable in v0.1; other known layers are planned-only.
EOF
}

fail() {
  printf '%s\n' "Error: $*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

find_python() {
  if command_exists python3; then
    printf '%s\n' "python3"
    return
  fi
  if command_exists python; then
    printf '%s\n' "python"
    return
  fi
  printf '%s\n' ""
}

redact_repo_url() {
  printf '%s\n' "$1" | sed -E 's#://[^/@]+@#://***@#'
}

default_manifest_url() {
  printf 'https://github.com/%s/%s/%s/%s/%s\n' "$DEFAULT_PROVIDER_REPO" "$DEFAULT_RELEASES_SEGMENT" "$DEFAULT_DOWNLOAD_SEGMENT" "$DEFAULT_PROVIDER_TAG" "$DEFAULT_MANIFEST_NAME"
}

detect_os_release() {
  if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_NAME="${NAME:-Unknown Linux}"
    OS_LIKE="${ID_LIKE:-}"
  fi

  case "${OS_ID}:${OS_LIKE}" in
    ubuntu:*|debian:*|*:debian*|*:ubuntu*) PKG_MANAGER="apt-get" ;;
    fedora:*|*:fedora*|*:rhel*|*:centos*)
      if command_exists dnf; then PKG_MANAGER="dnf"; else PKG_MANAGER="yum"; fi
      ;;
    alpine:*) PKG_MANAGER="apk" ;;
    opensuse*:*|sles:*) PKG_MANAGER="zypper" ;;
    *) PKG_MANAGER="" ;;
  esac
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --role) [ $# -ge 2 ] || fail "--role requires a value"; ROLE="$2"; shift 2 ;;
      --layer) [ $# -ge 2 ] || fail "--layer requires a value"; LAYER="$2"; shift 2 ;;
      --repo-url) [ $# -ge 2 ] || fail "--repo-url requires a value"; REPO_URL="$2"; shift 2 ;;
      --ref) [ $# -ge 2 ] || fail "--ref requires a value"; REF="$2"; shift 2 ;;
      --install-dir) [ $# -ge 2 ] || fail "--install-dir requires a value"; INSTALL_DIR="$2"; shift 2 ;;
      --with-binaries) WITH_BINARIES=1; shift ;;
      --without-binaries) WITH_BINARIES=0; shift ;;
      --manifest-url) [ $# -ge 2 ] || fail "--manifest-url requires a value"; MANIFEST_URL="$2"; shift 2 ;;
      --manifest-file) [ $# -ge 2 ] || fail "--manifest-file requires a value"; MANIFEST_FILE="$2"; shift 2 ;;
      --allow-provider-host) [ $# -ge 2 ] || fail "--allow-provider-host requires a value"; ALLOW_PROVIDER_HOST="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      --confirm) [ $# -ge 2 ] || fail "--confirm requires a value"; CONFIRM_VALUE="$2"; shift 2 ;;
      --help|-h) usage; exit 0 ;;
      *) fail "Unknown argument: $1" ;;
    esac
  done
}

validate_layer() {
  case "$LAYER" in
    layer4) LAYER_SUPPORTED=1 ;;
    layer3|layer5_6|layer7|xray_based|experimental) LAYER_SUPPORTED=0 ;;
    *) fail "Unknown layer '$LAYER'. Use layer4, layer3, layer5_6, layer7, xray_based, or experimental." ;;
  esac
}

apply_defaults_and_validate() {
  [ -n "$ROLE" ] || fail "Role is required in non-interactive mode. Use --role controller or --role worker."
  [ "$ROLE" = "controller" ] || [ "$ROLE" = "worker" ] || fail "--role must be controller or worker"
  validate_layer

  if [ "$WITH_BINARIES" -eq 1 ] && [ -z "$MANIFEST_URL" ] && [ -z "$MANIFEST_FILE" ]; then
    MANIFEST_URL="$(default_manifest_url)"
  fi
  if [ -n "$MANIFEST_URL" ] && [ -n "$MANIFEST_FILE" ]; then
    fail "Use exactly one of --manifest-url or --manifest-file."
  fi
  if [ "$WITH_BINARIES" -eq 1 ] && [ -z "$MANIFEST_URL" ] && [ -z "$MANIFEST_FILE" ]; then
    fail "Binary-first bootstrap requires a provider manifest."
  fi
  if [ "$DRY_RUN" -eq 1 ] && [ -n "$CONFIRM_VALUE" ]; then
    fail "Use either --dry-run or --confirm ${CONFIRM_TOKEN}, not both."
  fi
  if [ "$DRY_RUN" -eq 0 ] && [ "$CONFIRM_VALUE" != "$CONFIRM_TOKEN" ]; then
    fail "Apply mode requires --confirm INSTALL_PILOTTUNNEL"
  fi
}

collect_missing_dependencies() {
  MISSING_DEPENDENCIES=""
  command_exists git || MISSING_DEPENDENCIES="${MISSING_DEPENDENCIES} git"
  command_exists python3 || command_exists python || MISSING_DEPENDENCIES="${MISSING_DEPENDENCIES} python3"
  command_exists curl || command_exists wget || MISSING_DEPENDENCIES="${MISSING_DEPENDENCIES} curl"
  command_exists tar || MISSING_DEPENDENCIES="${MISSING_DEPENDENCIES} tar"
  command_exists unzip || MISSING_DEPENDENCIES="${MISSING_DEPENDENCIES} unzip"
  [ -d /etc/ssl/certs ] || MISSING_DEPENDENCIES="${MISSING_DEPENDENCIES} ca-certificates"
}

install_dependencies() {
  collect_missing_dependencies
  [ -z "$MISSING_DEPENDENCIES" ] && return
  if [ "$DRY_RUN" -eq 1 ]; then
    info "Dry-run: missing dependencies:${MISSING_DEPENDENCIES}"
    return
  fi
  [ "$(id -u)" -eq 0 ] || fail "Missing dependencies:${MISSING_DEPENDENCIES}. Re-run as root only if intentional."
  [ -n "$PKG_MANAGER" ] || fail "Automatic dependency install is not supported on this distro."

  case "$PKG_MANAGER" in
    apt-get) apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 curl ca-certificates tar unzip ;;
    dnf) dnf install -y git python3 curl ca-certificates tar unzip ;;
    yum) yum install -y git python3 curl ca-certificates tar unzip ;;
    apk) apk add --no-cache git python3 curl ca-certificates tar unzip ;;
    zypper) zypper --non-interactive install git python3 curl ca-certificates tar unzip ;;
    *) fail "Unsupported package manager '$PKG_MANAGER'" ;;
  esac
}

prepare_layout() {
  case "$INSTALL_DIR" in
    /*) BASE_DIR="$INSTALL_DIR" ;;
    *) BASE_DIR="$(pwd -P)/$INSTALL_DIR" ;;
  esac
  REPO_DIR="${BASE_DIR}/repo"
  STATE_DIR="${BASE_DIR}/state"
  WORK_DIR="${BASE_DIR}/work"
  STAGING_ROOT="${BASE_DIR}/staging"
  RUNTIME_DIR="${BASE_DIR}/runtime"
  SERVICE_DIR="${BASE_DIR}/service-staging"
  TARGET_DIR="${BASE_DIR}/systemd-target"
  INSTALL_ROOT="${BASE_DIR}/install-root"
  CONFIG_FILE="${STATE_DIR}/config.json"
  STATE_FILE="${STATE_DIR}/state.json"
  REGISTRY_FILE="${STATE_DIR}/registry.json"
  AUDIT_LOG="${STATE_DIR}/audit.log"
  LOCK_DIR="${STATE_DIR}/locks"
}

print_plan() {
  redacted_repo="$(redact_repo_url "$REPO_URL")"
  cat <<EOF
PilotTunnel installer plan
  mode: $( [ "$DRY_RUN" -eq 1 ] && printf '%s' 'dry-run' || printf '%s' 'apply' )
  role: $ROLE
  layer: $LAYER
  layer_supported_now: $( [ "$LAYER_SUPPORTED" -eq 1 ] && printf '%s' 'true' || printf '%s' 'false' )
  repo_url: $redacted_repo
  ref: $REF
  install_dir: $BASE_DIR
  with_binaries: $( [ "$WITH_BINARIES" -eq 1 ] && printf '%s' 'true' || printf '%s' 'false' )
  manifest_url: ${MANIFEST_URL:-}
  manifest_file: ${MANIFEST_FILE:-}
  allow_provider_host: ${ALLOW_PROVIDER_HOST:-}
  detected_os: $OS_NAME
  package_manager: ${PKG_MANAGER:-unavailable}

Safety defaults
  - No writes to /etc/systemd/system
  - No daemon reload
  - No service start or stop
  - No firewall, route, or interface changes
  - No tunnel adapter execution
EOF
}

run_quiet_git() {
  log_file="$(mktemp)"
  if ! "$@" >"$log_file" 2>&1; then
    rm -f "$log_file"
    fail "Git operation failed. Check repository access, repo URL, and ref."
  fi
  rm -f "$log_file"
}

sync_repo() {
  mkdir -p "$BASE_DIR" "$STATE_DIR" "$WORK_DIR" "$STAGING_ROOT" "$RUNTIME_DIR" "$SERVICE_DIR" "$TARGET_DIR" "$INSTALL_ROOT"
  if [ ! -d "$REPO_DIR/.git" ]; then
    run_quiet_git git clone "$REPO_URL" "$REPO_DIR"
  else
    run_quiet_git git -C "$REPO_DIR" fetch --tags --prune origin
  fi
  run_quiet_git git -C "$REPO_DIR" checkout "$REF"
}

pt_cli() {
  "$PYTHON_BIN" -m pilottunnel.cli \
    --config "$CONFIG_FILE" \
    --state "$STATE_FILE" \
    --registry "$REGISTRY_FILE" \
    --audit-log "$AUDIT_LOG" \
    --lock-dir "$LOCK_DIR" \
    --work-dir "$WORK_DIR" \
    --staging-root "$STAGING_ROOT" \
    "$@"
}

binary_first_args() {
  if [ -n "$MANIFEST_URL" ]; then
    printf '%s\n' "--manifest-url" "$MANIFEST_URL"
  elif [ -n "$MANIFEST_FILE" ]; then
    printf '%s\n' "--manifest-file" "$MANIFEST_FILE"
  fi
  [ -n "$ALLOW_PROVIDER_HOST" ] && printf '%s\n' "--allow-provider-host" "$ALLOW_PROVIDER_HOST"
}

prepare_binaries() {
  [ "$WITH_BINARIES" -eq 0 ] && return
  mapfile -t binary_args < <(binary_first_args)
  (
    cd "$REPO_DIR"
    pt_cli binary download-all "${binary_args[@]}" --confirm DOWNLOAD_ALL_BINARIES
    pt_cli binary status --require-all "${binary_args[@]}" --json
  )
}

run_safe_checks() {
  (
    cd "$REPO_DIR"
    pt_cli init --role "$ROLE" --force
    pt_cli layer select --layer "$LAYER"
    pt_cli node status
    pt_cli layer status
    pt_cli readiness report --staging-root "$STAGING_ROOT" --install-root "$INSTALL_ROOT" --json
  )
}

main() {
  parse_args "$@"
  detect_os_release
  apply_defaults_and_validate
  install_dependencies
  PYTHON_BIN="$(find_python)"
  [ -n "$PYTHON_BIN" ] || fail "Python is required but was not found."
  prepare_layout
  print_plan

  if [ "$DRY_RUN" -eq 1 ]; then
    exit 0
  fi

  sync_repo
  prepare_binaries
  run_safe_checks
  info "PilotTunnel bootstrap apply completed without starting services or modifying systemd targets."
}

main "$@"
