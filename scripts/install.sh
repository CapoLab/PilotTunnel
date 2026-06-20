#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
CONFIRM_TOKEN="INSTALL_PILOTTUNNEL"
PYTHON_BIN=""

ROLE=""
REPO_URL=""
REF="main"
INSTALL_DIR=""
DRY_RUN=0
CONFIRM_VALUE=""
MANIFEST_URL=""
MANIFEST_FILE=""
ALLOW_PROVIDER_HOST=""
WITH_BINARIES=0

usage() {
  cat <<'EOF'
PilotTunnel safe Linux installer/bootstrap

Usage:
  bash scripts/install.sh --role <controller|worker> --repo-url <REPO_URL> --ref <REF> --install-dir <INSTALL_DIR> --with-binaries --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --dry-run
  bash scripts/install.sh --role <controller|worker> --repo-url <REPO_URL> --ref <REF> --install-dir <INSTALL_DIR> --with-binaries --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --confirm INSTALL_PILOTTUNNEL

Options:
  --role <ROLE>           Required. Use controller or worker.
  --repo-url <REPO_URL>   Required. Supports HTTPS, SSH, or a local path.
  --ref <REF>             Optional. Defaults to main.
  --install-dir <DIR>     Required. PilotTunnel-owned base directory.
  --with-binaries         Require complete v0.1 Layer 4 binary coverage before setup continues.
  --manifest-url <URL>    Allowlisted provider manifest URL for binary-first bootstrap.
  --manifest-file <FILE>  Local provider manifest file for binary-first bootstrap.
  --allow-provider-host <HOST[,HOST...]>
                          Required for remote provider manifests and remote binary artifacts.
  --dry-run               Print the safe installation plan without writing files.
  --confirm <TOKEN>       Required for apply mode. Must be INSTALL_PILOTTUNNEL.
  --help                  Show this help text.

Safety:
  - No service start, stop, restart, enable, or disable is performed.
  - No daemon reload is performed.
  - No firewall, route, or interface changes are performed.
  - No tunnel adapter binaries are executed.
EOF
}

fail() {
  printf '%s\n' "Error: $*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

redact_repo_url() {
  "$PYTHON_BIN" - "$1" <<'PY'
import re
import sys

value = sys.argv[1]
value = re.sub(r'://[^/@]+@', '://***@', value)
print(value)
PY
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return
  fi
  if command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
    return
  fi
  fail "Python is required but neither python3 nor python was found."
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --role)
        [[ $# -ge 2 ]] || fail "--role requires a value"
        ROLE="$2"
        shift 2
        ;;
      --repo-url)
        [[ $# -ge 2 ]] || fail "--repo-url requires a value"
        REPO_URL="$2"
        shift 2
        ;;
      --ref)
        [[ $# -ge 2 ]] || fail "--ref requires a value"
        REF="$2"
        shift 2
        ;;
      --install-dir)
        [[ $# -ge 2 ]] || fail "--install-dir requires a value"
        INSTALL_DIR="$2"
        shift 2
        ;;
      --with-binaries)
        WITH_BINARIES=1
        shift
        ;;
      --manifest-url)
        [[ $# -ge 2 ]] || fail "--manifest-url requires a value"
        MANIFEST_URL="$2"
        shift 2
        ;;
      --manifest-file)
        [[ $# -ge 2 ]] || fail "--manifest-file requires a value"
        MANIFEST_FILE="$2"
        shift 2
        ;;
      --allow-provider-host)
        [[ $# -ge 2 ]] || fail "--allow-provider-host requires a value"
        ALLOW_PROVIDER_HOST="$2"
        shift 2
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --confirm)
        [[ $# -ge 2 ]] || fail "--confirm requires a value"
        CONFIRM_VALUE="$2"
        shift 2
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        fail "Unknown argument: $1"
        ;;
    esac
  done
}

validate_args() {
  [[ -n "$ROLE" ]] || fail "--role is required"
  [[ "$ROLE" == "controller" || "$ROLE" == "worker" ]] || fail "--role must be controller or worker"
  [[ -n "$REPO_URL" ]] || fail "--repo-url is required"
  [[ -n "$INSTALL_DIR" ]] || fail "--install-dir is required"
  if [[ -n "$MANIFEST_URL" && -n "$MANIFEST_FILE" ]]; then
    fail "Use exactly one of --manifest-url or --manifest-file."
  fi
  if [[ $WITH_BINARIES -eq 1 && -z "$MANIFEST_URL" && -z "$MANIFEST_FILE" ]]; then
    fail "--with-binaries requires --manifest-url or --manifest-file"
  fi
  if [[ $DRY_RUN -eq 1 && -n "$CONFIRM_VALUE" ]]; then
    fail "Use either --dry-run or --confirm ${CONFIRM_TOKEN}, not both."
  fi
  if [[ $DRY_RUN -eq 0 && "$CONFIRM_VALUE" != "$CONFIRM_TOKEN" ]]; then
    fail "Apply mode requires --confirm INSTALL_PILOTTUNNEL"
  fi
}

require_tools() {
  command -v git >/dev/null 2>&1 || fail "git is required but was not found."
}

prepare_layout() {
  BASE_DIR="$("$PYTHON_BIN" - "$INSTALL_DIR" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
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

check_apply_permissions() {
  if [[ $DRY_RUN -eq 1 ]]; then
    return
  fi
  local parent_dir
  parent_dir="$(dirname "$BASE_DIR")"
  if [[ -e "$BASE_DIR" ]]; then
    [[ -w "$BASE_DIR" ]] || fail "Install directory is not writable: $BASE_DIR. Use sudo only if you intentionally chose a system path."
    return
  fi
  if [[ ! -d "$parent_dir" || ! -w "$parent_dir" ]]; then
    fail "Cannot create install directory under $parent_dir. Use a writable path or sudo for intentional system installs."
  fi
}

ensure_layout_dirs() {
  mkdir -p "$BASE_DIR" "$STATE_DIR" "$WORK_DIR" "$STAGING_ROOT" "$RUNTIME_DIR" "$SERVICE_DIR" "$TARGET_DIR" "$INSTALL_ROOT"
}

run_quiet_git() {
  local log_file
  log_file="$(mktemp)"
  if ! "$@" >"$log_file" 2>&1; then
    rm -f "$log_file"
    fail "Git operation failed. Check repository access, repo URL, and ref."
  fi
  rm -f "$log_file"
}

sync_repo() {
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    run_quiet_git git clone "$REPO_URL" "$REPO_DIR"
  else
    local current_url
    current_url="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || printf '%s' '')"
    if [[ "$current_url" != "$REPO_URL" ]]; then
      run_quiet_git git -C "$REPO_DIR" remote set-url origin "$REPO_URL"
    fi
    run_quiet_git git -C "$REPO_DIR" fetch --tags --prune origin
  fi

  run_quiet_git git -C "$REPO_DIR" checkout "$REF"
  if git -C "$REPO_DIR" show-ref --verify --quiet "refs/remotes/origin/$REF"; then
    run_quiet_git git -C "$REPO_DIR" pull --ff-only origin "$REF"
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
  if [[ -n "$MANIFEST_URL" ]]; then
    printf '%s\n' "--manifest-url" "$MANIFEST_URL"
  elif [[ -n "$MANIFEST_FILE" ]]; then
    printf '%s\n' "--manifest-file" "$MANIFEST_FILE"
  fi
  if [[ -n "$ALLOW_PROVIDER_HOST" ]]; then
    printf '%s\n' "--allow-provider-host" "$ALLOW_PROVIDER_HOST"
  fi
}

config_profile_count() {
  "$PYTHON_BIN" - "$CONFIG_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print(0)
    raise SystemExit(0)
data = json.loads(path.read_text(encoding="utf-8"))
print(len(data.get("profiles", [])))
PY
}

current_role() {
  "$PYTHON_BIN" - "$CONFIG_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
data = json.loads(path.read_text(encoding="utf-8"))
print((data.get("node") or {}).get("normalized_role", ""))
PY
}

maybe_init_role() {
  local existing_role
  existing_role="$(current_role)"
  if [[ -z "$existing_role" ]]; then
    pt_cli init --role "$ROLE"
    return
  fi
  if [[ "$existing_role" != "$ROLE" ]]; then
    fail "Existing PilotTunnel role '$existing_role' does not match requested role '$ROLE'."
  fi
  info "Role already initialized as $ROLE. Skipping init."
}

run_safe_checks() {
  (
    cd "$REPO_DIR"
    "$PYTHON_BIN" -m pilottunnel.cli version
    "$PYTHON_BIN" -m compileall pilottunnel
    if [[ $WITH_BINARIES -eq 1 ]]; then
      mapfile -t binary_args < <(binary_first_args)
      pt_cli binary status --require-all "${binary_args[@]}" --json
    fi
    maybe_init_role
    pt_cli readiness report --staging-root "$STAGING_ROOT" --install-root "$INSTALL_ROOT" --json
    local profile_count
    profile_count="$(config_profile_count)"
    if [[ "$profile_count" =~ ^[0-9]+$ ]] && [[ "$profile_count" -gt 0 ]]; then
      pt_cli rc check --runtime-dir "$RUNTIME_DIR" --service-dir "$SERVICE_DIR" --target-dir "$TARGET_DIR" --json
      pt_cli rc smoke --runtime-dir "$RUNTIME_DIR" --service-dir "$SERVICE_DIR" --target-dir "$TARGET_DIR" --json
    else
      info "Skipping rc check and rc smoke because no profiles are configured yet."
    fi
  )
}

prepare_binaries() {
  if [[ $WITH_BINARIES -eq 0 ]]; then
    return
  fi
  mapfile -t binary_args < <(binary_first_args)
  (
    cd "$REPO_DIR"
    pt_cli binary download-all "${binary_args[@]}" --confirm DOWNLOAD_ALL_BINARIES
    pt_cli binary status --require-all "${binary_args[@]}" --json
  )
}

dry_run_binary_status() {
  if [[ $WITH_BINARIES -eq 0 ]]; then
    return
  fi
  mapfile -t binary_args < <(binary_first_args)
  (
    cd "$SOURCE_REPO_ROOT"
    "$PYTHON_BIN" -m pilottunnel.cli \
      --config "$CONFIG_FILE" \
      --state "$STATE_FILE" \
      --registry "$REGISTRY_FILE" \
      --audit-log "$AUDIT_LOG" \
      --lock-dir "$LOCK_DIR" \
      --work-dir "$WORK_DIR" \
      --staging-root "$STAGING_ROOT" \
      binary status --require-all "${binary_args[@]}" --json || true
  )
}

print_plan() {
  local redacted_repo
  redacted_repo="$(redact_repo_url "$REPO_URL")"
  cat <<EOF
PilotTunnel installer plan
  mode: $( [[ $DRY_RUN -eq 1 ]] && printf '%s' 'dry-run' || printf '%s' 'apply' )
  role: $ROLE
  repo_url: $redacted_repo
  ref: $REF
  install_dir: $BASE_DIR
  with_binaries: $( [[ $WITH_BINARIES -eq 1 ]] && printf '%s' 'true' || printf '%s' 'false' )
  repo_dir: $REPO_DIR
  config_file: $CONFIG_FILE
  runtime_dir: $RUNTIME_DIR
  service_dir: $SERVICE_DIR
  target_dir: $TARGET_DIR
  install_root: $INSTALL_ROOT

Safety defaults
  - No writes to /etc/systemd/system
  - No daemon reload
  - No service start or stop
  - No firewall, route, or interface changes
  - No tunnel adapter execution

Next operator steps after install
  1. python -m pilottunnel.cli version
  2. python -m pilottunnel.cli --config "$CONFIG_FILE" node status
  3. python -m pilottunnel.cli --config "$CONFIG_FILE" readiness report --json
  4. Configure a profile before expecting rc validation to pass.
EOF
}

main() {
  SOURCE_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  parse_args "$@"
  validate_args
  require_tools
  PYTHON_BIN="$(find_python)"
  prepare_layout
  print_plan
  if [[ $DRY_RUN -eq 1 ]]; then
    dry_run_binary_status
    exit 0
  fi

  check_apply_permissions
  ensure_layout_dirs
  sync_repo
  prepare_binaries
  run_safe_checks
  info "PilotTunnel installer apply completed without starting services or modifying systemd targets."
}

main "$@"
