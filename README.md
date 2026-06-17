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

## What Is Implemented

- Role-aware profile config with `controller/iran` and `worker/foreign` normalization.
- Profile safety settings and explicit `main/control/service/check` port ownership.
- Port mapping parser and validation for common mapping shapes.
- Dry-run Backhaul and Rathole config/systemd generation with deterministic service names.
- Registry ownership tracking for ports, services, firewall tags, and routes.
- Audit entries that capture dry-run switch metadata while redacting secrets.

## What Is Still Dry-Run Only

- Real Backhaul or Rathole binary install/execution.
- Real systemd unit deployment outside explicit render targets.
- Real firewall, route, interface, or SSH/API-based worker coordination.
- Real tunnel health verification beyond local-only stubs.
