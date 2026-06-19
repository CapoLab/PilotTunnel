# PilotTunnel

PilotTunnel is a server-only Python CLI project for managing multiple tunnel adapters behind a stable public port. There is no UI, no dashboard, and no frontend. Version `v0.1` remains focused on safe Layer 4 orchestration with dry-run behavior by default.

## Current Status

- Layer-first, adapter-based, and profile/port-based architecture is in place.
- Only `layer4` is active in `v0.1`; other layers remain listed as metadata and are intentionally blocked.
- Backhaul and Rathole now have richer dry-run planning for `controller` and `worker` roles.
- Real remote coordination, real systemd changes, firewall rules, and host networking changes are still not implemented.

## Dry-Run Safety Model

- Default behavior stays dry-run unless `--apply` is passed.
- Dry-run planning renders deterministic config and systemd unit content without executing binaries.
- The switch engine still enforces lock, stop-old, cleanup, registry validation, start-new plan, healthcheck stub, commit, and rollback behavior.
- No real systemd, iptables, nftables, routes, or interfaces are changed in `v0.1`.

## Layer 4 Scope

- Supported practical dry-run flows:
  - `backhaul tcp`
  - `backhaul tcpmux`
  - `backhaul ws`
  - `rathole tcp`
- Backhaul metadata also includes planned transports such as `utcpmux`, `uwsmux`, `tcptun`, and `faketcptun`.
- Experimental TUN-style Backhaul transports are blocked from active switching in `v0.1`.

## Example Commands

```bash
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --audit-log ./tmp/audit.log --lock-dir ./tmp/locks --work-dir ./tmp/work init
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json profile create --name <PROFILE> --main-port <MAIN_PORT> --target-port <TARGET_PORT> --role controller --control-port <CONTROL_PORT> --service-port <SERVICE_PORT> --check-port <CHECK_PORT> --candidate backhaul:tcp --candidate backhaul:tcpmux --candidate rathole:tcp
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json adapter list
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile <PROFILE> --adapter rathole --transport tcp
```

## Dry-Run CLI Workflow

```bash
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --audit-log ./tmp/audit.log --lock-dir ./tmp/locks --work-dir ./tmp/work init
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json profile create --name <PROFILE> --main-port <MAIN_PORT> --target-host <TARGET_HOST> --target-port <TARGET_PORT> --role controller --control-port <CONTROL_PORT> --service-port <SERVICE_PORT> --check-port <CHECK_PORT>
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json adapter list
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json adapter show --name backhaul
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile <PROFILE> --adapter rathole --transport tcp
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json status --profile <PROFILE>
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --audit-log ./tmp/audit.log logs --profile <PROFILE> --limit 10
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json registry check
```

## Staged Apply Mode

- `dry-run`: no files are written.
- `staged apply`: `--apply` writes generated config and unit files into the staging root only.
- `real apply`: not implemented yet.

In this stage, `--apply` does not call `systemctl`, does not touch real systemd locations, does not modify firewall rules or routes, and does not download or execute tunnel binaries.

```bash
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --staging-root .var/pilottunnel/staging plan --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --staging-root .var/pilottunnel/staging --apply switch --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --staging-root .var/pilottunnel/staging staged list
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --staging-root .var/pilottunnel/staging staged show --profile <PROFILE> --adapter backhaul --transport tcpmux
```

## Host Preflight And Binary Planning

- `preflight` is read-only.
- `binary plan` does not download anything yet.
- `real apply` is still not implemented.
- `staged apply` remains the safest review mode.

```bash
python -m pilottunnel.cli preflight
python -m pilottunnel.cli preflight --profile <PROFILE>
python -m pilottunnel.cli binary list
python -m pilottunnel.cli binary plan --adapter backhaul
python -m pilottunnel.cli binary plan --adapter rathole
```

## Binary Import And Verification

- Manual import is still supported for controlled local testing.
- Imported binaries are not executed unless `--run-version` is explicitly used.
- Services are not started.
- Real apply is still not implemented.

```bash
python -m pilottunnel.cli binary import --adapter backhaul --source ./backhaul --version manual-v0.0.0
python -m pilottunnel.cli binary import --adapter rathole --source ./rathole --version manual-v0.0.0
python -m pilottunnel.cli binary status
python -m pilottunnel.cli binary verify --adapter backhaul
python -m pilottunnel.cli binary verify --adapter rathole --run-version
```

## Preparing A Binary Provider Repository

- Binary provider manifests are required for managed downloads.
- SHA256 verification is mandatory before any binary is imported.
- `binary source list` shows the built-in upstream catalog without contacting external hosts.
- `binary source fetch` downloads only from the known upstream release catalog and stores verified binaries under a local provider source tree.
- `binary provider prepare` combines upstream fetch, manifest generation, and manifest verification for one platform without uploading anything.
- `binary install plan` shows how managed adapter binaries would be resolved from a provider manifest, the local cache, and optionally the system PATH.
- `binary install apply` copies verified adapter binaries into a chosen managed install directory without executing them.
- `binary install list` inspects the managed install directory and install summary file.
- `runtime plan` resolves managed binaries, renders adapter runtime configs, and prints dry-run argv plans without starting tunnel processes.
- Remote provider hosts must be allowlisted with `--allow-provider-host`.
- `binary download-all` prepares every required Layer 4 provider-managed adapter in one run.
- `binary provider generate-manifest` scans a local provider source tree and writes a manifest without downloading anything.
- `binary provider verify-manifest` validates schema, URLs, checksums, and required adapter coverage without changing the host.
- `bootstrap command` prints safe copy-paste prepare commands for controller and worker roles.
- `bootstrap` prepares role, profile, bundle, staging, backup, and readiness state without real deployment.
- Real deploy remains a separate gated workflow.

```bash
python -m pilottunnel.cli binary source list
python -m pilottunnel.cli binary source fetch --source-dir <SOURCE_DIR> --platform <PLATFORM> --dry-run
python -m pilottunnel.cli binary source fetch --source-dir <SOURCE_DIR> --platform <PLATFORM> --confirm FETCH_UPSTREAM_BINARIES
python -m pilottunnel.cli binary provider prepare --source-dir <SOURCE_DIR> --provider-name <PROVIDER_NAME> --base-url https://<PROVIDER_HOST>/<BASE_PATH> --platform <PLATFORM> --output <MANIFEST_FILE> --confirm PREPARE_PROVIDER_BINARIES
python -m pilottunnel.cli binary install plan --manifest <MANIFEST_FILE> --platform <PLATFORM>
python -m pilottunnel.cli binary install apply --manifest <MANIFEST_FILE> --platform <PLATFORM> --install-dir <INSTALL_DIR> --confirm INSTALL_PROVIDER_BINARIES
python -m pilottunnel.cli binary install list --install-dir <INSTALL_DIR>
python -m pilottunnel.cli --config <CONFIG_FILE> runtime plan --runtime-dir <RUNTIME_DIR>
python -m pilottunnel.cli binary provider generate-manifest --provider-name <PROVIDER_HOST> --base-url <MANIFEST_URL> --source-dir <SOURCE_DIR> --output <MANIFEST_FILE>
python -m pilottunnel.cli binary provider verify-manifest --manifest-file <MANIFEST_FILE>
python -m pilottunnel.cli binary provider inspect --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST>
python -m pilottunnel.cli binary provider inspect --manifest-file <MANIFEST_FILE>
python -m pilottunnel.cli binary download --adapter backhaul --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --confirm DOWNLOAD_BINARY
python -m pilottunnel.cli binary download-all --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --confirm DOWNLOAD_ALL_BINARIES
python -m pilottunnel.cli binary download-all --manifest-file <MANIFEST_FILE> --confirm DOWNLOAD_ALL_BINARIES
python -m pilottunnel.cli bootstrap command --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --bundle-output <BUNDLE_OUTPUT> --bundle-file <BUNDLE_FILE>
python -m pilottunnel.cli bootstrap plan --role controller --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --create-profile --target-host <TARGET_HOST> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST>
python -m pilottunnel.cli bootstrap apply --role controller --profile <PROFILE> --adapter <ADAPTER> --transport <TRANSPORT> --create-profile --target-host <TARGET_HOST> --ports auto --manifest-url <MANIFEST_URL> --allow-provider-host <PROVIDER_HOST> --bundle-output <BUNDLE_OUTPUT> --confirm BOOTSTRAP_APPLY
```

- Upstream fetch uses only the built-in adapter catalog for `backhaul`, `rathole`, `frp`, `gost`, `chisel`, `realm`, and `bore`.
- `ssh_reverse` remains a host system dependency and is intentionally skipped by upstream fetch.
- Upstream fetch writes a local `pilottunnel-source-summary.json` file under `<SOURCE_DIR>` for audit-friendly review.
- Managed install writes a local `pilottunnel-binary-install-summary.json` file under `<INSTALL_DIR>` and does not require root.
- Runtime planning writes adapter config files only under `<RUNTIME_DIR>` and keeps them in dry-run mode.
- No upstream fetch, provider prepare, managed install, runtime plan, or bootstrap command in this stage starts services, modifies `systemd`, changes firewall or routes, or executes downloaded binaries.

## Adapter Runtime Planning

- Recommended order in v0.1:
  1. fetch upstream sources
  2. prepare a provider manifest
  3. install managed binaries
  4. render and inspect a runtime plan
  5. wait for a later apply/start workflow
- `runtime plan` currently supports Layer 4 TCP planning for `rathole`, `frp`, and `gost`.
- It resolves binaries through the managed install layer, writes runtime config files under the chosen runtime directory, and reports active, hot-standby, and config-only tunnels.
- It does not start processes, bind ports, create `systemd` units, or execute adapter binaries.

```bash
python -m pilottunnel.cli binary source fetch --source-dir <SOURCE_DIR> --platform <PLATFORM> --confirm FETCH_UPSTREAM_BINARIES
python -m pilottunnel.cli binary provider prepare --source-dir <SOURCE_DIR> --provider-name <PROVIDER_NAME> --base-url https://<PROVIDER_HOST>/<BASE_PATH> --platform <PLATFORM> --output <MANIFEST_FILE> --confirm PREPARE_PROVIDER_BINARIES
python -m pilottunnel.cli binary install apply --manifest <MANIFEST_FILE> --platform <PLATFORM> --install-dir <INSTALL_DIR> --confirm INSTALL_PROVIDER_BINARIES
python -m pilottunnel.cli --config <CONFIG_FILE> runtime plan --runtime-dir <RUNTIME_DIR>
```

## Real-Host Install Planning

- `install plan` is read-only.
- It does not copy to real system paths.
- It does not run `systemctl`.
- It does not start or stop services.
- Future real apply will require explicit confirmation and backups.

```bash
python -m pilottunnel.cli install plan --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli install plan --profile <PROFILE> --adapter rathole --transport tcp --install-root .var/pilottunnel/install-root
python -m pilottunnel.cli uninstall plan --profile <PROFILE> --adapter backhaul --transport tcpmux
```

## Controlled Install Apply Gate

- Plan-only remains the default.
- Staged apply still writes only staging files.
- `install apply` in this stage copies files only into `--install-root`.
- Services are still not started.
- `systemctl`, firewall, routes, and interfaces are untouched.
- Real-host file mode is Linux-only, requires `--real-host-files`, and still does not execute `systemctl`.

```bash
python -m pilottunnel.cli install apply --profile <PROFILE> --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm APPLY
python -m pilottunnel.cli install rollback --profile <PROFILE> --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm ROLLBACK
python -m pilottunnel.cli uninstall apply --profile <PROFILE> --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm UNINSTALL
```

## Service Lifecycle Planning

- `service plan` shows the service action that would be taken, but it does not run `systemctl`.
- `service status` and `service logs` are read-only inspection commands.
- Windows hosts remain safe: these commands return warnings instead of crashing.
- Real service lifecycle changes remain gated behind explicit `--real-systemd` confirmations.

```bash
python -m pilottunnel.cli service plan --profile <PROFILE> --adapter backhaul --transport tcpmux --action start
python -m pilottunnel.cli service plan --profile <PROFILE> --adapter rathole --transport tcp --action stop
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli service logs --profile <PROFILE> --adapter backhaul --transport tcpmux --limit 50
```

## Real Systemd Read-Only And Daemon-Reload Gate

- `service status` and `service logs` can read from real systemd only with `--real-systemd`.
- `service daemon-reload` requires exact confirmation with `--confirm DAEMON_RELOAD`.
- `service start` remains gated and requires exact confirmation with `--confirm START_SERVICE`.
- `service stop` remains gated and requires exact confirmation with `--confirm STOP_SERVICE`.
- `service restart` remains gated and requires exact confirmation with `--confirm RESTART_SERVICE`.
- `service enable` remains gated and requires exact confirmation with `--confirm ENABLE_SERVICE`.
- `service disable` remains gated and requires exact confirmation with `--confirm DISABLE_SERVICE`.
- Only PilotTunnel-owned unit files are eligible for real start.
- Only PilotTunnel-owned unit files are eligible for real stop.
- Only PilotTunnel-owned unit files are eligible for real restart.
- Only PilotTunnel-owned unit files are eligible for real enable and disable.
- Firewall rules, routes, and interfaces remain untouched.

```bash
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --limit 50
python -m pilottunnel.cli service daemon-reload --real-systemd --confirm DAEMON_RELOAD
```

## Controlled Real Service Start Gate

- `service start` requires `--real-systemd` and exact `--confirm START_SERVICE`.
- Only PilotTunnel-owned unit files can be started.
- After start, PilotTunnel runs read-only `systemctl is-active` and `systemctl status`.
- Optional `--require-healthcheck` runs TCP healthchecks after start without stopping the service on failure.
- `service start` does not imply `enable`.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service start --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm START_SERVICE
python -m pilottunnel.cli service start --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm START_SERVICE --require-healthcheck
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --limit 50
```

## Controlled Real Service Stop Gate

- `service stop` requires `--real-systemd` and exact `--confirm STOP_SERVICE`.
- Only PilotTunnel-owned unit files can be stopped.
- After stop, PilotTunnel runs read-only `systemctl is-active` and `systemctl status`.
- `service stop` does not imply `disable`.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service stop --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm STOP_SERVICE
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --limit 50
```

## Controlled Real Service Enable/Disable Gates

- `service enable` requires `--real-systemd` and exact `--confirm ENABLE_SERVICE`.
- `service disable` requires `--real-systemd` and exact `--confirm DISABLE_SERVICE`.
- Only PilotTunnel-owned unit files can be enabled or disabled.
- `service enable` does not start the service.
- `service disable` does not stop the service.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service enable --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm ENABLE_SERVICE
python -m pilottunnel.cli service disable --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm DISABLE_SERVICE
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
```

## Controlled Real Service Restart Gate

- `service restart` requires `--real-systemd` and exact `--confirm RESTART_SERVICE`.
- Only PilotTunnel-owned unit files can be restarted.
- Restart does not enable or disable the service.
- Optional `--require-healthcheck` runs TCP healthchecks after restart without another automatic restart on failure.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service restart --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm RESTART_SERVICE
python -m pilottunnel.cli service restart --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd --confirm RESTART_SERVICE --require-healthcheck
python -m pilottunnel.cli service status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
```

## Controlled Deployment Workflow

- `deploy plan` is read-only.
- `deploy apply` is an orchestrator around existing guarded readiness, file apply, daemon-reload, service start, healthcheck, and optional enable steps.
- `deploy apply` requires `--real-host` and exact `--confirm DEPLOY_APPLY`.
- It does not touch firewall rules, routes, or network interfaces.
- It does not download anything.
- It does not auto-stop on healthcheck failure.
- Enabling the service is optional through `--enable-after-start`.

```bash
python -m pilottunnel.cli deploy plan --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli deploy apply --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host --confirm DEPLOY_APPLY --require-healthcheck
python -m pilottunnel.cli deploy apply --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host --confirm DEPLOY_APPLY --require-healthcheck --enable-after-start
python -m pilottunnel.cli deploy status --profile <PROFILE> --adapter backhaul --transport tcpmux --real-systemd
```

## Two-Sided Controller/Worker Bundles

- One unified CLI is used on both sides.
- The controller side exports a worker preparation bundle.
- The worker side inspects and imports that bundle to prepare local files.
- No real services are started.
- No firewall, routes, or systemd changes are performed on the host.

```bash
python -m pilottunnel.cli init --role controller
python -m pilottunnel.cli bundle export-worker --profile <PROFILE> --adapter backhaul --transport tcpmux --output .var/pilottunnel/bundles/<PROFILE>-worker.json
python -m pilottunnel.cli init --role worker
python -m pilottunnel.cli bundle inspect --input .var/pilottunnel/bundles/<PROFILE>-worker.json
python -m pilottunnel.cli bundle import --input .var/pilottunnel/bundles/<PROFILE>-worker.json --staging-root .var/pilottunnel/staging --confirm IMPORT
```

## End-to-End Local Simulation

- This simulates both controller and worker locally.
- It does not start services.
- It does not touch systemd, firewall rules, or routes.
- It is the recommended check before running on real servers.

```bash
python -m pilottunnel.cli simulate e2e --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli simulate e2e --profile <PROFILE> --adapter rathole --transport tcp --keep-files
python -m pilottunnel.cli simulate e2e --profile <PROFILE> --adapter backhaul --transport tcpmux --json
```

## Single Script, Two Roles

- The same `pilottunnel` CLI is used on both controller and worker servers.
- First `init` asks which role this server should use unless `--role` is provided.
- Controller nodes make switching and profile decisions.
- Worker nodes prepare passive-side tasks with the same CLI.
- No separate controller or worker scripts are needed.

```bash
python -m pilottunnel.cli init --role controller
python -m pilottunnel.cli init --role worker
python -m pilottunnel.cli node status
```

## TCP Healthchecks

- `healthcheck` is read-only.
- It only performs TCP connect probes.
- It does not start services.
- It does not modify firewall, routes, or systemd.

```bash
python -m pilottunnel.cli healthcheck --host <TARGET_HOST> --port <TARGET_PORT>
python -m pilottunnel.cli healthcheck --profile <PROFILE> --all
python -m pilottunnel.cli healthcheck --profile <PROFILE> --all --json
python -m pilottunnel.cli install apply --profile <PROFILE> --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm APPLY --require-healthcheck
```

## Server Readiness Report

- `readiness report` is read-only.
- It is the recommended check before any future real apply.
- It does not start services or modify system state.

```bash
python -m pilottunnel.cli readiness report
python -m pilottunnel.cli readiness report --profile <PROFILE> --adapter backhaul --transport tcpmux
python -m pilottunnel.cli readiness report --profile <PROFILE> --adapter rathole --transport tcp --json
```

## Controlled Real-Host File Apply

- This mode is Linux-only.
- It requires explicit `--real-host-files` and exact confirmation.
- It writes files only; it does not start, stop, enable, disable, or restart services.
- Systemd unit files may be copied, but `systemctl` is not executed.
- Firewall rules, routes, and interfaces remain untouched.
- Backups and a manifest are created before and during real-host file apply.
- Rollback restores backups and removes newly-created files from the manifest.

```bash
python -m pilottunnel.cli install apply --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_APPLY
python -m pilottunnel.cli install rollback --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_ROLLBACK
python -m pilottunnel.cli uninstall apply --profile <PROFILE> --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_UNINSTALL
```

## Backup and Restore Safety Layer

- Backups include PilotTunnel-owned files and metadata only.
- `restore apply` requires exact confirmation before writing anything.
- Restore verifies checksums before applying files.
- Restore creates a pre-restore safety backup before overwriting current files.
- No services are started, stopped, or restarted.
- No firewall rules, routes, or network interfaces are touched.
- It is recommended before first real Linux server testing.

```bash
python -m pilottunnel.cli backup plan
python -m pilottunnel.cli backup create --confirm BACKUP_CREATE
python -m pilottunnel.cli backup list
python -m pilottunnel.cli backup inspect --backup-id BACKUP_ID
python -m pilottunnel.cli backup verify --backup-id BACKUP_ID
python -m pilottunnel.cli restore plan --backup-id BACKUP_ID
python -m pilottunnel.cli restore apply --backup-id BACKUP_ID --confirm RESTORE_APPLY
```

## What Is Implemented

- Role-aware profile config with `controller` and `worker` normalization.
- Profile safety settings and explicit `main/control/service/check` port ownership.
- Port mapping parser and validation for common mapping shapes.
- Dry-run Backhaul and Rathole config/systemd generation with deterministic service names.
- Registry ownership tracking for ports, services, firewall tags, and routes.
- Audit entries that capture dry-run switch metadata while redacting secrets.

## What Is Still Dry-Run Only

- Real Backhaul or Rathole binary install/execution.
- Real service execution after file deployment.
- Real firewall, route, interface, or SSH/API-based worker coordination.
- Real tunnel health verification beyond local-only stubs.
