# PilotTunnel

PilotTunnel is a server-only Python CLI project for managing multiple tunnel adapters behind a stable public port. There is no UI, no dashboard, and no frontend. Version `v0.1` remains focused on safe Layer 4 orchestration with dry-run behavior by default.

## Current Status

- Layer-first, adapter-based, and profile/port-based architecture is in place.
- Only `layer4` is active in `v0.1`; other layers remain listed as metadata and are intentionally blocked.
- Backhaul and Rathole now have richer dry-run planning for `controller/iran` and `worker/foreign` roles.
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
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json profile create --name turkey-6221 --main-port 6221 --target-port 5201 --role controller --control-port 7001 --service-port 7002 --check-port 7003 --candidate backhaul:tcp --candidate backhaul:tcpmux --candidate rathole:tcp
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json adapter list
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile turkey-6221 --adapter rathole --transport tcp
```

## Dry-Run CLI Workflow

```bash
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --audit-log ./tmp/audit.log --lock-dir ./tmp/locks --work-dir ./tmp/work init
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json profile create --name turkey-6221 --main-port 6221 --target-host 127.0.0.1 --target-port 6221 --role controller --control-port 49323 --service-port 2106 --check-port 3106
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json adapter list
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json adapter show --name backhaul
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json switch --profile turkey-6221 --adapter rathole --transport tcp
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json status --profile turkey-6221
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --audit-log ./tmp/audit.log logs --profile turkey-6221 --limit 10
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json registry check
```

## Staged Apply Mode

- `dry-run`: no files are written.
- `staged apply`: `--apply` writes generated config and unit files into the staging root only.
- `real apply`: not implemented yet.

In this stage, `--apply` does not call `systemctl`, does not touch real systemd locations, does not modify firewall rules or routes, and does not download or execute tunnel binaries.

```bash
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --staging-root .var/pilottunnel/staging plan --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --staging-root .var/pilottunnel/staging --apply switch --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --staging-root .var/pilottunnel/staging staged list
python -m pilottunnel.cli --config ./tmp/config.json --state ./tmp/state.json --registry ./tmp/registry.json --staging-root .var/pilottunnel/staging staged show --profile turkey-6221 --adapter backhaul --transport tcpmux
```

## Host Preflight And Binary Planning

- `preflight` is read-only.
- `binary plan` does not download anything yet.
- `real apply` is still not implemented.
- `staged apply` remains the safest review mode.

```bash
python -m pilottunnel.cli preflight
python -m pilottunnel.cli preflight --profile turkey-6221
python -m pilottunnel.cli binary list
python -m pilottunnel.cli binary plan --adapter backhaul
python -m pilottunnel.cli binary plan --adapter rathole
```

## Binary Import And Verification

- No automatic downloads yet.
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

## Real-Host Install Planning

- `install plan` is read-only.
- It does not copy to real system paths.
- It does not run `systemctl`.
- It does not start or stop services.
- Future real apply will require explicit confirmation and backups.

```bash
python -m pilottunnel.cli install plan --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli install plan --profile turkey-6221 --adapter rathole --transport tcp --install-root .var/pilottunnel/install-root
python -m pilottunnel.cli uninstall plan --profile turkey-6221 --adapter backhaul --transport tcpmux
```

## Controlled Install Apply Gate

- Plan-only remains the default.
- Staged apply still writes only staging files.
- `install apply` in this stage copies files only into `--install-root`.
- Services are still not started.
- `systemctl`, firewall, routes, and interfaces are untouched.
- Real-host file mode is Linux-only, requires `--real-host-files`, and still does not execute `systemctl`.

```bash
python -m pilottunnel.cli install apply --profile turkey-6221 --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm APPLY
python -m pilottunnel.cli install rollback --profile turkey-6221 --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm ROLLBACK
python -m pilottunnel.cli uninstall apply --profile turkey-6221 --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm UNINSTALL
```

## Service Lifecycle Planning

- `service plan` shows the service action that would be taken, but it does not run `systemctl`.
- `service status` and `service logs` are read-only inspection commands.
- Windows hosts remain safe: these commands return warnings instead of crashing.
- Real service lifecycle changes remain gated behind explicit `--real-systemd` confirmations.

```bash
python -m pilottunnel.cli service plan --profile turkey-6221 --adapter backhaul --transport tcpmux --action start
python -m pilottunnel.cli service plan --profile turkey-6221 --adapter rathole --transport tcp --action stop
python -m pilottunnel.cli service status --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli service logs --profile turkey-6221 --adapter backhaul --transport tcpmux --limit 50
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
python -m pilottunnel.cli service status --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --limit 50
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
python -m pilottunnel.cli service start --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --confirm START_SERVICE
python -m pilottunnel.cli service start --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --confirm START_SERVICE --require-healthcheck
python -m pilottunnel.cli service status --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --limit 50
```

## Controlled Real Service Stop Gate

- `service stop` requires `--real-systemd` and exact `--confirm STOP_SERVICE`.
- Only PilotTunnel-owned unit files can be stopped.
- After stop, PilotTunnel runs read-only `systemctl is-active` and `systemctl status`.
- `service stop` does not imply `disable`.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service stop --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --confirm STOP_SERVICE
python -m pilottunnel.cli service status --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd
python -m pilottunnel.cli service logs --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --limit 50
```

## Controlled Real Service Enable/Disable Gates

- `service enable` requires `--real-systemd` and exact `--confirm ENABLE_SERVICE`.
- `service disable` requires `--real-systemd` and exact `--confirm DISABLE_SERVICE`.
- Only PilotTunnel-owned unit files can be enabled or disabled.
- `service enable` does not start the service.
- `service disable` does not stop the service.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service enable --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --confirm ENABLE_SERVICE
python -m pilottunnel.cli service disable --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --confirm DISABLE_SERVICE
python -m pilottunnel.cli service status --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd
```

## Controlled Real Service Restart Gate

- `service restart` requires `--real-systemd` and exact `--confirm RESTART_SERVICE`.
- Only PilotTunnel-owned unit files can be restarted.
- Restart does not enable or disable the service.
- Optional `--require-healthcheck` runs TCP healthchecks after restart without another automatic restart on failure.
- Firewall rules, routes, interfaces, and downloads remain untouched.

```bash
python -m pilottunnel.cli service restart --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --confirm RESTART_SERVICE
python -m pilottunnel.cli service restart --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd --confirm RESTART_SERVICE --require-healthcheck
python -m pilottunnel.cli service status --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd
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
python -m pilottunnel.cli deploy plan --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli deploy apply --profile turkey-6221 --adapter backhaul --transport tcpmux --real-host --confirm DEPLOY_APPLY --require-healthcheck
python -m pilottunnel.cli deploy apply --profile turkey-6221 --adapter backhaul --transport tcpmux --real-host --confirm DEPLOY_APPLY --require-healthcheck --enable-after-start
python -m pilottunnel.cli deploy status --profile turkey-6221 --adapter backhaul --transport tcpmux --real-systemd
```

## Two-Sided Controller/Worker Bundles

- One unified CLI is used on both sides.
- The controller/Iran side exports a worker preparation bundle.
- The worker/Foreign side inspects and imports that bundle to prepare local files.
- No real services are started.
- No firewall, routes, or systemd changes are performed on the host.

```bash
python -m pilottunnel.cli init --role controller
python -m pilottunnel.cli bundle export-worker --profile turkey-6221 --adapter backhaul --transport tcpmux --output .var/pilottunnel/bundles/turkey-6221-worker.json
python -m pilottunnel.cli init --role worker
python -m pilottunnel.cli bundle inspect --input .var/pilottunnel/bundles/turkey-6221-worker.json
python -m pilottunnel.cli bundle import --input .var/pilottunnel/bundles/turkey-6221-worker.json --staging-root .var/pilottunnel/staging --confirm IMPORT
```

## End-to-End Local Simulation

- This simulates both controller and worker locally.
- It does not start services.
- It does not touch systemd, firewall rules, or routes.
- It is the recommended check before running on real servers.

```bash
python -m pilottunnel.cli simulate e2e --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli simulate e2e --profile turkey-6221 --adapter rathole --transport tcp --keep-files
python -m pilottunnel.cli simulate e2e --profile turkey-6221 --adapter backhaul --transport tcpmux --json
```

## Single Script, Two Roles

- The same `pilottunnel` CLI is used on Iran and Foreign servers.
- First `init` asks which side this server is unless `--role` is provided.
- Iran/controller nodes make switching and profile decisions.
- Foreign/worker nodes prepare passive-side tasks with the same CLI.
- No separate Iran or Foreign scripts are needed.

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
python -m pilottunnel.cli healthcheck --host 127.0.0.1 --port 6221
python -m pilottunnel.cli healthcheck --profile turkey-6221 --all
python -m pilottunnel.cli healthcheck --profile turkey-6221 --all --json
python -m pilottunnel.cli install apply --profile turkey-6221 --adapter backhaul --transport tcpmux --install-root .var/pilottunnel/install-root --confirm APPLY --require-healthcheck
```

## Server Readiness Report

- `readiness report` is read-only.
- It is the recommended check before any future real apply.
- It does not start services or modify system state.

```bash
python -m pilottunnel.cli readiness report
python -m pilottunnel.cli readiness report --profile turkey-6221 --adapter backhaul --transport tcpmux
python -m pilottunnel.cli readiness report --profile turkey-6221 --adapter rathole --transport tcp --json
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
python -m pilottunnel.cli install apply --profile turkey-6221 --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_APPLY
python -m pilottunnel.cli install rollback --profile turkey-6221 --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_ROLLBACK
python -m pilottunnel.cli uninstall apply --profile turkey-6221 --adapter backhaul --transport tcpmux --real-host-files --confirm REAL_FILES_UNINSTALL
```

## What Is Implemented

- Role-aware profile config with `controller/iran` and `worker/foreign` normalization.
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
