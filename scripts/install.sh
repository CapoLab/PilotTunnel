#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

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
NO_MENU=0
DEBUG=0
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
PilotTunnel safety-first multi-layer tunnel bootstrap helper

Usage:
  bash ${SCRIPT_NAME}
  bash ${SCRIPT_NAME} --no-menu --role <controller|worker>
  bash ${SCRIPT_NAME} --debug
  bash ${SCRIPT_NAME} --dry-run

Options:
  --no-menu              Prepare PilotTunnel without launching the terminal menu.
  --role <ROLE>           Initialize controller or worker in non-interactive mode.
  --debug                Show detailed installer output for troubleshooting.
  --layer <LAYER>         Optional. Defaults to layer4.
  --repo-url <REPO_URL>   Optional. Defaults to the public PilotTunnel repo.
  --ref <REF>             Optional. Defaults to main.
  --install-dir <DIR>     Optional. Defaults to /opt/pilottunnel.
  --with-binaries         Download, import, and verify required provider binaries.
  --without-binaries      Skip binary download/import/verify during bootstrap.
  --no-binaries           Alias for --without-binaries.
  --manifest-url <URL>    Optional provider manifest URL override.
  --manifest-file <FILE>  Optional local provider manifest file override.
  --allow-provider-host <HOST[,HOST...]>
                          Optional provider manifest/artifact allowlist.
  --dry-run               Print the safe bootstrap plan without cloning or writing files.
  --help                  Show this help text.

Safety:
  - No service start, stop, restart, enable, or disable is performed.
  - No daemon reload is performed.
  - No firewall, route, or interface changes are performed.
  - No tunnel adapter binaries are executed.
  - Basic installation needs no typed confirmation.
  - Public install flow is presented as multi-layer and opens a terminal menu.
  - The current runnable workflow defaults to layer4 in v0.1.
EOF
}

fail() {
  printf '%s\n' "Error: $*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

debug() {
  [ "$DEBUG" -eq 1 ] || return 0
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
      --no-menu) NO_MENU=1; shift ;;
      --debug) DEBUG=1; shift ;;
      --with-binaries) WITH_BINARIES=1; shift ;;
      --without-binaries|--no-binaries) WITH_BINARIES=0; shift ;;
      --manifest-url) [ $# -ge 2 ] || fail "--manifest-url requires a value"; MANIFEST_URL="$2"; shift 2 ;;
      --manifest-file) [ $# -ge 2 ] || fail "--manifest-file requires a value"; MANIFEST_FILE="$2"; shift 2 ;;
      --allow-provider-host) [ $# -ge 2 ] || fail "--allow-provider-host requires a value"; ALLOW_PROVIDER_HOST="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
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
  if [ -n "$ROLE" ]; then
    [ "$ROLE" = "controller" ] || [ "$ROLE" = "worker" ] || fail "--role must be controller or worker"
  fi
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
  if [ "${PILOTTUNNEL_SKIP_DEPENDENCY_CHECKS:-0}" = "1" ]; then
    debug "Skipping dependency checks because PILOTTUNNEL_SKIP_DEPENDENCY_CHECKS=1"
    return
  fi
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
  BIN_DIR="${BASE_DIR}/bin"
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
  role: ${ROLE:-deferred until Setup / Configure this server}
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

cleanup_capture() {
  [ -n "${LAST_STDOUT:-}" ] && rm -f "$LAST_STDOUT"
  [ -n "${LAST_STDERR:-}" ] && rm -f "$LAST_STDERR"
  LAST_STDOUT=""
  LAST_STDERR=""
}

print_debug_capture() {
  [ "$DEBUG" -eq 1 ] || return 0
  [ -n "${LAST_STDOUT:-}" ] && [ -s "$LAST_STDOUT" ] && cat "$LAST_STDOUT"
  [ -n "${LAST_STDERR:-}" ] && [ -s "$LAST_STDERR" ] && cat "$LAST_STDERR" >&2
}

extract_capture_summary() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").strip()
if not text:
    raise SystemExit(0)
try:
    payload = json.loads(text)
except Exception:
    print(" ".join(text.split()))
    raise SystemExit(0)

parts = []
message = payload.get("message")
if isinstance(message, str) and message:
    parts.append(message)
blockers = payload.get("blockers")
if isinstance(blockers, list):
    parts.extend(str(item) for item in blockers if item)
failed = payload.get("failed_adapters")
if isinstance(failed, list) and failed:
    parts.append("failed adapters: " + ", ".join(str(item) for item in failed))
results = payload.get("results")
if not parts and isinstance(results, list):
    failed_items = [str(item.get("adapter")) for item in results if item.get("result") == "failed" and item.get("adapter")]
    if failed_items:
        parts.append("failed adapters: " + ", ".join(failed_items))
if parts:
    print("; ".join(parts))
PY
}

fail_with_capture() {
  summary="$(extract_capture_summary "$LAST_STDOUT" 2>/dev/null || true)"
  if [ -z "$summary" ] && [ -n "${LAST_STDERR:-}" ] && [ -s "$LAST_STDERR" ]; then
    summary="$(tr '\n' ' ' <"$LAST_STDERR" | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')"
  fi
  print_debug_capture
  cleanup_capture
  if [ -n "$summary" ]; then
    fail "$1: $summary"
  fi
  fail "$1"
}

run_pt_capture() {
  cleanup_capture
  LAST_STDOUT="$(mktemp)"
  LAST_STDERR="$(mktemp)"
  if ! (
    cd "$REPO_DIR"
    pt_cli "$@"
  ) >"$LAST_STDOUT" 2>"$LAST_STDERR"; then
    return 1
  fi
  return 0
}

print_installer_header() {
  info "PilotTunnel Installer"
}

summarize_binary_readiness() {
  "$PYTHON_BIN" - "$LAST_STDOUT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
required = payload.get("required_adapters") or []
verified = payload.get("verified_adapters") or []
missing = payload.get("missing_adapters") or []
print(f"Required binaries: {len(verified)}/{len(required)} verified")
if verified:
    print("Verified adapters: " + ", ".join(verified))
if missing:
    print("Missing adapters: " + ", ".join(missing))
PY
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
  mkdir -p "$BASE_DIR" "$BIN_DIR" "$STATE_DIR" "$WORK_DIR" "$STAGING_ROOT" "$RUNTIME_DIR" "$SERVICE_DIR" "$TARGET_DIR" "$INSTALL_ROOT"
  if [ ! -d "$REPO_DIR/.git" ]; then
    run_quiet_git git clone "$REPO_URL" "$REPO_DIR"
  else
    run_quiet_git git -C "$REPO_DIR" fetch --tags --prune origin
  fi
  if git -C "$REPO_DIR" rev-parse --verify --quiet "refs/remotes/origin/${REF}" >/dev/null; then
    run_quiet_git git -C "$REPO_DIR" checkout --detach "refs/remotes/origin/${REF}"
  else
    run_quiet_git git -C "$REPO_DIR" checkout --detach "$REF"
  fi
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
  if [ "$WITH_BINARIES" -eq 0 ]; then
    info "Required binaries: skipped (--without-binaries)"
    return
  fi
  mapfile -t binary_args < <(binary_first_args)
  if ! run_pt_capture binary download-all "${binary_args[@]}" --confirm DOWNLOAD_ALL_BINARIES; then
    fail_with_capture "Required binary preparation failed"
  fi
  print_debug_capture
  cleanup_capture

  if ! run_pt_capture binary status --require-all "${binary_args[@]}"; then
    fail_with_capture "Binary verification failed"
  fi

  if "$PYTHON_BIN" - "$LAST_STDOUT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if payload.get("ok") else 1)
PY
  then
    info "Binaries: OK"
  else
    fail_with_capture "Binary verification failed"
  fi
  summarize_binary_readiness
  print_debug_capture
  cleanup_capture
}

configured_role() {
  [ -f "$CONFIG_FILE" ] || return 0
  "$PYTHON_BIN" - "$CONFIG_FILE" <<'PY'
import json
import sys

try:
    payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
except (OSError, ValueError):
    raise SystemExit(0)
node = payload.get("node") or {}
if node.get("initialized"):
    print(node.get("normalized_role") or node.get("node_role") or "")
PY
}

initialize_role_if_requested() {
  [ -n "$ROLE" ] || return 0
  existing_role="$(configured_role)"
  if [ -n "$existing_role" ]; then
    [ "$existing_role" = "$ROLE" ] || fail "Node is already initialized as '$existing_role'. Use the Setup menu to change it safely."
    info "Node role is already initialized as '$ROLE'."
    return
  fi
  if ! run_pt_capture init --role "$ROLE"; then
    fail_with_capture "Node role initialization failed"
  fi
  print_debug_capture
  cleanup_capture
  if ! run_pt_capture layer select --layer "$LAYER"; then
    fail_with_capture "Layer selection failed"
  fi
  print_debug_capture
  cleanup_capture
  info "Node role initialized: $ROLE"
  info "Selected layer: $LAYER"
}

install_menu_launcher() {
  menu_source="${REPO_DIR}/scripts/pilottunnel-menu"
  [ -f "$menu_source" ] || fail "Installed repository does not contain scripts/pilottunnel-menu."
  menu_target="${BIN_DIR}/pilottunnel-menu"
  cp "$menu_source" "$menu_target"
  chmod 0755 "$menu_target"
}

launch_menu_if_requested() {
  if [ "$NO_MENU" -eq 1 ]; then
    info "PilotTunnel prepared. Run ${BIN_DIR}/pilottunnel-menu when you are ready to configure this server."
    return
  fi
  info "Opening PilotTunnel menu..."
  if [ "${PILOTTUNNEL_MENU_ALLOW_NON_TTY:-0}" = "1" ]; then
    if "${BIN_DIR}/pilottunnel-menu" --base-dir "$BASE_DIR"; then
      return
    fi
  elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
    if "${BIN_DIR}/pilottunnel-menu" --base-dir "$BASE_DIR" </dev/tty >/dev/tty 2>&1; then
      return
    fi
  fi
  info "Menu could not be opened automatically. Run ${BIN_DIR}/pilottunnel-menu"
}

main() {
  parse_args "$@"
  detect_os_release
  apply_defaults_and_validate
  prepare_layout
  if [ "$DRY_RUN" -eq 1 ]; then
    print_plan
    exit 0
  fi
  print_installer_header
  info "Checking system..."
  install_dependencies
  PYTHON_BIN="$(find_python)"
  [ -n "$PYTHON_BIN" ] || fail "Python is required but was not found."
  if [ "$DEBUG" -eq 1 ]; then
    print_plan
  fi

  info "Installing/updating PilotTunnel..."
  sync_repo
  info "Preparing required binaries..."
  prepare_binaries
  install_menu_launcher
  initialize_role_if_requested
  info "Safety: no services started, no firewall/routes changed"
  launch_menu_if_requested
}

main "$@"
